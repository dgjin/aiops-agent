"""控制台操作审计：双后端写入、游标分页、报表聚合、哈希链与归档（M4）。

每个写操作（审批 / 窗口指令 / 排队 / 配置变更 / 待办处置 / 证据包导出）成功后被记录，
前端不可绕过。

双后端说明：
    - **demo**（AIOPS_MODE=demo）：落 ``data/web-audit/audit-YYYYMMDD.jsonl``，
      保持既有形态与热行为（测试经 ``mock.patch.object(audit, "DATA_DIR", ...)`` 隔离）；
    - **production**：落 MySQL ``audit_events`` 表（索引 ts / workflow_id / actor），
      支持跨实例聚合、游标分页与报表导出；两后端返回的 record 结构一致。

M4 增强：
    - 每条新记录携带 ``event_id/prev_hash/entry_hash``，由
      :mod:`aiops_agent.audit_chain` 在文件锁 / 数据库行锁内串行追加；
    - :func:`verify` 可发现链中记录的修改、插入与删除（**tamper-evident**，
      不等同外部 WORM；拥有本机写权限者可整体重算链）；
    - :func:`archive` 只归档校验通过的过期记录，``purge=True`` 才会删除；
      未归档记录不会被 TTL 静默删除。
"""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from aiops_agent import audit_chain
from aiops_agent import db as db_layer
from aiops_agent import mode

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（既有数据零迁移）；生产走 DB，仅作归档兜底


def _audit_dir() -> Path:
    return DATA_DIR / "web-audit"


def _to_record(row) -> dict:
    """DB 行 → 与文件后端一致的 record 结构（``detail`` 即 ``params``）。"""
    keys = row.keys()
    return {
        "id": row["id"],
        "ts": db_layer.to_iso(row["ts"]),
        "actor": row["actor"],
        "action": row["action"],
        "wf_id": row["workflow_id"],
        "target": row["target"],
        "params": row["detail"] or {},
        "result": row["result"],
        "event_id": row["event_id"] if "event_id" in keys else "",
        "command_id": row["command_id"] if "command_id" in keys else "",
        "prev_hash": row["prev_hash"] if "prev_hash" in keys else "",
        "entry_hash": row["entry_hash"] if "entry_hash" in keys else "",
    }


def write_audit(
    *,
    actor: str,
    action: str,
    wf_id: str = "",
    params: dict | None = None,
    result: str = "ok",
    target: str | None = None,
    command_id: str = "",
) -> dict:
    """写入一条操作审计（带哈希链）。

    ``wf_id`` 仅用于**工作流类**操作；**配置类**操作（如 monitored-app:*）应留空 ``wf_id``
    并把被操作对象写入 ``target``，避免前端把它误当作工作流 ID 生成链接（历史缺陷 B1）。
    ``result`` 默认 ``ok``；工作流写操作可显式传 ``signaled``。
    ``command_id`` 用于把审计条目关联到 outbox 命令（`commands.record_intent` 同事务写入）。
    """
    record = {
        "ts": db_layer.now_iso(),
        "actor": actor,
        "action": action,
        "wf_id": wf_id,
        "target": target,
        "params": params or {},
        "result": result,
    }
    if command_id:
        record["command_id"] = command_id
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            return audit_chain.append_db(conn, record, command_id=command_id or None)

    directory = _audit_dir()
    directory.mkdir(parents=True, exist_ok=True)  # 先建目录再写盘，避免静默失败
    with audit_chain.file_lock(directory):
        return audit_chain.append_demo_locked(directory, record)


# ----------------------------------------------------------------------
# 读取：最近 N 条（兼容）+ 游标分页
# ----------------------------------------------------------------------


