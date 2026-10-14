"""飞书长连接网关单测（2026-10-14）：转发载荷 / BFF 转发降级 / 回调处理 / CARD 帧补丁 / CLI。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

feishu_gateway.py 位于仓库根目录（非包），按文件路径加载（同 test_watchdog 手法）；
lark-oapi 仅在被测函数内延迟导入，本文件只在帧补丁用例中触碰它。
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401 - 先导入隔离钩子

_SPEC = importlib.util.spec_from_file_location(
    "feishu_gateway", Path(__file__).resolve().parent.parent / "feishu_gateway.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gateway = importlib.util.module_from_spec(_SPEC)
sys.modules["feishu_gateway"] = gateway
_SPEC.loader.exec_module(gateway)

CALLBACK = {
    "schema": "2.0",
    "header": {"event_type": "card.action.trigger", "token": "from-feishu"},
    "event": {
        "operator": {"open_id": "ou_ops"},
        "action": {
            "tag": "button",
            "value": {"wf_id": "aiops-fix-order-a-1", "gate": "approval", "decision": "approve"},
        },
    },
}


def _resp(body: bytes) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


class TestBuildForwardPayload(unittest.TestCase):
    def test_token_overwrites_header_and_keeps_other_fields(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_FEISHU_VERIFY_TOKEN": "shared-token"}):
            out = gateway.build_forward_payload(CALLBACK)
        self.assertEqual(out["header"]["token"], "shared-token")
        self.assertEqual(out["header"]["event_type"], "card.action.trigger")
        self.assertEqual(CALLBACK["header"]["token"], "from-feishu")  # 原载荷不被就地修改

    def test_missing_token_keeps_payload(self) -> None:
        out = gateway.build_forward_payload(CALLBACK)
        self.assertEqual(out["header"]["token"], "from-feishu")

    def test_missing_header_still_gets_token(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_FEISHU_VERIFY_TOKEN": "t"}):
            out = gateway.build_forward_payload({"schema": "2.0"})
        self.assertEqual(out["header"], {"token": "t"})


class TestForwardToBff(unittest.TestCase):
    def test_success_returns_bff_body(self) -> None:
        opener = mock.MagicMock()
        opener.open.return_value = _resp(b'{"toast": {"type": "success", "content": "ok"}}')
        with mock.patch.object(gateway, "_OPENER", opener):
            result = gateway._forward_to_bff({"schema": "2.0"})
        self.assertEqual(result["toast"]["content"], "ok")
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/im/card-callback"))

    def test_bff_url_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_BFF_HOST": "10.0.0.5", "AIOPS_BFF_PORT": "9999"}):
            self.assertEqual(gateway._bff_callback_url(), "http://10.0.0.5:9999/api/im/card-callback")

    def test_http_error_maps_to_toast_error(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "http://x", 401, "Unauthorized", {}, io.BytesIO(b'{"error": {"message": "token mismatch"}}')
        )
        with mock.patch.object(gateway, "_OPENER", opener):
            result = gateway._forward_to_bff({"schema": "2.0"})
        self.assertEqual(result["toast"]["type"], "error")
        self.assertIn("401", result["toast"]["content"])
        self.assertIn("token mismatch", result["toast"]["content"])

    def test_transport_error_degrades(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.URLError("refused")
        with mock.patch.object(gateway, "_OPENER", opener):
            result = gateway._forward_to_bff({"schema": "2.0"})
        self.assertEqual(result["toast"]["type"], "error")
        self.assertIn("网关无法连接回调服务", result["toast"]["content"])


class TestHandleCallbackPayload(unittest.TestCase):
    def test_forwards_payload_with_shared_token(self) -> None:
        seen: list[dict] = []

        def forward(payload: dict) -> dict:
            seen.append(payload)
            return {"toast": {"type": "success", "content": "已记录"}}

        with mock.patch.dict(os.environ, {"AIOPS_FEISHU_VERIFY_TOKEN": "shared-token"}):
            result = gateway.handle_callback_payload(CALLBACK, forward_fn=forward)
        self.assertEqual(result["toast"]["content"], "已记录")  # BFF 响应原样回写
        self.assertEqual(seen[0]["header"]["token"], "shared-token")
        self.assertEqual(seen[0]["event"]["operator"]["open_id"], "ou_ops")


class TestExtractErrorMessage(unittest.TestCase):
    def test_error_message_variants(self) -> None:
        self.assertEqual(gateway._extract_error_message('{"error": {"message": "m1"}}'), "m1")
        self.assertEqual(gateway._extract_error_message('{"message": "m2"}'), "m2")
        self.assertEqual(gateway._extract_error_message('{"detail": "m3"}'), "m3")

    def test_fallback_to_truncated_raw(self) -> None:
        self.assertEqual(gateway._extract_error_message("plain text"), "plain text")
        self.assertEqual(gateway._extract_error_message("x" * 500), "x" * 200)
        self.assertEqual(gateway._extract_error_message('{"code": 1}'), '{"code": 1}')


class TestTestCard(unittest.TestCase):
    def test_button_value_carries_callback_keys(self) -> None:
        action = gateway.TEST_CARD["elements"][-1]
        button = action["actions"][0]
        self.assertEqual(button["tag"], "button")
        self.assertEqual(set(button["value"]), {"wf_id", "gate", "decision"})


class TestCardFramePatch(unittest.TestCase):
    """lark-oapi <= 1.7.4 丢弃 CARD 帧（issue #126）：帧 type 头改写为 event 后交官方派发。"""

    def test_card_frame_rewritten_event_frame_passthrough(self) -> None:
        from lark_oapi.ws import client as ws_client_mod
        from lark_oapi.ws.const import HEADER_TYPE

        real = ws_client_mod.Client._handle_data_frame
        captured: list[list[tuple[str, str]]] = []

        async def fake_original(self, frame):  # type: ignore[no-untyped-def]
            captured.append([(h.key, h.value) for h in frame.headers])

        def restore() -> None:
            ws_client_mod.Client._handle_data_frame = real
            gateway._CARD_PATCH_INSTALLED = False
            # 上游导入时创建的全局 loop（client.py 模块级 get_event_loop）：关闭避免 ResourceWarning
            loop = getattr(ws_client_mod, "loop", None)
            if loop is not None and not loop.is_closed():
                loop.close()

        self.addCleanup(restore)
        gateway._CARD_PATCH_INSTALLED = False
        with mock.patch.object(ws_client_mod.Client, "_handle_data_frame", fake_original):
            gateway._install_card_frame_patch()
            gateway._install_card_frame_patch()  # 幂等：重复安装不叠加
            card_frame = SimpleNamespace(headers=[
                SimpleNamespace(key="message_id", value="m-1"),
                SimpleNamespace(key=HEADER_TYPE, value="card"),
            ])
            event_frame = SimpleNamespace(headers=[SimpleNamespace(key=HEADER_TYPE, value="event")])
            client = SimpleNamespace()
            asyncio.run(ws_client_mod.Client._handle_data_frame(client, card_frame))
            asyncio.run(ws_client_mod.Client._handle_data_frame(client, event_frame))
        self.assertEqual(
            captured,
            [
                [("message_id", "m-1"), (HEADER_TYPE, "event")],
                [(HEADER_TYPE, "event")],
            ],
        )


