"""ChatOps 命令引擎单测（P2-3）：解析 / 入站规范化 / 渲染 / 权限模型（纯函数，零 IO）。"""

from __future__ import annotations

import unittest

from aiops_agent import chatops
from bff import auth as auth_store


class RoleOrderDriftTest(unittest.TestCase):
    """chatops.ROLE_ORDER 与控制台 auth.ROLE_ORDER 必须一致（防双轨漂移）。"""

    def test_role_order_matches_bff_auth(self):
        self.assertEqual(chatops.ROLE_ORDER, auth_store.ROLE_ORDER)


class ParseCommandTest(unittest.TestCase):
    def test_exact_verbs_and_aliases(self):
        cases = {
            "帮助": ("help", ""),
            "help": ("help", ""),
            "待办": ("pending", ""),
            "pending": ("pending", ""),
            "状态 abc": ("status", "abc"),
            "status abc": ("status", "abc"),
            "批准 abc": ("approve", "abc"),
            "同意 abc": ("approve", "abc"),
            "approve abc": ("approve", "abc"),
            "驳回 abc": ("reject", "abc"),
            "拒绝 abc": ("reject", "abc"),
            "reject abc": ("reject", "abc"),
            "二级批准 abc": ("second_approve", "abc"),
            "second-approve abc": ("second_approve", "abc"),
            "二级驳回 abc": ("second_reject", "abc"),
            "second-reject abc": ("second_reject", "abc"),
        }
        for text, (verb, arg) in cases.items():
            command = chatops.parse_command(text)
            self.assertIsNotNone(command, text)
            self.assertEqual((command.verb, command.arg), (verb, arg), text)

    def test_uppercase_alias_and_arg_case_preserved(self):
        command = chatops.parse_command("APPROVE AiOps-Fix-Order-A-1")
        self.assertEqual(command.verb, "approve")
        self.assertEqual(command.arg, "AiOps-Fix-Order-A-1")  # arg 保留原始大小写

    def test_no_space_prefix_match_for_arg_verbs(self):
        # 长别名优先：「二级批准xxx」不能被「批准」抢先命中
        command = chatops.parse_command("二级批准abc-123")
        self.assertEqual((command.verb, command.arg), ("second_approve", "abc-123"))
        command = chatops.parse_command("批准abc-123")
        self.assertEqual((command.verb, command.arg), ("approve", "abc-123"))

    def test_no_arg_verbs_do_not_prefix_match(self):
        # 无参命令不做前缀截取：「待办事项」是未知命令而非「待办」+ 垃圾参数
        self.assertIsNone(chatops.parse_command("待办事项"))
        self.assertIsNone(chatops.parse_command("帮助我"))

    def test_missing_arg_returns_empty(self):
        command = chatops.parse_command("批准")
        self.assertEqual((command.verb, command.arg), ("approve", ""))
        self.assertIn("批准", chatops.usage("approve"))

    def test_unknown_and_empty(self):
        self.assertIsNone(chatops.parse_command("部署 abc"))
        self.assertIsNone(chatops.parse_command(""))
        self.assertIsNone(chatops.parse_command("   "))
        self.assertIsNone(chatops.parse_command(None))

    def test_role_by_verb(self):
        self.assertEqual(chatops.parse_command("帮助").role, "viewer")
        self.assertEqual(chatops.parse_command("状态 abc").role, "viewer")
        self.assertEqual(chatops.parse_command("批准 abc").role, "operator")
        self.assertEqual(chatops.parse_command("二级驳回 abc").role, "operator")


class SignalTargetTest(unittest.TestCase):
    def test_decision_mapping(self):
        self.assertEqual(chatops.signal_target("approve"), ("approval", "approve"))
        self.assertEqual(chatops.signal_target("reject"), ("approval", "reject"))
        self.assertEqual(chatops.signal_target("second_approve"), ("second_approval", "approve"))
        self.assertEqual(chatops.signal_target("second_reject"), ("second_approval", "reject"))

    def test_non_decision_returns_none(self):
        self.assertIsNone(chatops.signal_target("help"))
        self.assertIsNone(chatops.signal_target("status"))


