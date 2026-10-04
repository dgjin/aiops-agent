"""通知层单测（WP8）：卡片渲染 / 真实投递与降级 / 加签 / 回调解析 / 活动集成。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import activities, notify
from aiops_agent.models import Alert, Patch, RootCause, TestReport

ALERT = Alert(alert_id="nt-1", service="order", description="通知单测")
ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["order_service.py"],
    confidence=0.92,
    summary="coupon 空值未防护。",
)
PATCH = Patch(
    patch_id="p-nt-1-r1",
    alert_id="nt-1",
    files=["order_service.py"],
    diff=(
        "--- a/order_service.py\n"
        "+++ b/order_service.py\n"
        "@@ -34 +34 @@\n"
        "-    discount = coupon[\"discount\"]\n"
        "+    discount = coupon[\"discount\"] if coupon else 0\n"
    ),
    description="演示补丁",
    risk="低：仅新增防御分支，不改动既有逻辑。",
    model_version="aiops-fix-demo-v0.1",
    confidence=0.92,
)
REPORT = TestReport(
    patch_id=PATCH.patch_id,
    passed=True,
    unit_tests="6/6 passed",
    regression_tests="通过",
    sast="无高危模式",
)


def _card_text(card: dict) -> str:
    """把卡片 payload 序列化用于全文断言（diff / 文案查找）。"""
    return json.dumps(card, ensure_ascii=False)


class TestApprovalCardFeishu(unittest.TestCase):
    def test_structure_and_buttons(self) -> None:
        card = notify.render_approval_card(ALERT, ROOT_CAUSE, PATCH, REPORT, False)
        self.assertEqual(card["msg_type"], "interactive")
        self.assertIn("nt-1", card["card"]["header"]["title"]["content"])
        action = card["card"]["elements"][-1]
        self.assertEqual(action["tag"], "action")
        approve, reject = action["actions"]
        self.assertEqual(
            approve["value"], {"wf_id": "aiops-fix-order-nt-1", "gate": "approval", "decision": "approve"}
        )
        self.assertEqual(reject["value"]["decision"], "reject")

    def test_full_diff_and_test_report_embedded(self) -> None:
        card = notify.render_approval_card(ALERT, ROOT_CAUSE, PATCH, REPORT, False)
        text = _card_text(card)
        self.assertIn("if coupon else 0", text)  # 完整 diff 嵌入
        self.assertIn("单测 6/6 passed", text)
        self.assertIn("回滚预案", text)

    def test_needs_second_hint(self) -> None:
        card = notify.render_approval_card(ALERT, ROOT_CAUSE, PATCH, REPORT, True)
        self.assertIn("需二级审批", _card_text(card))


class TestApprovalCardDingTalk(unittest.TestCase):
    def test_structure_and_callback_url(self) -> None:
        card = notify.render_approval_card(
            ALERT, ROOT_CAUSE, PATCH, REPORT, False,
            provider="dingtalk", callback_base="http://127.0.0.1:8000/aiops/callback",
        )
        self.assertEqual(card["msgtype"], "actionCard")
        btns = card["actionCard"]["btns"]
        self.assertEqual(len(btns), 2)
        url = btns[0]["actionURL"]
        self.assertIn("wf_id=aiops-fix-order-nt-1", url)
        self.assertIn("decision=approve", url)
        self.assertIn("if coupon else 0", card["actionCard"]["text"])  # diff 嵌入


class TestBroadcastAndEscalation(unittest.TestCase):
    def test_broadcast_contains_version_and_message(self) -> None:
        for provider in ("feishu", "dingtalk"):
            card = notify.render_broadcast_card(ALERT, PATCH, "v1.0.242", provider=provider)
            text = _card_text(card)
            self.assertIn("v1.0.242", text)
            self.assertIn(notify.BROADCAST_MESSAGE, text)

    def test_escalation_contains_reason(self) -> None:
        for provider in ("feishu", "dingtalk"):
            card = notify.render_escalation_card(ALERT, "金丝雀劣化自动回滚", provider=provider)
            self.assertIn("金丝雀劣化自动回滚", _card_text(card))


class TestSenderRecorded(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp8-notify-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recorded_mode_writes_trace(self) -> None:
        card = notify.render_broadcast_card(ALERT, PATCH, "v1.0.242")
        sender = notify.NotificationSender(webhook="")
        with mock.patch.object(notify, "NOTIFY_DIR", self.tmp / "notify"):
            result = sender.send(card, msg_id="broadcast-p-nt-1-r1")
        self.assertEqual(result["mode"], "recorded")
        record = Path(result["path"])
        self.assertTrue(record.is_file())
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), card)


class TestSenderLive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp8-notify-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resp(self) -> mock.MagicMock:
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        return resp

    def test_feishu_live_post_and_signature(self) -> None:
        card = notify.render_approval_card(ALERT, ROOT_CAUSE, PATCH, REPORT, False)
        sender = notify.NotificationSender(
            provider="feishu", webhook="http://feishu.example/hook", secret="s3cret"
        )
        with mock.patch.object(notify, "NOTIFY_DIR", self.tmp / "notify"), mock.patch(
            "aiops_agent.notify.urllib.request.urlopen", return_value=self._resp()
        ) as urlopen:
            result = sender.send(card, msg_id="approval-p-nt-1-r1")
        self.assertEqual(result["mode"], "live")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://feishu.example/hook")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["msg_type"], "interactive")
        self.assertEqual(body["sign"], notify._feishu_sign(int(body["timestamp"]), "s3cret"))

    def test_dingtalk_live_signature_in_url(self) -> None:
        card = notify.render_approval_card(ALERT, ROOT_CAUSE, PATCH, REPORT, False, provider="dingtalk")
        sender = notify.NotificationSender(
            provider="dingtalk", webhook="http://dingtalk.example/hook", secret="s3cret"
        )
        with mock.patch.object(notify, "NOTIFY_DIR", self.tmp / "notify"), mock.patch(
            "aiops_agent.notify.urllib.request.urlopen", return_value=self._resp()
        ) as urlopen:
            result = sender.send(card, msg_id="approval-p-nt-1-r1")
        self.assertEqual(result["mode"], "live")
        url = urlopen.call_args[0][0].full_url
        self.assertTrue(url.startswith("http://dingtalk.example/hook?"))
        self.assertIn("timestamp=", url)
        self.assertIn("sign=", url)

    def test_delivery_failure_degrades_without_raising(self) -> None:
        card = notify.render_broadcast_card(ALERT, PATCH, "v1.0.242")
        sender = notify.NotificationSender(webhook="http://unreachable.example/hook")
        with mock.patch.object(notify, "NOTIFY_DIR", self.tmp / "notify"), mock.patch(
            "aiops_agent.notify.urllib.request.urlopen", side_effect=OSError("conn refused")
        ):
            result = sender.send(card, msg_id="broadcast-p-nt-1-r1")
        self.assertEqual(result["mode"], "failed")
        self.assertIn("conn refused", result["error"])
        self.assertTrue(Path(result["path"]).is_file())  # 降级仍留痕


class TestParseCallback(unittest.TestCase):
    def test_feishu_action_value(self) -> None:
        payload = {"action": {"value": {"wf_id": "aiops-fix-order-a-1", "gate": "approval", "decision": "approve"}}}
        self.assertEqual(notify.parse_callback(payload), ("aiops-fix-order-a-1", "approval", "approve"))

    def test_flat_value_dict(self) -> None:
        payload = {"wf_id": "aiops-fix-order-a-1", "gate": "second_approval", "decision": "reject"}
        self.assertEqual(
            notify.parse_callback(payload), ("aiops-fix-order-a-1", "second_approval", "reject")
        )

    def test_dingtalk_action_url(self) -> None:
        url = "http://127.0.0.1:8000/aiops/callback?wf_id=aiops-fix-order-a-1&gate=approval&decision=approve"
        self.assertEqual(notify.parse_callback(url), ("aiops-fix-order-a-1", "approval", "approve"))

    def test_invalid_payloads_rejected(self) -> None:
        self.assertIsNone(notify.parse_callback({}))
        self.assertIsNone(
            notify.parse_callback({"action": {"value": {"wf_id": "x", "gate": "approval", "decision": "bogus"}}})
        )
        self.assertIsNone(
            notify.parse_callback({"action": {"value": {"gate": "approval", "decision": "approve"}}})
        )
        self.assertIsNone(
            notify.parse_callback({"action": {"value": {"wf_id": "x", "gate": "nope", "decision": "approve"}}})
        )


class TestActivityIntegration(unittest.TestCase):
    """活动层集成：直接调用活动函数（refer: test_code_rag_fix 先例），验证留痕与返回值契约。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp8-act-"))
        self.patcher = mock.patch.object(notify, "NOTIFY_DIR", self.tmp / "notify")
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notify_approvers_recorded_and_payload(self) -> None:
        result = asyncio.run(
            activities.notify_approvers(ALERT, ROOT_CAUSE, PATCH, REPORT, False)
        )
        self.assertEqual(result["card_id"], "card-nt-1")
        self.assertEqual(result["delivery"], "recorded")
        self.assertFalse(result["needs_second_approval"])
        record = self.tmp / "notify" / "approval-p-nt-1-r1.json"
        self.assertTrue(record.is_file())

    def test_notify_users_version_contract(self) -> None:
        result = asyncio.run(activities.notify_users(ALERT, PATCH))
        self.assertRegex(result["version"], r"^v1\.0\.\d{3}$")
        self.assertEqual(result["delivery"], "recorded")
        self.assertEqual(result["message"], notify.BROADCAST_MESSAGE)

    def test_escalate_to_human_traces(self) -> None:
        self.assertIsNone(
            asyncio.run(activities.escalate_to_human(ALERT, "测试始终失败，重试耗尽"))
        )
        record = self.tmp / "notify" / "escalation-nt-1.json"
        self.assertTrue(record.is_file())
        self.assertIn("重试耗尽", record.read_text(encoding="utf-8"))
