"""直连发布模式单测：补丁直连真实仓库 + 真实页面探针 + 劣化还原。

覆盖 release.run_canary_direct / release.run_finalize_direct：
    - 健康：补丁落盘生效且保持（=已发布）；
    - 劣化：探针失败 → 全部涉及文件还原为补丁前内容（=真实回滚）；
    - 目标文件缺失：抛错且不留半成品；
    - 多文件补丁：逐文件应用与还原。
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


def _patch(*, diff: str = DIFF) -> Patch:
    return Patch(
        patch_id="p-direct-u1-r0",
        alert_id="direct-u1",
        files=["index.html"],
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


if __name__ == "__main__":
    unittest.main()
