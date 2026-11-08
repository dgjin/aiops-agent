"""流程域：/api/overview、/api/flows*（列表 / 详情 / 结果）与写侧四端点。

写侧（approval / second-approval / deploy-command / queue-patch）统一走
``_write_guard`` 前置校验：运行中 + 阶段允许 + 业务附加条件 → Temporal signal →
操作审计（前端不可绕过；对已关闭 workflow 发 signal 会被 Temporal 静默接受，
因此必须 BFF 侧拦截，见设计方案 6.3）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from temporalio.service import RPCError, RPCStatusCode

from aiops_agent import change_evidence, evidence_store, metrics
from aiops_agent.models import Alert

from .. import aggregator, audit, escalations, runs_store
from ..deps import ApiError, close_dt, gw, log, ok
from ..temporal_gateway import check_stage_allowed

router = APIRouter()


# ----------------------------------------------------------------------
# 读侧：总览 / 列表 / 详情 / 结果
# ----------------------------------------------------------------------


def _open_escalations(items: list[dict]) -> int:
    """转人工待办未闭环数（侧栏角标，P1-3）。

    顺带幂等登记本批流程中的 ESCALATED 条目，保证角标与待办页同源；
    存储暂不可读时回退 0——巡检类派生计数不应阻塞总览。
    """
    try:
        escalations.sync_from_flows(items)
        stats = escalations.stats()
        return stats["open"] + stats["assigned"]
    except Exception:  # noqa: BLE001 - 存储故障不阻塞总览
        return 0


@router.get("/api/overview")
async def api_overview() -> dict:
    items = await gw.list_flows(limit=200)
    return ok(overview_payload(items))


def overview_payload(items: list[dict]) -> dict:
    """总览聚合（纯计算）：counts + running + 近 7 日终态分布。

    ``/api/overview`` 与 ``/api/systems/overview``（Phase 2 聚合端点）共用同一口径，
    避免两处计数规则漂移；幂等登记 ESCALATED 待办（_open_escalations）保留在此。
    """
    running = [i for i in items if i["exec_status"] == "RUNNING"]
    closed = [i for i in items if i["exec_status"] != "RUNNING"]

    today = date.today()
    today_done = today_escalated = today_failed = 0
    for item in closed:
        closed_at = close_dt(item)
        if closed_at and closed_at.astimezone().date() == today:
            if item["stage"] == "DONE":
                today_done += 1
            elif item["stage"] == "ESCALATED":
                today_escalated += 1
            elif item["stage"] == "FAILED":
                today_failed += 1

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    # FAILED 为「硬失败/超时」终态（见 temporal_gateway.stage_from_exec_status），单独成类；
    # SHADOWED 为 P1-2 影子档终态（只建议不执行）
    dist = {"DONE": 0, "ESCALATED": 0, "CANCELLED": 0, "FAILED": 0, "SHADOWED": 0}
    for item in closed:
        closed_at = close_dt(item)
        if closed_at and closed_at >= week_ago and item["stage"] in dist:
            dist[item["stage"]] += 1

    counts = {
        "running": len(running),
        "wait_approval": sum(1 for i in running if i["stage"] == "WAIT_APPROVAL"),
        "notifying": sum(1 for i in running if i["stage"] == "NOTIFYING"),
        "today_done": today_done,
        "today_escalated": today_escalated,
        "today_failed": today_failed,
        "escalations_open": _open_escalations(items),
    }
    return {"counts": counts, "running": running, "terminal_dist_7d": dist}


@router.get("/api/flows")
async def api_flows(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    service: str = "",
    stage: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    items = await gw.list_flows(limit=200)
    # production：增量镜像到 workflow_runs（delta 检测，失败仅告警不阻塞读接口）
    if items:
        try:
            await asyncio.to_thread(runs_store.sync_runs, items)
        except Exception as exc:  # noqa: BLE001 - 镜像失败降级（Temporal 仍为数据源）
            log.warning("[runs_store] workflow_runs 落库失败：%s", exc)
    filtered = []
    for item in items:
        if status == "open" and item["exec_status"] != "RUNNING":
            continue
        if status == "closed" and item["exec_status"] == "RUNNING":
            continue
        if service and (item.get("alert") or {}).get("service") != service:
            continue
        if stage and item["stage"] != stage:
            continue
        filtered.append(item)
    return ok({"items": filtered[:limit]})


async def _detail_or_error(wf_id: str) -> dict:
    try:
        return await gw.flow_detail(wf_id)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise ApiError(404, f"workflow 不存在: {wf_id}") from exc
        raise ApiError(502, f"Temporal 调用失败: {exc}") from exc


@router.get("/api/flows/{wf_id}")
async def api_flow_detail(wf_id: str) -> dict:
    detail = await _detail_or_error(wf_id)
    patch_id = (detail.get("result") or {}).get("patch_id") or (detail.get("status") or {}).get(
        "patch_id"
    )
    detail["artifacts"] = aggregator.artifacts_for_patch(patch_id) if patch_id else None
    # 日志证据摘要：终态优先用 result.evidence（已随审批记录返回），运行中读归档；
    # 详情页据此在「概览」旁给出证据入口，无需额外一次列表请求。
    detail["evidence"] = (detail.get("result") or {}).get("evidence") or await asyncio.to_thread(
        evidence_store.summary, wf_id
    )
    return ok(detail)


@router.get("/api/flows/{wf_id}/evidence")
async def api_flow_evidence(wf_id: str) -> dict:
    """日志证据详情（转人工核对）：原始日志行 + Drain3 模板聚类。

    证据在 TRIAGING 阶段由 archive_evidence 活动归档（与 workflow_id 绑定），
    因此跨进程/重启后仍可查；无归档（无告警日志或旧流程）返回 404。
    """
    record = await asyncio.to_thread(evidence_store.get, wf_id)
    if record is None:
        raise ApiError(404, "该流程未归档日志证据（可能未采集到日志，或在证据归档上线前运行）")
    return ok({"evidence": record})


@router.get("/api/flows/{wf_id}/result")
async def api_flow_result(wf_id: str) -> dict:
    detail = await _detail_or_error(wf_id)
    if detail["exec_status"] == "RUNNING":
        stage = (detail.get("status") or {}).get("stage")
        raise ApiError(409, f"流程仍在运行（阶段 {stage}），审计记录在终态后可用")
    if not detail.get("result"):
        raise ApiError(404, "审计记录不可用（工作流无结果）")
    return ok({"result": detail["result"]})


# ----------------------------------------------------------------------
# 写侧接口（前置校验 → signal → 操作审计）
# ----------------------------------------------------------------------


class ApprovalBody(BaseModel):
    """审批请求：``decision`` 必需；v2 流程要求驳回时提供 ``comment``。

    ``expected_case_id`` / ``expected_diff_hash`` / ``expected_run_id`` 由控制台从当前
    展示的 status 带回，用于拒绝基于过期页面/旧补丁的审批；旧流程忽略这些字段。
    """

    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=2000)
    expected_case_id: str = Field(default="", max_length=128)
    expected_diff_hash: str = Field(default="", max_length=128)
    expected_run_id: str = Field(default="", max_length=128)


class DeployCommandBody(BaseModel):
    command: str = Field(pattern="^(deploy_now|cancel)$")


class QueuePatchBody(BaseModel):
    new_wf_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    alert_id: str = Field(min_length=1)
    description: str = ""


async def _write_guard(
    wf_id: str, allowed: set[str], extra: Callable[[dict], str | None] | None = None
) -> dict:
    """写操作前置校验（设计方案 6.3）：运行中 + 阶段允许 + 业务附加条件。"""
    detail = await _detail_or_error(wf_id)
    if detail["exec_status"] != "RUNNING":
        raise ApiError(409, f"流程已结束（{detail['exec_status']}），不能执行写操作")
    status = detail.get("status")
    if status is None:
        raise ApiError(502, "无法读取流程状态: " + "; ".join(detail.get("errors") or ["未知错误"]))
    message = check_stage_allowed(status.get("stage"), allowed)
    if message:
        raise ApiError(409, message)
    if extra and (extra_message := extra(status)):
        raise ApiError(409, extra_message)
    return status


def _first_approval_extra(status: dict) -> str | None:
    """一级审批附加校验：已提交不能重复（Web 控制台与 ChatOps 双通道共用）。"""
    if status.get("approval"):
        return f"一级审批已提交（{status['approval']}），不能重复提交"
    return None


def _identity_key(value: str) -> str:
    """身份归一化：ChatOps/CLI 前缀不影响职责分离比较。"""
    key = str(value or "").strip()
    for prefix in ("im:", "cli:"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    return key


def duty_separation_error(status: dict, gate: str, actor: str) -> str | None:
    """BFF 前置职责分离（工作流仍会二次校验；这里用于即时反馈与审计）。

    - 发起人（告警重试操作者 / 需求批准人）不得审批自己的变更；
    - 二级审批人不得与一级审批人相同（旧流程无 actor 记录时无法比较，跳过）。
    """
    me = _identity_key(actor)
    requester = _identity_key(status.get("requested_by") or "")
    if requester and me == requester:
        return "职责分离：流程发起人不得审批自己的变更"
    if gate == "second_approval":
        first = next(
            (
                record
                for record in status.get("approvals") or []
                if isinstance(record, dict)
                and record.get("gate") == "approval"
                and record.get("decision") == "approve"
            ),
            None,
        )
        first_actor = _identity_key((first or {}).get("actor") or "")
        if first_actor and first_actor == me:
            return "职责分离：一级与二级审批人不得相同"
    return None


def _signal_envelope(
    status: dict,
    *,
    gate: str,
    decision: str,
    actor: str,
    channel: str,
    comment: str,
    expected_case_id: str = "",
    expected_diff_hash: str = "",
    expected_run_id: str = "",
) -> tuple[dict | str, str]:
    """按流程契约构造审批载荷：旧流程返回 decision 字符串，v2 返回证据信封。

    绑定从工作流 status 派生（不信任客户端提交的哈希）；客户端回传的期望值仅用于
    拒绝过期页面。返回 ``(payload, command_id)``；旧流程 command_id 为空串。
    """
    problem = change_evidence.decision_problem(decision, comment)
    if problem:
        raise ApiError(400, problem)
    if change_evidence.contract_of(status) != change_evidence.CONTRACT:
        return decision, ""
    binding = change_evidence.binding_from_status(status)
    if binding is None:
        raise ApiError(
            409,
            "流程审批证据不可用（绑定缺失或契约不匹配）；已拒绝审批，请刷新后重试",
        )
    stale = change_evidence.expected_evidence_problem(
        binding,
        expected_case_id=expected_case_id,
        expected_diff_hash=expected_diff_hash,
        expected_run_id=expected_run_id,
    )
    if stale:
        raise ApiError(409, stale)
    command_id = change_evidence.new_command_id()
    envelope = change_evidence.build_envelope(
        binding=binding,
        gate=gate,
        decision=decision,
        actor=actor,
        channel=channel,
        comment=comment,
        command_id=command_id,
        submitted_at=change_evidence.utc_now_iso(),
    )
    return envelope, command_id


def _approval_audit_params(
    payload: dict | str, command_id: str, comment: str, channel: str, delivery: str
) -> dict:
    """审批审计参数：命令、通道、意见与证据哈希（旧流程只记决策）。"""
    params: dict = {"channel": channel, "comment": comment, "delivery": delivery}
    if isinstance(payload, dict):
        params.update(
            command_id=command_id,
            diff_hash=payload.get("diff_hash"),
            test_report_hash=payload.get("test_report_hash"),
            policy_sha256=payload.get("policy_sha256"),
            run_id=payload.get("run_id"),
            case_id=payload.get("case_id"),
            patch_id=payload.get("patch_id"),
        )
    return params


async def _submit_approval(
    request: Request,
    wf_id: str,
    *,
    gate: str,
    body: ApprovalBody,
) -> dict:
    """审批提交统一链路：guard → 职责分离 → 信封 → signal → 回执 → 审计。"""
    actor = request.state.identity.user
    allowed_stage = {"WAIT_APPROVAL"}
    extra = _first_approval_extra if gate == "approval" else _second_approval_extra
    status = await _write_guard(wf_id, allowed_stage, extra)
    action = "approval" if gate == "approval" else "second-approval"

    separation = duty_separation_error(status, gate, actor)
    if separation:
        audit.write_audit(
            actor=actor,
            action=f"{action}:{body.decision}",
            wf_id=wf_id,
            params={"channel": "console", "comment": body.comment, "delivery": "denied"},
            result="denied",
        )
        raise ApiError(409, separation)

    payload, command_id = _signal_envelope(
        status,
        gate=gate,
        decision=body.decision,
        actor=actor,
        channel="console",
        comment=body.comment,
        expected_case_id=body.expected_case_id,
        expected_diff_hash=body.expected_diff_hash,
        expected_run_id=body.expected_run_id,
    )
    if gate == "approval":
        await gw.signal_approval(wf_id, payload)
    else:
        await gw.signal_second_approval(wf_id, payload)
    gw.invalidate_cache()

    # signal RPC 成功只代表命令已送达；业务回执以工作流记录为准。
    # 旧流程无命令 ID，无法关联回执（只能展示已提交）。
    delivery, receipt = (
        await gw.approval_receipt(wf_id, command_id) if command_id else ("submitted", None)
    )
    audit.write_audit(
        actor=actor,
        action=f"{action}:{body.decision}",
        wf_id=wf_id,
        params={
            **_approval_audit_params(payload, command_id, body.comment, "console", delivery),
            **({"receipt": receipt} if delivery == "rejected" else {}),
        },
        result="rejected" if delivery == "rejected" else "signaled",
    )
    if delivery == "rejected":
        reason = (receipt or {}).get("reason") or "流程拒绝了该审批命令"
        raise ApiError(409, f"审批未生效：{reason}")

    metrics.observe_gate_event(gate, body.decision)
    message = (
        "审批命令已受理并记录"
        if delivery == "accepted"
        else "审批命令已提交；业务结果以流程状态为准"
    )
    if not command_id:
        message = "审批命令已提交（旧流程）：业务结果以流程状态为准"
    return ok(
        {
            "ok": True,
            "wf_id": wf_id,
            "action": f"{action}:{body.decision}",
            "command_id": command_id,
            "delivery": delivery,
            "message": message,
        }
    )


@router.post("/api/flows/{wf_id}/approval")
async def api_approval(request: Request, wf_id: str, body: ApprovalBody) -> dict:
    return await _submit_approval(request, wf_id, gate="approval", body=body)


def _second_approval_extra(status: dict) -> str | None:
    """二级审批附加校验：受保护目录 + 一级已批 + 防重复（双通道共用）。"""
    if not status.get("needs_second"):
        return "该流程不涉及受保护目录，无需二级审批"
    if status.get("approval") != "approve":
        return "一级审批尚未批准，不能进行二级审批"
    if status.get("second_approval"):
        return f"二级审批已提交（{status['second_approval']}），不能重复提交"
    return None


@router.post("/api/flows/{wf_id}/second-approval")
async def api_second_approval(request: Request, wf_id: str, body: ApprovalBody) -> dict:
    return await _submit_approval(request, wf_id, gate="second_approval", body=body)


@router.post("/api/flows/{wf_id}/deploy-command")
async def api_deploy_command(request: Request, wf_id: str, body: DeployCommandBody) -> dict:
    await _write_guard(wf_id, {"NOTIFYING"})
    await gw.signal_deploy_command(wf_id, body.command)
    gw.invalidate_cache()
    metrics.observe_gate_event("deploy_command", body.command)
    audit.write_audit(
        actor=request.state.identity.user,
        action=f"deploy-command:{body.command}",
        wf_id=wf_id,
        result="signaled",
    )
    return ok({"ok": True, "wf_id": wf_id, "action": f"deploy-command:{body.command}"})


@router.post("/api/flows/{wf_id}/queue-patch")
async def api_queue_patch(request: Request, wf_id: str, body: QueuePatchBody) -> dict:
    if not body.new_wf_id.startswith("aiops-fix-"):
        raise ApiError(400, "new_wf_id 需遵循 aiops-fix-{service}-{alert_id} 命名")
    await _write_guard(wf_id, {"NOTIFYING"})
    alert = Alert(alert_id=body.alert_id, service=body.service, description=body.description)
    await gw.signal_queue_patch(wf_id, body.new_wf_id, alert)
    gw.invalidate_cache()
    audit.write_audit(
        actor=request.state.identity.user,
        action="queue-patch",
        wf_id=wf_id,
        params={"new_wf_id": body.new_wf_id, "alert_id": body.alert_id},
        result="signaled",
    )
    return ok({"ok": True, "wf_id": wf_id, "action": "queue-patch"})
