"""审核知识回流单测（M2）：清洗 / 入索引判定 / 导出原子写 / Code RAG 合并检索。

验收要点：
- 只有「已关闭 + 管理员 APPROVED + RESOLVED」的记录进入导出语料；
  待审核、已拒绝、撤销、未解决、旧格式（无 resolution）一律不入索引；
- 导出前清洗凭据 / 内部地址 / 个人信息；清洗后仍命中敏感模式则整条跳过；
- 导出只写文件，索引重建是显式动作（build_index），未批准记录检索不到。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import code_rag, knowledge
from bff import escalations as esc


def _flow(wf_id: str = "aiops-fix-coupon-a1", service: str = "coupon") -> dict:
    return {
        "wf_id": wf_id,
        "stage": "ESCALATED",
        "gate_events": ["gate1:blocked:confidence=0.42<0.8"],
        "alert": {
            "alert_id": "a1",
            "service": service,
            "severity": "critical",
            "description": "优惠券核销失败率升高",
        },
    }


def _entry(
    *,
    wf_id: str = "aiops-fix-coupon-a1",
    decision: str | None = None,
    close_reason: str = "RESOLVED",
    actual_action: str = "调大连接池上限并滚动重启",
) -> dict:
    """构造一条结构化关闭的待办（默认已审核通过，可直接入知识库）。"""
    entry = esc.new_entry(_flow(wf_id))
    payload = {
        "note": "已扩容连接池并观察 30 分钟",
        "close_reason": close_reason,
        "actual_root_cause": "数据库连接池耗尽导致核销请求超时",
        "actual_action": actual_action,
        "verification": "错误率回落至 0.1% 以下，持续 30 分钟",
        "verdict": "CORRECT",
        "knowledge_candidate": True,
        "sensitive_confirmed": True,
    }
    resolution = esc.build_resolution(payload, "alice")
    esc.apply_close(entry, "alice", "", resolution)
    if decision:
        esc.apply_knowledge_review(entry, decision, "admin")
    return entry


class SanitizeTest(unittest.TestCase):
    def test_masks_credentials_and_personal_data(self) -> None:
        text = (
            "连接串 password=hunter2，"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789，"
            "内网 10.20.30.40:3306，联系 ops@example.com / 13800138000"
        )
        cleaned = knowledge.sanitize(text)
        for leaked in ("hunter2", "abcdefghijklmnopqrstuvwxyz0123456789", "10.20.30.40", "ops@example.com", "13800138000"):
            self.assertNotIn(leaked, cleaned)
        self.assertIn("password=[REDACTED]", cleaned)
        self.assertIn("[内部地址]", cleaned)
        self.assertIn("[邮箱]", cleaned)
        self.assertIn("[手机号]", cleaned)

    def test_sanitize_is_idempotent(self) -> None:
        once = knowledge.sanitize("token=abc123 内网 192.168.1.10")
        self.assertEqual(knowledge.sanitize(once), once)

    def test_contains_sensitive_ignores_placeholders(self) -> None:
        self.assertTrue(knowledge.contains_sensitive("token=abc123"))
        self.assertFalse(knowledge.contains_sensitive(knowledge.sanitize("token=abc123")))
        self.assertFalse(knowledge.contains_sensitive("根因：连接池耗尽"))


class CollectTest(unittest.TestCase):
    def test_only_approved_resolved_records_are_collected(self) -> None:
        records, skipped = knowledge.collect(
            [
                _entry(wf_id="wf-draft"),
                _entry(wf_id="wf-approved", decision="approve"),
            ]
        )
        self.assertEqual([r["ticket_id"] for r in records], ["esc-wf-approved"])
        self.assertEqual(skipped, 0)

    def test_revoked_and_rejected_records_disappear(self) -> None:
        approved = _entry(wf_id="wf-a", decision="approve")
        self.assertEqual(len(knowledge.collect([approved])[0]), 1)
        esc.apply_knowledge_review(approved, "revoke", "admin", "根因不完整")
        records, skipped = knowledge.collect([approved])
        self.assertEqual(records, [])
        self.assertEqual(skipped, 0)  # 非 APPROVED 不计入「清洗跳过」

    def test_unresolved_approved_record_is_skipped(self) -> None:
        """防御分支：未解决记录即使被标为 APPROVED（历史/异常状态）也不得导出。"""
        entry = _entry(wf_id="wf-dup", close_reason="DUPLICATE")
        entry["resolution"]["knowledge_status"] = "APPROVED"
        records, skipped = knowledge.collect([entry])
        self.assertEqual(records, [])
        self.assertEqual(skipped, 1)

    def test_legacy_close_has_no_record(self) -> None:
        entry = esc.new_entry(_flow("wf-legacy"))
        esc.apply_close(entry, "alice", "手动处理")
        self.assertEqual(knowledge.collect([entry]), ([], 0))
        self.assertIsNone(knowledge.record_from_entry(entry))

    def test_record_keeps_source_trace(self) -> None:
        record = knowledge.record_from_entry(_entry(decision="approve"))
        self.assertEqual(record["ticket_id"], "esc-aiops-fix-coupon-a1")
        self.assertEqual(record["source_wf_id"], "aiops-fix-coupon-a1")
        self.assertEqual(record["service"], "coupon")
        self.assertEqual(record["source_kind"], "alert")
        self.assertIn("连接池", record["root_cause"])
        self.assertTrue(record["closed_at"])

    def test_sensitive_values_are_masked_in_record(self) -> None:
        record = knowledge.record_from_entry(
            _entry(decision="approve", actual_action="用 password=hunter2 重启连接池")
        )
        self.assertIsNotNone(record)
        self.assertNotIn("hunter2", record["resolution"])
        self.assertIn("[REDACTED]", record["resolution"])

    def test_residual_sensitive_blocks_export(self) -> None:
        """防御分支：清洗后仍命中敏感模式 → 整条跳过（宁可缺知识也不带密钥）。"""
        entry = _entry(decision="approve")
        with mock.patch.object(knowledge, "contains_sensitive", return_value=True):
            self.assertIsNone(knowledge.record_from_entry(entry))
            self.assertEqual(knowledge.collect([entry]), ([], 1))


class ExportFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "knowledge_records.json"

    def test_write_and_load_roundtrip(self) -> None:
        entries = [_entry(wf_id="wf-a", decision="approve")]
        report = knowledge.write_export(entries, self.path)
        self.assertEqual((report["exported"], report["skipped"]), (1, 0))
        self.assertEqual(report["path"], str(self.path))
        self.assertEqual(list(self.path.parent.glob(".knowledge-*")), [])  # 原子写无残留

        loaded = knowledge.load(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["ticket_id"], "esc-wf-a")

    def test_empty_export_and_corrupt_file(self) -> None:
        knowledge.write_export([_entry(wf_id="wf-draft")], self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [])
        self.path.write_text("{not-json", encoding="utf-8")
        self.assertEqual(knowledge.load(self.path), [])
        self.assertEqual(knowledge.load(Path(self._tmp.name) / "missing.json"), [])

    def test_export_path_env_override(self) -> None:
        with mock.patch.dict(os.environ, {knowledge.ENV_PATH: str(self.path)}):
            self.assertEqual(knowledge.export_path(), self.path)


class CodeRagMergeTest(unittest.TestCase):
    """索引合并：已审核知识以 kind=ticket 进入索引，未审核的检索不到。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = root / "app"
        self.repo.mkdir()
        (self.repo / "coupon.py").write_text(
            "def redeem(coupon):\n    return coupon\n", encoding="utf-8"
        )
        self.index = root / "code_index.json"
        self.knowledge = root / "knowledge_records.json"
        patcher = mock.patch.dict(os.environ, {"AIOPS_VECTOR_BACKEND": "python"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _build(self, entries: list[dict], service: str | None = None) -> dict:
        knowledge.write_export(entries, self.knowledge)
        embed = mock.patch.object(
            code_rag, "embed_texts", side_effect=lambda texts, model=None: [[1.0, 0.0] for _ in texts]
        )
        embed.start()
        self.addCleanup(embed.stop)
        return code_rag.build_index(
            self.repo,
            index_path=self.index,
            model="test-model",
            tickets_path=Path(self._tmp.name) / "no-tickets.json",
            knowledge_path=self.knowledge,
            knowledge_service=service,
        )

    def test_approved_record_is_merged_into_index(self) -> None:
        stats = self._build([_entry(decision="approve")])
        self.assertEqual(stats["knowledge"], 1)
        self.assertEqual(stats["files"], 1)
        index = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(index["knowledge"], 1)
        knowledge_chunks = [
            c for c in index["chunks"] if c["file"] == "data/knowledge_records.json"
        ]
        self.assertEqual(len(knowledge_chunks), 1)
        self.assertEqual(knowledge_chunks[0]["kind"], "ticket")
        self.assertIn("连接池", knowledge_chunks[0]["snippet"])
        self.assertEqual(knowledge_chunks[0]["qualname"], "esc-aiops-fix-coupon-a1")

    def test_unapproved_record_is_not_indexed(self) -> None:
        stats = self._build([_entry(wf_id="wf-draft")])
        self.assertEqual(stats["knowledge"], 0)
        index = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertFalse(
            [c for c in index["chunks"] if c["file"] == "data/knowledge_records.json"]
        )

    def test_app_index_filters_knowledge_by_service(self) -> None:
        """被监控应用专属索引只合并本服务知识，避免跨服务串入。"""
        self.assertEqual(self._build([_entry(decision="approve")], service="other")["knowledge"], 0)
        self.assertEqual(self._build([_entry(decision="approve")], service="coupon")["knowledge"], 1)

    def test_retrieval_finds_knowledge_chunk(self) -> None:
        self._build([_entry(decision="approve")])
        with mock.patch.object(
            code_rag, "embed_texts", side_effect=lambda texts, model=None: [[1.0, 0.0] for _ in texts]
        ):
            hits = code_rag.search_index(
                "优惠券核销超时", index_path=self.index, top_k=5, model="test-model"
            )
        files = {hit["file"] for hit in hits}
        self.assertIn("data/knowledge_records.json", files)


if __name__ == "__main__":
    unittest.main()
