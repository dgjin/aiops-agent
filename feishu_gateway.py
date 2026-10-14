"""飞书长连接网关（卡片回传交互接收，2026-10-14）。

链路：飞书开放平台（应用「事件与回调」订阅方式＝「使用长连接接收回调」）→
本进程经 WSS 收到 card.action.trigger 事件 → 注入 Verification Token 后转发
BFF ``POST /api/im/card-callback``（复用既有验签 / 解析 / 角色判定 / 写守卫 /
审批信号 / 审计全链）→ 将 BFF 响应体（toast + 处置结果卡）回写长连接。

为什么需要独立进程：
- 卡片回传交互只能由「应用」承载——自定义群机器人 webhook 发送的卡片按官方
  限制仅支持跳转 URL、不支持回传（本机原 webhook 卡片按钮点击无反应即此因）；
- 订阅方式二选一中本机无公网地址，只能选「长连接」：应用侧主动出站 WSS，
  无需内网穿透/公网域名/证书，且通道内建加密与鉴权；
- 长连接为常驻 WSS 客户端，与 BFF 的 HTTP 生命周期解耦，由 watchdog 托管
  （配置齐全才注册组件，见 scripts/aiops-watchdog.py）。

上游缺陷补丁（重要）：lark-oapi ≤ 1.7.4 的长连接客户端在 _handle_data_frame
中直接丢弃 MessageType.CARD 帧（larksuite/oapi-sdk-python issue #126）——卡片
处理器永不触发、无响应回写，飞书客户端点击后报错 200340。本模块安装帧类型
改写补丁：把 CARD 帧的 type 头改写为 event 后交官方派发（路由依据 payload 内
的 event_type="card.action.trigger"，响应回写逻辑完全复用）。上游修复后
_install_card_frame_patch 可整体移除。

运行（自动加载工程根目录 .env）：
    .venv/bin/python feishu_gateway.py                     # 启动长连接（常驻，watchdog 托管）
    .venv/bin/python feishu_gateway.py --list-chats        # 列出机器人所在群（取群 chat_id）
    .venv/bin/python feishu_gateway.py --send-test-card    # 发送链路验证卡（验证端到端回传）

环境变量：
    AIOPS_FEISHU_APP_ID / AIOPS_FEISHU_APP_SECRET  应用凭证（必填；未配置拒绝启动）
    AIOPS_FEISHU_VERIFY_TOKEN                      Verification Token（转发 BFF 时注入
                                                   header.token，须与 BFF 同值；未配置
                                                   ＝BFF 侧通道关闭 503）
    AIOPS_BFF_HOST / AIOPS_BFF_PORT                BFF 地址（默认 127.0.0.1:8600）
    AIOPS_FEISHU_RECEIVE_ID / AIOPS_FEISHU_RECEIVE_ID_TYPE  验证卡接收方（默认 chat_id）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

from aiops_agent import config as _config  # noqa: F401 - 触发工程根 .env 加载（与其他入口一致）
from aiops_agent import feishu_app, notify

log = logging.getLogger("feishu-gateway")

_CALLBACK_PATH = "/api/im/card-callback"
# 飞书要求 3 秒内响应：转发 BFF 的超时留出回写余量。
_FORWARD_TIMEOUT = 2.5
# 无代理 opener：同 watchdog / mcp_server 的 ProxyHandler({}) 惯例。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_CARD_PATCH_INSTALLED = False


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _bff_callback_url() -> str:
    host = _env("AIOPS_BFF_HOST") or "127.0.0.1"
    port = _env("AIOPS_BFF_PORT") or "8600"
    return f"http://{host}:{port}{_CALLBACK_PATH}"


def build_forward_payload(payload: dict) -> dict:
    """转发载荷构造：以本进程 AIOPS_FEISHU_VERIFY_TOKEN 覆写 header.token。

    长连接通道已由 App Secret 在建立连接时完成鉴权、推送为明文（官方文档），
    载荷本身无需再验签；此处注入 Token 只为满足 BFF 网关的凭证校验接口
    （两端同值即可，未配置时保持原样、由 BFF fail-closed 拒绝）。
    """
    token = _env("AIOPS_FEISHU_VERIFY_TOKEN")
    result = dict(payload)
    if token:
        header = result.get("header")
        header = dict(header) if isinstance(header, dict) else {}
        header["token"] = token
        result["header"] = header
    return result


def _extract_error_message(raw: str) -> str:
    """BFF 错误体 → 人类可读信息（兼容 error.message / message / detail / 原文截断）。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        for key in ("message", "detail"):
            if isinstance(data.get(key), str):
                return data[key]
    return raw[:200]


