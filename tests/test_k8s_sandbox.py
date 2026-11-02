"""M4：K8s Job 沙箱执行器六类路径（成功/测试失败/镜像失败/超时/日志失败/清理核对）。

用 fake batch/core 客户端替代真实集群，验证：
    - 清单的加固字段与幂等命名（spec 哈希、409 冲突复用/重建）；
    - 工作区以 Secret 传递且超限显式失败；production 未固定镜像摘要拒绝执行；
    - 状态推导、Pod 失败原因、日志解析、清理核对与超时回收。
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import sandbox


class ApiError(Exception):
    """带 status 的假 K8s 异常（与 kubernetes 客户端 ApiException 一致）。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeClock:
    """可控单调时钟：sleep 即推进，避免真实等待。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


def _pod(name: str, *, waiting: str = "", terminated: str = ""):
    state = types.SimpleNamespace(
        waiting=types.SimpleNamespace(reason=waiting) if waiting else None,
        terminated=types.SimpleNamespace(reason=terminated) if terminated else None,
    )
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(
            container_statuses=[types.SimpleNamespace(state=state)]
        ),
    )


def _status(*, succeeded: int = 0, failed: int = 0, condition: str = "", reason: str = ""):
    conditions = (
        [types.SimpleNamespace(type=condition, reason=reason)] if condition else []
    )
    return types.SimpleNamespace(
        status=types.SimpleNamespace(
            succeeded=succeeded, failed=failed, conditions=conditions
        )
    )


class FakeBatch:
    def __init__(self, *, conflict: bool = False, existing_labels=None) -> None:
        self.conflict = conflict
        self.existing_labels = existing_labels or {}
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.status = _status()
        self.status_queue: list = []

    def create_namespaced_job(self, namespace, body):
        if self.conflict:
            self.conflict = False
            raise ApiError(409)
        self.created.append(body)

    def read_namespaced_job(self, name, namespace):
        return types.SimpleNamespace(
            metadata=types.SimpleNamespace(labels=self.existing_labels)
        )

    def read_namespaced_job_status(self, name, namespace):
        if self.status_queue:
            return self.status_queue.pop(0)
        return self.status

    def delete_namespaced_job(
        self, name, namespace, propagation_policy=None, grace_period_seconds=None
    ):
        self.deleted.append(name)


class FakeCore:
    def __init__(
        self,
        *,
        logs: str = "",
        pods=None,
        log_error: Exception | None = None,
        sticky_pods: bool = False,
    ) -> None:
        self.logs = logs
        self.pods = list(pods or [])
        self.log_error = log_error
        self.sticky_pods = sticky_pods
        self.secrets: list[object] = []
        self.deleted_secrets: list[str] = []
        self.cleaned = False

    def create_namespaced_secret(self, namespace, body):
        self.secrets.append(body)

    def delete_namespaced_secret(self, name, namespace):
        self.deleted_secrets.append(name)
        self.cleaned = True

    def list_namespaced_pod(self, namespace, label_selector=None):
        if self.cleaned and not self.sticky_pods:
            return types.SimpleNamespace(items=[])
        return types.SimpleNamespace(items=self.pods)

    def read_namespaced_pod_log(self, name, namespace, container=None, tail_lines=None):
        if self.log_error:
            raise self.log_error
        return self.logs


def _runner(batch, core, clock: FakeClock | None = None) -> sandbox.K8sJobSandboxRunner:
    clock = clock or FakeClock()
    return sandbox.K8sJobSandboxRunner(
        batch_api=batch,
        core_api=core,
        poll_interval=1.0,
        cleanup_timeout=3.0,
        sleep=clock.sleep,
        clock=clock,
    )


def _workspace(tmp: str) -> Path:
    root = Path(tmp)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_ok.py").write_text("import unittest\n", encoding="utf-8")
    return root


OK_LOG = (
    "test_ok (t.T) ... ok\n"
    "----------------------------------------------------------------------\n"
    "Ran 3 tests in 0.002s\n\nOK\n"
)


class _TempWorkspace:
    def __enter__(self) -> str:
        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def tempfile_workspace() -> _TempWorkspace:
    return _TempWorkspace()


def _patch_v1job():
    return mock.patch.object(
        sandbox.K8sJobSandboxRunner, "_v1job", staticmethod(lambda manifest: manifest)
    )


class K8sRunnerPathsTest(unittest.TestCase):
    def test_success_reads_logs_and_cleans_up(self) -> None:
        batch = FakeBatch()
        batch.status = _status(succeeded=1)
        core = FakeCore(logs=OK_LOG, pods=[_pod("sandbox-abc")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k1", workspace=_workspace(tmp))
            )
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.tests_run, 3)
        self.assertEqual(outcome.runner, "k8s-job-gvisor")
        self.assertTrue(outcome.cleanup_verified)
        self.assertEqual(len(batch.created), 1)
        self.assertEqual(len(core.secrets), 1)
        manifest = batch.created[0]
        self.assertEqual(
            core.deleted_secrets, [manifest["metadata"]["name"] + "-workspace"]
        )
        self.assertIn("aiops.io/spec-hash", manifest["metadata"]["labels"])
        self.assertEqual(
            manifest["metadata"]["labels"]["aiops.io/spec-hash"],
            manifest["spec"]["template"]["metadata"]["labels"]["aiops.io/spec-hash"],
        )

    def test_failed_tests_reported_with_names(self) -> None:
        log = (
            "test_ok (t.T) ... ok\n"
            "======================================================================\n"
            "FAIL: test_submit (t.T)\n"
            "----------------------------------------------------------------------\n"
            "AssertionError: boom\n"
            "\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n"
        )
        batch = FakeBatch()
        batch.status = _status(failed=1)
        core = FakeCore(logs=log, pods=[_pod("sandbox-f")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k2", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failed_tests, ["test_submit"])
        self.assertIn("AssertionError", outcome.error_summary)

    def test_image_pull_failure_is_named(self) -> None:
        batch = FakeBatch()
        batch.status = _status()  # 一直 running
        core = FakeCore(pods=[_pod("sandbox-img", waiting="ImagePullBackOff")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k3", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertIn("镜像拉取失败", outcome.error_summary)
        self.assertIn("ImagePullBackOff", outcome.error_summary)

    def test_deadline_timeout_sets_timed_out(self) -> None:
        batch = FakeBatch()
        batch.status = _status()  # 永远 running
        core = FakeCore(pods=[_pod("sandbox-slow")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(
                    patch_id="m4-k4", workspace=_workspace(tmp), timeout_seconds=2
                )
            )
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.passed)
        self.assertTrue(outcome.cleanup_verified)

    def test_deadline_condition_maps_to_timeout(self) -> None:
        batch = FakeBatch()
        batch.status = _status(condition="Failed", reason="DeadlineExceeded")
        core = FakeCore(pods=[_pod("sandbox-d")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k5", workspace=_workspace(tmp))
            )
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.passed)

    def test_oom_killed_pod_is_named(self) -> None:
        batch = FakeBatch()
        batch.status = _status()
        core = FakeCore(pods=[_pod("sandbox-oom", terminated="OOMKilled")])
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k6", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertIn("OOMKilled", outcome.error_summary)

    def test_log_read_failure_is_reported(self) -> None:
        batch = FakeBatch()
        batch.status = _status(failed=1)
        core = FakeCore(pods=[_pod("sandbox-log")], log_error=RuntimeError("logs gone"))
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k7", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertIn("读取 Pod 日志失败", outcome.error_summary)

    def test_cleanup_timeout_marks_unverified(self) -> None:
        batch = FakeBatch()
        batch.status = _status(succeeded=1)
        core = FakeCore(logs=OK_LOG, pods=[_pod("sandbox-stuck")], sticky_pods=True)
        runner = _runner(batch, core)
        runner.cleanup_timeout = 2.0
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k8", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.cleanup_verified)
        self.assertIn("清理超时", outcome.error_summary)


class K8sRunnerIdempotencyTest(unittest.TestCase):
    def test_conflict_with_same_spec_reuses_job(self) -> None:
        batch = FakeBatch(conflict=True)
        core = FakeCore(logs=OK_LOG, pods=[_pod("sandbox-r")])
        batch.status = _status(succeeded=1)
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            root = _workspace(tmp)
            payload, _ = runner.pack_workspace(root)
            batch.existing_labels = {"aiops.io/spec-hash": runner._spec_hash(payload)}
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k9", workspace=root)
            )
        self.assertTrue(outcome.passed)
        self.assertEqual(len(batch.deleted), 1)  # 仅结束清理，未删除重建

    def test_conflict_with_different_spec_replaces_job(self) -> None:
        batch = FakeBatch(conflict=True, existing_labels={"aiops.io/spec-hash": "stale"})
        core = FakeCore(logs=OK_LOG, pods=[_pod("sandbox-p")])
        batch.status = _status(succeeded=1)
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k10", workspace=_workspace(tmp))
            )
        self.assertTrue(outcome.passed)
        self.assertEqual(len(batch.deleted), 2)  # 冲突重建 + 结束清理

    def test_job_name_is_deterministic_and_safe(self) -> None:
        runner = sandbox.K8sJobSandboxRunner(batch_api=mock.Mock(), core_api=mock.Mock())
        first = runner.job_name("Patch_ABC.1", "0123456789")
        self.assertEqual(first, runner.job_name("Patch_ABC.1", "0123456789"))
        self.assertTrue(first.startswith("aiops-sandbox-"))
        self.assertLessEqual(len(first), 63)
        self.assertNotIn("_", first)

    def test_manifest_records_mode_and_pull_policy(self) -> None:
        runner = sandbox.K8sJobSandboxRunner(batch_api=mock.Mock(), core_api=mock.Mock())
        job = sandbox.SandboxJob(
            patch_id="m4-k11",
            workspace=Path("/tmp/ws"),
            image="node:22-custom",
            image_pull_policy="Always",
            verification_mode=sandbox.MODE_STATIC_ONLY,
            workspace_writable=True,
        )
        manifest = runner.render_manifest(job, name="aiops-sandbox-x", secret_name="sec")
        template = manifest["spec"]["template"]
        self.assertEqual(
            template["metadata"]["annotations"]["aiops.io/verification-mode"],
            "STATIC_ONLY",
        )
        container = template["spec"]["containers"][0]
        self.assertEqual(container["imagePullPolicy"], "Always")
        self.assertFalse(container["volumeMounts"][0]["readOnly"])  # Node 构建需可写
        self.assertEqual(template["spec"]["volumes"][1]["secret"]["secretName"], "sec")


class K8sRunnerGuardsTest(unittest.TestCase):
    def test_oversized_workspace_fails_before_cluster(self) -> None:
        batch, core = FakeBatch(), FakeCore()
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp:
            root = _workspace(tmp)
            (root / "blob.bin").write_bytes(os.urandom(8192))
            with mock.patch.object(runner, "SECRET_MAX_BYTES", 64):
                payload, error = runner.pack_workspace(root)
                self.assertIsNone(payload)
                self.assertIn("超过 Secret 上限", error)
                outcome = runner.run(
                    sandbox.SandboxJob(patch_id="m4-k12", workspace=root)
                )
        self.assertFalse(outcome.passed)
        self.assertIn("超过 Secret 上限", outcome.error_summary)
        self.assertEqual(batch.created, [])

    def test_production_requires_pinned_image(self) -> None:
        batch, core = FakeBatch(), FakeCore()
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, mock.patch(
            "aiops_agent.mode.is_production", return_value=True
        ):
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k13", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertIn("@sha256:", outcome.error_summary)
        self.assertFalse(outcome.image_pinned)
        self.assertEqual(batch.created, [])

    def test_missing_cluster_client_fails_explicitly(self) -> None:
        runner = sandbox.K8sJobSandboxRunner(batch_api=None, core_api=None)
        with tempfile_workspace() as tmp, mock.patch.object(
            sandbox.K8sJobSandboxRunner,
            "_apis",
            side_effect=RuntimeError("no kubeconfig"),
        ):
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k14", workspace=_workspace(tmp))
            )
        self.assertFalse(outcome.passed)
        self.assertIn("K8s 客户端不可用", outcome.error_summary)

    def test_transient_404_is_retried(self) -> None:
        batch = FakeBatch()
        core = FakeCore(logs=OK_LOG, pods=[_pod("sandbox-404")])
        calls = {"n": 0}

        def _flaky(name, namespace):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ApiError(404)
            return _status(succeeded=1)

        batch.read_namespaced_job_status = _flaky
        runner = _runner(batch, core)
        with tempfile_workspace() as tmp, _patch_v1job():
            outcome = runner.run(
                sandbox.SandboxJob(patch_id="m4-k15", workspace=_workspace(tmp))
            )
        self.assertTrue(outcome.passed)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
