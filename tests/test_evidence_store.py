"""日志证据归档单测（转人工核对）：截断策略 / 双后端 / 批量摘要 / BFF 读取接口。

背景：告警流程 TRIAGING 采集的日志此前只进入 LLM，不落库；转人工后无法核对
「当时到底报了什么错」。本模块把证据按 workflow_id 归档，覆盖三个层面：
- 纯函数：末尾保留截断、单行截断、模板条数上限、空证据；
- 存储：demo 文件后端（AIOPS_DATA_ROOT 隔离）与 production SQLite 分支接口同构；
- 路由：/api/flows/{wf_id}/evidence 的 200/404，转人工列表附带证据摘要。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import db as db_layer
from aiops_agent import evidence_store
from bff.deps import ApiError
from bff.routes import escalations as esc_routes
from bff.routes import flows as flows_routes

_EVIDENCE = {
    "alert_id": "a-1",
    "service": "nl2sql",
    "collected_at": "2026-10-16T08:00:00+00:00",
    "loki_available": True,
    "lines": [
        "ERROR [trace_id=trace-0001] java.lang.NullPointerException: coupon is null",
        "WARN retry 1/3",
    ],
    "trace_ids": ["trace-0001"],
}

_CLUSTERED = {
    "templates": [
        {"id": "tpl-1", "pattern": "ERROR <*> NullPointerException: coupon is null", "count": 8},
        {"id": "tpl-2", "pattern": "WARN retry <*>", "count": 2},
    ],
    "compression_ratio": 0.4,
    "total_lines": 10,
}


class BuildRecordTest(unittest.TestCase):
    """纯函数：体积控制与摘要口径。"""

    def test_truncates_to_tail(self) -> None:
        lines = [f"line-{index}" for index in range(650)]
        record = evidence_store.build_record(
            "aiops-fix-a-1", {**_EVIDENCE, "lines": lines}, _CLUSTERED
        )
        self.assertEqual(record["total_lines"], 650)
        self.assertEqual(record["stored_lines"], evidence_store.MAX_STORED_LINES)
        self.assertTrue(record["truncated"])
        # 保留末尾：离故障时刻最近的行优先
        self.assertEqual(record["lines"][0], f"line-{650 - evidence_store.MAX_STORED_LINES}")
        self.assertEqual(record["lines"][-1], "line-649")

    def test_no_truncate_when_within_limit(self) -> None:
        record = evidence_store.build_record("wf", _EVIDENCE, _CLUSTERED)
        self.assertFalse(record["truncated"])
        self.assertEqual(record["stored_lines"], 2)

    def test_clips_long_line_and_template_cap(self) -> None:
        long_line = "x" * (evidence_store.MAX_LINE_CHARS + 100)
        record = evidence_store.build_record(
            "wf",
            {**_EVIDENCE, "lines": [long_line]},
            {"templates": [{"id": f"t{i}", "pattern": "p", "count": i} for i in range(80)]},
        )
        self.assertIn("已截断", record["lines"][0])
        self.assertEqual(len(record["templates"]), evidence_store.MAX_TEMPLATES)

    def test_empty_evidence_is_valid_record(self) -> None:
        record = evidence_store.build_record(
            "wf", {"loki_available": False, "lines": []}, None
        )
        self.assertEqual(record["stored_lines"], 0)
        self.assertFalse(record["loki_available"])
        summary = evidence_store.summary_of(record)
        assert summary is not None
        self.assertEqual(summary["template_count"], 0)

    def test_requires_wf_id(self) -> None:
        with self.assertRaises(evidence_store.EvidenceStoreError):
            evidence_store.build_record("", _EVIDENCE, _CLUSTERED)


class _FileStoreCase(unittest.TestCase):
    """demo 文件后端：AIOPS_DATA_ROOT 指向临时目录，不污染真实 data/。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_get_roundtrip(self) -> None:
        summary = evidence_store.save("aiops-fix-nl2sql-a-1", _EVIDENCE, _CLUSTERED)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["stored_lines"], 2)
        self.assertEqual(summary["template_count"], 2)
        record = evidence_store.get("aiops-fix-nl2sql-a-1")
        assert record is not None
        self.assertEqual(record["service"], "nl2sql")
        self.assertEqual(record["lines"][0], _EVIDENCE["lines"][0])
        self.assertEqual(record["templates"][0]["count"], 8)
        self.assertTrue(record["loki_available"])

    def test_rearchive_overwrites(self) -> None:
        evidence_store.save("wf-1", _EVIDENCE, _CLUSTERED)
        evidence_store.save("wf-1", {**_EVIDENCE, "lines": ["only-one"]}, None)
        record = evidence_store.get("wf-1")
        assert record is not None
        self.assertEqual(record["lines"], ["only-one"])
        self.assertEqual(record["templates"], [])

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(evidence_store.get("wf-not-exist"))
        self.assertIsNone(evidence_store.summary("wf-not-exist"))
        self.assertIsNone(evidence_store.get(""))

    def test_path_sanitized(self) -> None:
        path = evidence_store.evidence_path("../../etc/passwd")
        self.assertEqual(path.parent, evidence_store.evidence_dir())
        self.assertNotIn("/", path.name)

    def test_summaries_for_batch(self) -> None:
        evidence_store.save("wf-a", _EVIDENCE, _CLUSTERED)
        evidence_store.save("wf-b", _EVIDENCE, _CLUSTERED)
        summaries = evidence_store.summaries_for(["wf-a", "wf-b", "wf-missing", "wf-a", ""])
        self.assertEqual(set(summaries), {"wf-a", "wf-b"})


