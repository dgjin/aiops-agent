"""M4 沙箱加固：非 root/drop caps/seccomp、体积与配额、执行计划、静态不冒充测试。

与 test_sandbox.py 的分工：那里覆盖既有隔离语义与真实容器集成，这里覆盖 M4 新增
的加固参数、有界执行、Node 执行计划与 verification_mode 分流（不启动真实 docker）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import metrics, mode, sandbox
from aiops_agent.models import Patch


def _patch(files: list[str], diff: str, *, risk: str = "低：单文件") -> Patch:
    return Patch(
        patch_id="m4-s1",
        alert_id="m4-a1",
        files=files,
        diff=diff,
        description="M4 沙箱单测",
        risk=risk,
        model_version="qwen2.5-coder:32b",
        confidence=0.9,
    )


def _new_file_diff(path: str) -> str:
    return (
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+export const answer = 42;\n"
    )


class LimitsTest(unittest.TestCase):
    def test_env_limits_and_invalid_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_MAX_STREAM_KB": "8"}, clear=False):
            self.assertEqual(sandbox.max_stream_bytes(), 8 * 1024)
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_MAX_STREAM_KB": "abc"}, clear=False):
            self.assertEqual(sandbox.max_stream_bytes(), sandbox.DEFAULT_MAX_STREAM_BYTES)
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_MAX_WORKSPACE_MB": "0"}, clear=False):
            self.assertEqual(
                sandbox.max_workspace_bytes(), sandbox.DEFAULT_MAX_WORKSPACE_MB * 1024 * 1024
            )

    def test_image_pin_required_in_production_only(self) -> None:
        self.assertTrue(sandbox.image_is_pinned("python:3.12@sha256:abc"))
        self.assertFalse(sandbox.image_is_pinned("python:3.12-slim"))
        with mock.patch.object(mode, "is_production", return_value=True):
            self.assertIn("@sha256:", sandbox.resolve_image_error("python:3.12-slim"))
            self.assertEqual(sandbox.resolve_image_error("python:3.12@sha256:abc"), "")
        with mock.patch.object(mode, "is_production", return_value=False):
            self.assertEqual(sandbox.resolve_image_error("python:3.12-slim"), "")

    def test_workspace_quota_error_reports_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"x" * 2048)
            self.assertEqual(sandbox.workspace_quota_error(root, limit=4096), "")
            error = sandbox.workspace_quota_error(root, limit=1024)
            self.assertIn("超过配额", error)
            self.assertIn("2048B", error)


class StepParsingTest(unittest.TestCase):
    def test_parse_step_output_marks_failed_step(self) -> None:
        text = "\n".join(
            [
                f"{sandbox._STEP_START} build",
                f"{sandbox._STEP_OK} build",
                f"{sandbox._STEP_START} test",
                "boom",
            ]
        )
        steps = sandbox.parse_step_output(text)
        self.assertEqual([step["name"] for step in steps], ["build", "test"])
        self.assertEqual(sandbox._failed_step(steps), "test")

    def test_parse_step_output_ignores_unmarked_noise(self) -> None:
        self.assertEqual(sandbox.parse_step_output("npm run build\nok"), [])

    def test_node_script_stops_on_failure(self) -> None:
        script = sandbox._node_script([("build", "npm run build"), ("test", "npm test")])
        self.assertTrue(script.startswith("set -eu"))
        self.assertIn(f"{sandbox._STEP_START}' 'build", script)
        self.assertIn(f"{sandbox._STEP_OK}' 'test", script)
        self.assertLess(script.index("build"), script.index("test"))


class ProjectPlanTest(unittest.TestCase):
    def _repo(self, tmp: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return root

    def test_python_tests_take_unittest_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, {"tests/test_ok.py": "import unittest\n"})
            plan = sandbox.project_plan(root)
        self.assertEqual(plan.kind, "python")
        self.assertEqual(plan.verification_mode, sandbox.MODE_UNIT_TEST)
        self.assertEqual(plan.image, sandbox.SANDBOX_IMAGE)
        self.assertFalse(plan.workspace_writable)

    def test_node_with_test_script_is_real_unit_test(self) -> None:
        manifest = {"scripts": {"build": "tsc", "test": "vitest run"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, {"package.json": json.dumps(manifest)})
            plan = sandbox.project_plan(root)
        self.assertEqual(plan.kind, "node")
        self.assertEqual(plan.verification_mode, sandbox.MODE_UNIT_TEST)
        self.assertEqual(plan.image, sandbox.NODE_SANDBOX_IMAGE)
        self.assertEqual(plan.steps, ["build", "test"])
        self.assertTrue(plan.workspace_writable)
        script = plan.command[-1]
        self.assertIn("npm run --silent build", script)
        self.assertIn("npm test --silent", script)
        self.assertNotIn("npm install", script)  # 依赖须预装，沙箱内禁止联网安装

    def test_node_build_only_is_static(self) -> None:
        manifest = {"scripts": {"build": "tsc", "test": "echo \"Error: no test specified\""}}
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, {"package.json": json.dumps(manifest)})
            plan = sandbox.project_plan(root)
        self.assertEqual(plan.kind, "node")
        self.assertEqual(plan.verification_mode, sandbox.MODE_STATIC_ONLY)
        self.assertEqual(plan.steps, ["build"])

    def test_tsconfig_without_scripts_runs_typecheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, {"tsconfig.json": "{}"})
            plan = sandbox.project_plan(root)
        self.assertEqual(plan.kind, "node")
        self.assertEqual(plan.steps, ["typecheck"])
        self.assertEqual(plan.verification_mode, sandbox.MODE_STATIC_ONLY)
        self.assertIn("npx --no-install tsc --noEmit", plan.command[-1])

    def test_contract_and_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp, {"README.md": "# x"})
            contract = sandbox.project_plan(root, {"component": "App"})
            static = sandbox.project_plan(root)
        self.assertEqual(contract.kind, "contract")
        self.assertEqual(contract.verification_mode, sandbox.MODE_CONTRACT_TEST)
        self.assertEqual(static.kind, "static")
        self.assertEqual(static.verification_mode, sandbox.MODE_STATIC_ONLY)
        self.assertIn("未发现 Python 测试套件", static.static_reason)


class DockerHardeningTest(unittest.TestCase):
    def test_hardening_flags_present(self) -> None:
        job = sandbox.SandboxJob(patch_id="m4-s1", workspace=Path("/tmp/ws"))
        cmd = sandbox.DockerSandboxRunner()._build_cmd(job)
        self.assertEqual(cmd[cmd.index("--user") + 1], "65534:65534")
        self.assertEqual(cmd[cmd.index("--cap-drop") + 1], "ALL")
        self.assertEqual(cmd[cmd.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(cmd[cmd.index("--name") + 1], "aiops-sandbox-m4-s1")
        self.assertIn("aiops.patch-id=m4-s1", cmd)
        self.assertIn("--tmpfs", cmd)
        self.assertTrue(any(str(part).endswith(":/work:ro") for part in cmd))
        self.assertIn("npm_config_offline=true", cmd)

    def test_node_workspace_writable_and_seccomp_profile(self) -> None:
        job = sandbox.SandboxJob(
            patch_id="m4-s2",
            workspace=Path("/tmp/ws"),
            verification_mode=sandbox.MODE_STATIC_ONLY,
            workspace_writable=True,
        )
        with mock.patch.object(sandbox, "_SECCOMP_PROFILE", "/etc/seccomp/sandbox.json"):
            cmd = sandbox.DockerSandboxRunner()._build_cmd(job)
        self.assertTrue(any(str(part).endswith(":/work:rw") for part in cmd))
        self.assertIn("seccomp=/etc/seccomp/sandbox.json", cmd)

    def test_output_truncation_is_reported(self) -> None:
        job = sandbox.SandboxJob(patch_id="m4-s3", workspace=Path("/tmp/ws"))
        result = sandbox._ExecResult(
            exit_code=0,
            stdout="Ran 2 tests in 0.001s\n\nOK\n",
            stderr="",
            truncated=True,
        )
        with mock.patch.object(sandbox, "_execute", return_value=result):
            outcome = sandbox.DockerSandboxRunner().run(job)
        self.assertTrue(outcome.output_truncated)
        report = sandbox._report_from_outcome(_patch(["a.py"], ""), 0, outcome, "SAST 通过")
        self.assertIn("输出超限已截断", report.details)

    def test_reclaim_failure_marks_cleanup_unverified(self) -> None:
        job = sandbox.SandboxJob(patch_id="m4-s4", workspace=Path("/tmp/ws"), timeout_seconds=1)
        timed_out = sandbox._ExecResult(-1, "", "", timed_out=True)
        still_there = sandbox._ExecResult(0, "deadbeef\n", "")
        with mock.patch.object(sandbox, "_execute", side_effect=[still_there, timed_out, still_there, still_there]):
            outcome = sandbox.DockerSandboxRunner().run(job)
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.passed)
        self.assertFalse(outcome.cleanup_verified)
        self.assertIn("回收失败", outcome.error_summary)

    def test_default_runner_follows_runtime_env(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_RUNTIME": "k8s"}, clear=False):
            self.assertIsInstance(sandbox.default_runner(), sandbox.K8sJobSandboxRunner)
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_RUNTIME": "docker"}, clear=False):
            self.assertIsInstance(sandbox.default_runner(), sandbox.DockerSandboxRunner)


class StaticModeMetricsTest(unittest.TestCase):
    def _static_outcome(self, **overrides) -> sandbox.SandboxOutcome:
        payload = dict(
            exit_code=0,
            passed=True,
            tests_run=0,
            duration_seconds=1.0,
            runner="docker-container",
            image="node:22-bookworm-slim",
            verification_mode=sandbox.MODE_STATIC_ONLY,
            steps=[{"name": "build", "ok": True}],
        )
        payload.update(overrides)
        return sandbox.SandboxOutcome(**payload)

    def test_static_mode_never_counts_as_real_test(self) -> None:
        with mock.patch.object(metrics, "observe_sandbox_static") as static, mock.patch.object(
            metrics, "observe_sandbox_run"
        ) as real:
            report = sandbox._report_from_outcome(
                _patch(["src/app.ts"], ""), 0,
                self._static_outcome(verification_mode=sandbox.MODE_CONTRACT_TEST), "SAST 通过",
            )
        real.assert_not_called()
        static.assert_called_once_with(True, sandbox.MODE_CONTRACT_TEST)
        self.assertEqual(report.verification_mode, sandbox.MODE_CONTRACT_TEST)
        self.assertTrue(report.passed)
        self.assertIn("静态验收通过", report.unit_tests)

    def test_unit_test_mode_counts_as_real(self) -> None:
        with mock.patch.object(metrics, "observe_sandbox_static") as static, mock.patch.object(
            metrics, "observe_sandbox_run"
        ) as real:
            report = sandbox._report_from_outcome(
                _patch(["a.py"], ""), 0,
                self._static_outcome(
                    tests_run=5, steps=[], verification_mode=sandbox.MODE_UNIT_TEST
                ), "SAST 通过",
            )
        static.assert_not_called()
        real.assert_called_once()
        self.assertEqual(report.unit_tests, "5/5 passed")
        self.assertEqual(report.verification_mode, sandbox.MODE_UNIT_TEST)

    def test_empty_mode_from_archived_report_is_not_real(self) -> None:
        """M4 前的历史报告没有 verification_mode，不得计入真实通过率。"""
        with mock.patch.object(metrics, "observe_sandbox_static") as static, mock.patch.object(
            metrics, "observe_sandbox_run"
        ) as real:
            report = sandbox._report_from_outcome(
                _patch(["a.py"], ""), 0, self._static_outcome(verification_mode=""), "SAST 通过"
            )
        real.assert_not_called()
        static.assert_called_once_with(True, "UNKNOWN")
        self.assertEqual(report.verification_mode, "UNKNOWN")  # 空值不冒充真实测试
        self.assertIn("静态验收通过", report.unit_tests)


class RunPatchTestsNodeTest(unittest.TestCase):
    def test_node_build_plan_runs_runner_and_static_metrics(self) -> None:
        manifest = {"scripts": {"build": "tsc"}}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            sandbox_root = Path(tmp) / "sandbox"
            runner = mock.Mock()
            runner.run.return_value = sandbox.SandboxOutcome(
                exit_code=0,
                passed=True,
                tests_run=0,
                duration_seconds=2.0,
                runner="k8s-job-gvisor",
                image=sandbox.NODE_SANDBOX_IMAGE,
                verification_mode=sandbox.MODE_STATIC_ONLY,
                steps=[{"name": "build", "ok": True}],
            )
            with mock.patch.object(metrics, "observe_sandbox_static") as static, mock.patch.object(
                metrics, "observe_sandbox_run"
            ) as real:
                report = sandbox.run_patch_tests(
                    _patch(["src/app.ts"], _new_file_diff("src/app.ts")),
                    0,
                    repo_dir=repo,
                    sandbox_root=sandbox_root,
                    runner=runner,
                )
        static.assert_called_once()
        real.assert_not_called()
        job = runner.run.call_args[0][0]
        self.assertEqual(job.verification_mode, sandbox.MODE_STATIC_ONLY)
        self.assertEqual(job.steps, ["build"])
        self.assertEqual(job.image, sandbox.NODE_SANDBOX_IMAGE)
        self.assertTrue(job.workspace_writable)
        self.assertTrue(report.passed)
        self.assertEqual(report.verification_mode, sandbox.MODE_STATIC_ONLY)

    def test_workspace_quota_blocks_before_runner(self) -> None:
        manifest = {"scripts": {"build": "tsc"}}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            runner = mock.Mock()
            with mock.patch.object(sandbox, "max_workspace_bytes", return_value=1):
                report = sandbox.run_patch_tests(
                    _patch(["src/app.ts"], _new_file_diff("src/app.ts")),
                    0,
                    repo_dir=repo,
                    sandbox_root=Path(tmp) / "sandbox",
                    runner=runner,
                )
        runner.run.assert_not_called()
        self.assertFalse(report.passed)
        self.assertIn("配额", report.unit_tests)

    def test_node_artifact_growth_is_capped(self) -> None:
        manifest = {"scripts": {"build": "tsc"}}

        def _grow(job: sandbox.SandboxJob) -> sandbox.SandboxOutcome:
            (job.workspace / "dist").mkdir(exist_ok=True)
            (job.workspace / "dist" / "bundle.js").write_bytes(b"x" * 4096)
            return sandbox.SandboxOutcome(
                exit_code=0,
                passed=True,
                tests_run=0,
                duration_seconds=1.0,
                runner="docker-container",
                image=sandbox.NODE_SANDBOX_IMAGE,
                verification_mode=sandbox.MODE_STATIC_ONLY,
                steps=[{"name": "build", "ok": True}],
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            runner = mock.Mock()
            runner.run.side_effect = _grow
            with mock.patch.object(sandbox, "max_workspace_bytes", return_value=1024):
                report = sandbox.run_patch_tests(
                    _patch(["src/app.ts"], _new_file_diff("src/app.ts")),
                    0,
                    repo_dir=repo,
                    sandbox_root=Path(tmp) / "sandbox",
                    runner=runner,
                )
        self.assertFalse(report.passed)
        self.assertIn("产物超限", report.details)


if __name__ == "__main__":
    unittest.main()