class TestCliFailClosed(unittest.TestCase):
    """缺应用凭证 = 拒绝启动（exit 2）；子命令退出码映射（全部离线 mock）。"""

    def setUp(self) -> None:
        patcher = mock.patch.object(gateway, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv: list[str]) -> int:
        with mock.patch("logging.basicConfig"), mock.patch("urllib.request.install_opener"):
            return gateway.main(argv)

    def test_start_requires_credentials(self) -> None:
        for argv in ([], ["--list-chats"], ["--send-test-card"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._main(argv), 2)

    def test_send_test_card_requires_receive_id(self) -> None:
        env = {"AIOPS_FEISHU_APP_ID": "cli_x", "AIOPS_FEISHU_APP_SECRET": "s"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self._main(["--send-test-card"]), 2)

    def test_send_test_card_success_and_failure(self) -> None:
        env = {
            "AIOPS_FEISHU_APP_ID": "cli_x",
            "AIOPS_FEISHU_APP_SECRET": "s",
            "AIOPS_FEISHU_RECEIVE_ID": "oc_group",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(
            gateway.feishu_app, "send_card", return_value="om_test"
        ) as send_card:
            self.assertEqual(gateway._cmd_send_test_card(), 0)
        self.assertEqual(send_card.call_args[0][0], gateway.TEST_CARD)
        self.assertEqual(send_card.call_args[0][1:], ("oc_group", "chat_id"))
        with mock.patch.dict(os.environ, env), mock.patch.object(
            gateway.feishu_app, "send_card",
            side_effect=gateway.feishu_app.FeishuAppError("code=230002"),
        ):
            self.assertEqual(gateway._cmd_send_test_card(), 1)

    def test_list_chats_exit_codes(self) -> None:
        with mock.patch.object(gateway.feishu_app, "list_chats", return_value=[]):
            self.assertEqual(gateway._cmd_list_chats(), 0)
        with mock.patch.object(
            gateway.feishu_app, "list_chats",
            side_effect=gateway.feishu_app.FeishuAppError("no scope"),
        ):
            self.assertEqual(gateway._cmd_list_chats(), 1)
