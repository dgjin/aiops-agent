"""Temporal History 完整投影（M4）：全量分页、幂等写入、增量/终态/对账。

设计要点：

- **独立于 UI**：BFF 后台任务按周期扫描流程，直接调用 Temporal
  ``fetch_history_events``（SDK 自带分页），不受控制台列表 200 条上限影响；
- **幂等**：以 ``(workflow_id, run_id, event_id)`` 为主键/行键，只追加
  ``event_id > 已同步位置`` 的事件；重复执行不会产生重复行；
- **双后端**：production 写 ``workflow_events`` 并更新 ``workflow_runs``
  同步位；demo 写 ``data/workflow-history/<wf_id>/<run_id>.jsonl`` + meta；
- **终态最终同步**：运行中按增量同步；检测到终止类事件或执行状态非 RUNNING
  时置 ``history_complete``，之后不再重复拉取；
- **对账**：:func:`reconcile` 补齐未完成流程，并用事件数与最大事件号检测断档，
  通过 Prometheus gauge 暴露未完成数 / 断档数 / 最大滞后。

边界说明：本模块只投影**当前 run**；同一 workflow_id 重跑时 ``workflow_runs``
的同步位会按 run_id 重置，旧 run 的事件仍保留在 ``workflow_events``（主键含 run_id）。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from sqlalchemy import and_, func, select
from temporalio.api.enums.v1 import EventType

from aiops_agent import db as db_layer
from aiops_agent import metrics

from .deps import gw, log

HISTORY_ROOT = "workflow-history"
DEFAULT_PAGE_SIZE = 100
DEFAULT_SYNC_SECONDS = 30
DEFAULT_RECONCILE_SECONDS = 600
_RPC_TIMEOUT = timedelta(seconds=10)
_SYNC_CONCURRENCY = 4

# 终止类事件：出现即表示该 run 不会再有新事件（ContinuedAsNew 亦然）
_TERMINAL_EVENT_TYPES = {
    "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
    "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
    "EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT",
    "EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED",
    "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED",
    "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW",
}
_TERMINAL_EXEC = {"COMPLETED", "FAILED", "TIMED_OUT", "TERMINATED", "CANCELED"}

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_demo_lock = threading.Lock()
_last_stats: dict = {
    "runs": 0,
    "incomplete": 0,
    "gaps": 0,
    "complete": 0,
    "lag_seconds": 0.0,
    "checked_at": "",
}


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def page_size() -> int:
    """单页事件数（Temporal ``maximum_page_size``）。"""
    return _env_int("AIOPS_HISTORY_PAGE_SIZE", DEFAULT_PAGE_SIZE)


def sync_seconds() -> int:
    return _env_int("AIOPS_HISTORY_SYNC_SECONDS", DEFAULT_SYNC_SECONDS, minimum=5)


def reconcile_seconds() -> int:
    return _env_int("AIOPS_HISTORY_RECONCILE_SECONDS", DEFAULT_RECONCILE_SECONDS, minimum=30)


# ----------------------------------------------------------------------
# 通用：事件转换 / 时间
# ----------------------------------------------------------------------


def _event_type_name(event) -> str:
    try:
        return EventType.Name(int(event.event_type))
    except (ValueError, TypeError):
        return str(getattr(event, "event_type", "") or "UNKNOWN")


def _event_time(event) -> str | None:
    try:
        if not event.HasField("event_time"):
            return None
        return event.event_time.ToDatetime(tzinfo=timezone.utc).isoformat()
    except (ValueError, AttributeError):
        return None


def _event_detail(event) -> dict:
    """完整事件 JSON（保留 protobuf 字段原名，便于离线复核）。"""
    try:
        return MessageToDict(event, preserving_proto_field_name=True)
    except Exception as exc:  # noqa: BLE001 - 单个事件序列化失败不得中断整批
        return {"error": f"事件序列化失败：{type(exc).__name__}: {exc}"}


def _event_record(event) -> dict:
    return {
        "event_id": int(event.event_id),
        "event_type": _event_type_name(event),
        "event_time": _event_time(event),
        "detail": _event_detail(event),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lag_seconds(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (datetime.now(timezone.utc).replace(tzinfo=None) - value).total_seconds())


# ----------------------------------------------------------------------
# demo 文件后端
# ----------------------------------------------------------------------


def _root() -> Path:
    from aiops_agent import mode

    return mode.data_root() / HISTORY_ROOT


def _safe(value: str) -> str:
    text = str(value or "")
    cleaned = _SAFE_RE.sub("_", text).strip("._-")[:100]
    if cleaned != text:
        cleaned = f"{cleaned or 'unknown'}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
    return cleaned or "unknown"


def _paths(wf_id: str, run_id: str) -> tuple[Path, Path]:
    base = _root() / _safe(wf_id)
    return base / f"{_safe(run_id)}.jsonl", base / f"{_safe(run_id)}.json"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _scan_demo(path: Path) -> dict:
    """meta 缺失/损坏时，从 JSONL 恢复同步位（只读末行与行数）。"""
    state = {"last_event_id": 0, "event_count": 0, "complete": False}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return state
    state["event_count"] = sum(1 for line in lines if line.strip())
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            state["last_event_id"] = int(value.get("event_id") or 0)
        break
    return state


def _tail_event_id(path: Path) -> int:
    """只读文件末尾若干字节取最后一条 event_id，避免每次校验都全量扫描。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return 0
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return int(value.get("event_id") or 0)
    return 0


