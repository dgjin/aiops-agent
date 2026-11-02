"""M4：集群发布（ArgoCD + Rollouts）执行器选择、状态判定、回滚与终态分派。

全部使用假客户端/假 journal，不连接集群、不触发真实发布。
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import unittest
from unittest import mock

from aiops_agent import argo_status, release
from aiops_agent.models import Alert, CanaryResult, Patch, ReleaseResult
from aiops_agent.release_journal import ReleaseSafetyError

ALERT = Alert("m4-alert", "order")
PATCH = Patch(
    patch_id="m4-p1",
    alert_id=ALERT.alert_id,
    files=["order_service.py"],
    diff="--- a/order_service.py\n+++ b/order_service.py\n@@ -1 +1 @@\n-x\n+y\n",
    description="M4 集群发布单测",
    risk="低：单文件",
    model_version="qwen2.5-coder:32b",
    confidence=0.9,
)


class FakeRecord:
    def __init__(self, initial: dict | None = None) -> None:
        self.values: dict = dict(initial or {})
        self.release_status = ""

    def update(self, **values) -> None:
        self.values.update(values)
        if "release_status" in values:
            self.release_status = values["release_status"]


class FakeJournal:
    """最小 journal 桩：只实现发布编排实际用到的方法。"""

    def __init__(self, record: dict | None = None) -> None:
        self.record = record
        self.manual_calls: list[tuple[bool, str]] = []

    def claim(self, patch_hash, target, resources) -> None:
        self.record = {
            "status": "occupied",
            "patch_hash": patch_hash,
            "target": target,
            "resources": resources,
            "stages": {},
            "before": {},
        }

    def begin(self, stage, inputs):
        return None

    def finish(self, stage, result) -> None:
        self.record["stages"].setdefault(stage, {})["result"] = result

    def update(self, **values) -> None:
        self.record.update(values)

    def manual(self, recovered, reason="") -> None:
        self.manual_calls.append((recovered, reason))
        self.record["status"] = "manual"


def _record_for(canary: CanaryResult, *, target: dict | None = None) -> dict:
    return {
        "status": "occupied",
        "resources": [],
        "target": target or {"kind": "argocd-application", "name": "demo-app-m4-p1"},
        "stages": {"canary": {"input_hash": "x", "result": dataclasses.asdict(canary)}},
        "before": {},
    }


class ReleaseExecutorTest(unittest.TestCase):
    def test_explicit_executor_wins(self) -> None:
        env = {"AIOPS_RELEASE_EXECUTOR": "docker", "AIOPS_ARGOCD_URL": "https://argo"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(release.release_executor(), "docker")
        with mock.patch.dict(
            os.environ, {"AIOPS_RELEASE_EXECUTOR": "argocd"}, clear=False
        ):
            self.assertEqual(release.release_executor(), "argocd")

    def test_argocd_url_implies_cluster_executor(self) -> None:
        env = {"AIOPS_ARGOCD_URL": "https://argo", "AIOPS_RELEASE_EXECUTOR": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(release.release_executor(), "argocd")

    def test_default_is_local_docker(self) -> None:
        env = {"AIOPS_ARGOCD_URL": "", "AIOPS_RELEASE_EXECUTOR": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(release.release_executor(), "docker")

    def test_run_canary_dispatches_to_cluster(self) -> None:
        sentinel = CanaryResult(PATCH.patch_id, 5, True, -1, -1, "cluster", mode="argocd")
        with mock.patch.dict(os.environ, {"AIOPS_RELEASE_EXECUTOR": "argocd"}, clear=False), mock.patch.object(
            release, "_run_canary_cluster", return_value=sentinel
        ) as cluster, mock.patch.object(release, "DockerRolloutRunner") as docker:
            result = release.run_canary(ALERT, PATCH, 5, 10)
        self.assertIs(result, sentinel)
        cluster.assert_called_once()
        docker.assert_not_called()


class ClusterCanaryTest(unittest.TestCase):
    def _client(self, state: str, *, sync="Synced", health="Healthy", message="") -> mock.Mock:
        client = mock.Mock()
        client.configured = True
        client.wait.return_value = {
            "state": state,
            "sync": sync,
            "health": health,
            "message": message,
            "revision": "rev-1",
        }
        client.rollout_state.return_value = {
            "phase": "Paused",
            "current_step": 0,
            "total_steps": 3,
            "message": "",
            "paused": True,
        }
        return client

    def _publisher(self, mode: str = "live") -> mock.Mock:
        publisher = mock.Mock()
        publisher.apply.return_value = {"mode": mode, "application": "demo-app-m4-p1"}
        return publisher

    def _run(self, client, publisher=None, record=None, **kwargs):
        record = record or FakeRecord()
        result = release._run_cluster_canary(
            ALERT,
            PATCH,
            5,
            10,
            publisher=publisher or self._publisher(),
            record=record,
            client=client,
            **kwargs,
        )
        return result, record

    def test_healthy_returns_argocd_slo_source(self) -> None:
        result, record = self._run(self._client("HEALTHY"))
        self.assertTrue(result.healthy)
        self.assertEqual(result.mode, "argocd")
        self.assertEqual(result.release_status, "HEALTHY")
        self.assertEqual(result.slo_source, "argocd-rollout")
        # 数值口径未知，返回 -1 而不是伪造 0
        self.assertEqual(result.error_rate, -1.0)
        self.assertEqual(result.p99_latency_ms, -1.0)
        self.assertIn("ArgoCD 报告 HEALTHY", result.observation)
        self.assertEqual(record.release_status, "HEALTHY")

    def test_degraded_returns_unhealthy(self) -> None:
        client = self._client("DEGRADED", health="Degraded", message="5xx spike")
        result, record = self._run(client)
        self.assertFalse(result.healthy)
        self.assertEqual(result.release_status, "DEGRADED")
        self.assertIn("5xx spike", result.observation)
        self.assertEqual(record.release_status, "DEGRADED")

    def test_failed_state_raises_safety_error(self) -> None:
        client = self._client("FAILED", sync="", health="", message="超时")
        with self.assertRaises(ReleaseSafetyError):
            self._run(client)

    def test_recorded_publication_is_rejected(self) -> None:
        with self.assertRaises(ReleaseSafetyError):
            self._run(self._client("HEALTHY"), publisher=self._publisher("recorded"))

    def test_unconfigured_client_is_rejected(self) -> None:
        client = self._client("HEALTHY")
        client.configured = False
        record = FakeRecord()
        with self.assertRaises(ReleaseSafetyError):
            release._run_cluster_canary(
                ALERT, PATCH, 5, 10, publisher=self._publisher(), record=record, client=client
            )

    def test_rollout_progress_is_appended(self) -> None:
        client = self._client("HEALTHY")
        with mock.patch.dict(os.environ, {"AIOPS_ROLLOUT_NAME": "demo-rollout"}, clear=False):
            result, _ = self._run(client)
        self.assertIn("步骤 1/3", result.observation)
        self.assertIn("暂停中", result.observation)

    def test_state_transitions_recorded_on_journal(self) -> None:
        client = self._client("HEALTHY")

        def _wait(name, timeout=None, on_state=None):
            for state in ("SUBMITTED", "SYNCING", "HEALTHY"):
                if on_state:
                    on_state(state)
            return {"state": "HEALTHY", "sync": "Synced", "health": "Healthy", "message": ""}

        client.wait.side_effect = _wait
        record = FakeRecord()
        release._run_cluster_canary(
            ALERT, PATCH, 5, 10, publisher=self._publisher(), record=record, client=client
        )
        self.assertEqual(record.release_status, "HEALTHY")
        self.assertEqual(record.values.get("release_status"), "HEALTHY")
        self.assertEqual(record.values.get("application"), "demo-app-m4-p1")


class ClusterJournalTest(unittest.TestCase):
    def test_failure_triggers_argo_rollback(self) -> None:
        record = {
            "status": "occupied",
            "resources": [],
            "target": {"kind": "argocd-application", "name": "demo-app-m4-p1"},
            "stages": {},
            "before": {},
        }
        journal = FakeJournal(record)
        fake_client = mock.Mock()
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            release.journal, "locked", contextlib.nullcontext
        ), mock.patch.object(
            release.journal, "resource_locks", lambda resources: contextlib.nullcontext()
        ), mock.patch.object(
            release.argo_status, "ArgoStatusClient", return_value=fake_client
        ), mock.patch.object(
            release, "_run_cluster_canary", side_effect=RuntimeError("apply failed")
        ):
            with self.assertRaises(ReleaseSafetyError):
                release._run_canary_cluster(
                    ALERT, PATCH, 5, 10, workflow_id="wf", namespace="ns", cancel_check=None
                )
        fake_client.rollback.assert_called_once_with("demo-app-m4-p1")
        self.assertEqual(record.get("release_status"), "ROLLED_BACK")
        self.assertEqual(journal.manual_calls, [(True, "发布或留痕异常，已停止推进")])

    def test_claim_target_is_argocd_application(self) -> None:
        journal = FakeJournal(None)
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            release.journal, "locked", contextlib.nullcontext
        ), mock.patch.object(
            release.journal, "resource_locks", lambda resources: contextlib.nullcontext()
        ), mock.patch.object(
            release.argo_status, "ArgoStatusClient"
        ) as client_cls, mock.patch.object(
            release, "_run_cluster_canary",
            return_value=CanaryResult(PATCH.patch_id, 5, True, -1, -1, "ok", mode="argocd"),
        ):
            client_cls.return_value.configured = True
            release._run_canary_cluster(
                ALERT, PATCH, 5, 10, workflow_id="wf", namespace="ns", cancel_check=None
            )
        self.assertEqual(journal.record["target"]["kind"], "argocd-application")
        self.assertEqual(journal.record["resources"], ["argocd-app:demo-app-m4-p1"])


class FinalizeClusterTest(unittest.TestCase):
    def test_healthy_reports_release_complete(self) -> None:
        canary = CanaryResult(
            PATCH.patch_id, 5, True, -1, -1, "HEALTHY", mode="argocd",
            release_status="HEALTHY", slo_source="argocd-rollout",
        )
        journal = FakeJournal({"application": "demo-app-m4-p1", "version": "v1.0.100"})
        with mock.patch.object(release, "_identity", return_value=journal):
            result = release.run_finalize_cluster(canary, True)
        self.assertFalse(result.rolled_back)
        self.assertEqual(result.status, "HEALTHY")
        self.assertIn("demo-app-m4-p1", result.reason)

    def test_degraded_rolls_back(self) -> None:
        canary = CanaryResult(
            PATCH.patch_id, 5, False, -1, -1, "DEGRADED", mode="argocd",
            release_status="DEGRADED", slo_source="argocd-rollout",
        )
        journal = FakeJournal({"application": "demo-app-m4-p1", "version": "v1.0.100"})
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            argo_status, "ArgoStatusClient"
        ) as client_cls:
            result = release.run_finalize_cluster(canary, True)
        client_cls.return_value.rollback.assert_called_once_with("demo-app-m4-p1")
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.status, "ROLLED_BACK")

    def test_degraded_without_auto_rollback_keeps_state(self) -> None:
        canary = CanaryResult(
            PATCH.patch_id, 5, False, -1, -1, "DEGRADED", mode="argocd",
            release_status="DEGRADED", slo_source="argocd-rollout",
        )
        journal = FakeJournal({"application": "demo-app-m4-p1"})
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            argo_status, "ArgoStatusClient"
        ) as client_cls:
            result = release.run_finalize_cluster(canary, False)
        client_cls.return_value.rollback.assert_not_called()
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.status, "DEGRADED")
        self.assertIn("等待人工处理", result.reason)

    def test_rollback_failure_requires_human(self) -> None:
        canary = CanaryResult(
            PATCH.patch_id, 5, False, -1, -1, "DEGRADED", mode="argocd",
            release_status="DEGRADED", slo_source="argocd-rollout",
        )
        journal = FakeJournal({"application": "demo-app-m4-p1"})
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            argo_status, "ArgoStatusClient"
        ) as client_cls:
            client_cls.return_value.rollback.side_effect = argo_status.ArgoStatusError("boom")
            with self.assertRaises(ReleaseSafetyError):
                release.run_finalize_cluster(canary, True)


class FinalizeDispatchTest(unittest.TestCase):
    def _canary(self, healthy: bool = True) -> CanaryResult:
        return CanaryResult(
            PATCH.patch_id, 5, healthy, -1, -1, "cluster", mode="argocd",
            release_status="HEALTHY" if healthy else "DEGRADED",
            slo_source="argocd-rollout",
        )

    def test_argocd_canary_uses_cluster_finalize(self) -> None:
        canary = self._canary()
        journal = FakeJournal(_record_for(canary))
        expected = ReleaseResult("v1.0.100", False, "done", status="HEALTHY")
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            release.journal, "locked", contextlib.nullcontext
        ), mock.patch.object(
            release, "run_finalize_cluster", return_value=expected
        ) as cluster, mock.patch.object(release, "_run_finalize") as docker:
            result = release.run_finalize(canary, True)
        self.assertIs(result, expected)
        cluster.assert_called_once()
        docker.assert_not_called()

    def test_target_kind_mismatch_blocks(self) -> None:
        canary = self._canary()
        journal = FakeJournal(_record_for(canary, target={"kind": "container", "name": "x", "port": 1}))
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            release.journal, "locked", contextlib.nullcontext
        ), mock.patch.object(release, "run_finalize_cluster") as cluster:
            with self.assertRaises(ReleaseSafetyError):
                release.run_finalize(canary, True)
        cluster.assert_not_called()

    def test_canary_result_mismatch_blocks(self) -> None:
        canary = self._canary()
        record = _record_for(canary)
        record["stages"]["canary"]["result"] = {**record["stages"]["canary"]["result"], "healthy": False}
        journal = FakeJournal(record)
        with mock.patch.object(release, "_identity", return_value=journal), mock.patch.object(
            release.journal, "locked", contextlib.nullcontext
        ):
            with self.assertRaises(ReleaseSafetyError):
                release.run_finalize(canary, True)


if __name__ == "__main__":
    unittest.main()
