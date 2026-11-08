"""流程域：/api/overview、/api/flows*（列表 / 详情 / 结果）与写侧四端点。

写侧（approval / second-approval / deploy-command / queue-patch）统一走
``_write_guard`` 前置校验（运行中 + 阶段允许 + 业务附加条件），随后进入
``bff.commands`` 的 outbox：意图审计与命令同事务落库 → 立即投递 → 业务回执。
Temporal 暂不可达时命令保留为 PENDING 并退避重试，不再丢失用户意图；
``signal RPC 成功``不等于业务受理，业务结果仍以工作流记录为准（设计方案 6.3、M2）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from temporalio.service import RPCError, RPCStatusCode

from aiops_agent import change_evidence, command_contract, evidence_store, metrics

from .. import (
    aggregator,
    audit,
    commands,
    escalations,
    evidence_package,
    history_projector,
    runs_store,
    timeline,
)
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
        # retrying 仍属未闭环（M3）：侧栏角标与待办页口径一致
        return stats["open"] + stats["assigned"] + stats.get("retrying", 0)
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
    # 投影完整性：详情页据此展示「已同步事件数 / 断档 / 延迟」，不阻塞主流程详情
    try:
        detail["history"] = await asyncio.to_thread(
            history_projector.sync_status, wf_id, detail.get("run_id")
        )
    except Exception as exc:  # noqa: BLE001 - 投影状态不可读时降级为未知
        log.warning("[history] %s 同步状态读取失败：%s", wf_id, exc)
        detail["history"] = {"run_id": detail.get("run_id"), "error": f"{type(exc).__name__}: {exc}"}
    return ok(detail)


@router.get("/api/flows/{wf_id}/timeline")
async def api_flow_timeline(
    wf_id: str,
    detail: bool = False,
    all: bool = False,
    history_limit: int = Query(5000, ge=1, le=20000),
    limit: int = Query(0, ge=0, le=5000),
) -> dict:
    """统一时间线：History + 闸门/审批 + 操作审计 + 命令投递 + 通知 + 人工待办。

    ``detail=true`` 返回每个节点的原始 detail；``all=true`` 不裁剪 History 内部事件；
    ``limit>0`` 只取最近 N 条（时间线首屏），返回 ``has_more`` 提示可继续拉取。
    """
    flow = await _detail_or_error(wf_id)
    payload = await asyncio.to_thread(
        timeline.build,
        flow,
        include_history_detail=detail,
        include_all_history=all,
        history_limit=history_limit,
        limit=limit,
    )
    return ok(payload)


@router.get("/api/flows/{wf_id}/history")
async def api_flow_history(
    wf_id: str,
    run_id: str = "",
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
    event_type: str = "",
    include_detail: bool = True,
) -> dict:
    """已投影的 Temporal History 事件（游标分页，不受 UI 200 条上限约束）。"""
    payload = await asyncio.to_thread(
        history_projector.read_events,
        wf_id,
        run_id or None,
        after_id=after_id,
        limit=limit,
        event_type=event_type,
        include_detail=include_detail,
    )
    payload["sync"] = await asyncio.to_thread(
        history_projector.sync_status, wf_id, run_id or payload.get("run_id")
    )
    return ok(payload)


@router.get("/api/flows/{wf_id}/evidence-package")
async def api_flow_evidence_package(
    request: Request,
    wf_id: str,
    run_id: str = "",
) -> Response:
    """导出审计证据包（自包含 JSON + manifest/SHA-256），导出动作写审计链。

    权限：operator（见 middleware.WRITE_ROLE_RULES）；审计写入失败则导出失败（fail-closed）。
    """
    flow = await _detail_or_error(wf_id)
    actor = getattr(request.state.identity, "user", "") or ""
    try:
        package, entry = await evidence_package.build_and_audit(
            wf_id, actor=actor, detail=flow, run_id=run_id or None
        )
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 证据包构造失败统一 503，避免半成品下载
        log.exception("[evidence] %s 证据包导出失败", wf_id)
        raise ApiError(503, f"证据包导出失败：{type(exc).__name__}: {exc}") from exc
    filename = f"evidence-{wf_id}.json"
    return Response(
        content=json.dumps(package, ensure_ascii=False, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Audit-Event-Id": str(entry.get("event_id") or ""),
            "X-Evidence-Root": str(package["manifest"]["root_sha256"]),
            "X-History-Complete": "1" if package["history"].get("complete") else "0",
        },
    )


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


async def _record_and_deliver(
    *,
    command_type: str,
    wf_id: str,
    actor: str,
    channel: str,
    payload: dict | str,
    audit_action: str,
    command_id: str | None = None,
    run_id: str = "",
    audit_params: dict | None = None,
) -> dict:
    """意图审计 + 命令入库（同一事务）→ 立即投递一次；返回命令记录。

    存储不可用即拒绝新操作（fail-closed），绝不出现「无审计推进」。
    """
    try:
        record = await asyncio.to_thread(
            commands.record_intent,
            command_type=command_type,
            wf_id=wf_id,
            actor=actor,
            channel=channel,
            payload=payload,
            audit_action=audit_action,
            command_id=command_id,
            run_id=run_id,
            audit_params=audit_params,
        )
    except commands.CommandStoreError as exc:
        raise ApiError(503, f"写操作未能登记（审计/命令存储不可用）：{exc}") from exc
    try:
        return await commands.deliver(record["command_id"])
    except commands.CommandNotFound as exc:
        raise ApiError(503, "命令已登记但读取失败，请稍后用命令 ID 查询") from exc
    except Exception as exc:  # noqa: BLE001 - 投递异常不得谎报成功
        log.warning("[commands] 投递异常：command=%s %s", record["command_id"], exc)
        raise ApiError(
            503, f"命令已登记（{record['command_id']}）但投递状态未知：{exc}"
        ) from exc


def _command_outcome(record: dict, *, legacy: bool = False) -> dict:
    """命令记录 → 路由语义：业务拒收用返回值表达，系统类失败抛 503。"""
    if record["status"] == "FAILED":
        reason = record.get("last_error") or "命令投递失败"
        if str(reason).startswith(("Temporal", "kill switch", "命令存储")):
            raise ApiError(503, f"命令未送达（{record['command_id']}）：{reason}")
        return {
            "record": record,
            "rejected": True,
            "reason": reason,
            "delivery": "rejected",
            "command_id": record["command_id"],
            "legacy": legacy,
        }
    delivery = {"ACCEPTED": "accepted", "SUBMITTED": "submitted"}.get(
        record["status"], "pending"
    )
    if legacy and delivery != "pending":
        delivery = "submitted"
    return {
        "record": record,
        "rejected": False,
        "reason": "",
        "delivery": delivery,
        "command_id": record["command_id"],
        "legacy": legacy,
    }


async def submit_approval_command(
    *,
    wf_id: str,
    gate: str,
    actor: str,
    channel: str,
    body: ApprovalBody,
    audit_action: str,
    audit_actor: str | None = None,
) -> dict:
    """审批统一写链路（Web 控制台 / ChatOps / 飞书卡片共用）。

    前置校验 → 职责分离 → 证据信封 → 意图审计+命令入库 → 投递 → 结果口径。
    业务拒收由返回值 ``rejected`` 表达（调用方决定 HTTP/卡片语义）；
    存储或投递系统故障抛 ``ApiError(503)``，保证未送达不会被谎报为已提交。
    """
    extra = _first_approval_extra if gate == "approval" else _second_approval_extra
    status = await _write_guard(wf_id, {"WAIT_APPROVAL"}, extra)

    separation = duty_separation_error(status, gate, actor)
    if separation:
        audit.write_audit(
            actor=audit_actor or actor,
            action=audit_action,
            wf_id=wf_id,
            params={"channel": channel, "comment": body.comment, "delivery": "denied"},
            result="denied",
        )
        raise ApiError(409, separation)

    payload, envelope_id = _signal_envelope(
        status,
        gate=gate,
        decision=body.decision,
        actor=actor,
        channel=channel,
        comment=body.comment,
        expected_case_id=body.expected_case_id,
        expected_diff_hash=body.expected_diff_hash,
        expected_run_id=body.expected_run_id,
    )
    record = await _record_and_deliver(
        command_type="approval" if gate == "approval" else "second-approval",
        wf_id=wf_id,
        actor=audit_actor or actor,
        channel=channel,
        payload=payload,
        audit_action=audit_action,
        command_id=envelope_id or None,
        run_id=str(status.get("run_id") or ""),
        audit_params={"decision": body.decision, "comment": body.comment},
    )
    outcome = _command_outcome(record, legacy=not envelope_id)
    if not outcome["rejected"] and outcome["delivery"] in ("accepted", "submitted"):
        metrics.observe_gate_event(gate, body.decision)
    return outcome


async def _submit_approval(
    request: Request,
    wf_id: str,
    *,
    gate: str,
    body: ApprovalBody,
) -> dict:
    """Web 控制台审批入口：统一链路 + 409/503 语义。"""
    actor = request.state.identity.user
    action = "approval" if gate == "approval" else "second-approval"
    outcome = await submit_approval_command(
        wf_id=wf_id,
        gate=gate,
        actor=actor,
        channel="console",
        body=body,
        audit_action=f"{action}:{body.decision}",
    )
    if outcome["rejected"]:
        raise ApiError(409, f"审批未生效：{outcome['reason']}")

    delivery = outcome["delivery"]
    if delivery == "accepted":
        message = "审批命令已受理并记录"
    elif delivery == "pending":
        message = "审批命令已登记，系统暂不可达，将自动重试；请稍后用命令 ID 查询"
    elif outcome["legacy"]:
        message = "审批命令已提交（旧流程）：业务结果以流程状态为准"
    else:
        message = "审批命令已提交；业务结果以流程状态为准"
    return ok(
        {
            "ok": True,
            "wf_id": wf_id,
            "action": f"{action}:{body.decision}",
            "command_id": outcome["command_id"],
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
    status = await _write_guard(wf_id, {"NOTIFYING"})
    actor = request.state.identity.user
    command_id = command_contract.new_command_id()
    envelope = command_contract.build_envelope(
        command_type="deploy-command",
        command_id=command_id,
        actor=actor,
        channel="console",
        submitted_at=command_contract.utc_now_iso(),
        payload=command_contract.deploy_payload(body.command),
    )
    record = await _record_and_deliver(
        command_type="deploy-command",
        wf_id=wf_id,
        actor=actor,
        channel="console",
        payload=envelope,
        audit_action=f"deploy-command:{body.command}",
        command_id=command_id,
        run_id=str(status.get("run_id") or ""),
        audit_params={"command": body.command},
    )
    outcome = _command_outcome(record)
    if outcome["rejected"]:
        raise ApiError(409, f"发布指令未生效：{outcome['reason']}")
    if outcome["delivery"] != "pending":
        metrics.observe_gate_event("deploy_command", body.command)
    message = {
        "accepted": "发布指令已受理",
        "submitted": "发布指令已提交；业务结果以流程状态为准",
    }.get(outcome["delivery"], "发布指令已登记，系统暂不可达，将自动重试")
    return ok(
        {
            "ok": True,
            "wf_id": wf_id,
            "action": f"deploy-command:{body.command}",
            "command_id": outcome["command_id"],
            "delivery": outcome["delivery"],
            "message": message,
        }
    )


@router.post("/api/flows/{wf_id}/queue-patch")
async def api_queue_patch(request: Request, wf_id: str, body: QueuePatchBody) -> dict:
    if not body.new_wf_id.startswith("aiops-fix-"):
        raise ApiError(400, "new_wf_id 需遵循 aiops-fix-{service}-{alert_id} 命名")
    status = await _write_guard(wf_id, {"NOTIFYING"})
    actor = request.state.identity.user
    command_id = command_contract.new_command_id()
    envelope = command_contract.build_envelope(
        command_type="queue-patch",
        command_id=command_id,
        actor=actor,
        channel="console",
        submitted_at=command_contract.utc_now_iso(),
        payload=command_contract.queue_payload(
            new_wf_id=body.new_wf_id,
            service=body.service,
            alert_id=body.alert_id,
            description=body.description,
        ),
    )
    record = await _record_and_deliver(
        command_type="queue-patch",
        wf_id=wf_id,
        actor=actor,
        channel="console",
        payload=envelope,
        audit_action="queue-patch",
        command_id=command_id,
        run_id=str(status.get("run_id") or ""),
        audit_params={"new_wf_id": body.new_wf_id, "alert_id": body.alert_id},
    )
    outcome = _command_outcome(record)
    if outcome["rejected"]:
        raise ApiError(409, f"排队指令未生效：{outcome['reason']}")
    message = {
        "accepted": "排队指令已受理",
        "submitted": "排队指令已提交；业务结果以流程状态为准",
    }.get(outcome["delivery"], "排队指令已登记，系统暂不可达，将自动重试")
    return ok(
        {
            "ok": True,
            "wf_id": wf_id,
            "action": "queue-patch",
            "command_id": outcome["command_id"],
            "delivery": outcome["delivery"],
            "message": message,
        }
    )
