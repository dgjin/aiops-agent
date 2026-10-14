"""飞书卡片回调网关（审批交互）：POST /api/im/card-callback。

链路：审批卡片（「批准 / 驳回」按钮，载荷 {"wf_id","gate","decision"}）→ 用户点击 →
飞书服务器 POST 本端点（新版 card.action.trigger，Verification Token 位于 header.token）→
校验凭证 → 解析载荷与操作者 → 操作者角色（AIOPS_IM_USERS，operator 才可审批）→
转发 Temporal signal → 200 响应 toast + 处置结果卡片（替代按钮区，防重复点击）。

安全模型（fail-closed）：
- 本端点由 middleware ``_CALLBACK_ENDPOINTS`` 豁免 Bearer（飞书无法携带控制台凭证），
  改以 Verification Token 校验：``AIOPS_FEISHU_VERIFY_TOKEN`` 未配置＝通道关闭（503）、
  凭证不符＝认证失败（401）——挑战握手与动作回调走同一校验，不回落常规鉴权；
- 加密模式（Encrypt Key）不支持：携带 ``encrypt`` 字段的载荷明确拒绝（400），
  飞书后台需保持明文回调模式；
- 操作者审批权限在路由内判定（未映射发送者＝viewer，仅可查询不可审批）。

响应策略：除凭证/格式错误外一律 HTTP 200 + toast——业务拒绝（未找到流程 / 阶段不符 /
重复提交 / 权限不足 / kill switch）在卡片内呈现；5xx 会触发飞书重试与客户端错误码，
且协议要求 3 秒内响应（本地 Temporal 调用为毫秒级）。兼容旧版回调结构（action 在
顶层）的解析与响应包装；飞书后台建议仅订阅新版 card.action.trigger。
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from fastapi import APIRouter
from temporalio.service import RPCError

from aiops_agent import chatops as chatops_engine
from aiops_agent import kill_switch, metrics, notify

from .. import audit
from ..deps import ApiError, gw, log
from .chatops import _find_flow, _sender_roles
from .flows import _first_approval_extra, _second_approval_extra, _write_guard

router = APIRouter()

_TOKEN_ENV = "AIOPS_FEISHU_VERIFY_TOKEN"
_ACTION_LABELS = {"approval": "一级审批", "second_approval": "二级审批"}
_RESULT_LABELS = {"approve": "已批准", "reject": "已驳回"}


def _declared_token(payload: dict) -> str:
    """回调载荷中的 Verification Token：挑战 / 旧版在顶层，v2 在 header.token。"""
    header = payload.get("header")
    if isinstance(header, dict):
        token = header.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    token = payload.get("token")
    return token.strip() if isinstance(token, str) else ""


def _verify_channel(payload: dict) -> None:
    """Verification Token 校验（fail-closed）：未配置＝503、不符＝401。"""
    expected = (os.environ.get(_TOKEN_ENV) or "").strip()
    if not expected:
        raise ApiError(503, f"飞书卡片回调通道未配置（{_TOKEN_ENV}=空），通道关闭")
    if not hmac.compare_digest(_declared_token(payload), expected):
        log.warning("[card-callback] Verification Token 校验失败")
        raise ApiError(401, "飞书卡片回调凭证校验失败")


def _toast(kind: str, content: str) -> dict:
    """错误/提示 toast 响应（HTTP 200：卡片内呈现原因，避免 5xx 触发飞书重试）。"""
    return {"toast": {"type": kind, "content": content}}


@router.post("/api/im/card-callback")
async def api_im_card_callback(payload: dict) -> dict:
    """飞书卡片动作回调：审批信号直通（一级 / 二级审批批准与驳回）。"""
    if payload.get("encrypt"):
        raise ApiError(400, "收到加密回调载荷：本通道不支持加密模式，请在飞书后台关闭 Encrypt Key")
    if payload.get("type") == "url_verification":  # 配置回调地址时的挑战握手
        _verify_channel(payload)
        return {"challenge": str(payload.get("challenge") or "")}
    _verify_channel(payload)
    header = payload.get("header")
    event_type = header.get("event_type") if isinstance(header, dict) else None
    if event_type and event_type != "card.action.trigger":
        raise ApiError(400, f"不支持的飞书回调事件类型：{event_type}（仅支持 card.action.trigger）")
    parsed = notify.parse_callback(payload)
    if parsed is None:
        return _toast("error", "无法解析回调载荷（wf_id/gate/decision 缺失或非法）")
    wf_arg, gate, decision = parsed
    operator = notify.extract_callback_operator(payload)
    if not operator:
        return _toast("error", "无法识别操作者身份，已拒绝本次操作")
    role = chatops_engine.resolve_sender_role(operator, _sender_roles())
    if not chatops_engine.has_permission(role, "operator"):
        log.warning(
            "[card-callback] 权限拒绝：operator=%s role=%s gate=%s wf=%s",
            operator, role, gate, wf_arg,
        )
        return _toast("error", f"权限不足：{operator}（{role}）无权执行审批（要求 operator）")
    try:
        state = kill_switch.get_state()
    except kill_switch.KillSwitchError as exc:
        log.warning("[card-callback] kill switch 状态不可读：%s", exc)
        return _toast("error", "kill switch 状态不可读，写操作已拒绝")
    if state.get("active"):
        return _toast(
            "error",
            "kill switch 已激活，写操作被拒绝"
            f"（原因：{state.get('reason') or '未注明'}；操作者：{state.get('actor') or '未知'}）",
        )
    try:
        wf_id, _ = await _find_flow(wf_arg)
    except ApiError as exc:
        return _toast("error", exc.message)
    extra = _first_approval_extra if gate == "approval" else _second_approval_extra
    try:
        await _write_guard(wf_id, {"WAIT_APPROVAL"}, extra)
    except ApiError as exc:
        log.info("[card-callback] 拒绝：operator=%s wf=%s：%s", operator, wf_id, exc.message)
        return _toast("error", f"未执行：{exc.message}")
    try:
        if gate == "approval":
            await gw.signal_approval(wf_id, decision)
        else:
            await gw.signal_second_approval(wf_id, decision)
    except RPCError as exc:
        log.warning("[card-callback] Temporal 调用失败：%s", exc)
        return _toast("error", "系统暂时不可用，请稍后重试或在控制台处置")
    gw.invalidate_cache()
    metrics.observe_gate_event(gate, decision)
    audit.write_audit(
        actor=f"im:{operator}",
        action=f"card-callback:{'approval' if gate == 'approval' else 'second-approval'}:{decision}",
        wf_id=wf_id,
        params={"channel": "feishu-card", "operator": operator},
        result="signaled",
    )
    at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    label = f"{_ACTION_LABELS.get(gate, gate)}{_RESULT_LABELS.get(decision, decision)}"
    log.info("[card-callback] %s → %s/%s（wf=%s）", operator, gate, decision, wf_id)
    result_card = notify.render_approval_result_card(wf_id, gate, decision, operator, at)
    if isinstance(header, dict):  # 新版回调：card 带 type=raw 包装
        card_field: dict = {"type": "raw", "data": result_card["card"]}
    else:  # 旧版回调：card 直接为卡片 JSON
        card_field = result_card["card"]
    return {
        "toast": {"type": "success", "content": f"{label}（操作者 {operator}）"},
        "card": card_field,
    }
