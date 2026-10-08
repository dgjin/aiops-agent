"""需求分析闭环存储单测（bff/requirement_analyses）：状态机 / 双模读写 / 执行线程体。

状态机（管理员视角闭环）：
    new → analyzing → analyzed →（反馈 / 重试 → analyzing → analyzed）→ approved（终态）
失败路径：LLM 兜底降级 / 进程中断 → failed（可反馈或重试）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bff import requirement_analyses as store

_SNAPSHOT = {
    "id": 7,
    "kind": "REQUIREMENT",
    "title": "首页增加需求反馈入口",
    "content": "希望在首页顶端增加需求反馈入口链接",
    "priority": "P1",
}

_ANALYSIS = {
    "understanding": "需要在首页顶部新增需求反馈入口。",
    "plan": ["步骤1"],
    "suspect_files": ["demo-app/http_server.py"],
    "acceptance": ["要点1"],
    "risk": "低",
    "complexity": "低",
    "confidence": 0.9,
    "degraded": False,
}

_META = {"model": "m1", "degraded": False, "reason": "", "elapsed_seconds": 1.0}


class StateMachineTest(unittest.TestCase):
    """纯函数状态机（不触盘）。"""

    def _entry(self) -> dict:
        return store.new_entry("app-a", "svc-a", _SNAPSHOT, "admin1")

    def test_new_entry_starts_v1_running(self) -> None:
        entry = self._entry()
        self.assertEqual(entry["id"], "ra-app-a-7")
        self.assertEqual(entry["status"], "analyzing")
        self.assertEqual(entry["current_version"], 1)
        self.assertEqual(entry["versions"][0]["status"], "running")
        self.assertEqual(entry["versions"][0]["trigger"], "initial")

    def test_feedback_appends_version(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        self.assertEqual(entry["status"], "analyzed")
        store.apply_feedback(entry, "请补充验收要点", "admin1")
        self.assertEqual(entry["status"], "analyzing")
        self.assertEqual(entry["current_version"], 2)
        self.assertEqual(entry["versions"][1]["trigger"], "feedback")
        self.assertEqual(entry["versions"][1]["feedback"], "请补充验收要点")

    def test_feedback_requires_text(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_feedback(entry, "   ", "admin1")

    def test_feedback_rejected_while_analyzing(self) -> None:
        entry = self._entry()
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_feedback(entry, "再来一遍", "admin1")

    def test_feedback_rejected_after_approved(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.apply_approved(entry, 1, "aiops-req-ra-app-a-7-v1", "admin1")
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_feedback(entry, "改一下", "admin1")

    def test_degraded_marks_failed(self) -> None:
        entry = self._entry()
        degraded = dict(_ANALYSIS, degraded=True, confidence=0.0)
        store.complete_version(
            entry, 1, degraded, {"degraded": True, "reason": "boom", "elapsed_seconds": 0.1}
        )
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["versions"][0]["status"], "failed")
        self.assertEqual(entry["versions"][0]["error"], "boom")

    def test_stale_version_writeback_does_not_flip_status(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.apply_feedback(entry, "再改", "admin1")  # v2 running
        # v1 迟到写回：不得把顶层状态从 analyzing 翻回 analyzed
        store.complete_version(entry, 1, _ANALYSIS, _META)
        self.assertEqual(entry["status"], "analyzing")

    def test_latest_analysis_inherits_on_running(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.apply_feedback(entry, "再改", "admin1")
        self.assertEqual(store.latest_analysis(entry)["confidence"], 0.9)

    def test_approve_requires_analyzed_and_valid(self) -> None:
        entry = self._entry()
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_approved(entry, 1, "wf", "admin1")  # analyzing 不允许
        store.complete_version(
            entry,
            1,
            dict(_ANALYSIS, degraded=True, confidence=0.0),
            {"degraded": True, "reason": "x", "elapsed_seconds": 0.1},
        )
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_approved(entry, 1, "wf", "admin1")  # degraded 不允许
        store.apply_retry(entry, "admin1")
        store.complete_version(entry, 2, _ANALYSIS, _META)
        store.apply_approved(entry, 2, "wf-1", "admin1")
        self.assertEqual(entry["status"], "approved")
        self.assertEqual(entry["approved"]["wf_id"], "wf-1")
        self.assertEqual(entry["approved"]["version"], 2)

    def test_approve_twice_rejected(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.apply_approved(entry, 1, "wf-1", "admin1")
        with self.assertRaises(store.RequirementAnalysisError):
            store.apply_approved(entry, 1, "wf-2", "admin1")

    def test_maybe_mark_stale(self) -> None:
        entry = self._entry()
        # 未超时：不变
        self.assertFalse(store.maybe_mark_stale(entry, datetime.now(timezone.utc)))
        # 超时：转 failed（可重试）
        future = datetime.now(timezone.utc) + timedelta(
            seconds=store.DEFAULT_STALE_SECONDS + 10
        )
        self.assertTrue(store.maybe_mark_stale(entry, future))
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["versions"][0]["status"], "failed")

    def test_feedbacks_upto(self) -> None:
        entry = self._entry()
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.apply_feedback(entry, "反馈一", "a")
        store.complete_version(entry, 2, _ANALYSIS, _META)
        store.apply_feedback(entry, "反馈二", "a")
        feedbacks = store.feedbacks_upto(entry, 2)
        self.assertEqual([item["feedback"] for item in feedbacks], ["反馈一"])


class DemoFileStoreTest(unittest.TestCase):
    """demo 后端：文件读写 / create_or_touch 分派 / 执行线程体写回。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path_patcher = mock.patch.object(
            store, "DATA_PATH", Path(self._tmp.name) / "requirement_analyses.json"
        )
        path_patcher.start()
        self.addCleanup(path_patcher.stop)

        ref_patcher = mock.patch.object(store, "_code_references", return_value=[])
        ref_patcher.start()
        self.addCleanup(ref_patcher.stop)

    def test_create_or_touch_new_entry_persisted(self) -> None:
        entry, need_run = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        self.assertTrue(need_run)
        self.assertEqual(entry["status"], "analyzing")
        self.assertTrue(store.DATA_PATH.is_file())
        items = store.list_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "ra-app-a-7")

    def test_create_or_touch_existing_analyzed_returns_without_run(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.persist_entry(entry)
        again, need_run = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        self.assertFalse(need_run)
        self.assertEqual(again["status"], "analyzed")

    def test_create_or_touch_while_analyzing_raises(self) -> None:
        store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        with self.assertRaises(store.RequirementAnalysisError):
            store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")

    def test_create_or_touch_failed_retries(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        store.complete_version(
            entry,
            1,
            dict(_ANALYSIS, degraded=True, confidence=0.0),
            {"degraded": True, "reason": "x", "elapsed_seconds": 0.1},
        )
        store.persist_entry(entry)
        again, need_run = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        self.assertTrue(need_run)
        self.assertEqual(again["current_version"], 2)
        self.assertEqual(again["versions"][-1]["trigger"], "retry")

    def test_execute_analysis_writes_back(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        with mock.patch(
            "aiops_agent.requirement_agent.analyze_requirement",
            return_value=(dict(_ANALYSIS), dict(_META)),
        ):
            store.execute_analysis(entry["id"], 1)
        saved = store.get(entry["id"])
        self.assertEqual(saved["status"], "analyzed")
        self.assertEqual(saved["versions"][0]["status"], "done")
        self.assertEqual(saved["versions"][0]["analysis"]["confidence"], 0.9)

    def test_execute_analysis_swallows_errors(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        with mock.patch(
            "aiops_agent.requirement_agent.analyze_requirement",
            side_effect=RuntimeError("boom"),
        ):
            store.execute_analysis(entry["id"], 1)  # 线程体绝不抛
        saved = store.get(entry["id"])
        self.assertEqual(saved["status"], "analyzing")  # 保持 running，由读取路径自愈兜底

    def test_execute_analysis_skips_finished_version(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.persist_entry(entry)
        with mock.patch(
            "aiops_agent.requirement_agent.analyze_requirement"
        ) as analyze:
            store.execute_analysis(entry["id"], 1)  # 版本已 done：放弃本次执行
        analyze.assert_not_called()

    def test_summary_shape(self) -> None:
        entry, _ = store.create_or_touch("app-a", "svc-a", _SNAPSHOT, "admin1")
        store.complete_version(entry, 1, _ANALYSIS, _META)
        store.persist_entry(entry)
        summary = store.summary(store.get(entry["id"]))
        self.assertEqual(summary["id"], "ra-app-a-7")
        self.assertNotIn("versions", summary)
        self.assertEqual(summary["latest_confidence"], 0.9)
        self.assertFalse(summary["latest_degraded"])


if __name__ == "__main__":
    unittest.main()
