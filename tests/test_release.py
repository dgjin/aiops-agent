"""执行层单测（WP7）：ArgoCD 渲染与提交 / 金丝雀编排 / 发布终态 / 真实容器集成。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

说明：真实容器集成类需要本机 docker 与 python:3.12-slim 镜像，不可用时自动跳过。
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import code_rag, fix_agent, release, sandbox
from aiops_agent.models import Alert, CanaryResult, Patch, RootCause

ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["com/example/service/OrderService.java"],
    confidence=0.95,
    summary="coupon 空值未防护。",
)

PLAN = {
    "patch_id": "p-wp7-u1-r0",
    "alert_id": "wp7-u1",
    "service": "order",
    "image": "aiops-demo-app:p-wp7-u1-r0",
    "traffic_percent": 5,
    "observe_seconds": 10,
    "version": "v1.0.123",
}


def _patch(alert_id: str, *, bad: bool = False) -> Patch:
    alert = Alert(alert_id=alert_id, service="order", description="发布单测")
    return fix_agent.fallback_patch(alert, ROOT_CAUSE, 0, "order_service.py", bad=bad)


class TestReleaseVersionAndManifest(unittest.TestCase):
    def test_version_format_stable(self) -> None:
        version = release.release_version("p-wp7-u1-r0")
        self.assertRegex(version, r"^v1\.0\.\d{3}$")
        self.assertEqual(version, release.release_version("p-wp7-u1-r0"))

    def test_manifest_canary_steps_and_labels(self) -> None:
        manifest = release.render_application(PLAN)
        self.assertEqual(manifest["kind"], "Application")
        labels = manifest["metadata"]["labels"]
        self.assertEqual(labels["aiops.patch-id"], PLAN["patch_id"])
        self.assertEqual(labels["aiops.service"], "order")
        spec = manifest["spec"]
        self.assertEqual(spec["destination"]["namespace"], "demo")
        steps = spec["rollout"]["steps"]
        self.assertEqual(steps[0], {"setWeight": 5})
        self.assertEqual(steps[1], {"pause": {"durationSeconds": 10}})
        self.assertEqual(steps[2], {"setWeight": 100})
        # manifest 必须 JSON 可序列化（Publisher 落盘/提交用）
        self.assertIsInstance(manifest, dict)


class TestArgoCDPublisher(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp7-publisher-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recorded_mode_writes_manifest(self) -> None:
        manifest = release.render_application(PLAN)
        with mock.patch.object(release, "ARGOCD_DIR", self.tmp / "argocd"):
            result = release.ArgoCDPublisher(base_url="").apply(manifest)
        self.assertEqual(result["mode"], "recorded")
        record = Path(result["path"])
        self.assertTrue(record.is_file())
        self.assertIn(PLAN["patch_id"], record.read_text(encoding="utf-8"))

    def test_live_mode_posts_application(self) -> None:
        manifest = release.render_application(PLAN)
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__.return_value = resp
        with mock.patch.object(release, "ARGOCD_DIR", self.tmp / "argocd"), mock.patch(
            "aiops_agent.release.urllib.request.urlopen", return_value=resp
        ) as urlopen:
            result = release.ArgoCDPublisher(base_url="http://argo.example", token="t").apply(manifest)
        self.assertEqual(result["mode"], "live")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://argo.example/api/v1/applications")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer t")

    def test_live_mode_failure_raises(self) -> None:
        manifest = release.render_application(PLAN)
        with mock.patch.object(release, "ARGOCD_DIR", self.tmp / "argocd"), mock.patch(
            "aiops_agent.release.urllib.request.urlopen", side_effect=OSError("conn refused")
        ):
            with self.assertRaises(RuntimeError):
                release.ArgoCDPublisher(base_url="http://argo.example").apply(manifest)


class TestObserve(unittest.TestCase):
    def test_observe_counts_errors_and_latency(self) -> None:
        runner = release.DockerRolloutRunner()
        replies = itertools.cycle([(200, {}), (500, {"error": "boom"}), (200, {})])
        with mock.patch("aiops_agent.release._http_json", side_effect=lambda *a, **k: next(replies)):
            observed = runner.observe("http://127.0.0.1:1", duration_seconds=1)
        self.assertGreaterEqual(observed["total"], 2)
        self.assertGreaterEqual(observed["errors"], 1)
        self.assertGreater(observed["error_rate"], 0)
        self.assertLessEqual(observed["error_rate"], 100)
        self.assertGreaterEqual(observed["p99_latency_ms"], 0)


class TestRunCanaryWithFakeRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp7-canary-"))
        self.publisher = mock.Mock()
        self.publisher.apply.return_value = {"mode": "recorded", "application": "x", "path": "y"}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_runner(self, observed: dict) -> mock.Mock:
        runner = mock.Mock()
        runner.build_image.return_value = "aiops-demo-app:p-x"
        runner.start_canary.return_value = "http://127.0.0.1:12345"
        runner.observe.return_value = observed
        return runner

    def test_healthy_path(self) -> None:
        alert = Alert(alert_id="wp7-u2", service="order", description="发布单测")
        patch = _patch("wp7-u2")
        runner = self._fake_runner(
            {"total": 20, "errors": 0, "error_rate": 0.0, "p99_latency_ms": 32.0}
        )
        result = release.run_canary(
            alert, patch, 5, 10,
            sandbox_root=self.tmp, runner=runner, publisher=self.publisher,
        )
        self.assertTrue(result.healthy)
        self.assertEqual(result.error_rate, 0.0)
        self.assertIn("金丝雀健康", result.observation)
        runner.build_image.assert_called_once()
        runner.wait_ready.assert_called_once()
        runner.stop.assert_not_called()

    def test_degraded_path_injects_bad_canary(self) -> None:
        alert = Alert(alert_id="wp7-u3", service="order", description="canary-bad 演示")
        patch = _patch("wp7-u3")
        runner = self._fake_runner(
            {"total": 20, "errors": 10, "error_rate": 50.0, "p99_latency_ms": 520.0}
        )
        result = release.run_canary(
            alert, patch, 5, 10,
            sandbox_root=self.tmp, runner=runner, publisher=self.publisher,
        )
        self.assertFalse(result.healthy)
        self.assertIn("金丝雀劣化", result.observation)
        self.assertIn("错误率", result.observation)
        self.assertTrue(runner.start_canary.call_args.kwargs["bad"])

    def test_ready_failure_recycles_container(self) -> None:
        alert = Alert(alert_id="wp7-u4", service="order", description="发布单测")
        patch = _patch("wp7-u4")
        runner = self._fake_runner({"total": 0, "errors": 0, "error_rate": 0, "p99_latency_ms": 0})
        runner.wait_ready.side_effect = RuntimeError("未就绪")
        with self.assertRaises(RuntimeError):
            release.run_canary(
                alert, patch, 5, 10,
                sandbox_root=self.tmp, runner=runner, publisher=self.publisher,
            )
        runner.stop.assert_called_once()


class TestRunFinalize(unittest.TestCase):
    def _canary(self, *, healthy: bool) -> CanaryResult:
        return CanaryResult(
            patch_id="p-wp7-u9-r0",
            traffic_percent=5,
            healthy=healthy,
            error_rate=0.0 if healthy else 50.0,
            p99_latency_ms=30.0 if healthy else 520.0,
            observation="",
        )

    def test_healthy_promotes_to_stable(self) -> None:
        runner = mock.Mock()
        result = release.run_finalize(self._canary(healthy=True), True, runner=runner)
        self.assertFalse(result.rolled_back)
        self.assertIn("全量发布", result.reason)
        runner.promote.assert_called_once()
        self.assertRegex(result.version, r"^v1\.0\.\d{3}$")

    def test_degraded_rolls_back_and_recycles(self) -> None:
        runner = mock.Mock()
        result = release.run_finalize(self._canary(healthy=False), True, runner=runner)
        self.assertTrue(result.rolled_back)
        self.assertIn("自动回滚", result.reason)
        runner.stop.assert_called_once()
        runner.promote.assert_not_called()

    def test_degraded_without_auto_rollback(self) -> None:
        runner = mock.Mock()
        result = release.run_finalize(self._canary(healthy=False), False, runner=runner)
        self.assertTrue(result.rolled_back)
        self.assertIn("未开启", result.reason)
        runner.stop.assert_called_once()


class TestRunnerIsolation(unittest.TestCase):
    """回归防护：稳定容器名/端口必须可注入，且默认沿用生产常量。

    背景：`promote` 会「停旧稳定 → 起新稳定」，结尾还有清理；若测试复用默认的
    `aiops-stable-order:18080`，跑单测就会把**在跑的真实发布**打掉。
    """

    def test_defaults_match_module_constants(self) -> None:
        runner = release.DockerRolloutRunner()
        self.assertEqual(runner.stable_container, release.STABLE_CONTAINER)
        self.assertEqual(runner.stable_port, release.STABLE_PORT)

    def test_injected_target(self) -> None:
        runner = release.DockerRolloutRunner(
            stable_container="aiops-test-stable", stable_port=18099
        )
        self.assertEqual(runner.stable_container, "aiops-test-stable")
        self.assertEqual(runner.stable_port, 18099)

    def test_promote_uses_instance_target_not_module_constant(self) -> None:
        """promote 必须用实例的容器名/端口，绝不回落到模块常量（否则会误伤真实发布）。"""
        runner = release.DockerRolloutRunner(
            stable_container="aiops-test-stable", stable_port=18099
        )
        calls: list[list[str]] = []

        def fake_run(cmd, timeout=120):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, b"cid\n", b"")

        with mock.patch.object(runner, "_run", side_effect=fake_run), mock.patch.object(
            runner, "wait_ready"
        ), mock.patch.object(runner, "stop") as stop:
            runner.promote("aiops-canary-p-x", "img:tag", "v1.0.1")

        run_cmd = next(c for c in calls if c[1:2] == ["run"])
        self.assertIn("aiops-test-stable", run_cmd)
        self.assertIn("127.0.0.1:18099:8000", run_cmd)
        self.assertNotIn(release.STABLE_CONTAINER, run_cmd)
        self.assertNotIn(f":{release.STABLE_PORT}:", " ".join(run_cmd))
        stop.assert_any_call("aiops-canary-p-x")  # 金丝雀正常回收


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


# 真实发布集成专用容器名/端口：**必须**与生产默认（aiops-stable-order:18080）区分，
# 否则测试的 promote（停旧稳定）与结尾清理（rm -f）会把在跑的真实发布打掉。
_TEST_STABLE_CONTAINER = "aiops-test-stable"
_TEST_STABLE_PORT = 18099


def _test_runner() -> release.DockerRolloutRunner:
    return release.DockerRolloutRunner(
        stable_container=_TEST_STABLE_CONTAINER, stable_port=_TEST_STABLE_PORT
    )


@unittest.skipUnless(_docker_available(), "docker/镜像不可用，跳过真实发布集成")
class TestRealCanaryIntegration(unittest.TestCase):
    """真实发布集成：镜像构建 → 金丝雀容器 → 真实探活 → 晋级 / 回滚。

    隔离约定：只用 `aiops-test-stable:18099`，**绝不触碰**真实发布的 `aiops-stable-order:18080`。
    """

    def setUp(self) -> None:
        self.runner = _test_runner()
        # 兜底清理：即使断言失败也回收测试容器（只回收测试命名）
        self.addCleanup(self.runner.stop, _TEST_STABLE_CONTAINER)

    def test_healthy_canary_promotes_to_stable(self) -> None:
        alert = Alert(alert_id="wp7-int-good", service="order", description="发布集成（健康）")
        patch = _patch("wp7-int-good")
        result = release.run_canary(alert, patch, 5, observe_seconds=4, runner=self.runner)
        self.assertTrue(result.healthy, result.observation)
        self.assertLessEqual(result.error_rate, release.SLO_MAX_ERROR_RATE)

        final = release.run_finalize(result, True, runner=self.runner)
        self.assertFalse(final.rolled_back)
        self.assertIn(_TEST_STABLE_CONTAINER, final.reason)  # 报告的是隔离容器，非生产容器
        code, body = release._http_json(f"http://127.0.0.1:{_TEST_STABLE_PORT}/health")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("version"), final.version)
        # 只清理测试容器；真实 aiops-stable-order 始终不受影响
        self.runner.stop(_TEST_STABLE_CONTAINER)

    def test_bad_canary_detected_and_rolled_back(self) -> None:
        alert = Alert(alert_id="wp7-int-bad", service="order", description="canary-bad 发布集成")
        patch = _patch("wp7-int-bad")
        result = release.run_canary(alert, patch, 5, observe_seconds=4, runner=self.runner)
        self.assertFalse(result.healthy, result.observation)
        # 劣化判定必须至少有一项 SLO 越线。注意：BAD_CANARY 注入 500ms 延迟后，4s 窗口内仅有
        # 数个请求，小样本下错误率可能采到 0.0（约 6% 概率）——故不断言单个指标，避免 flaky。
        self.assertTrue(
            result.error_rate > release.SLO_MAX_ERROR_RATE
            or result.p99_latency_ms > release.SLO_MAX_P99_LATENCY_MS,
            result.observation,
        )

        final = release.run_finalize(result, True, runner=self.runner)
        self.assertTrue(final.rolled_back)
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=aiops-canary-{patch.patch_id}"],
            capture_output=True,
            timeout=15,
        )
        self.assertNotIn(b"aiops-canary", proc.stdout)  # 金丝雀已回收


if __name__ == "__main__":
    unittest.main()
