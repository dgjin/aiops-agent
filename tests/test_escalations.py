"""转人工处置闭环单测（bff/escalations，P1-3）：状态机 / 幂等登记 / 存储双模 / SLA 巡检。

覆盖三个层面：
- 纯函数：升级原因翻译（gate_events → 中文）、记录构造、assign/close/retry 状态迁移；
- 存储（demo JSON / production SQLite 注入）：幂等登记、closed 不重开、统计、热生效；
- SLA：到期判定（open/assigned 且超时且未再升级）、sweep_due 每单只升级一次。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiops_agent import db as db_layer
from bff import escalations as esc


def _flow(
    wf_id: str = "aiops-fix-order-a1",
    stage: str = "ESCALATED",
    gate_events: list[str] | None = None,
    service: str = "order",
) -> dict:
    return {
        "wf_id": wf_id,
        "stage": stage,
        "gate_events": gate_events
        if gate_events is not None
        else ["gate1:blocked:confidence-low"],
        "alert": {
            "alert_id": "a1",
            "service": service,
            "severity": "critical",
            "description": "订单错误率激增",
        },
    }


class PureFunctionTest(unittest.TestCase):
    """纯函数：升级原因翻译 / 记录构造 / 状态迁移。"""

    def test_auto_reason_gate1_blocked(self) -> None:
        reason = esc.auto_reason_from_events(["gate1:blocked:confidence=0.42<0.6"])
        self.assertIn("闸门1", reason)

    def test_auto_reason_takes_last_meaningful_event(self) -> None:
        reason = esc.auto_reason_from_events(["canary:degraded:5xx", "gate2:timeout:900s"])
        self.assertIn("审批超时", reason)

    def test_auto_reason_fallback(self) -> None:
        self.assertIn("转人工", esc.auto_reason_from_events([]))
        self.assertIn("转人工", esc.auto_reason_from_events(None))

    def test_auto_reason_gate2b_tests_canary(self) -> None:
        self.assertIn("二级审批", esc.auto_reason_from_events(["gate2b:rejected"]))
        self.assertIn("回炉", esc.auto_reason_from_events(["tests:failed:3"]))
        self.assertIn("回滚", esc.auto_reason_from_events(["canary:degraded:1"]))

    def test_new_entry_shape(self) -> None:
        entry = esc.new_entry(_flow())
        self.assertEqual(entry["id"], "esc-aiops-fix-order-a1")
        self.assertEqual(entry["status"], "open")
        self.assertEqual(entry["service"], "order")
        self.assertEqual(entry["alert_id"], "a1")
        self.assertEqual(entry["history"][0]["action"], "open")
        self.assertFalse(entry["re_escalated"])
        self.assertIsNone(entry["closed_at"])

    def test_assign_rejects_blank_and_closed(self) -> None:
        entry = esc.new_entry(_flow())
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_assign(entry, "   ", "alice")
        esc.apply_close(entry, "alice", "手动处置")
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_assign(entry, "bob", "alice")

    def test_assign_marks_assigned_and_supports_reassign(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_assign(entry, "alice", "bob")
        self.assertEqual(entry["status"], "assigned")
        self.assertEqual(entry["assignee"], "alice")
        esc.apply_assign(entry, "carol", "bob")
        self.assertIn("改派", entry["history"][-1]["detail"])
        self.assertEqual(entry["assignee"], "carol")

    def test_close_records_note_and_rejects_repeat(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_close(entry, "alice", "已回滚发布")
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["note"], "已回滚发布")
        self.assertIsNotNone(entry["closed_at"])
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_close(entry, "alice", "")

    def test_retried_closes_with_new_wf_id(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        self.assertEqual(entry["status"], "closed")
        self.assertIn("retry1", entry["note"])
        self.assertEqual(entry["history"][-1]["action"], "retry")

    def test_re_escalate_marks_once(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_re_escalate(entry, "system")
        self.assertTrue(entry["re_escalated"])
        self.assertEqual(entry["history"][-1]["action"], "re-escalate")


class DemoStoreTest(unittest.TestCase):
    """demo 后端：JSON 文件持久化 + 幂等登记。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "escalations.json"
        patcher = mock.patch.object(esc, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_registers_once_and_skips_non_escalated(self) -> None:
        flows = [_flow(), _flow(wf_id="wf-ok", stage="RELEASED")]
        self.assertEqual(esc.sync_from_flows(flows), 1)
        self.assertEqual(esc.sync_from_flows(flows), 0)  # 幂等：不重复登记
        items = esc.list_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["wf_id"], "aiops-fix-order-a1")

    def test_closed_entry_not_reopened_by_sync(self) -> None:
        esc.sync_from_flows([_flow()])
        entry = esc.get("esc-aiops-fix-order-a1")
        esc.apply_close(entry, "alice", "")
        esc.persist_entry(entry)
        self.assertEqual(esc.sync_from_flows([_flow()]), 0)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "closed")

    def test_list_filters_and_orders_desc(self) -> None:
        first = esc.new_entry(_flow(wf_id="wf-1"))
        first["escalated_at"] = "2026-10-06T00:00:00+00:00"
        second = esc.new_entry(_flow(wf_id="wf-2"))
        second["escalated_at"] = "2026-10-06T01:00:00+00:00"
        esc.persist_entry(first)
        esc.persist_entry(second)
        self.assertEqual(
            [e["id"] for e in esc.list_all()], ["esc-wf-2", "esc-wf-1"]
        )
        esc.apply_close(second, "alice", "")
        esc.persist_entry(second)
        self.assertEqual([e["id"] for e in esc.list_all("open")], ["esc-wf-1"])

    def test_stats(self) -> None:
        esc.sync_from_flows([_flow(wf_id="wf-1"), _flow(wf_id="wf-2")])
        entry = esc.get("esc-wf-2")
        esc.apply_assign(entry, "alice", "bob")
        esc.persist_entry(entry)
        s = esc.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["open"], 1)
        self.assertEqual(s["assigned"], 1)
        self.assertEqual(s["closed"], 0)
        self.assertEqual(s["sla_minutes"], esc.DEFAULT_SLA_MINUTES)

    def test_write_is_atomic_no_tmp_left(self) -> None:
        esc.sync_from_flows([_flow()])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class SlaSweepTest(unittest.TestCase):
    """SLA 到期判定与再升级巡检（每单只升级一次，防轰炸）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "escalations.json"
        patcher = mock.patch.object(esc, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_overdue(self, wf_id: str = "wf-old") -> dict:
        entry = esc.new_entry(_flow(wf_id=wf_id))
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
        entry["escalated_at"] = old
        esc.persist_entry(entry)
        return entry

    def test_due_only_overdue_open_entries(self) -> None:
        self._seed_overdue()
        fresh = esc.new_entry(_flow(wf_id="wf-fresh"))  # 刚登记：未超时
        esc.persist_entry(fresh)
        self.assertEqual(
            [e["id"] for e in esc.due_for_re_escalation()], ["esc-wf-old"]
        )

    def test_closed_and_re_escalated_excluded(self) -> None:
        entry = self._seed_overdue()
        entry["re_escalated"] = True  # 已升级过：不再重复
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])

        entry["re_escalated"] = False
        esc.apply_close(entry, "alice", "")
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])  # 已关闭：不再巡检

    def test_sweep_sends_once_per_entry(self) -> None:
        self._seed_overdue()
        with mock.patch.object(
            esc, "_send_re_escalation", return_value={"mode": "demo"}
        ) as sender:
            first = esc.sweep_due()
            second = esc.sweep_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        sender.assert_called_once()
        self.assertTrue(esc.get("esc-wf-old")["re_escalated"])

    def test_sla_minutes_env_override_and_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "5"}):
            self.assertEqual(esc.sla_minutes(), 5)
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "abc"}):
            self.assertEqual(esc.sla_minutes(), esc.DEFAULT_SLA_MINUTES)
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "-3"}):
            self.assertEqual(esc.sla_minutes(), esc.DEFAULT_SLA_MINUTES)


class ProductionDbBackendTest(unittest.TestCase):
    """production（SQLite 注入）：upsert / 读库 / 幂等登记同源。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{Path(self._tmp.name) / 'aiops.sqlite3'}",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db_layer.reset_engine)

    def test_roundtrip_and_idempotent_sync(self) -> None:
        self.assertEqual(esc.sync_from_flows([_flow()]), 1)
        entry = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(entry["service"], "order")
        self.assertEqual(entry["auto_reason"], "闸门1：根因置信度不足（confidence-low）")

        esc.apply_assign(entry, "alice", "bob")
        esc.persist_entry(entry)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["assignee"], "alice")

        self.assertEqual(esc.sync_from_flows([_flow()]), 0)  # 幂等仍在（读库）
        self.assertEqual(esc.stats()["assigned"], 1)

    def test_sla_due_reads_from_db(self) -> None:
        entry = esc.new_entry(_flow())
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
        entry["escalated_at"] = old
        esc.persist_entry(entry)
        due = esc.due_for_re_escalation()
        self.assertEqual([e["id"] for e in due], ["esc-aiops-fix-order-a1"])


class WiringTest(unittest.TestCase):
    """接线冒烟：路由挂载与角色规则（防遗漏注册导致 404 / 权限错配）。"""

    def test_escalation_routes_registered(self) -> None:
        from bff.routes import escalations as routes

        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/escalations", paths)
        self.assertIn("/api/escalations/{esc_id}/assign", paths)
        self.assertIn("/api/escalations/{esc_id}/close", paths)
        self.assertIn("/api/escalations/{esc_id}/retry", paths)

    def test_write_role_rule_present(self) -> None:
        from bff.middleware import _WRITE_ROLE_RULES

        self.assertIn(("/api/escalations", "operator"), _WRITE_ROLE_RULES)


if __name__ == "__main__":
    unittest.main()