def _iter_demo_reversed(audit_dir: Path):
    """按文件与行倒序逐条产出（坏行跳过并保留 source 便于排查）。"""
    if not audit_dir.is_dir():
        return
    for path in sorted(audit_dir.glob("audit-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                value["source"] = path.name
                yield value


def read_audit(limit: int = 100) -> list[dict]:
    """读取最近操作审计（最新在前）。"""
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(db_layer.audit_events)
                .order_by(db_layer.audit_events.c.id.desc())
                .limit(limit)
            ).mappings()
            return [_to_record(row) for row in rows]

    rows: list[dict] = []
    for row in _iter_demo_reversed(_audit_dir()):
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode_cursor(payload: dict) -> str:
    """公开游标编码（流程审计 / 操作审计共用同一不可读游标格式）。"""
    return _encode_cursor(payload)


def decode_cursor(value: str) -> dict:
    """公开游标解码；非法游标抛 ``ValueError``（路由转 400）。"""
    return _decode_cursor(value)


def _decode_cursor(value: str) -> dict:
    padded = value + "=" * (-len(value) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("游标非法") from exc
    if not isinstance(data, dict):
        raise ValueError("游标非法")
    return data


def _parse_time(value: str, *, field: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法 ISO 时间：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches(row: dict, *, actor: str, action: str, result: str, wf_id: str) -> bool:
    if actor and str(row.get("actor") or "") != actor:
        return False
    if action and action.lower() not in str(row.get("action") or "").lower():
        return False
    if result and str(row.get("result") or "") != result:
        return False
    if wf_id and str(row.get("wf_id") or "") != wf_id:
        return False
    return True


def _row_time(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("ts") or "")) if row.get("ts") else None
    except ValueError:
        return None


def list_audit(
    *,
    limit: int = 50,
    cursor: str = "",
    actor: str = "",
    action: str = "",
    result: str = "",
    wf_id: str = "",
    start: str = "",
    end: str = "",
) -> dict:
    """操作审计游标分页（M4）。

    排序固定为写入倒序（production 按自增 id，demo 按 ``(ts, event_id)``）；
    游标只向前翻页，避免 offset 在并发写入时重复/漏读。
    """
    limit = max(1, min(int(limit), 500))
    start_dt = _parse_time(start, field="start")
    end_dt = _parse_time(end, field="end")
    payload = _decode_cursor(cursor) if cursor else {}

    if mode.is_production():
        table = db_layer.audit_events
        query = select(table).order_by(table.c.id.desc()).limit(limit + 1)
        if payload.get("id"):
            query = query.where(table.c.id < int(payload["id"]))
        if actor:
            query = query.where(table.c.actor == actor)
        if action:
            query = query.where(table.c.action.like(f"%{action}%"))
        if result:
            query = query.where(table.c.result == result)
        if wf_id:
            query = query.where(table.c.workflow_id == wf_id)
        if start_dt is not None:
            query = query.where(table.c.ts >= db_layer.to_dt(start_dt))
        if end_dt is not None:
            query = query.where(table.c.ts <= db_layer.to_dt(end_dt))
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = [_to_record(row) for row in conn.execute(query).mappings()]
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = _encode_cursor({"id": items[-1]["id"]}) if has_more and items else ""
        return {"items": items, "next_cursor": next_cursor or None, "has_more": has_more}

    cursor_ts = str(payload.get("ts") or "")
    cursor_event = str(payload.get("event_id") or "")
    items: list[dict] = []
    has_more = False
    for row in _iter_demo_reversed(_audit_dir()):
        row_ts = str(row.get("ts") or "")
        row_event = str(row.get("event_id") or "")
        if cursor and (row_ts, row_event) >= (cursor_ts, cursor_event):
            continue
        if not _matches(row, actor=actor, action=action, result=result, wf_id=wf_id):
            continue
        stamp = _row_time(row)
        if start_dt is not None and (stamp is None or stamp < start_dt):
            continue
        if end_dt is not None and (stamp is None or stamp > end_dt):
            continue
        items.append(row)
        if len(items) > limit:
            has_more = True
            items.pop()
            break
    next_cursor = ""
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(
            {"ts": str(last.get("ts") or ""), "event_id": str(last.get("event_id") or "")}
        )
    return {"items": items, "next_cursor": next_cursor or None, "has_more": has_more}


def rows_for_workflow(wf_id: str, limit: int = 500) -> list[dict]:
    """某流程的全部操作审计（时间线 / 证据包共用；最多 limit 条）。"""
    return list_audit(limit=limit, wf_id=wf_id)["items"]


# ----------------------------------------------------------------------
# 聚合报表（P3-07，保持既有口径）
# ----------------------------------------------------------------------


def _top(counter: Counter, top: int) -> list[dict]:
    return [{"key": key, "count": count} for key, count in counter.most_common(top)]


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aggregate_audit(*, days: int = 30, top: int = 10, sample_limit: int = 5000) -> dict:
    """审计报表聚合：按操作者 / 动作 / 日期 / 结果聚合计数。

    - production：SQL ``GROUP BY`` 全量聚合（无采样截断）；
    - demo：读最近 ``sample_limit`` 条后内存聚合（``truncated=true`` 时报表为
      采样口径，仅作趋势参考）。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if mode.is_production():
        table = db_layer.audit_events
        where = table.c.ts >= db_layer.to_dt(cutoff)
        engine = db_layer.get_engine()

        def _group(column):  # noqa: ANN001, ANN202 - 内部辅助
            return [
                {"key": str(key or "unknown"), "count": int(count)}
                for key, count in conn.execute(
                    select(column, func.count())
                    .where(where)
                    .group_by(column)
                    .order_by(func.count().desc())
                    .limit(top)
                ).all()
            ]

        with engine.connect() as conn:
            total = int(conn.execute(select(func.count()).select_from(table).where(where)).scalar() or 0)
            latest = conn.execute(select(func.max(table.c.ts)).where(where)).scalar()
            by_actor = _group(table.c.actor)
            by_action = _group(table.c.action)
            by_result = _group(table.c.result)
            by_day = [
                {"key": str(day), "count": int(count)}
                for day, count in conn.execute(
                    select(func.date(table.c.ts), func.count())
                    .where(where)
                    .group_by(func.date(table.c.ts))
                    .order_by(func.date(table.c.ts))
                ).all()
            ]
        return {
            "total": total,
            "days": days,
            "truncated": False,
            "by_actor": by_actor,
            "by_action": by_action,
            "by_day": by_day,
            "by_result": by_result,
            "latest_ts": db_layer.to_iso(latest),
        }

    rows = read_audit(limit=sample_limit)
    actors: Counter = Counter()
    actions: Counter = Counter()
    results: Counter = Counter()
    days_counter: Counter = Counter()
    latest_ts: str | None = None
    total = 0
    for row in rows:  # 最新在前
        parsed = _parse_ts(row.get("ts") or "")
        if parsed is None or parsed < cutoff:
            continue
        total += 1
        if latest_ts is None:
            latest_ts = row.get("ts")
        actors[str(row.get("actor") or "unknown")] += 1
        actions[str(row.get("action") or "unknown")] += 1
        results[str(row.get("result") or "unknown")] += 1
        days_counter[parsed.strftime("%Y-%m-%d")] += 1
    return {
        "total": total,
        "days": days,
        "truncated": len(rows) >= sample_limit,
        "by_actor": _top(actors, top),
        "by_action": _top(actions, top),
        "by_day": [{"key": day, "count": count} for day, count in sorted(days_counter.items())],
        "by_result": _top(results, top),
        "latest_ts": latest_ts,
    }


# ----------------------------------------------------------------------
# 完整性校验与归档（M4）
# ----------------------------------------------------------------------


def _demo_archives() -> list[dict]:
    try:
        manifest = audit_chain.load_manifest(_audit_dir())
    except audit_chain.AuditChainError as exc:
        return [{"error": str(exc)}]
    return list(manifest.get("entries") or [])[:20]


def _db_archives(limit: int = 20) -> list[dict]:
    table = db_layer.audit_archives
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(table).order_by(table.c.created_at.desc()).limit(limit)
        ).mappings()
        return [
            {
                "archive_id": row["archive_id"],
                "day": row["day"],
                "path": row["path"],
                "rows": int(row["rows"] or 0),
                "first_id": row["first_id"],
                "last_id": row["last_id"],
                "chain_ok": bool(row["chain_ok"]),
                "sha256": row["manifest_sha256"],
                "status": row["status"],
                "created_at": db_layer.to_iso(row["created_at"]),
            }
            for row in rows
        ]


def verify() -> dict:
    """校验审计哈希链并返回链头/归档清单。"""
    if mode.is_production():
        engine = db_layer.get_engine()
        result = audit_chain.verify_db(engine)
        with engine.connect() as conn:
            head = conn.execute(
                select(db_layer.audit_chain_heads.c.last_hash).where(
                    db_layer.audit_chain_heads.c.scope == audit_chain.SCOPE
                )
            ).scalar() or ""
        result["head"] = head
        result["archives"] = _db_archives()
    else:
        result = audit_chain.verify_demo(_audit_dir())
        result["head"] = audit_chain.chain_tail(_audit_dir())["last_hash"]
        result["archives"] = _demo_archives()
    result["mode"] = mode.mode()
    result["algorithm"] = "sha256(prev_hash + canonical(record))"
    result["boundary"] = "tamper-evident（本机可发现篡改）；不等同外部 WORM 不可变存储"
    return result


def archive(*, days: int = 365, purge: bool = False) -> dict:
    """归档过期审计；``purge=True`` 仅删除已归档且 SHA-256 复核通过的文件/行。"""
    now = datetime.now(timezone.utc)
    if mode.is_production():
        engine = db_layer.get_engine()
        result = audit_chain.archive_db(
            engine, _audit_dir(), now - timedelta(days=days), purge=purge, now=now
        )
    else:
        cutoff_day = (now - timedelta(days=days)).strftime("%Y%m%d")
        result = audit_chain.archive_demo(_audit_dir(), cutoff_day, purge=purge, now=now)
    result["days"] = days
    result["purge"] = purge
    return result
