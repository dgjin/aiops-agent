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
重复提交 / 权限不足 / 职责分离 / 证据过期 / kill switch）在卡片内呈现；5xx 会触发
飞书重试与客户端错误码，且协议要求 3 秒内响应（本地 Temporal 调用为毫秒级）。

审批治理（与 Web 控制台 / ChatOps 同源）：
- 职责分离：发起人不得自审，二级审批人与一级审批人不得相同；
- 证据绑定：卡片携带 case_id/diff_hash/run_id，仅用于拒绝过期页面，可信绑定从
  工作流 status 派生（不信任回调字段）；
- 命令回执：signal RPC 成功只代表命令已送达，端点短轮询 status 取业务回执；
  仅「已记录」时替换按钮区为回执卡，拒收时保留卡片便于修正重试；
- 驳回意见：卡片按钮无法输入文本，无意见时拒绝并引导至控制台 / ChatOps，
  绝不代填理由。

兼容旧版回调结构（action 在顶层）的解析与响应包装；飞书后台建议仅订阅新版
card.action.trigger。
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from fastapi import APIRouter

from aiops_agent import chatops as chatops_engine
from aiops_agent import kill_switch, notify

from ..deps import ApiError, log
from .chatops import _find_flow, _sender_roles
from .flows import ApprovalBody, submit_approval_command

router = APIRouter()

_TOKEN_ENV = "AIOPS_FEISHU_VERIFY_TOKEN"
_ACTION_LABELS = {"approval": "一级审批", "second_approval": "二级审批"}
_DECISION_LABELS = {"approve": "批准", "reject": "驳回"}


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
    comment = notify.extract_callback_comment(payload)
    if decision == "reject" and not comment:
        # 按钮无法输入理由：不代填、不推进，引导到可留痕的通道
        log.info("[card-callback] 驳回缺少意见：gate=%s wf=%s", gate, wf_arg)
        return _toast(
            "error",
            "驳回必须填写审批意见：请在控制台或群内发送「驳回 <ID前缀> -- 意见」",
        )
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

    evidence = notify.extract_callback_evidence(payload)
    action = f"card-callback:{'approval' if gate == 'approval' else 'second-approval'}:{decision}"
    try:
        outcome = await submit_approval_command(
            wf_id=wf_id,
            gate=gate,
            actor=operator,
            channel="feishu-card",
            body=ApprovalBody(
                decision=decision,
                comment=comment,
                expected_case_id=evidence.get("case_id", ""),
                expected_diff_hash=evidence.get("diff_hash", ""),
                expected_run_id=evidence.get("run_id", ""),
            ),
            audit_action=action,
            audit_actor=f"im:{operator}",
        )
    except ApiError as exc:
        log.info("[card-callback] 拒绝：operator=%s wf=%s：%s", operator, wf_id, exc.message)
        return _toast("error", f"未执行：{exc.message}")

    if outcome["rejected"]:
        reason = outcome["reason"] or "流程拒绝了该审批命令"
        log.info("[card-callback] 流程拒绝：operator=%s wf=%s：%s", operator, wf_id, reason)
        return _toast("error", f"未生效：{reason}")
    if outcome["delivery"] == "pending":
        return _toast(
            "warning",
            (
                f"命令已登记（{outcome['command_id']}），系统暂不可用，将自动重试；"
                "请稍后查看流程状态"
            ),
        )

    at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    label = f"{_ACTION_LABELS.get(gate, gate)}{_DECISION_LABELS.get(decision, decision)}命令已提交"
    log.info("[card-callback] %s → %s/%s（wf=%s）", operator, gate, decision, wf_id)
    result_card = notify.render_approval_result_card(
        wf_id, gate, decision, operator, at, comment=comment, command_id=outcome["command_id"]
    )
    if isinstance(header, dict):  # 新版回调：card 带 type=raw 包装
        card_field: dict = {"type": "raw", "data": result_card["card"]}
    else:  # 旧版回调：card 直接为卡片 JSON
        card_field = result_card["card"]
    return {
        "toast": {
            "type": "success",
            "content": f"{label}（操作者 {operator}）{_delivery_suffix(outcome['delivery'])}",
        },
        "card": card_field,
    }


def _delivery_suffix(delivery: str) -> str:
    """回执不确定时提示以流程状态为准；已记录时无需附加说明。"""
    return "（业务结果以流程状态为准）" if delivery == "submitted" else ""
