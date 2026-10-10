"""转人工处置域：/api/escalations*（待办列表 / 指派 / 关闭 / 重试修复）。

读侧每次先做**幂等登记**（sync_from_flows：ESCALATED 流程 → open 待办），
再返回列表与统计；写侧三动作全部落操作审计（wf_id 记录待办的 wf_id）：
    - assign：指派/改派责任人（open/assigned 均可）；
    - close：处置关闭（备注留档）；
    - retry：以**新幂等键**重启修复流程（同 alert_id 的旧流程已存在，不能复用 wf_id），
      成功后待办自动关闭并注明新流程 ID。

需求来源的 retry 特殊化：待办源于需求修复流（wf_id ``aiops-req-*``，或旧版误按
告警流重启后级联的 ``req-N-retryM*``）时**重启需求修复流**（沿用批准版方案、
跳过闸门 1 日志诊断）——按告警流重启必然被闸门 1 置信度拦截，重试永远无法成功。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aiops_agent.models import Alert

from .. import audit, escalations, requirement_analyses
from ..deps import ApiError, gw, ok

router = APIRouter()


class AssignBody(BaseModel):
    assignee: str = Field(min_length=1, max_length=64)


class CloseBody(BaseModel):
    note: str = Field(default="", max_length=500)


# 需求来源识别：需求流的 alert_id 固定为 ``req-<条目ID>``（workflows.build_requirement_context），
# 级联重试会追加 ``-retryN`` 后缀（req-19 → req-19-retry1 → req-19-retry1-retry1）。
_REQUIREMENT_ALERT_ID = re.compile(r"^req-(?P<entry_id>.+?)(?:-retry\d+)*$")


def _looks_requirement(entry: dict) -> bool:
    """待办是否源于需求修复流（wf 前缀或 alert_id 形态；供列表展示与重试路由初判）。"""
    if str(entry.get("wf_id") or "").startswith("aiops-req-"):
        return True
    return bool(_REQUIREMENT_ALERT_ID.match(str(entry.get("alert_id") or "")))


def resolve_requirement_session(entry: dict) -> dict | None:
    """反查需求来源待办对应的分析会话；非需求来源返回 None。

    两级识别（覆盖历史级联数据）：
    ① 流程本身即需求流（wf_id ``aiops-req-*``）→ 按 ``approved.wf_id`` 精确反查；
    ② 旧逻辑把需求流按告警流重启后级联产生的待办（alert_id ``req-N-retryM*``）
       → 剥离重试后缀，按 entry_id + service 反查（服务名消歧多应用同号条目）。
    """
    sessions = requirement_analyses.list_all()
    wf_id = str(entry.get("wf_id") or "")
    if wf_id.startswith("aiops-req-"):
        for session in sessions:
            approved = session.get("approved") or {}
            if str(approved.get("wf_id") or "") == wf_id:
                return session
    match = _REQUIREMENT_ALERT_ID.match(str(entry.get("alert_id") or ""))
    if match is None:
        return None
    candidates = [
        session
        for session in sessions
        if str(session.get("entry_id") or "") == match.group("entry_id")
    ]
    service = str(entry.get("service") or "")
    return next(
        (s for s in candidates if str(s.get("service") or "") == service),
        candidates[0] if candidates else None,
    )


def _decorate(entry: dict) -> dict:
    """附加 SLA 派生状态（前端展示「已超时未处置」）与需求来源标识。"""
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
    return {
        **entry,
        "overdue": overdue,
        "sla_minutes": escalations.sla_minutes(),
        "requirement": _looks_requirement(entry),
    }


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
    """重试修复：以新 alert_id 派生新幂等键启动流程，成功后关闭待办。

    需求来源的待办必须重启**需求修复流**（携带批准版方案，跳过闸门 1 日志诊断）；
    否则告警流对无日志的需求告警必然在闸门 1 被置信度阈值拦截，重试永远无法成功。
    """
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    if entry.get("status") == "closed":
        raise ApiError(400, "该待办已关闭，无需重试")

    alert_meta = entry.get("alert") or {}
    retry_no = sum(1 for h in entry.get("history") or [] if h.get("action") == "retry") + 1
    alert_id = f"{entry.get('alert_id') or esc_id}-retry{retry_no}"
    actor = request.state.identity.user

    session = resolve_requirement_session(entry)
    if session is not None:
        # 需求来源：重启需求修复流（方案 → 补丁 → 沙箱 → 审批 → 公告 → 发布）
        if session.get("status") != "approved":
            raise ApiError(
                409,
                "该需求分析尚未批准进入修复"
                f"（当前状态 {session.get('status') or '未知'}）；请先在「需求反馈」页完成批准",
            )
        analysis = requirement_analyses.latest_analysis(session) or {}
        if analysis.get("degraded"):
            raise ApiError(
                409,
                "该需求最新分析未成功完成（无有效结果），无法重试修复；"
                "请在「需求反馈」页重试分析后再批准",
            )
        task, version = requirement_analyses.build_requirement_task(session, actor)
        new_wf_id = f"aiops-req-{session.get('id')}-v{version}-{alert_id}"
        try:
            started = await gw.start_requirement_flow(task, new_wf_id)
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc
        mode = "requirement"
    elif _looks_requirement(entry):
        # 疑似需求来源但分析会话缺失（已清理等）：不再按告警流空转（必然闸门1拦截）
        raise ApiError(
            409,
            "该待办疑似需求来源，但未找到对应分析会话（可能已被清理）；"
            "请在「需求反馈」页重新发起分析",
        )
    else:
        new_wf_id = f"aiops-fix-{entry.get('service')}-{alert_id}"
        alert = Alert(
            alert_id=alert_id,
            service=str(entry.get("service") or "unknown"),
            severity=str(alert_meta.get("severity") or "critical"),
            description=str(alert_meta.get("description") or "")
            or f"重试修复（{entry.get('auto_reason')}）",
        )
        try:
            started = await gw.start_flow(alert, new_wf_id)
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc
        mode = "alert"

    updated = escalations.apply_retried(entry, started, actor)
    escalations.persist_entry(updated)
    audit.write_audit(
        actor=actor,
        action="escalation:retry",
        wf_id=started,
        target=esc_id,
        params={"new_wf_id": started, "alert_id": alert_id, "mode": mode},
    )
    return ok(
        {"ok": True, "escalation": _decorate(updated), "new_wf_id": started, "mode": mode}
    )
