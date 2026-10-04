"""策略强校验单测：锁定策略的回归保护（设计方案第 5 节决策表）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

说明：本测试零外部依赖（不需要 Temporal 服务端），验证
1. 生产/演示策略可正常加载；
2. 快照转换（时长 → 秒）正确；
3. 任何试图通过配置绕过锁定策略的改动都会在启动校验阶段被拒绝。
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from aiops_agent.config import Policy, load_policy, snapshot_for_workflow


def _base_policy() -> dict:
    """标准策略 dict（与 release-gate-policy.yaml 结构一致），供篡改测试使用。"""
    return {
        "release_gate": {
            "triage": {"confidence_threshold": 0.8, "max_fix_retries": 2},
            "approval": {
                "timeout": "30m",
                "on_timeout": "reject",
                "on_timeout_escalation": "phone",
                "second_approval_dirs": ["auth/**", "payment/**", "crypto/**"],
            },
            "notify_window": {
                "countdown": "15m",
                "deploy_on_expiry": True,
                "allow_deploy_now": True,
                "allow_cancel": True,
                "new_patch_during_window": {
                    "strategy": "queue",
                    "reset_timer": False,
                    "merge_with_current": False,
                    "queue_limit": 0,
                    "next_cycle": "auto",
                },
                "feedback_circuit_breaker": {"enabled": False},
                "concurrency": {"max_active_deployments": 1},
            },
            "canary": {
                "traffic_percent": 5,
                "observe_duration": "5m",
                "health_metrics": ["error_rate", "p99_latency"],
                "auto_rollback": True,
            },
        }
    }


class TestPolicyLoading(unittest.TestCase):
    def test_production_policy_loads(self) -> None:
        policy = load_policy("release-gate-policy.yaml")
        gate = policy.release_gate
        self.assertEqual(gate.approval.second_approval_dirs, ["auth/**", "payment/**", "crypto/**"])
        self.assertEqual(gate.approval.timeout.total_seconds(), 1800)
        self.assertEqual(gate.notify_window.countdown.total_seconds(), 900)
        self.assertEqual(gate.canary.observe_duration.total_seconds(), 300)

    def test_demo_policy_loads(self) -> None:
        policy = load_policy("demo-policy.yaml")
        gate = policy.release_gate
        self.assertEqual(gate.approval.timeout.total_seconds(), 300)
        self.assertEqual(gate.notify_window.countdown.total_seconds(), 30)
        self.assertEqual(gate.canary.observe_duration.total_seconds(), 10)

    def test_snapshot_converts_durations_to_seconds(self) -> None:
        snapshot = snapshot_for_workflow(Policy.model_validate(_base_policy()))
        self.assertEqual(snapshot["approval"]["timeout_seconds"], 1800)
        self.assertEqual(snapshot["approval"]["second_approval_dirs"], ["auth/**", "payment/**", "crypto/**"])
        self.assertEqual(snapshot["notify_window"]["countdown_seconds"], 900)
        self.assertEqual(snapshot["canary"]["observe_seconds"], 300)
        self.assertAlmostEqual(snapshot["triage"]["confidence_threshold"], 0.8)

    def test_bad_duration_rejected(self) -> None:
        raw = _base_policy()
        raw["release_gate"]["approval"]["timeout"] = "半小时"
        with self.assertRaises(ValidationError):
            Policy.model_validate(raw)


class TestLockedPoliciesCannotBeBypassed(unittest.TestCase):
    """锁定策略：任何绕过尝试都必须在启动校验阶段失败。"""

    def _assert_rejected(self, mutate) -> None:
        raw = _base_policy()
        mutate(raw)
        with self.assertRaises(ValidationError):
            Policy.model_validate(raw)

    def test_on_timeout_must_reject(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["approval"].update({"on_timeout": "approve"})
        )

    def test_new_patch_strategy_must_queue(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["new_patch_during_window"].update(
                {"strategy": "reset"}
            )
        )

    def test_reset_timer_locked_false(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["new_patch_during_window"].update(
                {"reset_timer": True}
            )
        )

    def test_merge_locked_false(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["new_patch_during_window"].update(
                {"merge_with_current": True}
            )
        )

    def test_queue_limit_locked_zero(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["new_patch_during_window"].update(
                {"queue_limit": 10}
            )
        )

    def test_feedback_breaker_locked_off(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["feedback_circuit_breaker"].update(
                {"enabled": True}
            )
        )

    def test_concurrency_locked_one(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"]["concurrency"].update(
                {"max_active_deployments": 2}
            )
        )

    def test_deploy_on_expiry_locked_true(self) -> None:
        self._assert_rejected(
            lambda p: p["release_gate"]["notify_window"].update({"deploy_on_expiry": False})
        )

    def test_unknown_key_rejected(self) -> None:
        self._assert_rejected(lambda p: p["release_gate"].update({"unknown_section": {}}))


if __name__ == "__main__":
    unittest.main()