class NormalizeInboundTest(unittest.TestCase):
    def test_generic_payload(self):
        inbound = chatops.normalize_inbound({"sender": "alice", "text": " 待办 "})
        self.assertEqual((inbound.sender, inbound.text), ("alice", "待办"))

    def test_feishu_event(self):
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_abc", "user_id": "uid-1"}},
                "message": {"content": '{"text": "@_user_1 批准 aiops-fix"}'},
            },
        }
        inbound = chatops.normalize_inbound(payload)
        self.assertEqual((inbound.sender, inbound.text), ("ou_abc", "批准 aiops-fix"))

    def test_feishu_bad_content_json(self):
        payload = {"event": {"sender": {"sender_id": {"open_id": "ou_1"}}, "message": {"content": "{bad"}}}
        self.assertIsNone(chatops.normalize_inbound(payload))

    def test_dingtalk_callback(self):
        payload = {"senderStaffId": "u-1", "senderNick": "张三", "text": {"content": " 状态 abc"}}
        inbound = chatops.normalize_inbound(payload)
        self.assertEqual((inbound.sender, inbound.text), ("u-1", "状态 abc"))
        # senderStaffId 缺失回落 senderNick
        fallback = chatops.normalize_inbound({"senderNick": "张三", "text": {"content": "帮助"}})
        self.assertEqual(fallback.sender, "张三")

    def test_missing_or_blank_fields(self):
        self.assertIsNone(chatops.normalize_inbound({}))
        self.assertIsNone(chatops.normalize_inbound({"sender": "alice"}))
        self.assertIsNone(chatops.normalize_inbound({"sender": "", "text": "帮助"}))
        self.assertIsNone(chatops.normalize_inbound({"sender": "alice", "text": "   "}))
        self.assertIsNone(chatops.normalize_inbound(None))
        self.assertIsNone(chatops.normalize_inbound("not-a-dict"))

    def test_mention_only_text_is_ignored(self):
        # 只 @ 机器人没说话 → 无命令文本，视为无法识别
        self.assertIsNone(chatops.normalize_inbound({"sender": "alice", "text": "@_user_1"}))


class ResolveSenderRoleTest(unittest.TestCase):
    def test_mapped_roles(self):
        mapping = {"alice": "operator", "bob": {"role": "admin"}}
        self.assertEqual(chatops.resolve_sender_role("alice", mapping), "operator")
        self.assertEqual(chatops.resolve_sender_role("bob", mapping), "admin")

    def test_unmapped_defaults_viewer(self):
        self.assertEqual(chatops.resolve_sender_role("carol", {"alice": "operator"}), "viewer")
        self.assertEqual(chatops.resolve_sender_role("alice", None), "viewer")
        self.assertEqual(chatops.resolve_sender_role("alice", {}), "viewer")

    def test_invalid_role_falls_back_viewer(self):
        mapping = {"alice": "superuser", "bob": {"role": 123}, "carol": {"name": "x"}}
        self.assertEqual(chatops.resolve_sender_role("alice", mapping), "viewer")
        self.assertEqual(chatops.resolve_sender_role("bob", mapping), "viewer")
        self.assertEqual(chatops.resolve_sender_role("carol", mapping), "viewer")


class HasPermissionTest(unittest.TestCase):
    def test_matrix(self):
        self.assertTrue(chatops.has_permission("viewer", "viewer"))
        self.assertFalse(chatops.has_permission("viewer", "operator"))
        self.assertTrue(chatops.has_permission("operator", "viewer"))
        self.assertTrue(chatops.has_permission("operator", "operator"))
        self.assertFalse(chatops.has_permission("operator", "admin"))
        self.assertTrue(chatops.has_permission("admin", "operator"))
        # 未知角色不满足任何要求
        self.assertFalse(chatops.has_permission("guest", "viewer"))
        self.assertFalse(chatops.has_permission("", "viewer"))


