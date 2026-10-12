"""直连发布模式单测：补丁直连真实仓库 + 真实页面探针 + 劣化还原。

覆盖 release.run_canary_direct / release.run_finalize_direct：
    - 健康：补丁落盘生效且保持（=已发布）；
    - 劣化：探针失败 → 全部涉及文件还原为补丁前内容（=真实回滚）；
    - 目标文件缺失：抛错且不留半成品；
    - 多文件补丁：逐文件应用与还原；
    - 新增文件（--- /dev/null）：健康时创建；劣化/失败时删除（不留残留）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import activities, release
from aiops_agent.models import Alert, CanaryResult, Patch

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

    def _run(
        self,
        *,
        probe_ok: bool,
        patch: Patch | None = None,
        service: str = "",
        publisher=None,
    ):
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
                service=service,
                # 默认注入 mock：避免单测向真实 data/argocd/ 写发布留痕
                publisher=publisher or mock.MagicMock(),
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

    def test_records_argocd_manifest_for_console(self) -> None:
        """真实 publisher + 重定向留痕目录：发布成功后写 manifest（控制台「最近发布」数据源）。"""
        argocd_dir = self.tmp / "argocd"
        with mock.patch.object(release, "ARGOCD_DIR", argocd_dir):
            result = self._run(
                probe_ok=True, service="nl2sql", publisher=release.ArgoCDPublisher()
            )
        record = argocd_dir / f"demo-app-{result.patch_id}.json"
        self.assertTrue(record.is_file(), "直连发布应写 ArgoCD 留痕")
        raw = json.loads(record.read_text(encoding="utf-8"))
        labels = raw["metadata"]["labels"]
        self.assertEqual(labels["aiops.service"], "nl2sql")
        self.assertEqual(labels["aiops.patch-id"], result.patch_id)
        self.assertEqual(labels["aiops.alert-id"], "direct-u1")
        self.assertEqual(
            raw["metadata"]["annotations"]["aiops.version"],
            release.release_version(result.patch_id),
        )

    def test_apply_failure_writes_no_manifest(self) -> None:
        """补丁应用失败（发布未发生）：不写留痕。"""
        self.page.unlink()
        argocd_dir = self.tmp / "argocd"
        with mock.patch.object(release, "ARGOCD_DIR", argocd_dir):
            with self.assertRaises(RuntimeError):
                self._run(probe_ok=True, publisher=release.ArgoCDPublisher())
        self.assertFalse(argocd_dir.exists(), "发布未发生不应有留痕")

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


class TestDeployCanaryRouting(unittest.TestCase):
    """activities.deploy_canary 发布模式路由（2026-10-11）：url 非空即直连发布，关键词可空。

    背景：注册表关键词被清空后，原门控（contract and url）令直连应用错走容器金丝雀；
    现以 url 为直连标志——无关键词时页面探针走通用白屏判据（release._page_probe_once
    → aiops_agent.page_health），不再仅校验 HTTP 200。
    """

    def _ctx(self) -> tuple[Alert, Patch]:
        alert = Alert(alert_id="a-route", service="svc-route", description="路由单测")
        patch = Patch(
            patch_id="p-route-r0",
            alert_id="a-route",
            files=["server/config.ts"],
            diff=(
                "--- a/server/config.ts\n"
                "+++ b/server/config.ts\n"
                "@@ -1 +1 @@\n"
                "-export const c = 1;\n"
                "+export const c = 2;\n"
            ),
            description="路由单测",
            risk="低",
            model_version="test-model",
            confidence=1.0,
        )
        return alert, patch

    def test_url_without_keyword_routes_direct(self) -> None:
        """配置 url、未配置关键词（contract=None）→ 直连发布，keyword 参数为空串。"""
        entry = {
            "name": "TS 前端",
            "repo": Path("/tmp/ts-repo"),
            "url": "http://127.0.0.1:3000/",
            "contract": None,
        }
        alert, patch = self._ctx()
        direct = CanaryResult(
            patch_id="p-route-r0",
            traffic_percent=10,
            healthy=True,
            error_rate=0.0,
            p99_latency_ms=5.0,
            observation="直连发布健康",
            mode="direct",
        )
        with mock.patch("aiops_agent.app_registry.resolve", return_value=entry), mock.patch(
            "aiops_agent.release.run_canary_direct", return_value=direct
        ) as m_direct, mock.patch("aiops_agent.release.run_canary") as m_container:
            result = asyncio.run(activities.deploy_canary(alert, patch, 10, 1))
        m_container.assert_not_called()
        m_direct.assert_called_once()
        self.assertEqual(m_direct.call_args.args[3], "")  # (patch, repo, url, keyword, …)
        self.assertEqual(result.mode, "direct")

    def test_no_url_routes_container(self) -> None:
        """未配置 url → 保持容器金丝雀（即使配置了关键词）。"""
        entry = {
            "name": "容器应用",
            "repo": Path("/tmp/py-app"),
            "url": "",
            "contract": {"keyword": "OK"},
        }
        alert, patch = self._ctx()
        container = CanaryResult(
            patch_id="p-route-r0",
            traffic_percent=10,
            healthy=True,
            error_rate=0.0,
            p99_latency_ms=8.0,
            observation="金丝雀健康",
        )
        with mock.patch("aiops_agent.app_registry.resolve", return_value=entry), mock.patch(
            "aiops_agent.release.run_canary_direct"
        ) as m_direct, mock.patch(
            "aiops_agent.release.run_canary", return_value=container
        ) as m_container:
            result = asyncio.run(activities.deploy_canary(alert, patch, 10, 1))
        m_direct.assert_not_called()
        m_container.assert_called_once()
        self.assertEqual(result.mode, "container")


class PageProbeOnceTest(unittest.TestCase):
    """直连发布页面探针（release._page_probe_once）：空关键词走通用白屏判据，配置关键词精确校验。"""

    def _probe(self, body: bytes, *, keyword: str = "", status: int = 200) -> bool:
        with mock.patch("aiops_agent.release.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = status
            resp.read.return_value = body
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            return release._page_probe_once("http://127.0.0.1:1/", keyword)

    def test_normal_page_healthy_by_default(self) -> None:
        self.assertTrue(
            self._probe(b'<!doctype html><html><body><div id="root"></div></body></html>')
        )

    def test_blank_page_unhealthy_by_default(self) -> None:
        """空壳页面（通用判据白屏）→ 探针失败 → 金丝雀劣化回滚。"""
        self.assertFalse(self._probe(b"<html><body></body></html>"))

    def test_script_only_page_unhealthy_by_default(self) -> None:
        """挂载点缺失（body 只剩入口脚本）→ 探针失败。"""
        self.assertFalse(
            self._probe(b'<html><body><script src="/src/main.tsx"></script></body></html>')
        )

    def test_keyword_mode_exact(self) -> None:
        self.assertTrue(self._probe(b'<div id="root">', keyword='<div id="root"'))
        self.assertFalse(self._probe(b'<div id="rroot">', keyword='<div id="root"'))

    def test_non_200_unhealthy(self) -> None:
        self.assertFalse(
            self._probe(
                b'<!doctype html><html><body><div id="root"></div></body></html>', status=204
            )
        )

    def test_truncated_large_body_healthy_by_default(self) -> None:
        """大响应读取达上限（截断）→ 跳过结构判定（保守放行）。"""
        self.assertTrue(self._probe(b"<html><body>" + b"x" * (256 * 1024)))


if __name__ == "__main__":
    unittest.main()
