"""转人工处置域：/api/escalations*（待办列表 / 指派 / 处置计划 / 关闭 / 重试修复 / 知识审核）。

读侧每次先做**幂等登记**（sync_from_flows：ESCALATED 流程 → open 待办），
再返回列表与统计；写侧动作全部落操作审计（wf_id 记录待办的 wf_id）：
    - assign：指派/改派责任人（open/assigned/retrying 均可）；
    - plan：登记未关闭待办的处理计划（允许保持 open，不强制清零）；
    - close：M3 起**只接受结构化记录**（关闭原因 / 实际根因 / 实际处置 /
      验证结果 / 对智能体判断的评价），仅备注的弱关闭返回明确迁移错误；
    - retry：以**新幂等键**重启修复流程，原待办进入 ``retrying``（不立即关闭），
      后台跟踪新流程终态：DONE 才自动关闭，失败/再升级重新打开；
    - knowledge/review：知识审核（approve / reject / revoke，仅管理员），批准后
      同步导出 ``knowledge_records.json``，索引重建仍需显式执行。

所有写操作携带 ``version`` 做 CAS：与库中版本不一致返回 409，不得用旧页面覆盖新状态。

需求来源的 retry 特殊化：待办源于需求修复流（wf_id ``aiops-req-*``，或旧版误按
告警流重启后级联的 ``req-N-retryM*``）时**重启需求修复流**（沿用批准版方案、
跳过闸门 1 日志诊断）——按告警流重启必然被闸门 1 置信度拦截，重试永远无法成功。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aiops_agent import app_registry, evidence_store, knowledge
from aiops_agent.models import Alert

from .. import audit, escalations, requirement_analyses
from ..deps import ApiError, gw, ok

router = APIRouter()


class AssignBody(BaseModel):
    assignee: str = Field(min_length=1, max_length=64)
    # 记录版本（M3 CAS）：0 表示旧客户端未提供，按服务端读取版本校验
    version: int = 0


class CloseBody(BaseModel):
    """结构化关闭请求（M3 起唯一关闭入口；仅备注关闭已停用）。

    关闭原因 ``RESOLVED`` 要求三项必填（实际根因 / 实际处置 / 验证结果），由
    ``escalations.build_resolution`` 统一校验，路由不重复实现规则。
    ``version`` 用于并发 CAS：与库中版本不一致返回 409。
    """

    note: str = Field(default="", max_length=2000)
    close_reason: str = Field(default="", max_length=32)
    actual_root_cause: str = Field(default="", max_length=2000)
    actual_action: str = Field(default="", max_length=2000)
    verification: str = Field(default="", max_length=2000)
    verdict: str = Field(default="", max_length=32)
    knowledge_candidate: bool = False
    sensitive_confirmed: bool = False
    version: int = 0


class PlanBody(BaseModel):
    plan: str = Field(default="", max_length=1000)
    version: int = 0


class RetryBody(BaseModel):
    """重试请求（可选请求体）：携带读取时的 ``version`` 防并发覆盖。"""

    version: int = 0


class KnowledgeReviewBody(BaseModel):
    decision: str = Field(min_length=1, max_length=16)
    reason: str = Field(default="", max_length=500)
    version: int = 0


# 需求来源识别：需求流的 alert_id 固定为 ``req-<条目ID>``（workflows.build_requirement_context），
# 级联重试会追加 ``-retryN`` 后缀（req-19 → req-19-retry1 → req-19-retry1-retry1）。
_REQUIREMENT_ALERT_ID = re.compile(r"^req-(?P<entry_id>.+?)(?:-retry\d+)*$")

# 通知投递方式（M3）：只有 live 才算“已通知”；recorded/failed 仍在退避重试
NOTIFY_LABELS = {
    "": "未通知",
    "live": "已送达",
    "recorded": "仅留档",
    "failed": "投递失败",
}

# 重试流程终态展示
RETRY_OUTCOME_LABELS = {
    "": "跟踪中",
    "DONE": "已成功",
    "ESCALATED": "再次升级",
    "FAILED": "执行失败",
    "CANCELLED": "已取消",
    "SHADOWED": "影子观察",
}

# _decorate 的哨兵：区分「未传（需单查）」与「批量查过但没有证据（None）」
_EVIDENCE_UNSET = object()


def _expected_version(body) -> int | None:
    """请求体里的版本号：0/缺省 → None（用服务端读取的版本做 CAS）。"""
    version = int(getattr(body, "version", 0) or 0)
    return version if version > 0 else None


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


def _system_map() -> dict[str, dict]:
    """被监控系统清单（service → 应用），供待办按「监控系统」展示与筛选。

    读侧每次请求加载一次（清单热生效）；未纳管服务（如 node / redis 等基础设施）回退服务名。
    """
    return {
        str(app.get("service") or "").strip().lower(): app
        for app in app_registry.load_apps()
        if str(app.get("service") or "").strip()
    }


def _sync_knowledge() -> dict:
    """同步知识导出文件（关闭/审核后）；**不触发**嵌入与索引重建。

    导出失败不阻断业务动作：待办状态已落库，重建索引前可重试导出（部署手册）。
    """
    try:
        return knowledge.write_export(escalations.list_all())
    except OSError as exc:
        return {"exported": 0, "skipped": 0, "error": f"知识导出写入失败：{exc}"}


def _is_structured_close(payload: dict) -> bool:
    """是否为结构化关闭（任一结构化字段非空）；M3 起全空一律拒绝。"""
    return any(
        payload.get(key)
        for key in (
            "close_reason",
            "actual_root_cause",
            "actual_action",
            "verification",
            "verdict",
            "knowledge_candidate",
        )
    )


def _decorate(
    entry: dict, systems: dict[str, dict] | None = None, evidence: object = _EVIDENCE_UNSET
) -> dict:
    """附加 SLA 派生、需求来源、处置分类、监控系统、重试跟踪与通知送达状态。

    systems / evidence 可由列表侧批量取好传入（避免逐条查库 / 读盘），缺省时按需现查。
    ``retrying`` 暂停 SLA 计超时（新流程运行中）；recorded/failed 仍保持 overdue。
    """
    status = str(entry.get("status") or "")
    overdue = False
    if status in ("open", "assigned"):
        try:
            escalated_at = datetime.fromisoformat(str(entry.get("escalated_at")))
            if escalated_at.tzinfo is None:
                escalated_at = escalated_at.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - escalated_at).total_seconds() / 60
            overdue = age_min > escalations.sla_minutes()
        except (ValueError, TypeError):
            overdue = False
    if evidence is _EVIDENCE_UNSET:
        evidence = evidence_store.summary(str(entry.get("wf_id") or ""))
    service = str(entry.get("service") or "")
    app = (systems if systems is not None else _system_map()).get(service.strip().lower())
    # 处置分类：新记录读字段；历史记录按升级原因确定性回填（只读派生，不写库）
    category = escalations.entry_category(entry)
    # 结构化关闭记录（M2）：旧数据为 null；标签供列表/详情直接展示
    resolution = entry.get("resolution")
    resolution = resolution if isinstance(resolution, dict) else None
    close_reason = str((resolution or {}).get("close_reason") or "")
    review_status = escalations.knowledge_status(entry)
    notify_mode = str(entry.get("notify_mode") or "")
    notify_pending = (
        status in ("open", "assigned")
        and not entry.get("re_escalated")
        and notify_mode in ("recorded", "failed")
    )
    retry_outcome = str(entry.get("retry_outcome") or "")
    return {
        **entry,
        "category": category,
        "category_label": escalations.category_label(category),
        "overdue": overdue,
        "sla_minutes": escalations.sla_minutes(),
        "requirement": _looks_requirement(entry),
        # 监控系统展示名：纳管应用取应用名，未纳管回退服务名（前端分组 / 筛选 / 搜索共用）
        "system": str(app.get("name") or service) if app else service,
        "system_managed": app is not None,
        # 日志证据摘要（转人工核对；无归档为 null）
        "evidence": evidence,
        "resolution": resolution,
        "close_reason": close_reason,
        "close_reason_label": escalations.CLOSE_REASONS.get(close_reason, ""),
        "knowledge_status": review_status,
        "knowledge_label": escalations.KNOWLEDGE_LABELS.get(review_status, review_status),
        # ---- M3：重试跟踪与通知真实送达 ----
        "retry_wf_id": str(entry.get("retry_wf_id") or ""),
        "parent_run_id": str(entry.get("parent_run_id") or ""),
        "retry_started_at": entry.get("retry_started_at"),
        "retry_outcome": retry_outcome,
        "retry_outcome_label": RETRY_OUTCOME_LABELS.get(retry_outcome, retry_outcome),
        "notify_mode": notify_mode,
        "notify_label": NOTIFY_LABELS.get(notify_mode, notify_mode),
        "notify_attempts": int(entry.get("notify_attempts") or 0),
        "notify_next_retry_at": entry.get("notify_next_retry_at"),
        "notify_error": str(entry.get("notify_error") or ""),
        "notify_updated_at": entry.get("notify_updated_at"),
        "notify_pending": notify_pending,
        "version": int(entry.get("version") or 1),
    }


@router.get("/api/escalations")
async def api_escalations(
    status: str = Query("all", pattern="^(all|open|assigned|retrying|closed)$"),
) -> dict:
    flows = await gw.list_flows(limit=200)
    added = escalations.sync_from_flows(flows)
    systems = _system_map()  # 单次加载，避免逐条查库
    items = escalations.list_all(status)
    # 证据摘要一次取齐：转人工卡片直接展示「可核对日志行数」，不必逐条读盘
    summaries = evidence_store.summaries_for([str(e.get("wf_id") or "") for e in items])
    decorated = [
        _decorate(entry, systems, summaries.get(str(entry.get("wf_id") or "")))
        for entry in items
    ]
    return ok({"escalations": decorated, "stats": escalations.stats(), "registered": added})


@router.post("/api/escalations/{esc_id}/assign")
async def api_escalation_assign(request: Request, esc_id: str, body: AssignBody) -> dict:
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    try:
        updated = escalations.apply_assign(entry, body.assignee, request.state.identity.user)
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        escalations.persist_entry(updated, expected_version=_expected_version(body))
    except escalations.EscalationConflictError as exc:
        raise ApiError(409, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="escalation:assign",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={"assignee": updated["assignee"], "version": updated["version"]},
    )
    return ok({"ok": True, "escalation": _decorate(updated)})


@router.post("/api/escalations/{esc_id}/close")
async def api_escalation_close(
    request: Request, esc_id: str, body: CloseBody | None = None
) -> dict:
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    actor = request.state.identity.user
    payload = body.model_dump() if body else {}
    # M3：仅备注的弱关闭已停用；production 与 demo 均要求结构化处置记录
    if not _is_structured_close(payload):
        raise ApiError(
            400,
            "关闭必须提交结构化处置记录（关闭原因 / 实际根因 / 实际处置 / 验证结果）；"
            "仅备注关闭已停用，请升级客户端后重试",
        )
    try:
        resolution = escalations.build_resolution(payload, actor)
        updated = escalations.apply_close(
            entry, actor, str(payload.get("note") or ""), resolution
        )
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        escalations.persist_entry(updated, expected_version=_expected_version(body))
    except escalations.EscalationConflictError as exc:
        raise ApiError(409, str(exc)) from exc
    result: dict = {
        "ok": True,
        "escalation": _decorate(updated),
        "knowledge_export": _sync_knowledge(),
    }
    audit.write_audit(
        actor=actor,
        action="escalation:close",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={
            "note": updated["note"],
            "close_reason": resolution["close_reason"],
            "verdict": resolution["verdict"],
            "knowledge_status": escalations.knowledge_status(updated),
            "version": updated["version"],
        },
    )
    return ok(result)


@router.post("/api/escalations/{esc_id}/plan")
async def api_escalation_plan(request: Request, esc_id: str, body: PlanBody) -> dict:
    """登记/清空未关闭待办的处理计划（历史积压逐条给出责任动作，不批量关闭）。"""
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    actor = request.state.identity.user
    try:
        updated = escalations.apply_plan(entry, body.plan, actor)
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        escalations.persist_entry(updated, expected_version=_expected_version(body))
    except escalations.EscalationConflictError as exc:
        raise ApiError(409, str(exc)) from exc
    audit.write_audit(
        actor=actor,
        action="escalation:plan",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={"plan": updated["plan"], "version": updated["version"]},
    )
    return ok({"ok": True, "escalation": _decorate(updated)})


@router.post("/api/escalations/{esc_id}/knowledge/review")
async def api_escalation_knowledge_review(
    request: Request, esc_id: str, body: KnowledgeReviewBody
) -> dict:
    """知识审核（仅管理员，见 middleware 角色规则）：approve / reject / revoke。"""
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    actor = request.state.identity.user
    try:
        updated = escalations.apply_knowledge_review(entry, body.decision, actor, body.reason)
    except escalations.EscalationStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        escalations.persist_entry(updated, expected_version=_expected_version(body))
    except escalations.EscalationConflictError as exc:
        raise ApiError(409, str(exc)) from exc
    export = _sync_knowledge()
    audit.write_audit(
        actor=actor,
        action=f"knowledge:{body.decision.strip().lower()}",
        wf_id=updated["wf_id"],
        target=esc_id,
        params={"reason": body.reason, "version": updated["version"], **export},
    )
    return ok({"ok": True, "escalation": _decorate(updated), "knowledge_export": export})


@router.post("/api/escalations/{esc_id}/retry")
async def api_escalation_retry(
    request: Request, esc_id: str, body: RetryBody | None = None
) -> dict:
    """重试修复：以新 alert_id 派生新幂等键启动流程，原待办进入 ``retrying``。

    新流程启动后**不立即关闭**原待办：后台跟踪新流程终态，DONE 才自动结构化关闭，
    ESCALATED/FAILED/CANCELLED/SHADOWED 重新打开并记录原因（保留父子链）。

    需求来源的待办必须重启**需求修复流**（携带批准版方案，跳过闸门 1 日志诊断）；
    否则告警流对无日志的需求告警必然在闸门 1 被置信度阈值拦截，重试永远无法成功。

    重试溯源（approval-evidence-v1）：新流程快照写入 ``case_id/requested_by/
    parent_run_id/parent_wf_id/retry_wf_id``，并使重试发起人不得审批自己的变更。
    """
    entry = escalations.get(esc_id)
    if entry is None:
        raise ApiError(404, f"未找到待办：{esc_id}")
    if entry.get("status") == "closed":
        raise ApiError(400, "该待办已关闭，无需重试")
    if entry.get("status") == "retrying":
        raise ApiError(409, "该待办已在重试跟踪中，请等待新流程终态或手动关闭")

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
        context = _retry_evidence_context(
            entry,
            case_id=str(session.get("id") or ""),
            actor=actor,
            retry_wf_id=new_wf_id,
        )
        try:
            started = await gw.start_requirement_flow(
                task, new_wf_id, evidence_context=context
            )
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
        context = _retry_evidence_context(
            entry,
            case_id=str(entry.get("alert_id") or esc_id),
            actor=actor,
            retry_wf_id=new_wf_id,
        )
        try:
            started = await gw.start_flow(alert, new_wf_id, evidence_context=context)
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc
        mode = "alert"

    updated = escalations.apply_retried(entry, started, actor)
    try:
        escalations.persist_entry(updated, expected_version=_expected_version(body))
    except escalations.EscalationStoreError as exc:
        # 含版本冲突（EscalationConflictError）与状态校验失败：均不得静默覆盖
        raise ApiError(409, str(exc)) from exc
    audit.write_audit(
        actor=actor,
        action="escalation:retry",
        wf_id=started,
        target=esc_id,
        params={
            "new_wf_id": started,
            "alert_id": alert_id,
            "mode": mode,
            "version": updated["version"],
            **context,
        },
    )
    return ok(
        {
            "ok": True,
            "escalation": _decorate(updated),
            "new_wf_id": started,
            "retry_wf_id": started,
            "mode": mode,
        }
    )


def _retry_evidence_context(
    entry: dict, *, case_id: str, actor: str, retry_wf_id: str
) -> dict:
    """重试溯源上下文（写入新流程策略快照）：关联原件并支持职责分离。"""
    return {
        "case_id": case_id,
        "requested_by": actor,
        "parent_run_id": str(entry.get("run_id") or ""),
        "parent_wf_id": str(entry.get("wf_id") or ""),
        "retry_wf_id": retry_wf_id,
    }
