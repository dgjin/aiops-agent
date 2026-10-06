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

from aiops_agent import metrics
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
    return ok({"counts": counts, "running": running, "terminal_dist_7d": dist})


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
    return ok(detail)


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
    decision: str = Field(pattern="^(approve|reject)$")


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


@router.post("/api/flows/{wf_id}/approval")
async def api_approval(request: Request, wf_id: str, body: ApprovalBody) -> dict:
    def _extra(status: dict) -> str | None:
        if status.get("approval"):
            return f"一级审批已提交（{status['approval']}），不能重复提交"
        return None

    await _write_guard(wf_id, {"WAIT_APPROVAL"}, _extra)
    await gw.signal_approval(wf_id, body.decision)
    gw.invalidate_cache()
    metrics.observe_gate_event("approval", body.decision)
    audit.write_audit(
        actor=request.state.identity.user,
        action=f"approval:{body.decision}",
        wf_id=wf_id,
        result="signaled",
    )
    return ok({"ok": True, "wf_id": wf_id, "action": f"approval:{body.decision}"})


@router.post("/api/flows/{wf_id}/second-approval")
async def api_second_approval(request: Request, wf_id: str, body: ApprovalBody) -> dict:
    def _extra(status: dict) -> str | None:
        if not status.get("needs_second"):
            return "该流程不涉及受保护目录，无需二级审批"
        if status.get("approval") != "approve":
            return "一级审批尚未批准，不能进行二级审批"
        if status.get("second_approval"):
            return f"二级审批已提交（{status['second_approval']}），不能重复提交"
        return None

    await _write_guard(wf_id, {"WAIT_APPROVAL"}, _extra)
    await gw.signal_second_approval(wf_id, body.decision)
    gw.invalidate_cache()
    metrics.observe_gate_event("second_approval", body.decision)
    audit.write_audit(
        actor=request.state.identity.user,
        action=f"second-approval:{body.decision}",
        wf_id=wf_id,
        result="signaled",
    )
    return ok({"ok": True, "wf_id": wf_id, "action": f"second-approval:{body.decision}"})


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
