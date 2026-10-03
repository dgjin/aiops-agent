"""系统域：/api/system（策略 / 稳定版 / 清单 / kill switch 快照）、/api/system/kill-switch。"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aiops_agent import kill_switch
from aiops_agent.config import load_policy, snapshot_for_workflow

from .. import aggregator, audit, config_items
from ..deps import ApiError, gw, log, ok
from .monitored_apps import apps_with_probe

router = APIRouter()

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


@router.get("/api/system")
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
    # 稳定版探测与被监控应用清单探测互不阻塞（探测失败均降级返回）
    stable = await asyncio.to_thread(aggregator.probe_stable, stable_port)
    monitored_app_payload = await apps_with_probe()

    # kill switch 状态（读侧容错：不可读时展示错误提示，不阻塞系统页其余信息）
    try:
        kill_switch_payload = {"state": kill_switch.get_state(), "error": None}
    except kill_switch.KillSwitchError as exc:
        kill_switch_payload = {"state": None, "error": str(exc)}

    return ok(
        {
            "temporal": {
                "connected": connected,
                "latency_ms": round(latency_ms, 1),
                "address": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
                "error": error,
            },
            "kill_switch": kill_switch_payload,
            "policy": policy_payload,
            "policy_error": policy_error,
            "index": aggregator.index_stats(),
            "stable": stable,
            "monitored_apps": monitored_app_payload,
            # 配置项元数据：当前值 / 来源 / 生效方式 / 对运行中流程是否生效
            "config_items": config_items.collect(
                policy_source=(policy_payload or {}).get("source"),
                policy_summary=(policy_payload or {}).get("summary"),
                monitored_app_count=len(monitored_app_payload),
            ),
            "recent_releases": aggregator.recent_releases(5),
        }
    )


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = Field(default="", max_length=200)


@router.post("/api/system/kill-switch")
async def api_kill_switch(request: Request, body: KillSwitchBody) -> dict:
    """紧急停止开关（admin）：激活后拒绝一切写操作与新流程启动；自身可关闭。

    激活/关闭均落操作审计（配置类操作：wf_id 留空 + target 记录动作方向）。
    """
    try:
        state = kill_switch.set_state(
            active=body.active, actor=request.state.identity.user, reason=body.reason
        )
    except kill_switch.KillSwitchError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="system:kill-switch",
        target="activate" if body.active else "deactivate",
        params={"reason": body.reason},
    )
    log.warning(
        "[kill-switch] %s（操作者=%s，原因=%s）",
        "已激活" if body.active else "已关闭",
        request.state.identity.user,
        body.reason or "未注明",
    )
    return ok({"kill_switch": state})