def _demo_meta(wf_id: str, run_id: str) -> dict:
    event_path, meta_path = _paths(wf_id, run_id)
    meta = _read_json(meta_path) or {}
    if meta.get("workflow_id") != wf_id or meta.get("run_id") != run_id:
        meta = {"workflow_id": wf_id, "run_id": run_id}
    if not meta.get("synced_at"):
        # 首次/损坏：全量扫描恢复（只在 meta 缺失时发生）
        scanned = _scan_demo(event_path)
        meta.update(
            last_event_id=scanned["last_event_id"], event_count=scanned["event_count"]
        )
        return meta
    tail = _tail_event_id(event_path)
    if tail > int(meta.get("last_event_id") or 0):
        # 崩溃恢复：事件已落盘但 meta 未更新，按文件实际状态重建
        scanned = _scan_demo(event_path)
        meta["last_event_id"] = scanned["last_event_id"]
        meta["event_count"] = scanned["event_count"]
    return meta


def _append_demo(
    wf_id: str,
    run_id: str,
    events: list[dict],
    *,
    complete: bool,
    exec_status: str,
    start_time: str | None,
    close_time: str | None,
) -> int:
    event_path, meta_path = _paths(wf_id, run_id)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = event_path.with_suffix(".lock")
    with _demo_lock, open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            meta = _demo_meta(wf_id, run_id)
            last = int(meta.get("last_event_id") or 0)
            fresh = [event for event in events if int(event["event_id"]) > last]
            if fresh:
                with open(event_path, "a", encoding="utf-8") as fh:
                    for event in fresh:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                last = int(fresh[-1]["event_id"])
            _write_json(
                meta_path,
                {
                    "workflow_id": wf_id,
                    "run_id": run_id,
                    "last_event_id": last,
                    "event_count": int(meta.get("event_count") or 0) + len(fresh),
                    "complete": bool(complete),
                    "exec_status": exec_status,
                    "start_time": start_time,
                    "close_time": close_time,
                    "synced_at": _now_iso(),
                },
            )
            return len(fresh)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_demo_events(wf_id: str, run_id: str, *, after_id: int, limit: int, event_type: str) -> list[dict]:
    event_path, _ = _paths(wf_id, run_id)
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if int(value.get("event_id") or 0) <= after_id:
            continue
        if event_type and value.get("event_type") != event_type:
            continue
        rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def _demo_runs(limit: int = 1000) -> list[dict]:
    root = _root()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for meta_path in root.glob("*/*.json"):
        if meta_path.name.endswith(".tmp"):
            continue
        meta = _read_json(meta_path)
        if not meta or not meta.get("workflow_id"):
            continue
        meta["event_count"] = int(meta.get("event_count") or 0)
        meta["last_event_id"] = int(meta.get("last_event_id") or 0)
        rows.append(meta)
    rows.sort(key=lambda item: str(item.get("synced_at") or ""), reverse=True)
    return rows[:limit]


# ----------------------------------------------------------------------
# production 数据库后端
# ----------------------------------------------------------------------


def _run_row(conn, wf_id: str):
    return conn.execute(
        select(db_layer.workflow_runs).where(db_layer.workflow_runs.c.workflow_id == wf_id)
    ).mappings().first()


def _last_event_id_db(wf_id: str, run_id: str) -> int:
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        row = _run_row(conn, wf_id)
        if row is None or row["run_id"] != run_id:
            return 0
        return int(row["last_history_event_id"] or 0)


