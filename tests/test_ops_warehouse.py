"""M5 运营指标 SQL 数据面（``bff.ops_warehouse``）单测。

用 SQLite 临时库覆盖 production 分支：窗口过滤、行结构归一、单分量降级、
以及「同一数据集下 SQL 数据面与手算逐位一致」的验收口径。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiops_agent import db, mode

from bff import ops_metrics, ops_warehouse

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _naive(days: float = 0, hours: float = 0) -> datetime:
    return (NOW - timedelta(days=days, hours=hours)).astimezone(timezone.utc).replace(tzinfo=None)


class WarehouseDbCase(unittest.TestCase):
    """production + SQLite 的公共夹具（每个用例独立库）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self._tmp.name) / 'warehouse.db'}"
        self._env = mock.patch.dict(
            os.environ,
            {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": self.url, "PYTHON_DOTENV_DISABLED": "1"},
        )
        self._env.start()
        db.reset_engine()
        self.engine = db.get_engine()

    def tearDown(self) -> None:
        db.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    # ---- 行构造 ----

    def insert_run(self, wf_id: str, **overrides) -> None:
        values = {
            "workflow_id": wf_id,
            "run_id": f"run-{wf_id}",
            "stage": "DONE",
            "exec_status": "COMPLETED",
            "kind": "alert",
            "risk": "",
            "alert": {"service": "pay", "alert_id": f"alert-{wf_id}"},
            "confidence": 0.9,
            "start_time": _naive(days=1),
            "close_time": _naive(days=0.5),
            "duration_seconds": 600.0,
            "result": None,
            "updated_at": _naive(),
        }
        values.update(overrides)
        with self.engine.begin() as conn:
            conn.execute(db.workflow_runs.insert().values(**values))

    def insert_audit(self, wf_id: str, action: str, *, hours_ago: float = 1) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                db.audit_events.insert().values(
                    ts=_naive(hours=hours_ago),
                    actor="ops",
                    action=action,
                    workflow_id=wf_id,
                    detail={},
                    result="ok",
                )
            )

    def insert_evidence(self, wf_id: str, status: str | None, *, days_ago: float = 0.5) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                db.log_evidence.insert().values(
                    wf_id=wf_id,
                    alert_id=f"alert-{wf_id}",
                    service="pay",
                    collected_at=_naive(days=days_ago),
                    archived_at=_naive(days=days_ago),
                    loki_available=True,
                    total_lines=10,
                    stored_lines=10,
                    truncated=False,
                    trace_ids=[],
                    templates=[],
                    lines=[],
                    quality={"status": status} if status else None,
                )
            )

    def insert_app(self, app_id: str, service: str, *, enabled: bool = True) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                db.monitored_apps.insert().values(
                    id=app_id,
                    name=app_id,
                    url="http://localhost",
                    service=service,
                    enabled=enabled,
                    note="",
                    created_at=_naive(),
                    updated_at=_naive(),
                )
            )


