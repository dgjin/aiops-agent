"""AIOps 运维控制台 BFF：FastAPI 入口（设计方案 5.1–5.6、6.1–6.4）。

接口职责：
- 读侧：overview / flows / flow detail / result / approvals / window / audit / system；
- 写侧：approval / second-approval / deploy-command / queue-patch
  （前置 stage 校验 → Temporal signal → 操作审计，前端不可绕过）；
- 静态托管：web/dist 构建产物（同域提供，SPA fallback）。

运行（工程根目录）：
    .venv/bin/uvicorn bff.app:app --host 127.0.0.1 --port 8600
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.service import RPCError, RPCStatusCode

from aiops_agent.config import load_policy, snapshot_for_workflow
from aiops_agent.models import Alert

from . import aggregator, audit
from .temporal_gateway import TemporalGateway, check_stage_allowed

gw = TemporalGateway()
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

# 策略锁定项（与 aiops_agent/config.py 的校验器一一对应，只读展示）
_POLICY_LOCKS = [
    {"key": "approval.on_timeout", "value": "reject", "reason": "审批超时仅自动驳回（锁定）"},
    {"key": "new_patch_during_window.strategy", "value": "queue", "reason": "窗口内新补丁仅 FIFO 排队（锁定）"},
    {"key": "new_patch_during_window.reset_timer", "value": False, "reason": "排队不重置倒计时（锁定）"},
    {"key": "new_patch_during_window.merge_with_current", "value": False, "reason": "排队不合并（锁定）"},
    {"key": "new_patch_during_window.queue_limit", "value": 0, "reason": "队列不设上限（锁定）"},
    {"key": "feedback_circuit_breaker.enabled", "value": False, "reason": "反馈不触发自动熔断（锁定）"},
    {"key": "concurrency.max_active_deployments", "value": 1, "reason": "全局仅允许一个发布（锁定）"},
    {"key": "notify_window.deploy_on_expiry", "value": True, "reason": "到期自动进入金丝雀（锁定）"},
]


class ApiError(Exception):
    """业务错误：由异常处理器统一转 JSON（设计方案 6.3）。"""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message


def ok(data: dict) -> dict:
    """统一响应包装：所有响应携带服务端时间（前端倒计时校准）。"""
    return {"server_time": datetime.now(timezone.utc).isoformat(), **data}


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await gw.client()  # 预热连接；失败不阻塞启动（接口按需重连，降级为只读提示）
    except Exception:  # noqa: BLE001 - Temporal 暂不可达时仍允许启动
        pass
    yield


app = FastAPI(title="AIOps Console BFF", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ApiError)
async def _api_error_handler(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=ok({"error": exc.message}))


# ----------------------------------------------------------------------
# 读侧接口
# ----------------------------------------------------------------------


@app.get("/api/health")
async def api_health() -> dict:
    connected, latency_ms, error = await gw.ping()
    return ok(
        {
            "ok": True,
            "temporal": {"connected": connected, "latency_ms": round(latency_ms, 1), "error": error},
        }
    )


def _close_dt(item: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(item["close_time"]) if item.get("close_time") else None
    except ValueError:
        return None


@app.get("/api/overview")
async def api_overview() -> dict:
    items = await gw.list_flows(limit=200)
    running = [i for i in items if i["exec_status"] == "RUNNING"]
    closed = [i for i in items if i["exec_status"] != "RUNNING"]

    today = date.today()
    today_done = today_escalated = 0
    for item in closed:
        close_dt = _close_dt(item)
        if close_dt and close_dt.astimezone().date() == today:
            if item["stage"] == "DONE":
                today_done += 1
            elif item["stage"] == "ESCALATED":
                today_escalated += 1

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    dist = {"DONE": 0, "ESCALATED": 0, "CANCELLED": 0}
    for item in closed:
        close_dt = _close_dt(item)
        if close_dt and close_dt >= week_ago and item["stage"] in dist:
            dist[item["stage"]] += 1

    counts = {
        "running": len(running),
        "wait_approval": sum(1 for i in running if i["stage"] == "WAIT_APPROVAL"),
        "notifying": sum(1 for i in running if i["stage"] == "NOTIFYING"),
        "today_done": today_done,
        "today_escalated": today_escalated,
    }
    return ok({"counts": counts, "running": running, "terminal_dist_7d": dist})


@app.get("/api/flows")
async def api_flows(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    service: str = "",
    stage: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    items = await gw.list_flows(limit=200)
    filtered = []
    for item in items:
        if status == "open" and item["exec_status"] != "RUNNING":
            continue
        if status == "closed" and item["exec_status"] == "RUNNING":
            continue
        if service and (item["alert"] or {}).get("service") != service:
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


@app.get("/api/flows/{wf_id}")
async def api_flow_detail(wf_id: str) -> dict:
    detail = await _detail_or_error(wf_id)
    patch_id = (detail.get("result") or {}).get("patch_id") or (detail.get("status") or {}).get(
        "patch_id"
    )
    detail["artifacts"] = aggregator.artifacts_for_patch(patch_id) if patch_id else None
    return ok(detail)


@app.get("/api/flows/{wf_id}/result")
async def api_flow_result(wf_id: str) -> dict:
    detail = await _detail_or_error(wf_id)
    if detail["exec_status"] == "RUNNING":
        stage = (detail.get("status") or {}).get("stage")
        raise ApiError(409, f"流程仍在运行（阶段 {stage}），审计记录在终态后可用")
    if not detail.get("result"):
        raise ApiError(404, "审计记录不可用（工作流无结果）")
    return ok({"result": detail["result"]})


@app.get("/api/approvals")
async def api_approvals() -> dict:
    items = await gw.list_flows(limit=200)
    pending = []
    for item in items:
        if item["exec_status"] != "RUNNING" or item["stage"] != "WAIT_APPROVAL":
            continue
        if item["approval"] is None:
            pending.append({**item, "pending": "approval"})
        elif item["needs_second"] and item["approval"] == "approve" and item["second_approval"] is None:
            pending.append({**item, "pending": "second_approval"})
    pending.sort(key=lambda i: (i.get("deadline") or {}).get("at") or "9999")
    return ok({"items": pending})


@app.get("/api/window")
async def api_window() -> dict:
    items = await gw.list_flows(limit=200)
    notifying = [i for i in items if i["exec_status"] == "RUNNING" and i["stage"] == "NOTIFYING"]
    notifying.sort(key=lambda i: (i.get("deadline") or {}).get("at") or "9999")
    versions = await asyncio.gather(
        *(asyncio.to_thread(aggregator.broadcast_version, i.get("patch_id")) for i in notifying)
    )
    for item, version in zip(notifying, versions):
        item["version"] = version
    return ok({"items": notifying})


@app.get("/api/audit")
async def api_audit(
    q: str = "",
    stage: str = "",
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    items = await gw.list_flows(limit=200)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    needle = q.strip().lower()
    rows = []
    for item in items:
        if item["exec_status"] == "RUNNING":
            continue
        close_dt = _close_dt(item)
        if close_dt is None or close_dt < cutoff:
            continue
        if stage and item["stage"] != stage:
            continue
        if needle:
            alert = item["alert"] or {}
            haystack = " ".join(
                str(v) for v in [item["wf_id"], alert.get("alert_id"), alert.get("service"), item.get("patch_id")]
            ).lower()
            if needle not in haystack:
                continue
        rows.append(item)
    rows.sort(key=lambda i: i.get("close_time") or "", reverse=True)
    return ok({"items": rows[:limit], "ops": audit.read_audit(limit=100)})


@app.get("/api/system")
async def api_system() -> dict:
    connected, latency_ms, error = await gw.ping()

    policy_payload = None
    policy_error = None
    try:
        policy = load_policy()
        snapshot = snapshot_for_workflow(policy)
        policy_payload = {
            "source": snapshot["policy_source"],
            "summary": policy.summary(),
            "triage": snapshot["triage"],
            "approval": snapshot["approval"],
            "notify_window": snapshot["notify_window"],
            "canary": snapshot["canary"],
            "locks": _POLICY_LOCKS,
        }
    except Exception as exc:  # noqa: BLE001 - 策略缺失/非法时降级展示
        policy_error = str(exc)

    stable_port = os.environ.get("AIOPS_STABLE_PORT", "18080")
    stable = await asyncio.to_thread(aggregator.probe_stable, stable_port)

    return ok(
        {
            "temporal": {
                "connected": connected,
                "latency_ms": round(latency_ms, 1),
                "address": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
                "error": error,
            },
            "policy": policy_payload,
            "policy_error": policy_error,
            "index": aggregator.index_stats(),
            "stable": stable,
            "recent_releases": aggregator.recent_releases(5),
        }
    )


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


@app.post("/api/flows/{wf_id}/approval")
async def api_approval(wf_id: str, body: ApprovalBody) -> dict:
    def _extra(status: dict) -> str | None:
        if status.get("approval"):
            return f"一级审批已提交（{status['approval']}），不能重复提交"
        return None

    await _write_guard(wf_id, {"WAIT_APPROVAL"}, _extra)
    await gw.signal_approval(wf_id, body.decision)
    gw.invalidate_cache()
    audit.write_audit(actor="web-console", action=f"approval:{body.decision}", wf_id=wf_id)
    return ok({"ok": True, "wf_id": wf_id, "action": f"approval:{body.decision}"})


@app.post("/api/flows/{wf_id}/second-approval")
async def api_second_approval(wf_id: str, body: ApprovalBody) -> dict:
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
    audit.write_audit(actor="web-console", action=f"second-approval:{body.decision}", wf_id=wf_id)
    return ok({"ok": True, "wf_id": wf_id, "action": f"second-approval:{body.decision}"})


@app.post("/api/flows/{wf_id}/deploy-command")
async def api_deploy_command(wf_id: str, body: DeployCommandBody) -> dict:
    await _write_guard(wf_id, {"NOTIFYING"})
    await gw.signal_deploy_command(wf_id, body.command)
    gw.invalidate_cache()
    audit.write_audit(actor="web-console", action=f"deploy-command:{body.command}", wf_id=wf_id)
    return ok({"ok": True, "wf_id": wf_id, "action": f"deploy-command:{body.command}"})


@app.post("/api/flows/{wf_id}/queue-patch")
async def api_queue_patch(wf_id: str, body: QueuePatchBody) -> dict:
    if not body.new_wf_id.startswith("aiops-fix-"):
        raise ApiError(400, "new_wf_id 需遵循 aiops-fix-{service}-{alert_id} 命名")
    await _write_guard(wf_id, {"NOTIFYING"})
    alert = Alert(alert_id=body.alert_id, service=body.service, description=body.description)
    await gw.signal_queue_patch(wf_id, body.new_wf_id, alert)
    gw.invalidate_cache()
    audit.write_audit(
        actor="web-console",
        action="queue-patch",
        wf_id=wf_id,
        params={"new_wf_id": body.new_wf_id, "alert_id": body.alert_id},
    )
    return ok({"ok": True, "wf_id": wf_id, "action": "queue-patch"})


# ----------------------------------------------------------------------
# 静态托管（web/dist 存在时启用；SPA fallback）
# ----------------------------------------------------------------------

if (_WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    if _WEB_DIST.is_dir():
        dist_root = _WEB_DIST.resolve()
        candidate = (dist_root / full_path).resolve() if full_path else dist_root
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_root):
            return FileResponse(candidate)
        index = dist_root / "index.html"
        if index.is_file():
            return FileResponse(index)
    raise HTTPException(status_code=404, detail="前端未构建（先在 web/ 执行 npm run build）或接口不存在")