def _append_db(
    wf_id: str,
    run_id: str,
    events: list[dict],
    *,
    complete: bool,
    exec_status: str,
    start_time: str | None,
    close_time: str | None,
) -> int:
    engine = db_layer.get_engine()
    runs = db_layer.workflow_runs
    table = db_layer.workflow_events
    now = db_layer.now_dt()
    with engine.begin() as conn:
        row = _run_row(conn, wf_id)
        if row is None:
            conn.execute(
                runs.insert().values(
                    workflow_id=wf_id,
                    run_id=run_id,
                    stage="",
                    exec_status=exec_status,
                    kind="requirement" if wf_id.startswith("aiops-req-") else "alert",
                    alert=None,
                    confidence=None,
                    patch_id=None,
                    start_time=db_layer.to_dt(start_time),
                    close_time=db_layer.to_dt(close_time),
                    duration_seconds=None,
                    result=None,
                    last_history_event_id=None,
                    history_synced_at=now,
                    history_complete=False,
                    mode="production",
                    updated_at=now,
                )
            )
            last = 0
        elif row["run_id"] != run_id:
            # 同一 workflow_id 新 run：同步位重置，旧 run 事件仍保留（主键含 run_id）
            last = 0
            conn.execute(
                runs.update()
                .where(runs.c.workflow_id == wf_id)
                .values(
                    run_id=run_id,
                    last_history_event_id=None,
                    history_complete=False,
                    updated_at=now,
                )
            )
        else:
            last = int(row["last_history_event_id"] or 0)

        candidates = [event for event in events if int(event["event_id"]) > last]
        inserted = 0
        if candidates:
            ids = [int(event["event_id"]) for event in candidates]
            existing = set(
                conn.execute(
                    select(table.c.event_id).where(
                        and_(
                            table.c.workflow_id == wf_id,
                            table.c.run_id == run_id,
                            table.c.event_id.in_(ids),
                        )
                    )
                ).scalars()
            )
            payload = [
                {
                    "workflow_id": wf_id,
                    "run_id": run_id,
                    "event_id": int(event["event_id"]),
                    "event_time": db_layer.to_dt(event.get("event_time")),
                    "event_type": str(event.get("event_type") or ""),
                    "detail": event.get("detail") or {},
                    "mode": "production",
                    "synced_at": now,
                }
                for event in candidates
                if int(event["event_id"]) not in existing
            ]
            if payload:
                conn.execute(table.insert(), payload)
                inserted = len(payload)

        values = {
            "exec_status": exec_status,
            "history_synced_at": now,
            "history_complete": bool(complete),
            "updated_at": now,
        }
        if events:
            values["last_history_event_id"] = max(
                int(events[-1]["event_id"]), int(events[0]["event_id"]), last
            )
        elif last:
            values["last_history_event_id"] = last
        if close_time:
            values["close_time"] = db_layer.to_dt(close_time)
        if start_time:
            values["start_time"] = db_layer.to_dt(start_time)
        conn.execute(runs.update().where(runs.c.workflow_id == wf_id).values(**values))
        return inserted


# ----------------------------------------------------------------------
# 同步单个流程
# ----------------------------------------------------------------------


def _status_name(status) -> str:
    return getattr(status, "name", None) or "UNKNOWN"


