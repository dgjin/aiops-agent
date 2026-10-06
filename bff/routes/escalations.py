"""转人工处置域：/api/escalations*（待办列表 / 指派 / 关闭 / 重试修复）。

读侧每次先做**幂等登记**（sync_from_flows：ESCALATED 流程 → open 待办），
再返回列表与统计；写侧三动作全部落操作审计（wf_id 记录待办的 wf_id）：
    - assign：指派/改派责任人（open/assigned 均可）；
    - close：处置关闭（备注留档）；
    - retry：以**新幂等键**重启修复流程（同 alert_id 的旧流程已存在，不能复用 wf_id），
      成功后待办自动关闭并注明新流程 ID。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aiops_agent.models import Alert

from .. import audit, escalations
from ..deps import ApiError, gw, ok

router = APIRouter()


class AssignBody(BaseModel):
    assignee: str = Field(min_length=1, max_length=64)


class CloseBody(BaseModel):
    note: str = Field(default="", max_length=500)


def _decorate(entry: dict) -> dict:
    """附加 SLA 派生状态（前端展示「已超时未处置」）。"""
    overdue = False
    if entry.get("status") != "closed":
        try:
            escalated_at = datetime.fromisoformat(str(entry.get("escalated_at")))
            if escalated_at.tzinfo is None:
                escalated_at = escalated_at.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - escalated_at).total_seconds() / 60
            overdue = age_min > escalations.sla_minutes()
        except (ValueError, TypeError):
            overdue = False
    return {**entry, "overdue": overdue, "sla_minutes": escalations.sla_minutes()}


@router.get("/api/escalations")
async def api_escalations(
    status: str = Query("all", pattern="^(all|open|assigned|closed)$"),
) -> dict:
    flows = await gw.list_flows(limit=200)
    added = escalations.sync_from_flows(flows)
    items = [_decorate(entry) for entry in escalations.list_all(status)]
    return ok({"escalations": items, "stats": escalations.stats(), "registered": added})


@router.post("/api/escalations/{esc_id}/assign")
async def api_escalation_assign(request: Request, esc_id: str, body: AssignBody) -> dict:
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    try:
        updated = escalations.apply_assign(entry, body.assignee, request.state.identity.user)
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    escalations.persist_entry(updated)
    audit.write_audit(
        actor=request.state.identity.user,
        action="escalation:assign",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={"assignee": updated["assignee"]},
    )
    return ok({"ok": True, "escalation": _decorate(updated)})


@router.post("/api/escalations/{esc_id}/close")
async def api_escalation_close(
    request: Request, esc_id: str, body: CloseBody | None = None
) -> dict:
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    try:
        updated = escalations.apply_close(
            entry, request.state.identity.user, body.note if body else ""
        )
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    escalations.persist_entry(updated)
    audit.write_audit(
        actor=request.state.identity.user,
        action="escalation:close",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={"note": updated["note"]},
    )
    return ok({"ok": True, "escalation": _decorate(updated)})


@router.post("/api/escalations/{esc_id}/retry")
async def api_escalation_retry(request: Request, esc_id: str) -> dict:
    """重试修复：以新 alert_id 派生新幂等键启动流程，成功后关闭待办。"""
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    if entry.get("status") == "closed":
        raise ApiError(400, "该待办已关闭，无需重试")

    alert_meta = entry.get("alert") or {}
    retry_no = sum(1 for h in entry.get("history") or [] if h.get("action") == "retry") + 1
    alert_id = f"{entry.get('alert_id') or esc_id}-retry{retry_no}"
    new_wf_id = f"aiops-fix-{entry.get('service')}-{alert_id}"
    alert = Alert(
        alert_id=alert_id,
        service=str(entry.get("service") or "unknown"),
        severity=str(alert_meta.get("severity") or "critical"),
        description=str(alert_meta.get("description") or "") or f"重试修复（{entry.get('auto_reason')}）",
    )
    try:
        started = await gw.start_flow(alert, new_wf_id)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from exc

    updated = escalations.apply_retried(entry, started, request.state.identity.user)
    escalations.persist_entry(updated)
    audit.write_audit(
        actor=request.state.identity.user,
        action="escalation:retry",
        wf_id=started,
        target=esc_id,
        params={"new_wf_id": started, "alert_id": alert_id},
    )
    return ok({"ok": True, "escalation": _decorate(updated), "new_wf_id": started})
