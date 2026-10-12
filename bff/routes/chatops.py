"""IM 通道（ChatOps，P2-3）：POST /api/im/command——群内查询与审批指令。

链路：IM 网桥（飞书/钉钉机器人出口）把群消息 POST 到本端点，携带 AIOPS_IM_TOKEN
由 middleware ``_im_identity`` 做通道令牌旁路（未配置令牌＝通道关闭 fail-closed）。
回复契约 ``{"ok": bool, "reply": 文本}``：命令级失败（权限不足 / 未找到流程 /
前置校验不过）也返回 200，由网桥把 reply 直接转发回群；仅「入站无法解析 = 400」
与「系统级故障 = 5xx（KillSwitchError / Temporal 不可达）」走错误状态码。

权限：通道身份固定为 ``im:gateway``（viewer）；**命令级**权限在 ``api_im_command``
内按 ``AIOPS_IM_USERS``（JSON：{"<sender>": "operator"} 或 {"<sender>": {"role": ...}}）
解析发送者角色，未映射 = viewer（最小权限，仅可查询）。

审批命令执行前复用流程域 ``_write_guard``（运行中 + WAIT_APPROVAL + 防重复，
与 Web 控制台同款语义）；kill switch 激活时写命令同样被阻断，查询命令保留可用
（与控制台 GET 行为对齐）——故本端点豁免 middleware 粗粒度检查，改在路由内细粒度校验。
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter
from temporalio.service import RPCError

from aiops_agent import chatops, kill_switch, metrics

from .. import audit
from ..deps import ApiError, gw, log, ok
from .flows import _first_approval_extra, _second_approval_extra, _write_guard

router = APIRouter()

_ROLE_ENV = "AIOPS_IM_USERS"
_LIST_LIMIT = 200


def _sender_roles() -> dict:
    """AIOPS_IM_USERS → 映射字典；未配置 / 非法 JSON / 非对象一律空映射（全员 viewer）。"""
    raw = (os.environ.get(_ROLE_ENV) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[chatops] %s 非法 JSON，按空映射处理（未映射发送者 = viewer）", _ROLE_ENV)
        return {}
    if not isinstance(parsed, dict):
        log.warning("[chatops] %s 不是对象，按空映射处理", _ROLE_ENV)
        return {}
    return parsed


async def _find_flow(arg: str) -> tuple[str, dict]:
    """完整 ID / 唯一前缀 → (wf_id, item)；未命中或不唯一抛 ApiError。"""
    items = await gw.list_flows(limit=_LIST_LIMIT)
    exact = next((item for item in items if item.get("wf_id") == arg), None)
    if exact is not None:
        return exact["wf_id"], exact
    matches = [item for item in items if str(item.get("wf_id") or "").startswith(arg)]
    if len(matches) == 1:
        return matches[0]["wf_id"], matches[0]
    if not matches:
        raise ApiError(404, f"未找到流程（ID 前缀 {arg}）；发送「待办」查看待审批列表")
    candidates = "、".join(str(item["wf_id"]) for item in matches[:5])
    raise ApiError(409, f"前缀 {arg} 不唯一，请补充更多字符：{candidates}")


def _reply(*, success: bool, text: str) -> dict:
    return ok({"ok": success, "reply": text})


@router.post("/api/im/command")
async def api_im_command(payload: dict) -> dict:
    """IM 群消息入口：规范化 → 角色解析 → 命令分发（help/pending/status/决策）。"""
    inbound = chatops.normalize_inbound(payload)
    if inbound is None:
        raise ApiError(400, "无法解析入站消息（需 sender + text；兼容通用/飞书/钉钉结构）")
    role = chatops.resolve_sender_role(inbound.sender, _sender_roles())
    command = chatops.parse_command(inbound.text)
    if command is None:
        return _reply(success=False, text=chatops.render_unknown(inbound.text))
    if not chatops.has_permission(role, command.role):
        log.warning(
            "[chatops] 权限拒绝：sender=%s role=%s verb=%s", inbound.sender, role, command.verb
        )
        return _reply(
            success=False,
            text=f"权限不足：{inbound.sender}（{role}）无权执行该命令（要求 {command.role}）",
        )
    if command.verb == "help":
        return _reply(success=True, text=chatops.render_help())
    if command.verb == "pending":
        items = await gw.list_flows(limit=_LIST_LIMIT)
        return _reply(success=True, text=chatops.render_pending(items))
    if command.verb == "status":
        if not command.arg:
            return _reply(success=False, text=f"用法：{chatops.usage('status')}")
        try:
            _, item = await _find_flow(command.arg)
        except ApiError as exc:
            return _reply(success=False, text=exc.message)
        return _reply(success=True, text=chatops.render_status(item))
    return await _handle_decision(inbound.sender, command)


async def _handle_decision(sender: str, command: chatops.Command) -> dict:
    """审批决策：kill switch → 解析流程 → 前置校验（同款 _write_guard）→ signal → 审计。"""
    if not command.arg:
        return _reply(success=False, text=f"用法：{chatops.usage(command.verb)}")
    try:
        state = kill_switch.get_state()
    except kill_switch.KillSwitchError as exc:
        raise ApiError(503, str(exc)) from exc
    if state.get("active"):
        raise ApiError(
            503,
            "kill switch 已激活，写操作被拒绝"
            f"（原因：{state.get('reason') or '未注明'}；操作者：{state.get('actor') or '未知'}）",
        )
    target = chatops.signal_target(command.verb)
    if target is None:  # 防御：分发保证仅决策命令到达此处
        return _reply(success=False, text=chatops.render_unknown(command.verb))
    gate, decision = target

    try:
        wf_id, _ = await _find_flow(command.arg)
    except ApiError as exc:
        return _reply(success=False, text=exc.message)

    extra = _first_approval_extra if gate == "approval" else _second_approval_extra
    try:
        await _write_guard(wf_id, {"WAIT_APPROVAL"}, extra)
    except ApiError as exc:
        log.info("[chatops] 拒绝：sender=%s verb=%s wf=%s：%s", sender, command.verb, wf_id, exc.message)
        return _reply(success=False, text=f"未执行：{exc.message}")

    try:
        if gate == "approval":
            await gw.signal_approval(wf_id, decision)
        else:
            await gw.signal_second_approval(wf_id, decision)
    except RPCError as exc:
        raise ApiError(502, f"Temporal 调用失败：{exc}") from exc
    gw.invalidate_cache()
    metrics.observe_gate_event(gate, decision)
    audit.write_audit(
        actor=f"im:{sender}",
        action=f"chatops:{'approval' if gate == 'approval' else 'second-approval'}:{decision}",
        wf_id=wf_id,
        params={"channel": "im", "sender": sender},
        result="signaled",
    )
    log.info("[chatops] %s → %s（wf=%s）", sender, command.verb, wf_id)
    return _reply(success=True, text=f"✓ {wf_id}：{chatops.DECISION_ACTIONS[command.verb]}（操作者 {sender}）")