class WindowQueryTest(WarehouseDbCase):
    def test_items_window_filters_and_maps_result(self) -> None:
        result = {
            "gate_events": ["gate1:blocked:confidence=0.5<0.8", "tests:failed:attempt=1"],
            "test_report": {"verification_mode": "UNIT_TEST", "passed": False},
            "patch": {"risk": "高", "diff": "--- a\n+++ b", "files": ["pay.py"]},
        }
        self.insert_run("wf-in", kind="requirement", risk="高", result=result)
        self.insert_run("wf-old", close_time=_naive(days=9))
        self.insert_run("wf-running", exec_status="RUNNING")
        self.insert_run("wf-open", close_time=None)

        items = ops_warehouse.items_window(days=7, now=NOW)
        self.assertEqual([item["wf_id"] for item in items], ["wf-in"])
        item = items[0]
        self.assertEqual(item["kind"], "requirement")
        self.assertEqual(item["risk"], "高")
        self.assertEqual(len(item["gate_events"]), 2)
        self.assertEqual(item["test_report"]["verification_mode"], "UNIT_TEST")
        self.assertEqual(item["patch"]["files"], ["pay.py"])
        self.assertEqual(item["alert"]["service"], "pay")
        self.assertTrue(item["close_time"].startswith("2026-11-01"))

    def test_items_window_legacy_row_without_result(self) -> None:
        self.insert_run("wf-legacy", kind="alert", risk="中", result=None)
        items = ops_warehouse.items_window(days=7, now=NOW)
        self.assertEqual(items[0]["gate_events"], [])
        self.assertIsNone(items[0]["test_report"])
        self.assertIsNone(items[0]["patch"])
        self.assertEqual(items[0]["risk"], "中")

    def test_audit_and_evidence_window(self) -> None:
        self.insert_audit("wf-1", "approval:approve", hours_ago=2)
        self.insert_audit("wf-1", "login", hours_ago=24 * 9)  # 窗口外
        rows = ops_warehouse.audit_rows(days=7, now=NOW)
        self.assertEqual([row["action"] for row in rows], ["approval:approve"])
        self.assertEqual(rows[0]["wf_id"], "wf-1")

        self.insert_evidence("wf-1", "OK")
        self.insert_evidence("wf-2", None)
        self.insert_evidence("wf-old", "OK", days_ago=9)
        evidence = ops_warehouse.evidence_rows(days=7, now=NOW)
        statuses = {row["wf_id"]: row["quality_status"] for row in evidence}
        self.assertEqual(statuses, {"wf-1": "OK", "wf-2": None})

    def test_managed_services_only_enabled(self) -> None:
        self.insert_app("pay", "pay")
        self.insert_app("search", "search", enabled=False)
        self.insert_app("blank", "")
        self.assertEqual(ops_warehouse.managed_services(), ["pay"])

    def test_mirror_updated_at(self) -> None:
        self.assertIsNone(ops_warehouse.mirror_updated_at())
        self.insert_run("wf-1")
        self.assertIsNotNone(ops_warehouse.mirror_updated_at())

    def test_load_requires_production(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            with self.assertRaises(mode.ModeConfigError):
                ops_warehouse.load(days=7)


class LoadTest(WarehouseDbCase):
    def test_load_merges_components(self) -> None:
        self.insert_run("wf-1")
        self.insert_audit("wf-1", "approval:approve")
        self.insert_evidence("wf-1", "OK")
        self.insert_app("pay", "pay")
        plane = ops_warehouse.load(days=7, now=NOW)
        self.assertEqual(plane["source"], "warehouse")
        self.assertEqual(len(plane["items"]), 1)
        self.assertEqual(len(plane["audit_rows"]), 1)
        self.assertEqual(len(plane["evidence_rows"]), 1)
        self.assertEqual(plane["managed_services"], ["pay"])
        self.assertEqual(plane["degraded"], [])
        self.assertIsNotNone(plane["mirror_updated_at"])

    def test_single_component_failure_degrades_only_itself(self) -> None:
        self.insert_run("wf-1")
        with mock.patch.object(ops_warehouse, "audit_rows", side_effect=RuntimeError("boom")):
            plane = ops_warehouse.load(days=7, now=NOW)
        self.assertEqual(plane["degraded"], ["warehouse_audit"])
        self.assertEqual(len(plane["items"]), 1)
        self.assertEqual(plane["audit_rows"], [])

    def test_evidence_failure_is_none_not_zero(self) -> None:
        with mock.patch.object(ops_warehouse, "evidence_rows", side_effect=RuntimeError("boom")):
            plane = ops_warehouse.load(days=7, now=NOW)
        self.assertIsNone(plane["evidence_rows"])
        self.assertIn("warehouse_evidence", plane["degraded"])


class ConsistencyTest(WarehouseDbCase):
    """验收：「同一窗口、同一数据集下接口与 SQL 手算一致」。"""

    def test_compute_matches_hand_calculated_sql(self) -> None:
        self.insert_run(
            "wf-1",
            duration_seconds=600.0,
            result={"gate_events": ["gate2:approved", "canary:healthy->full-rollout"]},
        )
        self.insert_run(
            "wf-2",
            stage="ESCALATED",
            duration_seconds=1200.0,
            kind="requirement",
            risk="高",
            result={"gate_events": ["tests:failed:attempt=1"]},
        )
        self.insert_run("wf-3", duration_seconds=900.0, risk="低")
        self.insert_run(
            "wf-4",
            stage="ESCALATED",
            duration_seconds=300.0,
            result={"gate_events": ["canary:degraded->rollback"]},
        )
        self.insert_audit("wf-2", "approval:approve", hours_ago=2)
        self.insert_evidence("wf-1", "OK")
        self.insert_evidence("wf-2", "STALE_LOGS")

        plane = ops_warehouse.load(days=7, now=NOW)
        data = ops_metrics.compute(
            plane["items"],
            plane["audit_rows"],
            None,
            days=7,
            now=NOW,
            evidence_rows=plane["evidence_rows"],
            managed_services=plane["managed_services"],
            source=plane["source"],
        )

        # 手算：4 条终态；DONE 2 条 → 自动完成率 0.5
        self.assertEqual(data["closed_total"], 4)
        self.assertEqual(data["auto_fix_rate"], 0.5)
        self.assertEqual(data["auto_fix_sample"], 2)
        # 人工干预：仅 wf-2 有审批写操作 → 1/4
        self.assertEqual(data["human_intervention_rate"], 0.25)
        # MTTR：DONE 的 600s 与 900s → 均值 750
        self.assertEqual(data["mttr"]["avg_seconds"], 750.0)
        self.assertEqual(data["mttr"]["sample"], 2)
        # 分来源：告警 3 条 / 需求 1 条
        self.assertEqual(data["by_kind"]["alert"]["closed"], 3)
        self.assertEqual(data["by_kind"]["requirement"]["closed"], 1)
        # 分风险：高 1、低 1、未知 2
        risks = {row["key"]: row["closed"] for row in data["by_risk"]}
        self.assertEqual(risks, {"high": 1, "medium": 0, "low": 1, "unknown": 2})
        # 闸门拦截：wf-2（tests:failed）与 wf-4（canary:degraded）→ 2/4
        self.assertEqual(data["gate_block_sample"], 2)
        self.assertEqual(data["gate_block_rate"], 0.5)
        # 回滚：degraded 1 / released 2（wf-1 healthy、wf-4 degraded）
        self.assertEqual(data["rollback"], {"rate": 0.5, "sample": 1, "released": 2})
        # 证据：2 条归档，1 OK → 0.5；STALE_LOGS 计入观测异常
        self.assertEqual(data["evidence"]["sample"], 2)
        self.assertEqual(data["evidence"]["ok_rate"], 0.5)
        self.assertEqual(data["evidence"]["observability_share"], 0.5)
        # 数据源标记透传
        self.assertEqual(data["source"], "warehouse")
        # 未接心跳：采集新鲜度不可用（不是「正常」）
        self.assertFalse(data["logship"]["available"])


class DemoEvidenceFilesTest(unittest.TestCase):
    def test_demo_scans_evidence_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "evidence"
            directory.mkdir()
            (directory / "wf-1.json").write_text(
                json.dumps({"wf_id": "wf-1", "service": "pay", "archived_at": _iso(NOW - timedelta(hours=2)),
                            "quality": {"status": "OK"}}),
                encoding="utf-8",
            )
            (directory / "wf-old.json").write_text(
                json.dumps({"wf_id": "wf-old", "service": "pay", "archived_at": _iso(NOW - timedelta(days=9)),
                            "quality": {"status": "OK"}}),
                encoding="utf-8",
            )
            (directory / "broken.json").write_text("{not json", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"AIOPS_MODE": "demo", "AIOPS_DATA_ROOT": tmp}, clear=False
            ):
                rows = ops_warehouse.evidence_rows(days=7, now=NOW)
        self.assertEqual([row["wf_id"] for row in rows], ["wf-1"])
        self.assertEqual(rows[0]["quality_status"], "OK")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