async def sync_workflow(wf_id: str, run_id: str | None = None, *, handle=None) -> dict:
    """同步一个流程当前 run 的完整 History（幂等，可在对账中反复调用）。

    ``handle`` 可注入（测试）；缺省用共享 Temporal 网关。
    """
    result = {
        "wf_id": wf_id,
        "run_id": run_id or "",
        "exec_status": "UNKNOWN",
        "fetched": 0,
        "stored": 0,
        "last_event_id": 0,
        "complete": False,
        "error": "",
    }
    try:
        if handle is None:
            client = await gw.client()
            handle = client.get_workflow_handle(wf_id, run_id=run_id)
        desc = await handle.describe(rpc_timeout=_RPC_TIMEOUT)
        actual_run = desc.run_id
        exec_status = _status_name(desc.status)
        start_time = desc.start_time.isoformat() if desc.start_time else None
        close_time = desc.close_time.isoformat() if desc.close_time else None
        result.update(run_id=actual_run, exec_status=exec_status)

        last = await asyncio.to_thread(_last_event_id, wf_id, actual_run)
        events: list[dict] = []
        terminal_seen = False
        async for event in handle.fetch_history_events(
            page_size=page_size(), rpc_timeout=_RPC_TIMEOUT
        ):
            result["fetched"] += 1
            type_name = _event_type_name(event)
            if type_name in _TERMINAL_EVENT_TYPES:
                terminal_seen = True
            if int(event.event_id) <= last:
                continue
            events.append(_event_record(event))

        complete = terminal_seen or exec_status in _TERMINAL_EXEC
        if events:
            last = max(last, int(events[-1]["event_id"]))
        stored = await asyncio.to_thread(
            _store,
            wf_id,
            actual_run,
            events,
            complete=complete,
            exec_status=exec_status,
            start_time=start_time,
            close_time=close_time,
        )
        result.update(stored=stored, last_event_id=last, complete=complete)
    except Exception as exc:  # noqa: BLE001 - 单流程失败由调用方汇总，不中断整轮
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("[history] 流程 %s 同步失败：%s", wf_id, result["error"])
    return result


def _last_event_id(wf_id: str, run_id: str) -> int:
    from aiops_agent import mode

    if mode.is_production():
        return _last_event_id_db(wf_id, run_id)
    meta = _demo_meta(wf_id, run_id)
    return int(meta.get("last_event_id") or 0)


def _store(wf_id: str, run_id: str, events: list[dict], **kwargs) -> int:
    from aiops_agent import mode

    if mode.is_production():
        return _append_db(wf_id, run_id, events, **kwargs)
    return _append_demo(wf_id, run_id, events, **kwargs)


def is_complete(wf_id: str, run_id: str) -> bool:
    """该 run 是否已完成最终同步（production 查同步位，demo 读 meta）。"""
    from aiops_agent import mode

    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            row = _run_row(conn, wf_id)
            return bool(row and row["run_id"] == run_id and row["history_complete"])
    meta = _demo_meta(wf_id, run_id)
    return bool(meta.get("complete"))


# ----------------------------------------------------------------------
# 读取（时间线 / 证据包共用）
# ----------------------------------------------------------------------


def _current_run_id(wf_id: str) -> str:
    from aiops_agent import mode

    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            row = _run_row(conn, wf_id)
            return str(row["run_id"] or "") if row else ""
    rows = [
        meta
        for meta in _demo_runs()
        if meta.get("workflow_id") == wf_id and meta.get("run_id")
    ]
    return str(rows[0]["run_id"]) if rows else ""


def read_events(
    wf_id: str,
    run_id: str | None = None,
    *,
    after_id: int = 0,
    limit: int = 1000,
    event_type: str = "",
    include_detail: bool = True,
) -> dict:
    """分页读取已投影事件（``event_id`` 升序，游标为 ``after_id``）。"""
    from aiops_agent import mode

    limit = max(1, min(int(limit), 10000))
    run_id = run_id or _current_run_id(wf_id)
    if not run_id:
        return {
            "run_id": "",
            "items": [],
            "next_after": None,
            "has_more": False,
        }
    if mode.is_production():
        table = db_layer.workflow_events
        query = (
            select(table)
            .where(
                and_(
                    table.c.workflow_id == wf_id,
                    table.c.run_id == run_id,
                    table.c.event_id > int(after_id),
                )
            )
            .order_by(table.c.event_id.asc())
            .limit(limit + 1)
        )
        if event_type:
            query = query.where(table.c.event_type == event_type)
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        has_more = len(rows) > limit
        items = [
            {
                "event_id": int(row["event_id"]),
                "event_type": row["event_type"],
                "event_time": db_layer.to_iso(row["event_time"]),
                "detail": row["detail"] if include_detail else None,
            }
            for row in rows[:limit]
        ]
    else:
        items = _read_demo_events(
            wf_id,
            run_id,
            after_id=int(after_id),
            limit=limit + 1,
            event_type=event_type,
        )
        has_more = len(items) > limit
        items = items[:limit]
        if not include_detail:
            items = [{**item, "detail": None} for item in items]
    next_after = items[-1]["event_id"] if has_more and items else None
    return {
        "run_id": run_id,
        "items": items,
        "next_after": next_after,
        "has_more": has_more,
    }