class _DbBackedTest(unittest.TestCase):
    """production 分支：SQLite 临时库，验证与文件后端接口同构。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        url = f"sqlite:///{Path(self._tmp.name) / 'evidence.db'}"
        patcher = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": url}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        db_layer.reset_engine()
        self.addCleanup(db_layer.reset_engine)

    def test_db_roundtrip_and_overwrite(self) -> None:
        evidence_store.save("aiops-fix-order-a-1", _EVIDENCE, _CLUSTERED)
        record = evidence_store.get("aiops-fix-order-a-1")
        assert record is not None
        self.assertEqual(record["stored_lines"], 2)
        self.assertEqual(record["trace_ids"], ["trace-0001"])
        self.assertTrue(record["collected_at"].startswith("2026-10-16T08:00:00"))

        evidence_store.save(
            "aiops-fix-order-a-1", {**_EVIDENCE, "lines": ["rewritten"]}, _CLUSTERED
        )
        updated = evidence_store.get("aiops-fix-order-a-1")
        assert updated is not None
        self.assertEqual(updated["lines"], ["rewritten"])


class _StoredEvidenceCase(unittest.TestCase):
    """路由层：证据读取接口与转人工列表摘要。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        evidence_store.save("aiops-fix-nl2sql-a-1", _EVIDENCE, _CLUSTERED)

    def test_flow_evidence_ok(self) -> None:
        payload = asyncio.run(flows_routes.api_flow_evidence("aiops-fix-nl2sql-a-1"))
        self.assertEqual(payload["evidence"]["lines"], _EVIDENCE["lines"])
        self.assertIn("server_time", payload)

    def test_flow_evidence_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(flows_routes.api_flow_evidence("wf-not-exist"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("未归档日志证据", ctx.exception.message)

    def test_escalations_decorate_with_evidence(self) -> None:
        entry = {
            "id": "esc-aiops-fix-nl2sql-a-1",
            "wf_id": "aiops-fix-nl2sql-a-1",
            "alert_id": "a-1",
            "service": "nl2sql",
            "auto_reason": "闸门1：根因置信度不足",
            "status": "open",
            "escalated_at": "2026-10-16T08:00:00+00:00",
        }
        decorated = esc_routes._decorate(entry)
        self.assertEqual(decorated["evidence"]["stored_lines"], 2)
        self.assertEqual(decorated["evidence"]["template_count"], 2)

    def test_escalations_list_attaches_evidence(self) -> None:
        entry = {
            "id": "esc-aiops-fix-nl2sql-a-1",
            "wf_id": "aiops-fix-nl2sql-a-1",
            "alert_id": "a-1",
            "service": "nl2sql",
            "auto_reason": "闸门1",
            "status": "open",
            "escalated_at": "2026-10-16T08:00:00+00:00",
        }
        with mock.patch.object(
            esc_routes.gw, "list_flows", new=mock.AsyncMock(return_value=[])
        ), mock.patch.object(esc_routes.escalations, "list_all", return_value=[entry]), mock.patch.object(
            esc_routes.escalations, "stats", return_value={"total": 1, "open": 1}
        ), mock.patch.object(esc_routes.escalations, "sync_from_flows", return_value=0):
            payload = asyncio.run(esc_routes.api_escalations("all"))
        self.assertEqual(payload["escalations"][0]["evidence"]["stored_lines"], 2)

    def test_escalations_list_without_evidence(self) -> None:
        entry = {
            "id": "esc-wf-none",
            "wf_id": "wf-none",
            "alert_id": "req-1",
            "service": "nl2sql",
            "auto_reason": "需求来源",
            "status": "open",
            "escalated_at": "2026-10-16T08:00:00+00:00",
        }
        with mock.patch.object(
            esc_routes.gw, "list_flows", new=mock.AsyncMock(return_value=[])
        ), mock.patch.object(esc_routes.escalations, "list_all", return_value=[entry]), mock.patch.object(
            esc_routes.escalations, "stats", return_value={"total": 1, "open": 1}
        ), mock.patch.object(esc_routes.escalations, "sync_from_flows", return_value=0):
            payload = asyncio.run(esc_routes.api_escalations("all"))
        self.assertIsNone(payload["escalations"][0]["evidence"])


class ArchiveActivityTest(unittest.TestCase):
    """归档活动：显式 wf_id 与 activity 上下文缺省取值。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_wf_id(self) -> None:
        from aiops_agent import activities

        summary = asyncio.run(
            activities.archive_evidence(_EVIDENCE, _CLUSTERED, "aiops-fix-x-1")
        )
        self.assertEqual(summary["stored_lines"], 2)
        self.assertIsNotNone(evidence_store.get("aiops-fix-x-1"))

    def test_falls_back_to_activity_context(self) -> None:
        from aiops_agent import activities

        info = mock.Mock(workflow_id="aiops-fix-ctx-1")
        with mock.patch.object(activities.activity, "info", return_value=info):
            asyncio.run(activities.archive_evidence(_EVIDENCE, _CLUSTERED))
        self.assertIsNotNone(evidence_store.get("aiops-fix-ctx-1"))


if __name__ == "__main__":
    unittest.main()
