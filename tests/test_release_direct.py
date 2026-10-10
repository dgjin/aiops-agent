"""直连发布模式单测：补丁直连真实仓库 + 真实页面探针 + 劣化还原。

覆盖 release.run_canary_direct / release.run_finalize_direct：
    - 健康：补丁落盘生效且保持（=已发布）；
    - 劣化：探针失败 → 全部涉及文件还原为补丁前内容（=真实回滚）；
    - 目标文件缺失：抛错且不留半成品；
    - 多文件补丁：逐文件应用与还原；
    - 新增文件（--- /dev/null）：健康时创建；劣化/失败时删除（不留残留）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import release
from aiops_agent.models import Patch

FAIL_TEXT = '<div id="eeroot"></div>'
OK_TEXT = '<div id="root"></div>'
EXTRA_OLD = "alpha\nold value\n"

BROKEN_PAGE = (
    "<!doctype html>\n"
    "<html>\n"
    "  <body>\n"
    f"    {FAIL_TEXT}\n"
    "  </body>\n"
    "</html>\n"
)

DIFF = (
    "--- a/index.html\n"
    "+++ b/index.html\n"
    "@@ -1,6 +1,6 @@\n"
    " <!doctype html>\n"
    " <html>\n"
    "   <body>\n"
    f"-    {FAIL_TEXT}\n"
    f"+    {OK_TEXT}\n"
    "   </body>\n"
    " </html>\n"
)

MULTI_DIFF = DIFF + (
    "--- a/extra.txt\n"
    "+++ b/extra.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " alpha\n"
    "-old value\n"
    "+new value\n"
)

NEW_FILE_DIFF = (
    "--- /dev/null\n"
    "+++ b/userPersonal.ts\n"
    "@@ -0,0 +1,3 @@\n"
    "+export const displayName = 'x';\n"
    "+export const theme = 'dark';\n"
    "+export const unit = '元';\n"
)

MIXED_NEW_DIFF = DIFF + NEW_FILE_DIFF


def _patch(*, diff: str = DIFF, files: list[str] | None = None) -> Patch:
    return Patch(
        patch_id="p-direct-u1-r0",
        alert_id="direct-u1",
        files=files or ["index.html"],
        diff=diff,
        description="直连发布单测",
        risk="low",
        model_version="test-model",
        confidence=1.0,
    )


class TestRunCanaryDirect(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-direct-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.page = self.repo / "index.html"
        self.page.write_text(BROKEN_PAGE, encoding="utf-8")

    def _run(self, *, probe_ok: bool, patch: Patch | None = None):
        with mock.patch.object(release, "OBSERVE_INTERVAL_SECONDS", 0.05), mock.patch.object(
            release, "_page_probe_once", return_value=probe_ok
        ):
            return release.run_canary_direct(
                patch or _patch(),
                self.repo,
                "http://127.0.0.1:1/",
                OK_TEXT,
                5,
                observe_seconds=1,
            )

    def test_healthy_applies_patch_and_keeps(self) -> None:
        result = self._run(probe_ok=True)
        self.assertTrue(result.healthy, result.observation)
        self.assertEqual(result.mode, "direct")
        self.assertIn(OK_TEXT, self.page.read_text(encoding="utf-8"))

        final = release.run_finalize_direct(result, True)
        self.assertFalse(final.rolled_back)
        self.assertIn("直连发布达标", final.reason)
        self.assertRegex(final.version, r"^v1\.0\.\d{3}$")

    def test_degraded_restores_backup(self) -> None:
        result = self._run(probe_ok=False)
        self.assertFalse(result.healthy, result.observation)
        self.assertEqual(result.mode, "direct")
        restored = self.page.read_text(encoding="utf-8")
        self.assertIn(FAIL_TEXT, restored)
        self.assertNotIn(OK_TEXT, restored)

        final = release.run_finalize_direct(result, True)
        self.assertTrue(final.rolled_back)
        self.assertIn("已还原", final.reason)

    def test_missing_target_raises_without_side_effect(self) -> None:
        self.page.unlink()
        with self.assertRaises(RuntimeError) as ctx:
            self._run(probe_ok=True)
        self.assertIn("目标文件不存在", str(ctx.exception))

    def test_multi_file_healthy_applies_all(self) -> None:
        extra = self.repo / "extra.txt"
        extra.write_text(EXTRA_OLD, encoding="utf-8")
        result = self._run(probe_ok=True, patch=_patch(diff=MULTI_DIFF))
        self.assertTrue(result.healthy, result.observation)
        self.assertIn(OK_TEXT, self.page.read_text(encoding="utf-8"))
        self.assertIn("new value", extra.read_text(encoding="utf-8"))

    def test_multi_file_degraded_restores_all(self) -> None:
        extra = self.repo / "extra.txt"
        extra.write_text(EXTRA_OLD, encoding="utf-8")
        result = self._run(probe_ok=False, patch=_patch(diff=MULTI_DIFF))
        self.assertFalse(result.healthy, result.observation)
        self.assertIn(FAIL_TEXT, self.page.read_text(encoding="utf-8"))
        self.assertIn("old value", extra.read_text(encoding="utf-8"))
        self.assertNotIn("new value", extra.read_text(encoding="utf-8"))

    def test_new_file_healthy_creates_and_keeps(self) -> None:
        new_file = self.repo / "userPersonal.ts"
        self.assertFalse(new_file.exists())
        result = self._run(
            probe_ok=True, patch=_patch(diff=NEW_FILE_DIFF, files=["userPersonal.ts"])
        )
        self.assertTrue(result.healthy, result.observation)
        content = new_file.read_text(encoding="utf-8")
        self.assertIn("displayName", content)
        self.assertTrue(content.endswith("\n"))

    def test_new_file_degraded_removes_created(self) -> None:
        new_file = self.repo / "userPersonal.ts"
        result = self._run(
            probe_ok=False, patch=_patch(diff=NEW_FILE_DIFF, files=["userPersonal.ts"])
        )
        self.assertFalse(result.healthy, result.observation)
        self.assertFalse(new_file.exists(), "劣化回滚后新增文件不应残留")

    def test_mixed_existing_and_new_degraded_restores_both(self) -> None:
        new_file = self.repo / "userPersonal.ts"
        result = self._run(probe_ok=False, patch=_patch(diff=MIXED_NEW_DIFF))
        self.assertFalse(result.healthy, result.observation)
        self.assertIn(FAIL_TEXT, self.page.read_text(encoding="utf-8"))
        self.assertNotIn(OK_TEXT, self.page.read_text(encoding="utf-8"))
        self.assertFalse(new_file.exists(), "混合补丁劣化后新增文件不应残留")

    def test_mixed_existing_and_new_apply_failure_cleans_up(self) -> None:
        # 既有文件先写入成功、新增文件段不可应用（含需匹配的上下文行但空原文无从匹配）
        # → 抛错且既有文件回滚、无新文件残留
        broken_new = (
            "--- /dev/null\n"
            "+++ b/userPersonal.ts\n"
            "@@ -1,2 +1,3 @@\n"
            " pre-existing context\n"
            "+export const x = 1;\n"
        )
        new_file = self.repo / "userPersonal.ts"
        with self.assertRaises(RuntimeError):
            self._run(probe_ok=True, patch=_patch(diff=DIFF + broken_new))
        self.assertIn(FAIL_TEXT, self.page.read_text(encoding="utf-8"))
        self.assertFalse(new_file.exists())


if __name__ == "__main__":
    unittest.main()
