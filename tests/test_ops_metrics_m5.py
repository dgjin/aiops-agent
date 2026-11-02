"""M5 运营指标聚合（纯函数）单测：分维度、证据/沙箱质量、待办 SLA、值班告警。

全部输入为注入数据，不依赖 Temporal / DB；口径关键点：
    - 分维度分母各自独立（告警/需求、风险桶、服务），空样本返回 None；
    - 真实沙箱通过率只认 ``UNIT_TEST``，其余口径单列；
    - 值班告警数据不足给 ``unknown``，不给「正常」。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bff import ops_metrics

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: float = 0, hours: float = 0) -> str:
    return (NOW - timedelta(days=days_ago, hours=hours)).isoformat(timespec="seconds")


def _flow(
    wf_id: str,
    stage: str = "DONE",
    *,
    kind: str = "alert",
    service: str = "pay",
    days_ago: float = 0.5,
    duration: float | None = 100.0,
    confidence: float | None = 0.9,
    gate_events: list[str] | None = None,
    test_report: dict | None = None,
    patch: dict | None = None,
    risk: str = "",
    start_hours_ago: float | None = None,
) -> dict:
    started = _ts(hours=start_hours_ago) if start_hours_ago is not None else _ts(days_ago=days_ago + 1)
    return {
        "wf_id": wf_id,
        "exec_status": "COMPLETED",
        "stage": stage,
        "kind": kind,
        "risk": risk,
        "close_time": _ts(days_ago=days_ago),
        "start_time": started,
        "duration_seconds": duration,
        "confidence": confidence,
        "gate_events": gate_events or [],
        "test_report": test_report,
        "patch": patch,
        "alert": {"service": service, "alert_id": f"alert-{wf_id}"},
    }


def _esc(
    status: str = "open",
    *,
    escalated_hours_ago: float = 2,
    closed_hours_ago: float | None = None,
    close_reason: str | None = None,
    verdict: str | None = None,
) -> dict:
    resolution = {}
    if close_reason is not None:
        resolution["close_reason"] = close_reason
    if verdict is not None:
        resolution["verdict"] = verdict
    return {
        "status": status,
        "escalated_at": _ts(hours=escalated_hours_ago),
        "closed_at": _ts(hours=closed_hours_ago) if closed_hours_ago is not None else None,
        "resolution": resolution or None,
    }


class DimensionTest(unittest.TestCase):
    def test_by_kind_uses_independent_denominator(self) -> None:
        items = [
            _flow("a1", "DONE", kind="alert"),
            _flow("a2", "ESCALATED", kind="alert"),
            _flow("r1", "DONE", kind="requirement"),
            _flow("r2", "DONE", kind="requirement"),
            _flow("r3", "DONE", kind="requirement"),
            _flow("r4", "SHADOWED", kind="requirement"),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["by_kind"]["alert"]["closed"], 2)
        self.assertEqual(data["by_kind"]["alert"]["auto_fix_rate"], 0.5)
        self.assertEqual(data["by_kind"]["requirement"]["closed"], 4)
        self.assertEqual(data["by_kind"]["requirement"]["auto_fix_rate"], 0.75)
        # 总口径仍为全部终态（SHADOWED 不计入 DONE）
        self.assertEqual(data["closed_total"], 6)
        self.assertEqual(data["auto_fix_rate"], round(4 / 6, 4))

    def test_by_kind_empty_bucket_is_null(self) -> None:
        data = ops_metrics.compute([_flow("a1")], [], None, days=7, now=NOW)
        self.assertEqual(data["by_kind"]["requirement"]["closed"], 0)
        self.assertIsNone(data["by_kind"]["requirement"]["auto_fix_rate"])

    def test_by_risk_buckets(self) -> None:
        items = [
            _flow("w1", risk="高危变更"),
            _flow("w2", risk="高风险"),
            _flow("w3", "ESCALATED", risk="高"),
            _flow("w4", risk="中风险"),
            _flow("w5", risk="low"),
            _flow("w6", risk=""),  # 未知
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        buckets = {row["key"]: row for row in data["by_risk"]}
        self.assertEqual([row["key"] for row in data["by_risk"]], ["high", "medium", "low", "unknown"])
        self.assertEqual(buckets["high"]["closed"], 3)
        self.assertEqual(buckets["high"]["done"], 2)
        self.assertEqual(buckets["high"]["auto_fix_rate"], round(2 / 3, 4))
        self.assertEqual(buckets["low"]["closed"], 1)
        self.assertEqual(buckets["unknown"]["closed"], 1)
        self.assertEqual(buckets["unknown"]["auto_fix_rate"], 1.0)

    def test_risk_from_patch_when_column_missing(self) -> None:
        """Temporal 读侧没有 risk 列，回退 patch.risk（两套数据面口径一致）。"""
        items = [_flow("w1", risk="", patch={"risk": "中风险", "diff": "x", "files": ["a.py"]})]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        buckets = {row["key"]: row["closed"] for row in data["by_risk"]}
        self.assertEqual(buckets["medium"], 1)

    def test_by_service_sorted_and_truncated(self) -> None:
        items = [_flow("pay-1", service="pay"), _flow("pay-2", service="pay")]
        items += [_flow(f"svc-{index}", service=f"svc{index:02d}") for index in range(12)]
        items += [
            {"wf_id": "no-alert", "exec_status": "COMPLETED", "stage": "DONE",
             "close_time": _ts(days_ago=1), "alert": None}
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        rows = data["by_service"]
        self.assertEqual(len(rows), 10)  # 截断前 10
        self.assertEqual(rows[0]["service"], "pay")
        self.assertEqual(rows[0]["closed"], 2)
        # 同为 1 条时按服务名升序；unknown 排在末尾被截断
        self.assertEqual([row["service"] for row in rows[1:4]], ["svc00", "svc01", "svc02"])
        self.assertNotIn("unknown", {row["service"] for row in rows})


class MttaTest(unittest.TestCase):
    def test_first_intervention_only(self) -> None:
        items = [
            _flow("wf-1", "ESCALATED", start_hours_ago=10, days_ago=0.1),
            _flow("wf-2", "DONE", start_hours_ago=10, days_ago=0.1),
            _flow("wf-3", "DONE", start_hours_ago=10, days_ago=0.1),
        ]
        audit_rows = [
            {"wf_id": "wf-1", "action": "approval:approve", "ts": _ts(hours=8)},  # 2h 后（首个）
            {"wf_id": "wf-1", "action": "deploy-command:deploy_now", "ts": _ts(hours=1)},  # 更晚，不计
            {"wf_id": "wf-2", "action": "login", "ts": _ts(hours=9)},  # 非干预动作
            {"wf_id": "wf-3", "action": "escalation:assign", "ts": _ts(hours=4)},  # 6h 后
            {"wf_id": "wf-outside", "action": "approval:approve", "ts": _ts(hours=1)},
        ]
        data = ops_metrics.compute(items, audit_rows, None, days=7, now=NOW)
        mtta = data["mtta"]
        self.assertEqual(mtta["sample"], 2)
        self.assertEqual(mtta["avg_seconds"], 4 * 3600)
        self.assertEqual(mtta["p95_seconds"], 6 * 3600)

    def test_no_intervention_is_null(self) -> None:
        data = ops_metrics.compute([_flow("wf-1")], [], None, days=7, now=NOW)
        self.assertIsNone(data["mtta"]["avg_seconds"])
        self.assertEqual(data["mtta"]["sample"], 0)


class RollbackTest(unittest.TestCase):
    def test_canary_degraded_vs_released(self) -> None:
        items = [
            _flow("wf-1", gate_events=["canary:degraded->rollback"]),
            _flow("wf-2", gate_events=["canary:healthy->full-rollout"]),
            _flow("wf-3", gate_events=["gate2:approved"]),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["rollback"], {"rate": 0.5, "sample": 1, "released": 2})

    def test_no_release_is_null(self) -> None:
        data = ops_metrics.compute([_flow("wf-1")], [], None, days=7, now=NOW)
        self.assertIsNone(data["rollback"]["rate"])
        self.assertEqual(data["rollback"]["released"], 0)


class EvidenceTest(unittest.TestCase):
    def test_status_distribution_and_observability(self) -> None:
        rows = [
            {"quality": {"status": "OK"}},
            {"quality": {"status": "OK"}},
            {"quality": {"status": "STALE_LOGS"}},
            {"quality": {"status": "NO_LOGS"}},
            {"quality": None},  # 旧归档 → UNKNOWN
        ]
        data = ops_metrics.compute([], [], None, days=7, now=NOW, evidence_rows=rows)
        evidence = data["evidence"]
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["sample"], 5)
        self.assertEqual(evidence["ok_rate"], 0.4)
        self.assertEqual(evidence["observability_share"], 0.4)
        counts = {row["key"]: row["count"] for row in evidence["by_status"]}
        self.assertEqual(counts["UNKNOWN"], 1)
        self.assertEqual(counts["INSUFFICIENT_SIGNAL"], 0)
        flags = {row["key"]: row["observability"] for row in evidence["by_status"]}
        self.assertTrue(flags["LOKI_UNAVAILABLE"])
        self.assertFalse(flags["INSUFFICIENT_SIGNAL"])

    def test_unavailable_is_not_zero(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW, evidence_rows=None)
        self.assertFalse(data["evidence"]["available"])
        self.assertIsNone(data["evidence"]["ok_rate"])


class LogShipTest(unittest.TestCase):
    def test_no_heartbeat_is_unavailable(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW, log_ship=[])
        self.assertFalse(data["logship"]["available"])
        self.assertIsNone(data["logship"]["coverage"])

    def test_healthy_and_coverage(self) -> None:
        log_ship = [
            {"service": "pay", "age_seconds": 30, "healthy": True},
            {"service": "coupon", "age_seconds": 90, "healthy": True},
            {"service": "order", "age_seconds": 900, "healthy": False},
        ]
        data = ops_metrics.compute(
            [], [], None, days=7, now=NOW, log_ship=log_ship,
            managed_services=["pay", "coupon", "order", "search"],
        )
        logship = data["logship"]
        self.assertEqual(logship["healthy"], 2)
        self.assertEqual(logship["unhealthy"], 1)
        self.assertEqual(logship["unhealthy_services"], ["order"])
        self.assertEqual(logship["freshness_p95_seconds"], 900)
        self.assertEqual(logship["coverage"], {"covered": 3, "total": 4, "rate": 0.75})

    def test_coverage_null_without_managed_services(self) -> None:
        log_ship = [{"service": "pay", "age_seconds": 10, "healthy": True}]
        data = ops_metrics.compute([], [], None, days=7, now=NOW, log_ship=log_ship)
        self.assertIsNone(data["logship"]["coverage"])


class SandboxTest(unittest.TestCase):
    def test_real_rate_only_unit_test(self) -> None:
        items = [
            _flow("wf-1", test_report={"verification_mode": "UNIT_TEST", "passed": True}),
            _flow("wf-2", test_report={"verification_mode": "UNIT_TEST", "passed": False}),
            _flow("wf-3", test_report={"verification_mode": "CONTRACT_TEST", "passed": True}),
            _flow("wf-4", test_report={"verification_mode": "STATIC_ONLY", "passed": False}),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        sandbox = data["sandbox"]
        self.assertTrue(sandbox["available"])
        self.assertEqual(sandbox["sample"], 4)
        self.assertEqual(sandbox["real_sample"], 2)
        self.assertEqual(sandbox["real_pass_rate"], 0.5)
        self.assertEqual(sandbox["static_count"], 1)
        self.assertEqual(sandbox["contract_count"], 1)
        modes = {row["key"]: row for row in sandbox["by_mode"]}
        self.assertTrue(modes["UNIT_TEST"]["is_real"])
        self.assertFalse(modes["STATIC_ONLY"]["is_real"])
        self.assertEqual(modes["STATIC_ONLY"]["count"], 1)
        self.assertEqual(modes["UNKNOWN"]["count"], 0)

    def test_patch_structural_and_override(self) -> None:
        items = [
            _flow("wf-1", patch={"diff": "--- a\n+++ b", "files": ["b.py"], "risk": "低"}),
            _flow("wf-2", patch={"risk": "低"}),  # 缺 diff/files：结构不可应用
        ]
        esc_entries = [
            _esc(status="closed", closed_hours_ago=1, verdict="INCORRECT"),
            _esc(status="closed", closed_hours_ago=1, verdict="CORRECT"),
            _esc(status="closed", closed_hours_ago=1, verdict="CORRECT"),
            _esc(status="closed", closed_hours_ago=1),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW, esc_entries=esc_entries)
        sandbox = data["sandbox"]
        self.assertEqual(sandbox["patch_structural_sample"], 2)
        self.assertEqual(sandbox["patch_structural_rate"], 0.5)
        self.assertEqual(sandbox["override_sample"], 3)
        self.assertEqual(sandbox["override_rate"], round(1 / 3, 4))

    def test_high_confidence_false_pass(self) -> None:
        items = [
            _flow("wf-1", "ESCALATED", confidence=0.9, gate_events=["gate1:composite:pass"]),
            _flow("wf-2", "ESCALATED", confidence=0.5, gate_events=["gate1:composite:pass"]),
            _flow("wf-3", "ESCALATED", confidence=0.95),
            _flow("wf-4", "DONE", confidence=0.99, gate_events=["gate1:composite:pass"]),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["sandbox"]["high_confidence_false_pass"], 1)


class EscalationDetailTest(unittest.TestCase):
    def test_open_age_sla_and_close_reason(self) -> None:
        entries = [
            _esc("open", escalated_hours_ago=3),
            _esc("assigned", escalated_hours_ago=1),
            _esc("closed", escalated_hours_ago=10, closed_hours_ago=6, close_reason="RESOLVED"),
            _esc("closed", escalated_hours_ago=10, closed_hours_ago=6),  # 未结构化关闭
        ]
        stats = {"sla_minutes": 60, "knowledge": {"draft": 2, "approved": 1, "rejected": 0}}
        data = ops_metrics.compute([], [], stats, days=7, now=NOW, esc_entries=entries)
        detail = data["escalation_detail"]
        self.assertTrue(detail["available"])
        self.assertEqual(detail["open_sample"], 2)
        self.assertEqual(detail["open_avg_seconds"], 2 * 3600)
        self.assertEqual(detail["open_p95_seconds"], 3 * 3600)
        self.assertEqual(detail["sla_breach"], 1)  # 3h > 60min
        self.assertEqual(detail["handle_avg_seconds"], 4 * 3600)
        reasons = {row["key"]: row["count"] for row in detail["close_reasons"]}
        self.assertEqual(reasons["RESOLVED"], 1)
        self.assertEqual(reasons["unstructured"], 1)
        self.assertEqual(detail["knowledge"]["approved"], 1)
        self.assertIsNone(detail["knowledge"]["adopted"])

    def test_unreadable_entries(self) -> None:
        data = ops_metrics.compute([], [], {"sla_minutes": 30}, days=7, now=NOW, esc_entries=None)
        detail = data["escalation_detail"]
        self.assertFalse(detail["available"])
        self.assertIsNone(detail["sla_breach"])
        self.assertEqual(detail["sla_minutes"], 30)


class DutyAlertTest(unittest.TestCase):
    def _codes(self, data: dict) -> dict:
        return {alert["code"]: alert for alert in data["duty_alerts"]}

    def test_unknown_when_data_missing(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW, log_ship=[], esc_entries=None)
        codes = self._codes(data)
        self.assertEqual(codes["log-stall"]["level"], "unknown")
        self.assertEqual(codes["escalation-backlog"]["level"], "unknown")
        self.assertNotIn("sandbox-failure-streak", codes)

    def test_all_five_signals(self) -> None:
        items = [
            _flow("wf-1", "ESCALATED", gate_events=["tests:failed:attempt=1"]),
            _flow("wf-2", "ESCALATED", gate_events=["tests:failed:attempt=1"]),
            _flow("wf-3", "ESCALATED", gate_events=["tests:failed:attempt=1"]),
            _flow("wf-4", "ESCALATED", days_ago=1, gate_events=["gate1:blocked:confidence=0.4<0.8"]),
            _flow("wf-5", "DONE", days_ago=2, gate_events=["canary:degraded->rollback"]),
            _flow("wf-6", "ESCALATED", days_ago=3, confidence=0.9, gate_events=["gate1:composite:pass"]),
        ]
        log_ship = [{"service": f"s{index}", "age_seconds": 900, "healthy": False} for index in range(3)]
        entries = [_esc("open", escalated_hours_ago=5)]
        stats = {"sla_minutes": 30, "knowledge": {}}
        data = ops_metrics.compute(
            items, [], stats, days=7, now=NOW,
            esc_entries=entries, log_ship=log_ship, managed_services=[],
        )
        codes = self._codes(data)
        self.assertEqual(codes["log-stall"]["level"], "danger")
        self.assertEqual(codes["escalation-backlog"]["level"], "danger")
        self.assertEqual(codes["sandbox-failure-streak"]["value"], 3)
        self.assertEqual(codes["rollback"]["value"], 1)
        self.assertEqual(codes["high-confidence-false-pass"]["value"], 1)
        self.assertNotIn("model-quality-streak", codes)  # 最新三条不是连续低置信
        for alert in data["duty_alerts"]:
            self.assertTrue(alert["runbook"].startswith("docs/AIOps 值班 Runbook.md#"))

    def test_model_quality_streak(self) -> None:
        items = [
            _flow(f"wf-{index}", "ESCALATED", days_ago=index / 10,
                  gate_events=["gate1:blocked:confidence=0.3<0.8"])
            for index in range(3)
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(self._codes(data)["model-quality-streak"]["value"], 3)


class SourceTest(unittest.TestCase):
    def test_source_passthrough(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW, source="warehouse")
        self.assertEqual(data["source"], "warehouse")
        self.assertEqual(data["period_days"], 7)


if __name__ == "__main__":
    unittest.main()
