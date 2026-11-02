"""值班 Runbook 读取（M5）：按告警 code 取 docs 小节 + 与聚合产出的 code 保持同源。

关键约束：``ops_metrics`` 产出的每个 ``duty_alerts[].code`` 必须在 Runbook 中有对应
``## <code>`` 小节，否则面板「Runbook」按钮点开是空的——本文件用聚合结果做交叉校验。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from bff import ops_metrics, runbooks

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


def _flow(wf_id: str, hours: float, gate_events: list[str], *, stage: str = "ESCALATED") -> dict:
    return {
        "wf_id": wf_id,
        "exec_status": "COMPLETED",
        "stage": stage,
        "close_time": _ts(hours),
        "confidence": 0.9,
        "gate_events": gate_events,
        "alert": {"service": "pay"},
    }


class SectionTest(unittest.TestCase):
    def test_known_section(self) -> None:
        data = runbooks.section("log-stall")
        self.assertTrue(data["known"])
        self.assertIn("log-stall", data["title"])
        self.assertIn("判定", data["body"])
        self.assertEqual(data["path"], "docs/AIOps 值班 Runbook.md")

    def test_section_stops_at_next_heading(self) -> None:
        body = runbooks.section("log-stall")["body"]
        self.assertNotIn("## escalation-backlog", body)
        self.assertNotIn("escalation-backlog 待办积压", body)

    def test_unknown_code(self) -> None:
        data = runbooks.section("not-a-real-code")
        self.assertFalse(data["known"])
        self.assertEqual(data["body"], "")

    def test_path_traversal_rejected(self) -> None:
        for code in ("../etc/passwd", "Log-Stall", "log stall", ""):
            self.assertFalse(runbooks.section(code)["known"], code)

    def test_missing_file_degrades(self) -> None:
        with mock.patch.object(runbooks, "runbook_path", return_value=mock.MagicMock()):
            with mock.patch("pathlib.Path.read_text", side_effect=OSError("missing")):
                self.assertFalse(runbooks.section("log-stall")["known"])


class SyncWithDutyAlertsTest(unittest.TestCase):
    """聚合产出的 code 与 Runbook 小节必须一一对应（防告警改码后文档失联）。"""

    def test_every_duty_alert_code_has_section(self) -> None:
        items = [
            _flow("wf-1", 1, ["tests:failed:attempt=1", "gate1:blocked:confidence=0.4<0.8"]),
            _flow("wf-2", 2, ["tests:failed:attempt=1", "gate1:blocked:confidence=0.4<0.8"]),
            _flow("wf-3", 3, ["tests:failed:attempt=1", "gate1:blocked:confidence=0.4<0.8"]),
            _flow("wf-4", 4, ["gate2:approved"]),
            _flow("wf-5", 5, ["gate2:approved"]),
            _flow("wf-6", 6, ["gate2:approved"]),
            _flow("wf-7", 7, ["canary:degraded->rollback"]),
            _flow("wf-8", 8, ["gate1:composite:pass"]),
        ]
        log_ship = [{"service": f"s{i}", "age_seconds": 900, "healthy": False} for i in range(3)]
        entries = [
            {
                "status": "open",
                "escalated_at": _ts(5),
                "closed_at": None,
                "resolution": None,
            }
        ]
        data = ops_metrics.compute(
            items, [], {"sla_minutes": 30}, days=7, now=NOW,
            esc_entries=entries, log_ship=log_ship,
        )
        codes = {alert["code"] for alert in data["duty_alerts"]}
        self.assertEqual(
            codes,
            {
                "log-stall",
                "escalation-backlog",
                "sandbox-failure-streak",
                "model-quality-streak",
                "rollback",
                "high-confidence-false-pass",
            },
        )
        for code in codes:
            section = runbooks.section(code)
            self.assertTrue(section["known"], f"{code} 缺少 Runbook 小节")
            self.assertTrue(section["body"].strip())

    def test_runbook_link_points_to_same_doc(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW, log_ship=[], esc_entries=None)
        for alert in data["duty_alerts"]:
            self.assertEqual(
                alert["runbook"],
                f"{runbooks.RUNBOOK_RELATIVE}#{alert['code'].replace('_', '-')}",
            )


class WiringTest(unittest.TestCase):
    def test_route_registered(self) -> None:
        from bff.routes import ops_metrics as routes

        paths = {getattr(route, "path", "") for route in routes.router.routes}
        self.assertIn("/api/metrics/ops/runbook", paths)


if __name__ == "__main__":
    unittest.main()