def _forward_to_bff(payload: dict) -> dict:
    """转发 BFF 回调网关并返回响应体；任何失败转 toast.error 响应（不抛错）。"""
    request = urllib.request.Request(
        _bff_callback_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with _OPENER.open(request, timeout=_FORWARD_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响主判定
            raw = ""
        detail = _extract_error_message(raw) if raw else ""
        text = f"回调网关拒绝（HTTP {exc.code}）"
        return {"toast": {"type": "error", "content": f"{text}：{detail}" if detail else text}}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"toast": {"type": "error", "content": f"网关无法连接回调服务：{exc}"}}


def handle_callback_payload(
    payload: dict, forward_fn: Callable[[dict], dict] | None = None
) -> dict:
    """卡片回调主处理：日志留痕（操作者 open_id）→ 注入 Token → 转发 BFF。

    forward_fn 仅测试注入用；返回值为回写长连接的响应体（toast / card）。
    """
    operator = notify.extract_callback_operator(payload) or "-"
    parsed = notify.parse_callback(payload)
    gate, decision = (parsed[1], parsed[2]) if parsed else ("-", "-")
    log.info(
        "[feishu-gateway] 收到卡片回调：operator=%s gate=%s decision=%s → 转发 BFF",
        operator, gate, decision,
    )
    return (forward_fn or _forward_to_bff)(build_forward_payload(payload))


def _install_card_frame_patch() -> None:
    """CARD 帧类型改写补丁（见模块 docstring；上游修复后可移除）。"""
    global _CARD_PATCH_INSTALLED
    if _CARD_PATCH_INSTALLED:
        return
    import lark_oapi.ws.client as ws_client_mod  # noqa: PLC0415 - 延迟导入（依赖 lark-oapi）
    from lark_oapi.ws.const import HEADER_TYPE  # noqa: PLC0415

    original = ws_client_mod.Client._handle_data_frame

    async def _handle_data_frame_with_card(self, frame):  # type: ignore[no-untyped-def]
        try:
            for header in frame.headers:
                if header.key == HEADER_TYPE and header.value == "card":
                    header.value = "event"
                    break
        except Exception:  # noqa: BLE001 - 帧结构异常时不改写，交官方逻辑兜底
            pass
        return await original(self, frame)

    ws_client_mod.Client._handle_data_frame = _handle_data_frame_with_card
    _CARD_PATCH_INSTALLED = True
    log.info("[feishu-gateway] 已安装 CARD 帧改写补丁（lark-oapi issue #126）")


def _build_dispatcher():  # type: ignore[no-untyped-def]
    """EventDispatcherHandler：注册 card.action.trigger 处理器。"""
    import lark_oapi as lark  # noqa: PLC0415 - 延迟导入（依赖 lark-oapi）
    from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: PLC0415
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    def do_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            payload = json.loads(lark.JSON.marshal(data))
            response = handle_callback_payload(payload)
        except Exception as exc:  # noqa: BLE001 - 3 秒内必须响应：兜底转 toast.error
            log.exception("[feishu-gateway] 处理卡片回调异常：%s", exc)
            response = {"toast": {"type": "error", "content": "网关内部错误，请稍后在控制台处置"}}
        return P2CardActionTriggerResponse(response)

    return (
        lark.EventDispatcherHandler.builder("", "")  # 长连接模式两参数必须为空串（官方文档）
        .register_p2_card_action_trigger(do_card_action_trigger)
        .build()
    )


TEST_CARD = {
    "header": {"title": {"tag": "plain_text", "content": "AIOps 飞书链路验证卡"}, "template": "grey"},
    "elements": [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "点击「验证回调」按钮检查卡片交互链路。出现 toast 提示即验证成功"
                    "（「未找到流程」「权限不足」均代表回调已到达并由网关处理）。"
                ),
            },
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "验证回调"},
                    "type": "primary",
                    "value": {"wf_id": "link-check", "gate": "approval", "decision": "approve"},
                },
            ],
        },
    ],
}


