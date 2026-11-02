"""M4：ArgoCD/Rollouts 状态机与只读客户端（不访问真实集群）。"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from aiops_agent import argo_status


class DeriveStateTest(unittest.TestCase):
    def test_state_mapping(self) -> None:
        cases = {
            ("Synced", "Healthy"): "HEALTHY",
            ("Synced", "Degraded"): "DEGRADED",
            ("Synced", "Missing"): "FAILED",
            ("OutOfSync", "Progressing"): "SYNCING",
            ("Synced", "Progressing"): "SYNCED",
            ("Synced", ""): "SYNCED",
            ("", ""): "SUBMITTED",
        }
        for (sync, health), expected in cases.items():
            with self.subTest(sync=sync, health=health):
                self.assertEqual(argo_status.derive_state(sync, health), expected)

    def test_degraded_wins_over_sync_state(self) -> None:
        self.assertEqual(argo_status.derive_state("Unknown", "Degraded"), "DEGRADED")

    def test_terminal_states_are_explicit(self) -> None:
        self.assertEqual(
            argo_status.TERMINAL_STATES,
            frozenset({"HEALTHY", "DEGRADED", "ROLLED_BACK", "FAILED"}),
        )
        self.assertEqual(len(argo_status.STATES), 8)


class RolloutProgressTest(unittest.TestCase):
    def test_parses_steps_and_pause(self) -> None:
        payload = {
            "spec": {"strategy": {"canary": {"steps": [{"setWeight": 5}, {"pause": {}}]}}},
            "status": {
                "phase": "Paused",
                "currentStepIndex": 1,
                "message": "waiting",
                "pauseConditions": [{"reason": "CanaryPauseStep"}],
            },
        }
        progress = argo_status.rollout_progress(payload)
        self.assertEqual(progress["phase"], "Paused")
        self.assertEqual(progress["current_step"], 1)
        self.assertEqual(progress["total_steps"], 2)
        self.assertTrue(progress["paused"])

    def test_missing_fields_are_unknown_not_zero(self) -> None:
        progress = argo_status.rollout_progress({})
        self.assertEqual(progress["current_step"], -1)
        self.assertEqual(progress["total_steps"], -1)
        self.assertFalse(progress["paused"])


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


class ArgoStatusClientTest(unittest.TestCase):
    def test_configured_requires_url(self) -> None:
        self.assertFalse(argo_status.ArgoStatusClient(base_url="").configured)
        self.assertTrue(argo_status.ArgoStatusClient(base_url="https://argo").configured)

    def test_application_state_parses_status(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo")
        payload = {
            "status": {
                "sync": {"status": "Synced", "revision": "abc123"},
                "health": {"status": "Healthy"},
                "rollout": {"step": 2},
            }
        }
        with mock.patch.object(client, "get_application", return_value=payload):
            state = client.application_state("demo-app")
        self.assertEqual(state["state"], "HEALTHY")
        self.assertEqual(state["revision"], "abc123")
        self.assertEqual(state["rollout_step"], 2)

    def test_wait_returns_on_terminal_state_and_records_transitions(self) -> None:
        clock = _FakeClock()
        client = argo_status.ArgoStatusClient(
            base_url="https://argo", sleep=clock.sleep, clock=clock, interval=1
        )
        states = [
            {"state": "SUBMITTED", "sync": "", "health": ""},
            {"state": "SYNCING", "sync": "OutOfSync", "health": "Progressing"},
            {"state": "HEALTHY", "sync": "Synced", "health": "Healthy"},
        ]
        seen: list[str] = []
        with mock.patch.object(client, "application_state", side_effect=states):
            result = client.wait("demo-app", timeout=30, on_state=seen.append)
        self.assertEqual(result["state"], "HEALTHY")
        self.assertEqual(seen, ["SUBMITTED", "SYNCING", "HEALTHY"])

    def test_wait_timeout_reports_failed(self) -> None:
        clock = _FakeClock()
        client = argo_status.ArgoStatusClient(
            base_url="https://argo", sleep=clock.sleep, clock=clock, interval=1
        )
        stuck = {"state": "SYNCING", "sync": "OutOfSync", "health": "Progressing"}
        with mock.patch.object(client, "application_state", return_value=stuck):
            result = client.wait("demo-app", timeout=3)
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("未达终态", result["message"])

    def test_wait_turns_query_error_into_failed(self) -> None:
        clock = _FakeClock()
        client = argo_status.ArgoStatusClient(
            base_url="https://argo", sleep=clock.sleep, clock=clock, interval=1
        )
        with mock.patch.object(
            client, "application_state", side_effect=argo_status.ArgoStatusError("boom")
        ):
            result = client.wait("demo-app", timeout=10)
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("boom", result["message"])

    def test_wait_degrades_is_terminal(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo")
        with mock.patch.object(
            client,
            "application_state",
            return_value={"state": "DEGRADED", "sync": "Synced", "health": "Degraded"},
        ):
            result = client.wait("demo-app", timeout=10)
        self.assertEqual(result["state"], "DEGRADED")

    def test_rollout_state_without_custom_api_is_unknown(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo")
        progress = client.rollout_state("ns", "demo")
        self.assertEqual(progress["total_steps"], -1)
        self.assertEqual(progress["phase"], "")

    def test_rollout_state_error_does_not_fake_health(self) -> None:
        custom = mock.Mock()
        custom.get_namespaced_custom_object.side_effect = RuntimeError("forbidden")
        client = argo_status.ArgoStatusClient(base_url="https://argo", custom_api=custom)
        progress = client.rollout_state("ns", "demo")
        self.assertEqual(progress["total_steps"], -1)
        self.assertIn("Rollout 不可读", progress["message"])

    def test_rollback_posts_revision(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo", token="t")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.rollback("demo-app")
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/applications/demo-app/rollback"))
        self.assertEqual(json.loads(request.data)["revision"], 0)

    def test_rollback_failure_raises(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo")
        error = urllib.error.HTTPError("https://argo", 500, "boom", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(argo_status.ArgoStatusError):
                client.rollback("demo-app")

    def test_get_application_http_error_is_wrapped(self) -> None:
        client = argo_status.ArgoStatusClient(base_url="https://argo")
        error = urllib.error.HTTPError("https://argo", 404, "not found", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(argo_status.ArgoStatusError):
                client.get_application("missing")


if __name__ == "__main__":
    unittest.main()
