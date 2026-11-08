"""写命令 outbox（M2）：意图审计 → 持久化命令 → 同步投递 → 有界退避重试。

背景：此前的写链路是「先 signal 再写审计」——
- Temporal 不可达时直接 502，用户意图没有留痕、也无法重试；
- signal 成功但审计写失败时，出现「无审计推进」；
- 进程在 signal 后崩溃，无法判断命令是否已送达。

本模块把「操作意图审计 + 待投递命令」一次性持久化，再尝试投递：

    PENDING ──signal 成功──▶ SUBMITTED ──业务回执──▶ ACCEPTED
       │                         │
       └──重试耗尽/业务拒绝──────┴──────────────▶ FAILED

- **production**：``audit_events`` + ``workflow_commands`` 同一 MySQL 事务；
- **demo**：``data/workflow_commands.json``（文件锁 + 原子替换）+ JSONL 审计。
审计不可用时拒绝新操作（fail-closed），绝不出现「无审计推进」。

``signal RPC 成功``只代表命令已送达 Temporal；``SUBMITTED`` 不是审批通过，
业务结果由工作流记录（``ACCEPTED`` 仅表示工作流已记录该命令）。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import command_contract, kill_switch
from aiops_agent import audit_chain
from aiops_agent import db as db_layer
from aiops_agent import metrics, mode

from . import audit
from .deps import gw, log
from .temporal_gateway import approval_receipt_from

STATUSES = ("PENDING", "SUBMITTED", "ACCEPTED", "FAILED")
TERMINAL = frozenset({"ACCEPTED", "FAILED"})
TYPES = ("approval", "second-approval", "deploy-command", "queue-patch")
DEFAULT_MAX_ATTEMPTS = 5
# 有界退避：与 Temporal 短暂不可达/抖动匹配；第 5 次即 FAILED，保持人工可处理。
BACKOFF_SECONDS = (2, 5, 15, 45, 120)
DISPATCH_BATCH = 20

SPOOL_NAME = "workflow_commands.json"

_thread_lock = threading.Lock()
_delivery_locks: dict[str, asyncio.Lock] = {}

# 重试前仍须满足的阶段（业务前置条件在意图提交时已校验一次，重试不重复
# 「已提交/已批准」类检查——命令幂等由工作流 command_id 保证）。
_ALLOWED_STAGES = {
    "approval": {"WAIT_APPROVAL"},
    "second-approval": {"WAIT_APPROVAL"},
    "deploy-command": {"NOTIFYING"},
    "queue-patch": {"NOTIFYING"},
}


class CommandStoreError(RuntimeError):
    """命令/审计存储不可用（由路由转为 503，且不得下发 signal）。"""


class CommandNotFound(LookupError):
    """命令不存在。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="microseconds")


def _data_dir() -> Path:
    """演示数据根与审计共用：测试 patch ``audit.DATA_DIR`` 即可隔离整条写链路。"""
    return audit.DATA_DIR


def _spool_path() -> Path:
    return _data_dir() / SPOOL_NAME


def _lock_path() -> Path:
    return _data_dir() / ".workflow_commands.lock"


