"""M5 治理硬门禁的数据面分量：命令积压、审计链完整性、通知未送达。

口径约束：不可读一律 ``available=False``（门禁 unknown），不得用 0 冒充健康。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from bff import ops_metrics
from bff.routes import ops_metrics as ops_metrics_routes

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


class CommandStateTest(unittest.TestCase):
    def test_unavailable_is_not_zero(self) -> None:
        state = ops_metrics.command_state(None)
        self.assertFalse(state["available"])
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["oldest_pending_seconds"])
        self.assertIsNone(state["failed_24h"])

    def test_values_are_normalized(self) -> None:
        state = ops_metrics.command_state(
            {"pending": "2", "oldest_pending_seconds": "12.5", "failed_24h": 1}
        )
        self.assertTrue(state["available"])
        self.assertEqual(state["pending"], 2)
        self.assertEqual(state["oldest_pending_seconds"], 12.5)
        self.assertEqual(state["failed_24h"], 1)

    def test_missing_keys_default_to_zero_for_readable_store(self) -> None:
        state = ops_metrics.command_state({})
        self.assertTrue(state["available"])
        self.assertEqual(state["pending"], 0)
        self.assertEqual(state["oldest_pending_seconds"], 0.0)
        self.assertEqual(state["failed_24h"], 0)


class IntegrityStateTest(unittest.TestCase):
    def test_unavailable_is_not_healthy(self) -> None:
        state = ops_metrics.integrity_state(None)
        self.assertFalse(state["available"])
        self.assertIsNone(state["ok"])

    def test_ok_passes_through(self) -> None:
        state = ops_metrics.integrity_state(
            {"ok": True, "checked": 42, "legacy": 3, "archives": 2}
        )
        self.assertTrue(state["available"])
        self.assertTrue(state["ok"])
        self.assertEqual(state["checked"], 42)
        self.assertEqual(state["legacy"], 3)
        self.assertEqual(state["archives"], 2)

    def test_tampered_keeps_broken_at_and_truncates_error(self) -> None:
        state = ops_metrics.integrity_state(
            {
                "ok": False,
                "checked": 10,
                "error": "x" * 500,
                "broken_at": {"event_id": "e-1", "source": "audit-1.jsonl"},
            }
        )
        self.assertFalse(state["ok"])
        self.assertEqual(state["broken_at"]["event_id"], "e-1")
        self.assertLessEqual(len(state["error"]), 300)


class EscalationDetailTest(unittest.TestCase):
    def test_unavailable_keeps_notify_pending_from_stats(self) -> None:
        detail = ops_metrics._escalation_detail(
            {"notify_pending": 2, "sla_minutes": 30}, None, now=NOW
        )
        self.assertFalse(detail["available"])
        self.assertEqual(detail["notify_pending"], 2)

    def test_available_passes_notify_pending(self) -> None:
        detail = ops_metrics._escalation_detail(
            {"notify_pending": 0, "sla_minutes": 30}, [], now=NOW
        )
        self.assertTrue(detail["available"])
        self.assertEqual(detail["notify_pending"], 0)

    def test_missing_stats_keeps_none(self) -> None:
        detail = ops_metrics._escalation_detail(None, [], now=NOW)
        self.assertIsNone(detail["notify_pending"])


class ComputeGovernanceTest(unittest.TestCase):
    def test_governance_sections_are_exposed(self) -> None:
        data = ops_metrics.compute(
            [],
            [],
            None,
            now=NOW,
            command_stats={"pending": 0, "oldest_pending_seconds": 0.0, "failed_24h": 0},
            audit_integrity={"ok": True, "checked": 5},
        )
        self.assertEqual(data["commands"]["pending"], 0)
        self.assertTrue(data["commands"]["available"])
        self.assertTrue(data["audit_integrity"]["ok"])

    def test_missing_sections_mark_unavailable(self) -> None:
        data = ops_metrics.compute([], [], None, now=NOW)
        self.assertFalse(data["commands"]["available"])
        self.assertFalse(data["audit_integrity"]["available"])
        self.assertIsNone(data["audit_integrity"]["ok"])


class RouteCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_command_stats_success_and_failure(self) -> None:
        degraded: list[str] = []
        with mock.patch.object(
            ops_metrics_routes.commands,
            "stats",
            return_value={"pending": 1, "oldest_pending_seconds": 2.0, "failed_24h": 0},
        ):
            value = await ops_metrics_routes._command_stats(degraded)
        self.assertEqual(value["pending"], 1)
        self.assertEqual(degraded, [])

        with mock.patch.object(
            ops_metrics_routes.commands, "stats", side_effect=RuntimeError("unreadable")
        ):
            value = await ops_metrics_routes._command_stats(degraded)
        self.assertIsNone(value)
        self.assertIn("commands", degraded)

    async def test_audit_integrity_success_and_failure(self) -> None:
        degraded: list[str] = []
        with mock.patch.object(
            ops_metrics_routes.audit, "verify", return_value={"ok": False, "error": "tampered"}
        ):
            value = await ops_metrics_routes._audit_integrity(degraded)
        self.assertFalse(value["ok"])
        self.assertEqual(degraded, [])

        with mock.patch.object(
            ops_metrics_routes.audit, "verify", side_effect=RuntimeError("unreadable")
        ):
            value = await ops_metrics_routes._audit_integrity(degraded)
        self.assertIsNone(value)
        self.assertIn("audit_integrity", degraded)


if __name__ == "__main__":
    unittest.main()
