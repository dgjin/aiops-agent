"""M5：启动快照门禁（BFF + 工作流双侧）与确定性风险接入工作流。

覆盖：
    - ``bff.routes.flows.snapshot_command_error``：只认流程启动快照，空快照兼容；
    - 工作流 ``_capture_policy_snapshot`` / ``_snapshot_command_error``：
      ``allow_deploy_now`` / ``allow_cancel`` 禁用时拒收且不占用 command_id；
    - ``deterministic-risk-v1`` patched 门控：风险等级决定静态验收是否阻断、
      高风险是否强制双人审批；LLM 文本不再驱动闸门。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from aiops_agent import activities, command_contract, workflows
from aiops_agent.models import Alert, Patch, RootCause, TestReport
from bff.routes.flows import snapshot_command_error

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _deploy_envelope(
    command_id: str = "cmd-deploy-1", command: str = "deploy_now"
) -> dict:
    return command_contract.build_envelope(
        command_type="deploy-command",
        command_id=command_id,
        actor="alice",
        channel="console",
        submitted_at=command_contract.utc_now_iso(),
        payload=command_contract.deploy_payload(command),
    )


class SnapshotCommandErrorTest(unittest.TestCase):
    """BFF 前置校验：只读启动快照，运行期磁盘变更不影响已启动流程。"""

    def test_empty_or_missing_snapshot_is_compatible(self) -> None:
        self.assertIsNone(snapshot_command_error({}, "deploy_now"))
        self.assertIsNone(snapshot_command_error({"policy_snapshot": {}}, "cancel"))
        self.assertIsNone(snapshot_command_error({"policy_snapshot": None}, "cancel"))
        self.assertIsNone(snapshot_command_error({"policy_snapshot": "bad"}, "cancel"))

    def test_enabled_flags_allow_commands(self) -> None:
        status = {
            "policy_snapshot": {"allow_deploy_now": True, "allow_cancel": True}
        }
        self.assertIsNone(snapshot_command_error(status, "deploy_now"))
        self.assertIsNone(snapshot_command_error(status, "cancel"))

    def test_disabled_deploy_now_returns_reason(self) -> None:
        status = {"policy_snapshot": {"allow_deploy_now": False}}
        reason = snapshot_command_error(status, "deploy_now")
        self.assertIn("allow_deploy_now=false", reason)
        self.assertIn("重新发起流程", reason)
        # cancel 未禁用：不受影响
        self.assertIsNone(snapshot_command_error(status, "cancel"))

    def test_disabled_cancel_returns_reason(self) -> None:
        status = {"policy_snapshot": {"allow_cancel": False}}
        reason = snapshot_command_error(status, "cancel")
        self.assertIn("allow_cancel=false", reason)
        self.assertIsNone(snapshot_command_error(status, "deploy_now"))

    def test_missing_key_defaults_to_allowed(self) -> None:
        self.assertIsNone(
            snapshot_command_error({"policy_snapshot": {"shadow_mode": True}}, "cancel")
        )


class WorkflowSnapshotTest(unittest.TestCase):
    """工作流侧二次执行：BFF 被绕过时仍拒收并留痕。"""

    def setUp(self) -> None:
        self.workflow = workflows.AIOpsFixWorkflow()
        self.workflow.stage = "NOTIFYING"

    def test_capture_snapshot_from_policy(self) -> None:
        self.workflow._policy = {
            "policy_sha256": "a" * 64,
            "shadow_mode": False,
            "notify_window": {"allow_deploy_now": False, "allow_cancel": True},
        }
        self.workflow._capture_policy_snapshot()
        self.assertEqual(
            self.workflow._policy_snapshot,
            {
                "policy_sha256": "a" * 64,
                "shadow_mode": False,
                "allow_deploy_now": False,
                "allow_cancel": True,
            },
        )
        status = self.workflow.status()
        self.assertEqual(status["policy_snapshot"], self.workflow._policy_snapshot)

    def test_capture_defaults_when_flags_missing(self) -> None:
        self.workflow._policy = {"notify_window": {}}
        self.workflow._capture_policy_snapshot()
        self.assertTrue(self.workflow._policy_snapshot["allow_deploy_now"])
        self.assertTrue(self.workflow._policy_snapshot["allow_cancel"])

    def test_capture_ignores_missing_notify_window(self) -> None:
        self.workflow._policy = {"shadow_mode": True}
        self.workflow._capture_policy_snapshot()
        self.assertEqual(self.workflow._policy_snapshot, {})

    def test_disabled_deploy_flag_records_rejection(self) -> None:
        self.workflow._policy_snapshot = {"allow_deploy_now": False, "allow_cancel": True}
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command(_deploy_envelope())
        self.assertIsNone(self.workflow._deploy_command)
        self.assertEqual(self.workflow._command_rejections[-1]["code"], "policy-disabled")
        self.assertIn("allow_deploy_now=false", self.workflow._command_rejections[-1]["reason"])
        # 被策略拒收的指令不占用 command_id：策略放开后可重投
        self.assertNotIn("cmd-deploy-1", self.workflow._command_ids)

    def test_disabled_cancel_flag_records_rejection(self) -> None:
        self.workflow._policy_snapshot = {"allow_deploy_now": True, "allow_cancel": False}
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command(_deploy_envelope(command="cancel"))
        self.assertIsNone(self.workflow._deploy_command)
        self.assertEqual(self.workflow._command_rejections[-1]["code"], "policy-disabled")

    def test_other_command_still_allowed(self) -> None:
        self.workflow._policy_snapshot = {"allow_deploy_now": True, "allow_cancel": False}
        self.workflow.submit_deploy_command(_deploy_envelope(command="deploy_now"))
        self.assertEqual(self.workflow._deploy_command, "deploy_now")
        self.assertIn("cmd-deploy-1", self.workflow._command_ids)

    def test_legacy_empty_snapshot_keeps_old_behavior(self) -> None:
        self.workflow.submit_deploy_command(_deploy_envelope(command="cancel"))
        self.assertEqual(self.workflow._deploy_command, "cancel")
        self.assertEqual(self.workflow._command_rejections, [])


POLICY = {
    "triage": {"confidence_threshold": 0.8, "max_fix_retries": 0, "composite_gate": {"enabled": False}},
    "approval": {"timeout_seconds": 30, "second_approval_dirs": ["demo-app"]},
    "notify_window": {"countdown_seconds": 30},
    "canary": {"traffic_percent": 5, "observe_seconds": 10, "auto_rollback": True},
    "sandbox": {"require_real_tests_for_high_risk": True},
}

ALERT = Alert("m5-risk-alert", "order")
ROOT_CAUSE = RootCause("NullPointerException", ["demo-app/order_service.py"], 0.95, "空值未防护")


def _patch(*, files: list[str] | None = None, risk: str = "", degraded: bool = False) -> Patch:
    return Patch(
        patch_id="m5-risk-p1",
        alert_id=ALERT.alert_id,
        files=files or ["src/app.py"],
        diff="--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n",
        description="M5 确定性风险单测",
        risk=risk,
        model_version="qwen2.5-coder:32b",
        confidence=0.9,
        degraded=degraded,
    )


class _StopAfterMR(Exception):
    """放行后中断，避免进入审批等待。"""


class DeterministicRiskWorkflowTest(unittest.IsolatedAsyncioTestCase):
    """patched 门控下的发布准入：风险等级替代 LLM 文本。"""

    def _patched(self, *, risk_gate: bool, sandbox_gate: bool):
        enabled = {"sandbox-verification-gate-v1"} if sandbox_gate else set()
        if risk_gate:
            enabled.add("deterministic-risk-v1")

        def patched(name: str) -> bool:
            return name in enabled

        return patched

    async def _run(
        self,
        report: TestReport,
        *,
        patch: Patch | None = None,
        risk_gate: bool = True,
        sandbox_gate: bool = True,
    ):
        instance = workflows.AIOpsFixWorkflow()
        calls: list[str] = []

        async def call(fn, *args, **kwargs):
            calls.append(fn.__name__)
            if fn is activities.collect_evidence:
                return {"lines": ["ERROR"], "trace_ids": ["t-1"]}
            if fn is activities.cluster_logs:
                return {"templates": [], "compression_ratio": 0.0}
            if fn is activities.analyze_root_cause:
                return ROOT_CAUSE
            if fn is activities.retrieve_similar_fixes:
                return []
            if fn is activities.generate_patch:
                return patch or _patch()
            if fn is activities.run_tests_in_sandbox:
                return report
            if fn is activities.escalate_to_human:
                return None
            if fn is activities.create_merge_request:
                raise _StopAfterMR()
            return None

        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
            workflows.workflow,
            "patched",
            side_effect=self._patched(risk_gate=risk_gate, sandbox_gate=sandbox_gate),
        ), mock.patch.object(workflows.workflow, "now", return_value=NOW), mock.patch.object(
            workflows.workflow, "info", return_value=mock.Mock(workflow_id="wf-m5")
        ):
            try:
                result = await instance.run(ALERT, POLICY)
            except _StopAfterMR:
                result = {
                    "stage": "MR_CREATED",
                    "gate_events": list(instance._gate_events),
                    "risk_assessment": instance._risk_assessment,
                }
        return result, calls

    async def test_high_risk_with_static_only_escalates(self) -> None:
        patch = _patch(files=["demo-app/auth/login.py"], risk="低：单文件")
        report = TestReport("m5-risk-p1", True, verification_mode="STATIC_ONLY")
        result, calls = await self._run(report, patch=patch)

        self.assertEqual(result["stage"], "ESCALATED")
        self.assertTrue(
            any(
                event.startswith("risk:assessed:HIGH:protected-path")
                for event in result["gate_events"]
            )
        )
        self.assertIn("release:blocked:static-only:high-risk", result["gate_events"])
        self.assertEqual(result["risk_assessment"]["level"], "HIGH")
        self.assertTrue(result["risk_assessment"]["requires_real_tests"])
        self.assertNotIn("create_merge_request", calls)
        self.assertIn("escalate_to_human", calls)

    async def test_high_risk_with_real_tests_passes(self) -> None:
        patch = _patch(files=["demo-app/auth/login.py"], risk="低：单文件")
        report = TestReport(
            "m5-risk-p1", True, unit_tests="12/12 passed", verification_mode="UNIT_TEST"
        )
        result, calls = await self._run(report, patch=patch)

        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertTrue(
            any(
                event.startswith("risk:assessed:HIGH")
                for event in result["gate_events"]
            )
        )
        self.assertNotIn("release:blocked:static-only:high-risk", result["gate_events"])
        self.assertIn("create_merge_request", calls)

    async def test_llm_high_text_no_longer_blocks(self) -> None:
        """同等文件与真实测试下，patch.risk 的「高」不再触发阻断。"""
        patch = _patch(files=["src/app.py"], risk="高：核心链路")
        report = TestReport(
            "m5-risk-p1", True, unit_tests="1/1 passed", verification_mode="UNIT_TEST"
        )
        result, calls = await self._run(report, patch=patch)

        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertIn("risk:assessed:LOW", result["gate_events"])
        self.assertIn("create_merge_request", calls)

    async def test_static_only_medium_risk_passes(self) -> None:
        patch = _patch(files=["src/app.py"])
        report = TestReport("m5-risk-p1", True, verification_mode="STATIC_ONLY")
        result, calls = await self._run(report, patch=patch)
        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertIn("risk:assessed:MEDIUM:test-not-real", result["gate_events"])

    async def test_old_flow_replay_keeps_static_text_gate(self) -> None:
        """旧在途流程（无 deterministic-risk-v1 标记）继续按文本高风险阻断。"""
        patch = _patch(files=["src/app.py"], risk="高：历史流程")
        report = TestReport("m5-risk-p1", True, verification_mode="STATIC_ONLY")
        result, calls = await self._run(report, patch=patch, risk_gate=False)

        self.assertEqual(result["stage"], "ESCALATED")
        self.assertNotIn("risk:assessed:", " ".join(result["gate_events"]))
        self.assertIn("release:blocked:static-only:high-risk", result["gate_events"])
        self.assertNotIn("create_merge_request", calls)


if __name__ == "__main__":
    unittest.main()
