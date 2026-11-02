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


def _node_plan(**overrides) -> sandbox.ProjectPlan:
    """测试用 Node 计划：默认不绑定依赖树/配额，便于单测精确控制。"""
    payload = dict(
        kind="node",
        verification_mode=sandbox.MODE_STATIC_ONLY,
        image=sandbox.node_sandbox_image(),
        image_env=sandbox.NODE_IMAGE_ENV,
        command=["/bin/sh", "-c", "npm run --silent build"],
        steps=["build"],
        workspace_writable=True,
        workspace_quota_mb=0,
    )
    payload.update(overrides)
    return sandbox.ProjectPlan(**payload)


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
            # Node 计划要指向 AIOPS_SANDBOX_NODE_IMAGE，而不是 python 变量
            node_error = sandbox.resolve_image_error(
                "node:22-bookworm-slim",
                env_var=sandbox.NODE_IMAGE_ENV,
                example="node:<tag>@sha256:<digest>",
            )
            self.assertIn("AIOPS_SANDBOX_NODE_IMAGE", node_error)
            self.assertIn("node:<tag>@sha256:<digest>", node_error)
            self.assertNotIn("python:", node_error)
        with mock.patch.object(mode, "is_production", return_value=False):
            self.assertEqual(sandbox.resolve_image_error("python:3.12-slim"), "")

    def test_images_are_lazy_so_dotenv_loading_late_still_applies(self) -> None:
        """config.load_dotenv 可能晚于 sandbox 导入：运行期取值必须跟随环境。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {"AIOPS_SANDBOX_NODE_IMAGE": "node:22@sha256:feed"}, clear=False
            ):
                self.assertEqual(sandbox.project_plan(root).image, "node:22@sha256:feed")
        with mock.patch.dict(
            os.environ, {"AIOPS_SANDBOX_IMAGE": "python:3.12@sha256:beef"}, clear=False
        ):
            self.assertEqual(sandbox.sandbox_image(), "python:3.12@sha256:beef")

    def test_container_user_defaults_to_host_uid_and_can_be_overridden(self) -> None:
        expected = f"{os.getuid()}:{os.getgid()}" if os.getuid() != 0 else "65534:65534"
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_UID": ""}, clear=False):
            self.assertEqual(sandbox.sandbox_user(), expected)
        with mock.patch.dict(os.environ, {"AIOPS_SANDBOX_UID": "65534:65534"}, clear=False):
            self.assertEqual(sandbox.sandbox_user(), "65534:65534")

    def test_workspace_quota_error_reports_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"x" * 2048)
            self.assertEqual(sandbox.workspace_quota_error(root, limit=4096), "")
            error = sandbox.workspace_quota_error(root, limit=1024)
            self.assertIn("超过配额", error)
            self.assertIn("2048B", error)

    def test_k8s_memory_uses_binary_quantity(self) -> None:
        """Docker 的 m/g 是二进制，K8s 只能写 Mi/Gi（M/G 是十进制，会缩小上限）。"""
        self.assertEqual(sandbox.k8s_memory("256m"), "256Mi")
        self.assertEqual(sandbox.k8s_memory("4g"), "4Gi")
        self.assertEqual(sandbox.k8s_memory("1k"), "1Ki")
        # 已合规或未知写法原样返回
        self.assertEqual(sandbox.k8s_memory("512Mi"), "512Mi")
        self.assertEqual(sandbox.k8s_memory(""), "")


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

    def test_parse_vitest_summary_as_real_tests(self) -> None:
        text = (
            " Test Files  172 passed (172)\n"
            "      Tests  2424 passed (2424)\n"
            "   Duration  1479.15s\n"
        )
        parsed = sandbox.parse_unittest_output(text)
        self.assertEqual(parsed["tests_run"], 2424)
        self.assertEqual(parsed["failed_tests"], [])

    def test_parse_vitest_failures(self) -> None:
        text = "\n".join(
            [
                " FAIL  src/utils/dataScope.test.ts > scope > filters rows",
                " Test Files  1 failed | 171 passed (172)",
                "      Tests  1 failed | 2423 passed (2424)",
            ]
        )
        parsed = sandbox.parse_unittest_output(text)
        self.assertEqual(parsed["tests_run"], 2424)
        self.assertEqual(
            parsed["failed_tests"], ["src/utils/dataScope.test.ts > scope > filters rows"]
        )
        self.assertIn("1 个 vitest 用例未通过", parsed["error_summary"])

    def test_parse_vitest_output_with_ansi_and_thousands(self) -> None:
        """vitest 输出到管道仍带 ANSI；总数用千分位且可能含 skipped。"""
        text = (
            "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m2,424 passed\x1b[39m"
            "\x1b[22m\x1b[90m (2,424)\x1b[39m\n"
        )
        parsed = sandbox.parse_unittest_output(text)
        self.assertEqual(parsed["tests_run"], 2424)
        skipped = (
            "\x1b[2m      Tests \x1b[22m\x1b[33m1 failed\x1b[39m | "
            "\x1b[32m2,420 passed\x1b[39m | \x1b[90m3 skipped\x1b[39m (2,424)\n"
        )
        parsed = sandbox.parse_unittest_output(skipped)
        self.assertEqual(parsed["tests_run"], 2424)
        self.assertIn("1 个 vitest 用例未通过", parsed["error_summary"])

    def test_node_report_shows_real_case_count(self) -> None:
        """step 标记 + 真实用例数并存时，报告要展示用例数而不是「未执行」。"""
        outcome = sandbox.SandboxOutcome(
            exit_code=0,
            passed=True,
            tests_run=2424,
            duration_seconds=1497.4,
            runner="docker-container",
            image="node:22@sha256:abc",
            verification_mode=sandbox.MODE_UNIT_TEST,
            steps=[
                {"name": "build", "ok": True},
                {"name": "typecheck", "ok": True},
                {"name": "test", "ok": True},
            ],
        )
        with mock.patch.object(metrics, "observe_sandbox_run"):
            report = sandbox._report_from_outcome(_patch(["a.ts"], ""), 3, outcome, "SAST 通过")
        self.assertEqual(report.unit_tests, "2424/2424 passed")
        self.assertEqual(report.regression_tests, "全量 2424 用例：通过")
        self.assertIn("共 2424 个用例", report.details)


class NodeDepsTest(unittest.TestCase):
    def _repo(self, root: Path, lock: str = "{}") -> Path:
        repo = root / "repo"
        repo.mkdir()
        (repo / "package-lock.json").write_text(lock, encoding="utf-8")
        return repo

    def test_missing_deps_tree_is_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            plan = _node_plan(node_deps=root / "deps")
            error = sandbox.install_node_deps(root / "ws", plan, repo)
        self.assertIn("依赖未准备", error)
        self.assertIn("prepare_node_sandbox_deps.sh", error)

    def test_stale_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            deps = root / "deps"
            (deps / "node_modules").mkdir(parents=True)
            (deps / "package-lock.sha256").write_text("stale", encoding="utf-8")
            error = sandbox.install_node_deps(
                root / "ws", _node_plan(node_deps=deps), repo
            )
        self.assertIn("不一致", error)
        self.assertIn("重新运行", error)

    def test_matching_lock_copies_modules_into_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            deps = root / "deps"
            (deps / "node_modules" / "vite").mkdir(parents=True)
            (deps / "node_modules" / "vite" / "package.json").write_text("{}", encoding="utf-8")
            (deps / "package-lock.sha256").write_text(
                sandbox._sha256_file(repo / "package-lock.json"), encoding="utf-8"
            )
            workspace = root / "ws"
            workspace.mkdir()
            error = sandbox.install_node_deps(
                workspace, _node_plan(node_deps=deps), repo
            )
            self.assertEqual(error, "")
            self.assertTrue((workspace / "node_modules" / "vite" / "package.json").is_file())


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
        self.assertEqual(plan.image, sandbox.node_sandbox_image())
        self.assertEqual(plan.image_env, sandbox.NODE_IMAGE_ENV)
        self.assertEqual(plan.steps, ["build", "test"])
        self.assertTrue(plan.workspace_writable)
        # Node 全量构建+测试远超 90s 默认值，资源与配额独立
        self.assertGreaterEqual(plan.timeout_seconds, 1800)
        self.assertEqual(plan.memory_limit, "4g")
        self.assertGreaterEqual(plan.workspace_quota_mb, 1024)
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
        self.assertEqual(cmd[cmd.index("--user") + 1], sandbox.sandbox_user())
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
            ) as real, mock.patch.object(
                sandbox, "project_plan", return_value=_node_plan()
            ), mock.patch.object(
                sandbox, "install_node_deps", return_value=""
            ):
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
        self.assertEqual(job.image, sandbox.node_sandbox_image())
        self.assertEqual(job.image_env, sandbox.NODE_IMAGE_ENV)
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
            with mock.patch.object(
                sandbox, "project_plan", return_value=_node_plan()
            ), mock.patch.object(
                sandbox, "install_node_deps", return_value=""
            ), mock.patch.object(
                sandbox, "workspace_quota_error", return_value="工作区体积 2B 超过配额 1B"
            ):
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
            with mock.patch.object(
                sandbox, "project_plan", return_value=_node_plan()
            ), mock.patch.object(
                sandbox, "install_node_deps", return_value=""
            ), mock.patch.object(
                sandbox, "max_workspace_bytes", return_value=1024
            ):
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
