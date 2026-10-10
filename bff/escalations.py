"""转人工处置闭环（P1-3）：ESCALATED 流程 → 待办 → 指派 → 处置/重试 → 超时再升级。

背景（评估报告 P1-3）：升级人工后此前仅剩 KPI 计数——没有待办列表、责任人指派、
超时再升级等处置通道，转人工场景容易「烂尾」。

双后端（AIOPS_MODE 决定，同 monitored_apps 模式）：
    - **demo**：``data/escalations.json``（临时文件 + 原子替换；每次读取重新解析——热生效）；
    - **production**：MySQL ``escalations`` 表（每次查询即读库）。

状态机：
    open（新登记）→ assigned（已指派责任人）→ closed（已处置）
    - assign 可反复改派（open/assigned 均可）；
    - close 记录处置备注并落 history；
    - retry（路由层以新幂等键重启修复流程——需求来源重开需求流——后调 ``apply_retried``）→ closed。

超时再升级：
    ``due_for_re_escalation`` 找出「open/assigned 且登记超过 SLA（默认 30 分钟，
    ``AIOPS_ESCALATION_SLA_MINUTES`` 可配）且未再升级」的条目；
    ``sweep_due``（由 BFF 后台循环周期调用）经通知渠道再升级一次并打标
    ``re_escalated``（每单只升级一次，防轰炸）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（与 monitored_apps 同口径）
DATA_PATH = DATA_DIR / "escalations.json"

STATUSES = ("open", "assigned", "closed")
DEFAULT_SLA_MINUTES = 30


class EscalationStoreError(ValueError):
    """处置操作校验错误（由 BFF 统一转 400/404）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sla_minutes() -> int:
    try:
        value = int(os.environ.get("AIOPS_ESCALATION_SLA_MINUTES", ""))
        return value if value > 0 else DEFAULT_SLA_MINUTES
    except ValueError:
        return DEFAULT_SLA_MINUTES


# ----------------------------------------------------------------------
# 自动升级原因：gate_events → 人类可读摘要
# ----------------------------------------------------------------------

def auto_reason_from_events(gate_events: list[str] | None) -> str:
    """从闸门事件序列推导升级原因摘要（取最后一条有意义的阻断/劣化事件）。"""
    for event in reversed([str(e) for e in gate_events or []]):
        if event.startswith("gate1:blocked"):
            detail = event.split(":", 2)[-1]
            return f"闸门1：根因置信度不足（{detail}）"
        if event.startswith("gate2b:timeout"):
            return "闸门2（二级）：受保护目录审批超时自动驳回"
        if event.startswith("gate2b:"):
            return f"闸门2（二级）：二级审批未通过（{event.split(':', 1)[1]}）"
        if event.startswith("gate2:timeout"):
            return "闸门2：审批超时自动驳回"
        if event.startswith("gate2:"):
            return f"闸门2：人工审批未通过（{event.split(':', 1)[1]}）"
        if event.startswith("tests:failed"):
            # 新格式 tests:failed:attempt=N:<具体失败摘要>（生成环节降级 / 静态扫描拦截 /
            # 工作区准备失败等）→ 升级原因直达真实原因；旧格式无第 4 段时回退概述
            base = "沙箱测试回炉重试耗尽（多轮补丁均未通过）"
            parts = event.split(":", 3)
            detail = parts[3].strip() if len(parts) > 3 else ""
            return f"{base}；最后一轮失败原因：{detail}" if detail else base
        if event.startswith("canary:degraded"):
            return "金丝雀劣化自动回滚"
    return "转人工（原因见流程详情与审计记录）"


# ----------------------------------------------------------------------
# 记录构造与状态迁移（纯函数，便于单测）
# ----------------------------------------------------------------------


def new_entry(flow: dict, now: datetime | None = None) -> dict:
    """由流程列表项构造待办条目（open 状态）。"""
    wf_id = str(flow.get("wf_id") or "")
    alert = flow.get("alert") or {}
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "id": f"esc-{wf_id}",
        "wf_id": wf_id,
        "alert_id": str(alert.get("alert_id") or ""),
        "service": str(alert.get("service") or ""),
        "auto_reason": auto_reason_from_events(flow.get("gate_events")),
        "alert": alert,
        "status": "open",
        "assignee": "",
        "note": "",
        "escalated_at": ts,
        "updated_at": ts,
        "closed_at": None,
        "re_escalated": False,
        "history": [{"ts": ts, "action": "open", "actor": "system", "detail": "ESCALATED 自动登记"}],
    }