def _cmd_list_chats() -> int:
    try:
        chats = feishu_app.list_chats()
    except feishu_app.FeishuAppError as exc:
        log.error("取群列表失败：%s", exc)
        log.error("若提示权限不足：请在飞书开放平台为应用添加「获取群组信息」权限（im:chat:readonly）并重新发布版本")
        return 1
    if not chats:
        log.info("机器人暂未加入任何群：请先把应用机器人拉进目标群后重试")
        return 0
    for item in chats:
        log.info("chat_id=%s  name=%s", item.get("chat_id"), item.get("name"))
    log.info("将目标群 chat_id 填入 .env 的 AIOPS_FEISHU_RECEIVE_ID 后重启组件生效")
    return 0


def _cmd_send_test_card() -> int:
    receive_id = _env("AIOPS_FEISHU_RECEIVE_ID")
    if not receive_id:
        log.error("未配置 AIOPS_FEISHU_RECEIVE_ID：先执行 --list-chats 获取群 chat_id 并填入 .env")
        return 2
    receive_id_type = _env("AIOPS_FEISHU_RECEIVE_ID_TYPE") or "chat_id"
    try:
        message_id = feishu_app.send_card(TEST_CARD, receive_id, receive_id_type)
    except feishu_app.FeishuAppError as exc:
        log.error("发送验证卡失败：%s", exc)
        return 1
    log.info("验证卡已发送：message_id=%s receive_id=%s（%s）", message_id, receive_id, receive_id_type)
    log.info("请在飞书群内点击「验证回调」：本进程日志出现「收到卡片回调 operator=ou_...」即链路已通；")
    log.info("把该 operator（open_id）按角色填入 .env 的 AIOPS_IM_USERS 后 ./start.sh restart 即可用于真实审批。")
    return 0


def main(argv: list[str] | None = None) -> int:
    urllib.request.install_opener(  # 无代理 opener（同 watchdog/mcp_server 惯例）
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="飞书长连接网关（卡片回传交互接收）")
    parser.add_argument("--list-chats", action="store_true", help="列出机器人所在群（取 chat_id）")
    parser.add_argument("--send-test-card", action="store_true", help="发送链路验证卡到配置的接收方")
    args = parser.parse_args(argv)

    app_id = _env("AIOPS_FEISHU_APP_ID")
    app_secret = _env("AIOPS_FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        log.error("未配置 AIOPS_FEISHU_APP_ID / AIOPS_FEISHU_APP_SECRET，网关拒绝启动")
        return 2

    if args.list_chats:
        return _cmd_list_chats()
    if args.send_test_card:
        return _cmd_send_test_card()

    import lark_oapi as lark  # noqa: PLC0415 - 延迟导入（依赖 lark-oapi）

    _install_card_frame_patch()
    log.info(
        "[feishu-gateway] 长连接启动：app_id=%s... token=%s bff=%s",
        app_id[:8], "已配置" if _env("AIOPS_FEISHU_VERIFY_TOKEN") else "未配置（BFF 将 503 拒绝）",
        _bff_callback_url(),
    )
    client = lark.ws.Client(
        app_id, app_secret, event_handler=_build_dispatcher(), log_level=lark.LogLevel.INFO
    )
    client.start()  # 阻塞 + 断线自动重连（SDK 内置）
    return 0


if __name__ == "__main__":
    sys.exit(main())
