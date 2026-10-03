"""BFF 层单元测试：前置校验 / 项归一化 / 聚合 / 操作审计（不依赖运行中的 Temporal）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bff import aggregator, audit
from bff.temporal_gateway import _item_from_result, _item_from_status, check_stage_allowed


class StageGuardTest(unittest.TestCase):
    def test_allowed(self):
        self.assertIsNone(check_stage_allowed("WAIT_APPROVAL", {"WAIT_APPROVAL"}))

    def test_blocked(self):
        message = check_stage_allowed("NOTIFYING", {"WAIT_APPROVAL"})
        self.assertIsNotNone(message)
        self.assertIn("NOTIFYING", message)
        self.assertIn("WAIT_APPROVAL", message)

    def test_unknown_stage(self):
        message = check_stage_allowed(None, {"NOTIFYING"})
        self.assertIn("未知", message)


class ItemNormalizeTest(unittest.TestCase):
    def test_from_status(self):
        base: dict = {}
        _item_from_status(
            base,
            {
                "stage": "WAIT_APPROVAL",
                "alert": {"alert_id": "a-1", "service": "order"},
                "deadline": {"kind": "approval", "at": "2026-10-01T14:00:00+00:00"},
                "needs_second": True,
                "approval": None,
                "second_approval": None,
                "deploy_command": None,
                "queued_patches": [{"workflow_id": "w-2", "alert_id": "a-2"}],
            },
        )
        self.assertEqual(base["stage"], "WAIT_APPROVAL")
        self.assertTrue(base["needs_second"])
        self.assertEqual(base["deadline"]["kind"], "approval")
        self.assertEqual(base["queued_patches"][0]["alert_id"], "a-2")

    def test_from_result(self):
        base: dict = {}
        _item_from_result(
            base,
            {
                "stage": "DONE",
                "alert_id": "a-1",
                "service": "order",
                "duration_seconds": 12.5,
                "confidence": 0.9,
                "model_version": "qwen3:8b",
                "patch_id": "p-1",
                "queued_patches": ["a-9"],
            },
        )
        self.assertEqual(base["alert"], {"alert_id": "a-1", "service": "order"})
        self.assertEqual(base["queued_patches"], [{"workflow_id": None, "alert_id": "a-9"}])
        self.assertEqual(base["confidence"], 0.9)
        self.assertEqual(base["model_version"], "qwen3:8b")


class AuditTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="tester", action="approval:approve", wf_id="aiops-fix-order-a-1")
                audit.write_audit(actor="tester", action="deploy-command:cancel", wf_id="aiops-fix-order-a-1")
                rows = audit.read_audit()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "deploy-command:cancel")  # 最近的在最前
        self.assertEqual(rows[0]["actor"], "tester")
        self.assertEqual(rows[0]["result"], "signaled")

    def test_read_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                self.assertEqual(audit.read_audit(), [])

    def test_write_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="tester", action="x", wf_id="w")
                self.assertTrue((Path(tmp) / "web-audit").is_dir())


class AggregatorTest(unittest.TestCase):
    def test_index_stats_shape(self):
        stats = aggregator.index_stats()
        if stats is None:
            self.skipTest("data/code_index.json 不存在")
        self.assertEqual(stats["dim"], 1024)
        self.assertGreater(stats["chunks"], 0)
        self.assertIn("backend", stats)

    def test_artifacts_for_patch_structure(self):
        result = aggregator.artifacts_for_patch("p-nonexistent-0")
        self.assertEqual(result["notify"], [])
        self.assertEqual(result["argocd"], [])
        self.assertIsNone(result["sandbox_dir"])

    def test_recent_releases(self):
        releases = aggregator.recent_releases(3)
        if not releases:
            self.skipTest("无 argocd 留痕")
        self.assertLessEqual(len(releases), 3)
        self.assertIn("version", releases[0])

    def test_probe_stable_down(self):
        result = aggregator.probe_stable("1", timeout=0.3)  # 端口 1 无服务
        self.assertFalse(result["running"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