class RenderTest(unittest.TestCase):
    def test_help_lists_all_verbs(self):
        help_text = chatops.render_help()
        for token in ("待办", "状态", "批准", "驳回", "二级批准", "二级驳回", "帮助"):
            self.assertIn(token, help_text)

    def test_unknown_truncates_echo(self):
        text = chatops.render_unknown("x" * 100)
        self.assertIn("x" * 40, text)
        self.assertNotIn("x" * 41, text)
        self.assertIn("帮助", text)

    def test_pending_filters_and_notes(self):
        items = [
            {
                "wf_id": "wf-a",
                "exec_status": "RUNNING",
                "stage": "WAIT_APPROVAL",
                "alert": {"service": "order"},
                "patch_id": "patch-1",
                "needs_second": True,
                "approval": "approve",
                "second_approval": None,
            },
            {  # 已结束 → 不列入
                "wf_id": "wf-done",
                "exec_status": "COMPLETED",
                "stage": "DONE",
            },
            {  # 非待审批阶段 → 不列入
                "wf_id": "wf-testing",
                "exec_status": "RUNNING",
                "stage": "TESTING",
            },
            {
                "wf_id": "wf-b",
                "exec_status": "RUNNING",
                "stage": "WAIT_APPROVAL",
                "alert": {"service": "coupon"},
                "patch_id": None,
                "needs_second": True,
                "approval": None,
                "second_approval": None,
            },
        ]
        text = chatops.render_pending(items)
        self.assertIn("待审批流程（2 个）", text)
        self.assertIn("wf-a", text)
        self.assertIn("wf-b", text)
        self.assertNotIn("wf-done", text)
        self.assertNotIn("wf-testing", text)
        self.assertIn("一级已批准，待二级审批", text)
        self.assertIn("受保护目录，需二级审批", text)
        self.assertIn("补丁生成中", text)  # patch_id 缺失占位

    def test_pending_empty(self):
        self.assertEqual(chatops.render_pending([]), "当前没有待审批流程。")

    def test_status_wait_approval(self):
        item = {
            "wf_id": "wf-1",
            "exec_status": "RUNNING",
            "stage": "WAIT_APPROVAL",
            "alert": {"service": "order", "alert_id": "a-1"},
            "root_cause": {"summary": "下游超时", "confidence": 0.87},
            "patch_id": "patch-1",
            "needs_second": True,
            "approval": None,
            "second_approval": None,
            "deadline": {"kind": "approval", "at": "2026-10-12T10:10:00+00:00"},
            "start_time": "2026-10-12T10:00:00+00:00",
        }
        text = chatops.render_status(item)
        self.assertIn("wf-1", text)
        self.assertIn("运行中", text)
        self.assertIn("等待人工审批", text)
        self.assertIn("order", text)
        self.assertIn("下游超时", text)
        self.assertIn("0.87", text)
        self.assertIn("补丁：patch-1", text)
        self.assertIn("一级审批：待处理", text)
        self.assertIn("二级审批：待处理", text)
        self.assertIn("审批截止", text)

    def test_status_terminal_minimal(self):
        text = chatops.render_status({"wf_id": "wf-2", "exec_status": "COMPLETED", "stage": "DONE"})
        self.assertIn("已完成", text)
        self.assertIn("已完成（已发布）", text)
        self.assertNotIn("一级审批", text)  # 非待审批期不渲染审批行

    def test_status_unknown_stage_falls_back(self):
        text = chatops.render_status({"wf_id": "wf-3", "exec_status": "RUNNING", "stage": "FUTURE"})
        self.assertIn("FUTURE", text)  # 未知阶段回落原文，不臆造


if __name__ == "__main__":
    unittest.main()
