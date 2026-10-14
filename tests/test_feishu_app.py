"""飞书应用机器人 API 客户端单测（2026-10-14）：token 缓存 / 发卡载荷 / 群列表 / 错误映射。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

全部走 mock opener，零外网（tests 隔离钩子禁止真实连接）。
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import tests  # noqa: F401 - 先导入隔离钩子

from aiops_agent import feishu_app

ENV = {"AIOPS_FEISHU_APP_ID": "cli_test", "AIOPS_FEISHU_APP_SECRET": "sec_test"}
TOKEN_OK = b'{"code": 0, "tenant_access_token": "tok", "expire": 7200}'


def _resp(body: bytes) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


class FeishuAppTestCase(unittest.TestCase):
    """公共装置：token 缓存清零 + 全 mock 的无代理 opener。"""

    def setUp(self) -> None:
        feishu_app._token_cache.update({"app_id": "", "value": "", "expire_at": 0.0})
        self.addCleanup(feishu_app._token_cache.update, {"app_id": "", "value": "", "expire_at": 0.0})
        self.opener = mock.MagicMock()
        patcher = mock.patch.object(feishu_app, "_OPENER", self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestToken(FeishuAppTestCase):
    def test_fetch_then_cache_hit(self) -> None:
        self.opener.open.return_value = _resp(TOKEN_OK)
        with mock.patch.dict(os.environ, ENV):
            self.assertEqual(feishu_app.fetch_tenant_access_token(), "tok")
            self.assertEqual(feishu_app.fetch_tenant_access_token(), "tok")
        self.assertEqual(self.opener.open.call_count, 1)  # 第二次命中进程内缓存
        request = self.opener.open.call_args[0][0]
        self.assertEqual(
            request.full_url,
            feishu_app.FEISHU_BASE + "/open-apis/auth/v3/tenant_access_token/internal",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")),
                         {"app_id": "cli_test", "app_secret": "sec_test"})

    def test_missing_credentials_raise(self) -> None:
        with self.assertRaises(feishu_app.FeishuAppError):
            feishu_app.fetch_tenant_access_token()
        self.opener.open.assert_not_called()

    def test_business_error_raises(self) -> None:
        self.opener.open.return_value = _resp(b'{"code": 10003, "msg": "invalid secret"}')
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError) as ctx:
            feishu_app.fetch_tenant_access_token()
        self.assertIn("10003", str(ctx.exception))

    def test_http_error_maps_to_domain_error(self) -> None:
        self.opener.open.side_effect = urllib.error.HTTPError(
            "http://x", 401, "Unauthorized", {}, io.BytesIO(b'{"msg": "bad app"}')
        )
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError) as ctx:
            feishu_app.fetch_tenant_access_token()
        self.assertIn("401", str(ctx.exception))

    def test_non_json_answer_raises(self) -> None:
        self.opener.open.return_value = _resp(b"<html>gateway</html>")
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError) as ctx:
            feishu_app.fetch_tenant_access_token()
        self.assertIn("非 JSON", str(ctx.exception))


class TestSendCard(FeishuAppTestCase):
    CARD = {"header": {"title": {"tag": "plain_text", "content": "t"}}, "elements": []}

    def _token_then(self, body: bytes) -> None:
        self.opener.open.side_effect = [_resp(TOKEN_OK), _resp(body)]

    def test_payload_authorization_and_message_id(self) -> None:
        self._token_then(b'{"code": 0, "data": {"message_id": "om_1"}}')
        with mock.patch.dict(os.environ, ENV):
            message_id = feishu_app.send_card(self.CARD, "oc_group", "chat_id")
        self.assertEqual(message_id, "om_1")
        request = self.opener.open.call_args_list[1][0][0]
        self.assertTrue(request.full_url.startswith(feishu_app.FEISHU_BASE + "/open-apis/im/v1/messages?"))
        self.assertIn("receive_id_type=chat_id", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["receive_id"], "oc_group")
        self.assertEqual(body["msg_type"], "interactive")
        self.assertEqual(json.loads(body["content"]), self.CARD)  # 卡片体 JSON 序列化进 content

    def test_default_receive_id_type_is_chat_id(self) -> None:
        self._token_then(b'{"code": 0, "data": {"message_id": "om_2"}}')
        with mock.patch.dict(os.environ, ENV):
            feishu_app.send_card(self.CARD, "oc_group")
        request = self.opener.open.call_args_list[1][0][0]
        self.assertIn("receive_id_type=chat_id", request.full_url)

    def test_empty_receive_id_refused_before_network(self) -> None:
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError):
            feishu_app.send_card(self.CARD, "   ")
        self.opener.open.assert_not_called()

    def test_business_error_raises(self) -> None:
        self._token_then(b'{"code": 230002, "msg": "bot is not in chat"}')
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError) as ctx:
            feishu_app.send_card(self.CARD, "oc_group")
        self.assertIn("230002", str(ctx.exception))


class TestListChats(FeishuAppTestCase):
    def test_returns_items_and_query(self) -> None:
        self.opener.open.side_effect = [
            _resp(TOKEN_OK),
            _resp(b'{"code": 0, "data": {"items": [{"chat_id": "oc_1", "name": "group"}]}}'),
        ]
        with mock.patch.dict(os.environ, ENV):
            chats = feishu_app.list_chats()
        self.assertEqual(chats, [{"chat_id": "oc_1", "name": "group"}])
        request = self.opener.open.call_args_list[1][0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("page_size=50", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    def test_missing_items_yields_empty_list(self) -> None:
        self.opener.open.side_effect = [_resp(TOKEN_OK), _resp(b'{"code": 0, "data": {}}')]
        with mock.patch.dict(os.environ, ENV):
            self.assertEqual(feishu_app.list_chats(), [])

    def test_permission_error_raises(self) -> None:
        self.opener.open.side_effect = [_resp(TOKEN_OK), _resp(b'{"code": 99991672, "msg": "no scope"}')]
        with mock.patch.dict(os.environ, ENV), self.assertRaises(feishu_app.FeishuAppError):
            feishu_app.list_chats()