def _append_history(entry: dict, action: str, actor: str, detail: str = "") -> None:
    entry.setdefault("history", []).append(
        {"ts": _now(), "action": action, "actor": actor or "system", "detail": detail}
    )
    entry["updated_at"] = _now()


def apply_assign(entry: dict, assignee: str, actor: str) -> dict:
    clean = (assignee or "").strip()
    if not clean:
        raise EscalationStoreError("责任人不能为空")
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已处置关闭，不能改派")
    before = entry.get("assignee") or ""
    entry["assignee"] = clean
    entry["status"] = "assigned"
    _append_history(
        entry, "assign", actor, f"指派给 {clean}" + (f"（由 {before} 改派）" if before else "")
    )
    return entry


def apply_close(entry: dict, actor: str, note: str = "") -> dict:
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已关闭，无需重复处置")
    entry["status"] = "closed"
    entry["note"] = (note or "").strip()
    entry["closed_at"] = _now()
    _append_history(entry, "close", actor, entry["note"] or "已处置")
    return entry


def apply_retried(entry: dict, new_wf_id: str, actor: str) -> dict:
    """记录「重试修复」处置：以新幂等键重启流程后自动关闭该待办。"""
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已关闭，无需重试")
    entry["status"] = "closed"
    entry["note"] = f"已重试修复（新流程 {new_wf_id}）"
    entry["closed_at"] = _now()
    _append_history(entry, "retry", actor, f"重启修复流程 {new_wf_id}")
    return entry


def apply_re_escalate(entry: dict, actor: str, detail: str = "") -> dict:
    entry["re_escalated"] = True
    _append_history(entry, "re-escalate", actor or "system", detail or "处置超时，再升级一次")
    return entry


# ----------------------------------------------------------------------
# DB 后端（production）
# ----------------------------------------------------------------------


def _row_to_entry(row) -> dict:
    return {
        "id": row["id"],
        "wf_id": row["wf_id"],
        "alert_id": row["alert_id"],
        "service": row["service"],
        "auto_reason": row["auto_reason"],
        "alert": row["alert"],
        "status": row["status"],
        "assignee": row["assignee"],
        "note": row["note"],
        "escalated_at": db_layer.to_iso(row["escalated_at"]) or "",
        "updated_at": db_layer.to_iso(row["updated_at"]) or "",
        "closed_at": db_layer.to_iso(row["closed_at"]),
        "re_escalated": bool(row["re_escalated"]),
        "history": row["history"] or [],
    }


def _entry_values(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "wf_id": entry["wf_id"],
        "alert_id": entry.get("alert_id") or "",
        "service": entry.get("service") or "",
        "auto_reason": entry.get("auto_reason") or "",
        "alert": entry.get("alert"),
        "status": entry.get("status") or "open",
        "assignee": entry.get("assignee") or "",
        "note": entry.get("note") or "",
        "escalated_at": db_layer.to_dt(entry.get("escalated_at")) or db_layer.now_dt(),
        "updated_at": db_layer.to_dt(entry.get("updated_at")) or db_layer.now_dt(),
        "closed_at": db_layer.to_dt(entry.get("closed_at")),
        "re_escalated": bool(entry.get("re_escalated")),
        "history": entry.get("history") or [],
    }


def _read_db() -> list[dict]:
    engine = db_layer.get_engine()
    with engine.begin() as conn:
        return [
            _row_to_entry(row)
            for row in conn.execute(
                select(db_layer.escalations).order_by(db_layer.escalations.c.escalated_at)
            ).mappings()
        ]


def _upsert_db(entry: dict) -> None:
    engine = db_layer.get_engine()
    values = _entry_values(entry)
    with engine.begin() as conn:
        exists = conn.execute(
            select(db_layer.escalations.c.id).where(db_layer.escalations.c.id == entry["id"])
        ).scalar()
        if exists is None:
            conn.execute(db_layer.escalations.insert().values(**values))
        else:
            conn.execute(
                db_layer.escalations.update()
                .where(db_layer.escalations.c.id == entry["id"])
                .values(**values)
            )


# ----------------------------------------------------------------------
# 读取 / 写入入口（按模式分发）
# ----------------------------------------------------------------------