def sync_status(wf_id: str, run_id: str | None = None) -> dict:
    """同步完整性快照（FlowDetail / 时间线 / 证据包共用）。"""
    from aiops_agent import mode

    run_id = run_id or _current_run_id(wf_id)
    if not run_id:
        return {
            "run_id": "",
            "projected": False,
            "last_event_id": 0,
            "event_count": 0,
            "complete": False,
            "synced_at": None,
            "lag_seconds": 0.0,
            "gap": False,
        }
    if mode.is_production():
        runs = db_layer.workflow_runs
        table = db_layer.workflow_events
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            row = _run_row(conn, wf_id)
            count = int(
                conn.execute(
                    select(func.count())
                    .select_from(table)
                    .where(
                        and_(
                            table.c.workflow_id == wf_id,
                            table.c.run_id == run_id,
                        )
                    )
                ).scalar()
                or 0
            )
        last = int(row["last_history_event_id"] or 0) if row and row["run_id"] == run_id else 0
        synced_at = row["history_synced_at"] if row and row["run_id"] == run_id else None
        complete = bool(row and row["run_id"] == run_id and row["history_complete"])
        lag = 0.0 if complete else _lag_seconds(synced_at)
        return {
            "run_id": run_id,
            "projected": count > 0 or last > 0,
            "last_event_id": last,
            "event_count": count,
            "complete": complete,
            "synced_at": db_layer.to_iso(synced_at),
            "lag_seconds": round(lag, 3),
            "gap": last > 0 and count != last,
        }
    meta = _demo_meta(wf_id, run_id)
    last = int(meta.get("last_event_id") or 0)
    count = int(meta.get("event_count") or 0)
    complete = bool(meta.get("complete"))
    synced_at = meta.get("synced_at")
    return {
        "run_id": run_id,
        "projected": count > 0 or last > 0,
        "last_event_id": last,
        "event_count": count,
        "complete": complete,
        "synced_at": synced_at,
        "lag_seconds": 0.0 if complete else _lag_seconds(_parse_dt(synced_at)),
        "gap": last > 0 and count != last,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 批量同步 / 对账 / 指标
# ----------------------------------------------------------------------


def _pending_runs(limit: int = 50) -> list[dict]:
    """未完成最终同步的 run（按同步时间升序，优先补最久未更新的）。"""
    from aiops_agent import mode

    if mode.is_production():
        runs = db_layer.workflow_runs
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(runs.c.workflow_id, runs.c.run_id, runs.c.exec_status, runs.c.history_synced_at)
                .where(runs.c.history_complete.is_(False))
                .order_by(runs.c.history_synced_at.asc())
                .limit(limit)
            ).mappings()
            return [
                {
                    "wf_id": row["workflow_id"],
                    "run_id": row["run_id"],
                    "exec_status": row["exec_status"],
                }
                for row in rows
                if row["run_id"]
            ]
    return [
        {
            "wf_id": meta["workflow_id"],
            "run_id": meta["run_id"],
            "exec_status": meta.get("exec_status") or "",
        }
        for meta in reversed(_demo_runs())
        if not meta.get("complete")
    ][:limit]


async def _sync_targets(targets: list[dict]) -> dict:
    semaphore = asyncio.Semaphore(_SYNC_CONCURRENCY)

    async def one(target: dict) -> dict:
        async with semaphore:
            return await sync_workflow(target["wf_id"], target.get("run_id") or None)

    results = await asyncio.gather(*(one(target) for target in targets))
    errors = [
        f"{item['wf_id']}: {item['error']}" for item in results if item.get("error")
    ]
    return {
        "targets": len(targets),
        "synced": sum(1 for item in results if not item.get("error")),
        "stored": sum(int(item.get("stored") or 0) for item in results),
        "errors": errors,
        "results": results,
    }


async def sync_all(*, limit: int = 200) -> dict:
    """同步当前可见流程：运行中全量增量，终态只补未完成的。"""
    executions: list[dict] = []
    try:
        executions = await gw.list_executions(limit=limit)
    except Exception as exc:  # noqa: BLE001 - Temporal 不可达时退回本地未完成清单
        log.warning("[history] Temporal 列表不可用，改按本地未完成清单对账：%s", exc)
    targets = [
        {"wf_id": item["wf_id"], "run_id": item.get("run_id") or ""}
        for item in executions
        if item.get("wf_id")
        and (
            item.get("exec_status") == "RUNNING"
            or not is_complete(item["wf_id"], item.get("run_id") or "")
        )
    ]
    if not targets:
        targets = await asyncio.to_thread(_pending_runs, 50)
    summary = await _sync_targets(targets)
    summary["executions"] = len(executions)
    return summary


