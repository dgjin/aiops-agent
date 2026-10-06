"""运营度量聚合单测（bff/ops_metrics，P1-4）：窗口 / MTTR / 三项比率 / 分档。

纯函数测试：``compute`` 的全部输入（流程项 / 审计行 / 待办统计）均为注入数据，
不依赖 Temporal / DB；另含路由接线冒烟（防遗漏注册）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bff import ops_metrics

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def _close_time(days_ago: float, hours: float = 0) -> str:
    ts = NOW - timedelta(days=days_ago, hours=hours)
    return ts.isoformat(timespec="seconds")


def _flow(
    wf_id: str,
    stage: str,
    days_ago: float = 1,
    duration: float | None = 100.0,
    gate_events: list[str] | None = None,
    exec_status: str = "COMPLETED",
) -> dict:
    return {
        "wf_id": wf_id,
        "exec_status": exec_status,
        "stage": stage,
        "close_time": _close_time(days_ago),
        "duration_seconds": duration,
        "gate_events": gate_events or [],
    }


class WindowAndMttrTest(unittest.TestCase):
    def test_window_filters_old_and_running(self) -> None:
        items = [
            _flow("wf-recent", "DONE", days_ago=2),
            _flow("wf-old", "DONE", days_ago=8),  # 窗口外
            _flow("wf-running", "TRIAGING", exec_status="RUNNING"),  # 运行中
            _flow("wf-no-close", "DONE", days_ago=1),
        ]
        items[3]["close_time"] = None
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["closed_total"], 1)
        self.assertEqual(data["by_stage"], {"DONE": 1})

    def test_mttr_avg_and_p95(self) -> None:
        items = [
            _flow("wf-1", "DONE", duration=100.0),
            _flow("wf-2", "DONE", duration=200.0),
            _flow("wf-3", "DONE", duration=300.0),
            _flow("wf-4", "ESCALATED", duration=999.0),  # 非 DONE：不计入 MTTR
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["mttr"]["sample"], 3)
        self.assertEqual(data["mttr"]["avg_seconds"], 200.0)
        self.assertEqual(data["mttr"]["p95_seconds"], 300.0)

    def test_mttr_empty_and_missing_duration(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW)
        self.assertIsNone(data["mttr"]["avg_seconds"])
        self.assertIsNone(data["mttr"]["p95_seconds"])
        self.assertEqual(data["mttr"]["sample"], 0)

        items = [_flow("wf-1", "DONE", duration=None)]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["mttr"]["sample"], 0)
        self.assertIsNone(data["mttr"]["avg_seconds"])


class RateTest(unittest.TestCase):
    def test_auto_fix_rate(self) -> None:
        items = [
            _flow("wf-1", "DONE"),
            _flow("wf-2", "DONE"),
            _flow("wf-3", "DONE"),
            _flow("wf-4", "ESCALATED"),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["auto_fix_rate"], 0.75)
        self.assertEqual(data["auto_fix_sample"], 3)

    def test_human_intervention_rate(self) -> None:
        items = [_flow("wf-1", "DONE"), _flow("wf-2", "ESCALATED"), _flow("wf-3", "DONE")]
        audit_rows = [
            {"wf_id": "wf-1", "action": "approval:approve"},  # 命中（审批）
            {"wf_id": "wf-2", "action": "escalation:assign"},  # 命中（转人工处置）
            {"wf_id": "wf-1", "action": "approval:approve"},  # 重复：去重
            {"wf_id": "wf-3", "action": "login"},  # 非干预动作
            {"wf_id": "wf-x", "action": "approval:approve"},  # 窗口外流程
            {"wf_id": "", "action": "deploy-command:deploy_now"},  # 无 wf_id
        ]
        data = ops_metrics.compute(items, audit_rows, None, days=7, now=NOW)
        self.assertEqual(data["human_intervention_sample"], 2)
        self.assertEqual(data["human_intervention_rate"], round(2 / 3, 4))

    def test_gate_block_rate_and_breakdown(self) -> None:
        items = [
            _flow("wf-1", "ESCALATED", gate_events=["gate1:blocked:confidence=0.5<0.8"]),
            _flow(
                "wf-2",
                "ESCALATED",
                gate_events=["tests:failed:attempt=1", "tests:failed:attempt=2"],
            ),
            _flow("wf-3", "ESCALATED", gate_events=["gate2:timeout->reject"]),
            _flow("wf-4", "DONE", gate_events=["gate2:approved", "canary:healthy->full-rollout"]),
            _flow("wf-5", "DONE", gate_events=[]),
        ]
        data = ops_metrics.compute(items, [], None, days=7, now=NOW)
        self.assertEqual(data["gate_block_sample"], 3)
        self.assertEqual(data["gate_block_rate"], 0.6)
        breakdown = {row["key"]: row["count"] for row in data["gate_breakdown"]}
        self.assertEqual(breakdown["gate1:blocked"], 1)
        self.assertEqual(breakdown["tests:failed"], 1)
        self.assertEqual(breakdown["gate2:timeout"], 1)
        self.assertEqual(breakdown["canary:degraded"], 0)
        # 通过态事件（approve / healthy）不构成拦截
        self.assertEqual(sum(breakdown.values()), 3)

    def test_zero_total_rates(self) -> None:
        data = ops_metrics.compute([], [], None, days=7, now=NOW)
        self.assertEqual(data["auto_fix_rate"], 0.0)
        self.assertEqual(data["human_intervention_rate"], 0.0)
        self.assertEqual(data["gate_block_rate"], 0.0)

    def test_escalations_snapshot_passthrough(self) -> None:
        stats = {"total": 3, "open": 1, "assigned": 1, "closed": 1}
        data = ops_metrics.compute([], [], stats, days=7, now=NOW)
        self.assertEqual(data["escalations"], stats)
        data = ops_metrics.compute([], [], None, days=7, now=NOW)
        self.assertIsNone(data["escalations"])


class WiringTest(unittest.TestCase):
    """路由接线冒烟：防遗漏注册导致 404。"""

    def test_route_registered(self) -> None:
        from bff.routes import ops_metrics as routes

        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/metrics/ops", paths)


if __name__ == "__main__":
    unittest.main()