def _read_raw() -> list[dict]:
    if mode.is_production():
        return _read_db()
    if not DATA_PATH.is_file():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []  # 文件损坏：返回空，不覆盖现场（下次写入前可由人工排查）
    return data if isinstance(data, list) else []


def _write_raw(entries: list[dict]) -> None:
    if mode.is_production():
        # 生产：单条 upsert（同步方法只写单条，见 persist_entry）
        return
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def persist_entry(entry: dict) -> None:
    """写入单条（demo：全量原子替换；production：单条 upsert）。"""
    if mode.is_production():
        _upsert_db(entry)
        return
    entries = [item for item in _read_raw() if item.get("id") != entry["id"]]
    entries.append(entry)
    _write_raw(entries)


def list_all(status: str | None = None) -> list[dict]:
    """全部待办（可按状态过滤）；每次读取即取最新——热生效。"""
    entries = _read_raw()
    if status and status != "all":
        entries = [e for e in entries if e.get("status") == status]
    entries.sort(key=lambda e: e.get("escalated_at") or "", reverse=True)
    return entries


def get(esc_id: str) -> dict | None:
    return next((e for e in _read_raw() if e.get("id") == esc_id), None)


def stats() -> dict:
    entries = _read_raw()
    return {
        "total": len(entries),
        "open": sum(1 for e in entries if e.get("status") == "open"),
        "assigned": sum(1 for e in entries if e.get("status") == "assigned"),
        "closed": sum(1 for e in entries if e.get("status") == "closed"),
        "re_escalated": sum(1 for e in entries if e.get("re_escalated")),
        "sla_minutes": sla_minutes(),
    }


def sync_from_flows(flows: list[dict]) -> int:
    """把流程列表中的 ESCALATED 流程幂等登记为待办；返回本次新增数。

    规则：wf_id 已登记（不论状态）→ 跳过——已关闭的待办不因流程仍在列表中而重开。
    """
    existing = {e.get("wf_id") for e in _read_raw()}
    added = 0
    for flow in flows:
        if flow.get("stage") != "ESCALATED":
            continue
        wf_id = str(flow.get("wf_id") or "")
        if not wf_id or wf_id in existing:
            continue
        persist_entry(new_entry(flow))
        existing.add(wf_id)
        added += 1
    return added


# ----------------------------------------------------------------------
# 超时再升级（SLA 巡检）
# ----------------------------------------------------------------------


def due_for_re_escalation(
    now: datetime | None = None, sla: int | None = None
) -> list[dict]:
    """超过 SLA 未处置且未再升级的待办（open/assigned）。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    minutes = sla if sla is not None else sla_minutes()
    threshold = current - timedelta(minutes=minutes)
    due: list[dict] = []
    for entry in _read_raw():
        if entry.get("status") == "closed" or entry.get("re_escalated"):
            continue
        escalated_at = _parse_ts(entry.get("escalated_at"))
        if escalated_at and escalated_at <= threshold:
            due.append(entry)
    return due


def _send_re_escalation(entry: dict, minutes: int) -> dict:
    """经通知渠道再升级一次（失败降级不抛错；留痕 data/notify/escalation-re-*.json）。"""
    from aiops_agent import notify
    from aiops_agent.models import Alert

    alert = Alert(
        alert_id=entry.get("alert_id") or entry.get("id") or "unknown",
        service=entry.get("service") or "unknown",
        severity="critical",
    )
    reason = (
        f"转人工待办处置超时：已超过 {minutes} 分钟未关闭（{entry.get('auto_reason') or '原因见详情'}）"
    )
    sender = notify.NotificationSender()
    card = notify.render_escalation_card(alert, reason, provider=sender.provider)
    return sender.send(card, msg_id=f"escalation-re-{entry['id']}")


def sweep_due(now: datetime | None = None) -> list[dict]:
    """SLA 巡检：对到期未处置的待办发再升级通知并打标（每单一次）。返回被升级条目。"""
    minutes = sla_minutes()
    escalated: list[dict] = []
    for entry in due_for_re_escalation(now, minutes):
        delivery = _send_re_escalation(entry, minutes)
        updated = apply_re_escalate(
            entry,
            "system",
            f"处置超时（>{minutes} 分钟），已再升级（投递={delivery.get('mode')}）",
        )
        persist_entry(updated)
        escalated.append(updated)
    return escalated