def _compute_stats() -> dict:
    from aiops_agent import mode

    now = datetime.now(timezone.utc).isoformat()
    if mode.is_production():
        runs = db_layer.workflow_runs
        table = db_layer.workflow_events
        engine = db_layer.get_engine()
        counts = (
            select(
                table.c.workflow_id.label("workflow_id"),
                table.c.run_id.label("run_id"),
                func.count().label("events"),
            )
            .group_by(table.c.workflow_id, table.c.run_id)
            .subquery()
        )
        with engine.connect() as conn:
            total = int(conn.execute(select(func.count()).select_from(runs)).scalar() or 0)
            incomplete = int(
                conn.execute(
                    select(func.count()).select_from(runs).where(runs.c.history_complete.is_(False))
                ).scalar()
                or 0
            )
            rows = conn.execute(
                select(
                    runs.c.workflow_id,
                    runs.c.run_id,
                    runs.c.last_history_event_id,
                    runs.c.history_complete,
                    runs.c.history_synced_at,
                    func.coalesce(counts.c.events, 0).label("events"),
                )
                .select_from(
                    runs.outerjoin(
                        counts,
                        and_(
                            counts.c.workflow_id == runs.c.workflow_id,
                            counts.c.run_id == runs.c.run_id,
                        ),
                    )
                )
                .order_by(runs.c.updated_at.desc())
                .limit(1000)
            ).all()
        gaps = sum(
            1
            for row in rows
            if int(row.last_history_event_id or 0) > 0
            and int(row.events or 0) != int(row.last_history_event_id or 0)
        )
        lag = max(
            (_lag_seconds(row.history_synced_at) for row in rows if not row.history_complete),
            default=0.0,
        )
    else:
        rows = _demo_runs()
        total = len(rows)
        incomplete = sum(1 for row in rows if not row.get("complete"))
        gaps = sum(
            1
            for row in rows
            if int(row.get("last_event_id") or 0) > 0
            and int(row.get("event_count") or 0) != int(row.get("last_event_id") or 0)
        )
        lag = max(
            (
                _lag_seconds(_parse_dt(row.get("synced_at")))
                for row in rows
                if not row.get("complete")
            ),
            default=0.0,
        )
    return {
        "runs": total,
        "incomplete": incomplete,
        "gaps": gaps,
        "complete": max(0, total - incomplete),
        "lag_seconds": round(lag, 3),
        "checked_at": now,
    }


def stats() -> dict:
    """返回最近一次对账结果（未对账时按当前存储现算一次）。"""
    snapshot = dict(_last_stats)
    if not snapshot.get("checked_at"):
        snapshot = refresh_metrics()
    return snapshot


def refresh_metrics() -> dict:
    """计算并写入 History 同步指标（未完成数 / 断档数 / 最大滞后）。"""
    global _last_stats
    snapshot = _compute_stats()
    _last_stats = snapshot
    metrics.set_history_state(
        incomplete=snapshot["incomplete"],
        gaps=snapshot["gaps"],
        lag_seconds=snapshot["lag_seconds"],
    )
    return snapshot


async def reconcile(*, limit: int = 50) -> dict:
    """对账补数：同步可见流程 + 本地未完成清单，并刷新完整性指标。"""
    summary = await sync_all()
    pending = await asyncio.to_thread(_pending_runs, limit)
    already = {
        (item.get("wf_id"), item.get("run_id"))
        for item in summary.get("results") or []
    }
    extra = [
        item
        for item in pending
        if (item.get("wf_id"), item.get("run_id")) not in already
    ]
    if extra:
        extra_summary = await _sync_targets(extra)
        summary["targets"] += extra_summary["targets"]
        summary["synced"] += extra_summary["synced"]
        summary["stored"] += extra_summary["stored"]
        summary["errors"].extend(extra_summary["errors"])
        summary["results"].extend(extra_summary["results"])
    snapshot = await asyncio.to_thread(refresh_metrics)
    summary["stats"] = snapshot
    return summary


def reset_for_tests() -> None:
    """清空内存统计（测试用；不删除任何投影数据）。"""
    _last_stats.clear()
    _last_stats.update(
        {
            "runs": 0,
            "incomplete": 0,
            "gaps": 0,
            "complete": 0,
            "lag_seconds": 0.0,
            "checked_at": "",
        }
    )