@contextmanager
def _file_lock():
    """跨线程/跨进程互斥（demo 单主机）；锁目录不可建即视为存储不可用。"""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_spool() -> dict:
    path = _spool_path()
    if not path.is_file():
        return {"version": 1, "commands": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # fail-closed：损坏的 spool 不能当作空库覆盖（否则命令会静默丢失）
        raise CommandStoreError(f"命令存储不可读：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("commands"), dict):
        raise CommandStoreError("命令存储格式非法")
    return data


def _write_spool(data: dict) -> None:
    """原子替换：同目录临时文件 + fsync + rename，崩溃不会留下半个 JSON。"""
    path = _spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _row_to_record(row) -> dict:
    return {
        "command_id": row["command_id"],
        "command_type": row["command_type"],
        "wf_id": row["wf_id"],
        "run_id": row["run_id"] or "",
        "actor": row["actor"],
        "channel": row["channel"],
        "audit_action": row["audit_action"] or "",
        "payload": row["payload"] or {},
        "audit_params": row["audit_params"] or {},
        "status": row["status"],
        "attempts": int(row["attempts"] or 0),
        "max_attempts": int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS),
        "next_retry_at": db_layer.to_iso(row["next_retry_at"]),
        "last_error": row["last_error"] or "",
        "created_at": db_layer.to_iso(row["created_at"]),
        "updated_at": db_layer.to_iso(row["updated_at"]),
        "submitted_at": db_layer.to_iso(row["submitted_at"]),
        "completed_at": db_layer.to_iso(row["completed_at"]),
    }


def record_intent(
    *,
    command_type: str,
    wf_id: str,
    actor: str,
    channel: str,
    payload: dict | str,
    audit_action: str,
    command_id: str | None = None,
    run_id: str = "",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    audit_params: dict | None = None,
) -> dict:
    """持久化「操作意图审计 + 待投递命令」；失败抛 :class:`CommandStoreError`。

    ``command_id`` 可由调用方传入，以与审批/窗口命令信封内的 ID 保持一致
    （工作流按该 ID 幂等去重；不一致会导致回执无法关联）。
    调用方在拿到返回的 ``command_id`` 后才能尝试投递；本函数不发送任何信号。
    """
    if command_type not in TYPES:
        raise ValueError(f"未知命令类型：{command_type}")
    command_id = str(command_id or "").strip() or command_contract.new_command_id()
    if len(command_id) > 64:
        raise CommandStoreError("命令 ID 过长（最多 64 字符）")
    if get(command_id) is not None:
        raise CommandStoreError(f"命令 ID 已存在：{command_id}")
    extra_params = dict(audit_params or {})
    now = _now()
    record = {
        "command_id": command_id,
        "command_type": command_type,
        "wf_id": wf_id,
        "run_id": run_id or "",
        "actor": actor,
        "channel": channel,
        "audit_action": audit_action,
        "payload": payload,
        "audit_params": extra_params,
        "status": "PENDING",
        "attempts": 0,
        "max_attempts": max(1, int(max_attempts)),
        "next_retry_at": now,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
        "submitted_at": None,
        "completed_at": None,
    }
    params = {
        **extra_params,
        "channel": channel,
        "delivery": "PENDING",
        "command_id": command_id,
    }
    try:
        if mode.is_production():
            engine = db_layer.get_engine()
            with engine.begin() as conn:
                # 同一事务：审计写不进去则命令也不存在，绝不出现「无审计推进」。
                # 审计走哈希链受控链头（与 bff.audit.write_audit 共用同一串行化入口）。
                audit_chain.append_db(
                    conn,
                    {
                        "ts": _now_iso(),
                        "actor": actor,
                        "action": audit_action,
                        "wf_id": wf_id,
                        "target": None,
                        "params": params,
                        "result": "pending",
                    },
                    command_id=command_id,
                )
                conn.execute(
                    db_layer.workflow_commands.insert().values(
                        command_id=command_id,
                        command_type=command_type,
                        wf_id=wf_id,
                        run_id=run_id or "",
                        actor=actor,
                        channel=channel,
                        audit_action=audit_action,
                        payload=payload,
                        audit_params=extra_params or None,
                        status="PENDING",
                        attempts=0,
                        max_attempts=record["max_attempts"],
                        next_retry_at=db_layer.to_dt(now),
                        last_error=None,
                        created_at=db_layer.to_dt(now),
                        updated_at=db_layer.to_dt(now),
                        submitted_at=None,
                        completed_at=None,
                    )
                )
        else:
            # demo：先写审计（不可用则不落 spool、不下发），再原子写命令。
            audit.write_audit(
                actor=actor,
                action=audit_action,
                wf_id=wf_id,
                params=params,
                result="pending",
            )
            with _file_lock():
                data = _load_spool()
                data["commands"][command_id] = _json_record(record)
                _write_spool(data)
    except CommandStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一 fail-closed
        raise CommandStoreError(f"审计或命令存储不可用：{exc}") from exc
    _refresh_metrics()
    return _json_record(record)


def _json_record(record: dict) -> dict:
    """datetime → ISO（demo JSON 与 DB 读取口径一致）。"""
    out = dict(record)
    for key in ("next_retry_at", "created_at", "updated_at", "submitted_at", "completed_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = db_layer.to_iso(value)
    return out


def get(command_id: str) -> dict | None:
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(db_layer.workflow_commands).where(
                    db_layer.workflow_commands.c.command_id == command_id
                )
            ).mappings().first()
            return _row_to_record(row) if row else None
    with _file_lock():
        data = _load_spool()
        record = data["commands"].get(command_id)
        return dict(record) if record else None


def list_commands(
    *,
    status: str | None = None,
    wf_id: str | None = None,
    limit: int = 50,
    oldest_first: bool = False,
) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    if mode.is_production():
        table = db_layer.workflow_commands
        order = table.c.created_at.asc() if oldest_first else table.c.created_at.desc()
        query = select(table).order_by(order).limit(limit)
        if status:
            query = query.where(table.c.status == status)
        if wf_id:
            query = query.where(table.c.wf_id == wf_id)
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            return [_row_to_record(row) for row in conn.execute(query).mappings()]
    with _file_lock():
        data = _load_spool()
        rows = [dict(item) for item in data["commands"].values()]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    if wf_id:
        rows = [row for row in rows if row.get("wf_id") == wf_id]
    rows.sort(
        key=lambda item: str(item.get("created_at") or ""),
        reverse=not oldest_first,
    )
    return rows[:limit]


def _update(command_id: str, **changes) -> dict:
    """更新命令状态（demo 文件锁内 read-modify-write；production 单行 UPDATE）。"""
    changes = {**changes, "updated_at": _now_iso()}
    if mode.is_production():
        values = {}
        for key, value in changes.items():
            values[key] = db_layer.to_dt(value) if key.endswith("_at") and value else value
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            conn.execute(
                db_layer.workflow_commands.update()
                .where(db_layer.workflow_commands.c.command_id == command_id)
                .values(**values)
            )
        record = get(command_id)
        if record is None:
            raise CommandNotFound(command_id)
        return record
    with _file_lock():
        data = _load_spool()
        record = data["commands"].get(command_id)
        if record is None:
            raise CommandNotFound(command_id)
        record.update(_json_record({**record, **changes}))
        data["commands"][command_id] = record
        _write_spool(data)
    return dict(record)


def _terminal(record: dict, status: str, *, error: str = "", outcome: str = "") -> dict:
    updated = _update(
        record["command_id"],
        status=status,
        last_error=error,
        completed_at=_now_iso(),
    )
    _write_outcome_audit(updated, outcome=outcome)
    _refresh_metrics()
    return updated


def _write_outcome_audit(record: dict, *, outcome: str = "") -> None:
    """结果回写审计（意图行保持 pending，结果行独立可追溯）。"""
    params = _audit_params(record)
    if record["status"] in ("ACCEPTED", "SUBMITTED"):
        result = "signaled"
    else:
        result = outcome or "failed"
        if result == "rejected":
            # 业务拒收区别于投递失败：审计可直接看出「已送达但未生效」。
            params["delivery"] = "rejected"
    try:
        audit.write_audit(
            actor=record["actor"],
            action=record["audit_action"] or record["command_type"],
            wf_id=record["wf_id"],
            params=params,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 - 命令状态已持久化；审计补记失败不得回滚业务
        log.warning("[commands] 结果审计写入失败：command=%s %s", record["command_id"], exc)


def _audit_params(record: dict) -> dict:
    payload = record.get("payload")
    params: dict = dict(record.get("audit_params") or {})
    params.update(
        channel=record.get("channel") or "",
        delivery=record.get("status", "").lower(),
        command_id=record.get("command_id") or "",
    )
    if isinstance(payload, dict) and payload.get("contract") == "approval-evidence-v1":
        params.update(
            decision=payload.get("decision"),
            comment=payload.get("comment", ""),
            case_id=payload.get("case_id"),
            patch_id=payload.get("patch_id"),
            diff_hash=payload.get("diff_hash"),
            test_report_hash=payload.get("test_report_hash"),
            policy_sha256=payload.get("policy_sha256"),
            run_id=payload.get("run_id"),
        )
    elif isinstance(payload, dict) and payload.get("contract") == "workflow-command-v1":
        params["request"] = payload.get("payload")
    else:
        params["decision"] = payload
    if record.get("last_error"):
        params["error"] = record["last_error"]
    return params


def stats() -> dict:
    """积压/最老等待/近 24h 失败数（metrics、readiness 降级共用）。"""
    cutoff = (_now() - timedelta(hours=24)).isoformat()
    if mode.is_production():
        table = db_layer.workflow_commands
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            pending_rows = conn.execute(
                select(table.c.created_at).where(table.c.status == "PENDING")
            ).all()
            failed_24h = conn.execute(
                select(table.c.command_id).where(
                    table.c.status == "FAILED", table.c.completed_at >= db_layer.to_dt(cutoff)
                )
            ).all()
    else:
        rows = list_commands(limit=200)
        pending_rows = [
            (row.get("created_at"),) for row in rows if row.get("status") == "PENDING"
        ]
        failed_24h = [
            row
            for row in rows
            if row.get("status") == "FAILED"
            and str(row.get("completed_at") or "") >= cutoff
        ]
    oldest = None
    for (created_at,) in pending_rows:
        try:
            stamp = datetime.fromisoformat(str(created_at))
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        oldest = stamp if oldest is None or stamp < oldest else oldest
    return {
        "pending": len(pending_rows),
        "oldest_pending_seconds": (
            max(0.0, (_now() - oldest).total_seconds()) if oldest is not None else 0.0
        ),
        "failed_24h": len(failed_24h),
    }


def _refresh_metrics() -> None:
    try:
        values = stats()
        metrics.set_command_state(
            pending=values["pending"],
            oldest_seconds=values["oldest_pending_seconds"],
            failed_24h=values["failed_24h"],
        )
    except Exception as exc:  # noqa: BLE001 - 指标不得干扰命令主链路
        log.debug("[commands] 指标刷新失败：%s", exc)


def due(limit: int = DISPATCH_BATCH) -> list[dict]:
    """到期的 PENDING 命令（最老优先，避免持续新增导致旧命令饥饿）。"""
    now = _now_iso()
    rows = list_commands(status="PENDING", limit=200, oldest_first=True)
    ready = [
        row for row in rows if not row.get("next_retry_at") or str(row["next_retry_at"]) <= now
    ]
    return ready[: max(1, int(limit))]


async def _safe_detail(wf_id: str) -> dict | None:
    try:
        return await gw.flow_detail(wf_id)
    except Exception as exc:  # noqa: BLE001 - Temporal 不可达/流程不存在都走重试或失败
        log.debug("[commands] 读取流程详情失败：wf=%s %s", wf_id, exc)
        return None


def _safety_problem(record: dict, detail: dict | None) -> str | None:
    """重试前复核最新安全状态：kill switch / 运行态 / 阶段。"""
    try:
        state = kill_switch.get_state()
    except kill_switch.KillSwitchError as exc:
        return f"kill switch 状态不可读：{exc}"
    if state.get("active"):
        return f"kill switch 已激活（{state.get('reason') or '未注明'}）"
    if detail is None:
        return None  # 交由投递本身失败并退避（Temporal 暂不可达）
    if detail.get("exec_status") != "RUNNING":
        return f"流程已结束（{detail.get('exec_status')}），命令不再适用"
    stage = (detail.get("status") or {}).get("stage")
    allowed = _ALLOWED_STAGES.get(record["command_type"], set())
    if stage not in allowed:
        return f"流程阶段已变化（当前 {stage}），命令不再适用"
    return None


async def _receipt(record: dict, detail: dict | None, *, attempts: int | None = None):
    """查询业务回执：accepted / rejected / None（未可见）。"""
    payload = record.get("payload")
    command_id = record.get("command_id") or ""
    command_type = record["command_type"]
    if command_type in ("approval", "second-approval"):
        if not isinstance(payload, dict):
            return None
        kwargs = {"attempts": attempts, "delay": 0.1} if attempts else {}
        state, receipt = await gw.approval_receipt(record["wf_id"], command_id, **kwargs)
        if state in ("accepted", "rejected"):
            return state, receipt
        return None

    payload = payload if isinstance(payload, dict) else {}
    request = payload.get("payload") or {}
    for source in (
        (detail or {}).get("status"),
        (detail or {}).get("result"),
    ):
        if not isinstance(source, dict):
            continue
        for rejection in source.get("command_rejections") or []:
            if rejection.get("command_id") == command_id:
                return "rejected", rejection
        # 工作流一旦应用命令就把 ID 记入 command_ids：即使随后消费/出队，
        # 回执仍可确认「已记录」，不会因可见性时序退回 submitted。
        if command_id and command_id in (source.get("command_ids") or []):
            return "accepted", {"command_id": command_id}
        if command_type == "deploy-command":
            if source.get("deploy_command") == request.get("command"):
                return "accepted", {"deploy_command": source.get("deploy_command")}
        elif command_type == "queue-patch":
            for queued in source.get("queued_patches") or []:
                if isinstance(queued, dict):
                    if queued.get("workflow_id") == request.get("new_wf_id"):
                        return "accepted", queued
                    if queued.get("alert_id") == request.get("alert_id"):
                        return "accepted", queued
                elif str(queued) == str(request.get("alert_id")):
                    return "accepted", {"alert_id": queued}
    return None


async def _send(record: dict) -> None:
    payload = record.get("payload")
    if record["command_type"] == "approval":
        await gw.signal_approval(record["wf_id"], payload)
    elif record["command_type"] == "second-approval":
        await gw.signal_second_approval(record["wf_id"], payload)
    elif record["command_type"] == "deploy-command":
        await gw.signal_deploy_command(record["wf_id"], payload)
    else:
        await gw.signal_queue_patch(record["wf_id"], payload)
    gw.invalidate_cache()


def _backoff(attempts: int) -> datetime:
    index = min(max(0, attempts - 1), len(BACKOFF_SECONDS) - 1)
    return _now() + timedelta(seconds=BACKOFF_SECONDS[index])


async def deliver(command_id: str) -> dict:
    """尝试投递一条命令；返回最新记录（不抛业务异常）。"""
    lock = _delivery_locks.setdefault(command_id, asyncio.Lock())
    async with lock:
        record = get(command_id)
        if record is None:
            raise CommandNotFound(command_id)
        if record["status"] in TERMINAL:
            return record
        if record["status"] == "SUBMITTED":
            return record  # 已送达 Temporal；业务回执由查询端点刷新

        detail = await _safe_detail(record["wf_id"])

        # 重放窗口：上次 signal 已送达但进程在回写前崩溃 → 先查回执，不重复发送。
        if record["attempts"] > 0:
            receipt = await _receipt(record, detail, attempts=1)
            if receipt is not None:
                kind, body = receipt
                if kind == "accepted":
                    return _terminal(record, "ACCEPTED")
                return _terminal(
                    record,
                    "FAILED",
                    error=str((body or {}).get("reason") or "工作流拒绝了该命令"),
                    outcome="rejected",
                )

        problem = _safety_problem(record, detail)
        # kill switch 为确定性拒绝（与 Temporal 是否可达无关）；阶段/运行态检查
        # 需要 detail；detail 缺失时按暂不可达退避。
        if problem and (detail is not None or problem.startswith("kill switch")):
            return _terminal(record, "FAILED", error=problem, outcome="rejected")

        attempts = record["attempts"] + 1
        if detail is None and problem is None:
            # Temporal 不可达：不盲目发 signal，按退避重试。
            error = "Temporal 暂不可达，命令已入队等待重试"
            if attempts >= record["max_attempts"]:
                return _terminal(record, "FAILED", error=error)
            return _update(
                record["command_id"],
                attempts=attempts,
                last_error=error,
                next_retry_at=db_layer.to_iso(_backoff(attempts)),
            )

        try:
            await _send(record)
        except Exception as exc:  # noqa: BLE001 - 投递失败必须转可查询状态
            error = f"Temporal 投递失败：{type(exc).__name__}: {exc}"
            if attempts >= record["max_attempts"]:
                return _terminal(record, "FAILED", error=error)
            updated = _update(
                record["command_id"],
                attempts=attempts,
                last_error=error,
                next_retry_at=db_layer.to_iso(_backoff(attempts)),
            )
            log.warning("[commands] 投递失败将重试：%s %s", command_id, error)
            _refresh_metrics()
            return updated

        updated = _update(
            record["command_id"],
            status="SUBMITTED",
            attempts=attempts,
            submitted_at=_now_iso(),
            last_error="",
            next_retry_at=None,
        )
        _refresh_metrics()
        receipt_detail = detail
        if record["command_type"] in ("deploy-command", "queue-patch"):
            # 窗口命令的回执来自 status（deploy_command/queued_patches/command_ids），
            # 发送前的 detail 不包含本命令，必须重新读取。
            receipt_detail = await _safe_detail(record["wf_id"])
        receipt = await _receipt(updated, receipt_detail)
        if receipt is not None:
            kind, body = receipt
            if kind == "accepted":
                return _terminal(updated, "ACCEPTED")
            return _terminal(
                updated,
                "FAILED",
                error=str((body or {}).get("reason") or "工作流拒绝了该命令"),
                outcome="rejected",
            )
        _write_outcome_audit(updated)
        return updated


async def refresh_receipt(command_id: str) -> dict:
    """查询端点用：SUBMITTED 命令补查一次业务回执（不重发信号）。"""
    record = get(command_id)
    if record is None:
        raise CommandNotFound(command_id)
    if record["status"] != "SUBMITTED":
        return record
    receipt = await _receipt(record, await _safe_detail(record["wf_id"]), attempts=1)
    if receipt is None:
        return record
    kind, body = receipt
    if kind == "accepted":
        return _terminal(record, "ACCEPTED")
    return _terminal(
        record, "FAILED", error=str((body or {}).get("reason") or "工作流拒绝了该命令"), outcome="rejected"
    )


async def dispatch_due(limit: int = DISPATCH_BATCH) -> list[dict]:
    """后台调度一轮：只处理到期的 PENDING（单进程锁，避免与请求路径并发重复发送）。"""
    results: list[dict] = []
    for record in due(limit):
        try:
            results.append(await deliver(record["command_id"]))
        except CommandNotFound:
            continue
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞后续
            log.warning("[commands] 调度失败：%s %s", record["command_id"], exc)
    return results


def sweep_seconds() -> int:
    try:
        return max(1, int(os.environ.get("AIOPS_COMMAND_SWEEP_SECONDS", "5")))
    except ValueError:
        return 5


def reset_for_tests() -> None:
    """清空进程内锁（测试切换 DATA_DIR / DB 时使用）。"""
    _delivery_locks.clear()
