"""M4：静态验收不得单独支撑高风险发布的放行/拦截双侧用例。

覆盖：
    - 判据函数本身（受保护目录 / 风险描述含「高」/ 开关关闭 / 历史空模式）；
    - 工作流接入：STATIC_ONLY 高风险 → 转人工且不出 MR；
      UNIT_TEST 高风险 → 继续推进（到创建 MR 为止）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from aiops_agent import activities, workflows
from aiops_agent.models import Alert, Patch, RootCause, TestReport

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ALERT = Alert("m4-static-alert", "order")

POLICY = {
    "triage": {"confidence_threshold": 0.8, "max_fix_retries": 0, "composite_gate": {"enabled": False}},
    "approval": {"timeout_seconds": 30, "second_approval_dirs": ["demo-app"]},
    "notify_window": {"countdown_seconds": 30},
    "canary": {"traffic_percent": 5, "observe_seconds": 10, "auto_rollback": True},
    "sandbox": {"require_real_tests_for_high_risk": True},
}

ROOT_CAUSE = RootCause("NullPointerException", ["demo-app/order_service.py"], 0.95, "空值未防护")


def _patch(*, risk: str = "高：涉及订单核心链路") -> Patch:
    return Patch(
        patch_id="m4-static-p1",
        alert_id=ALERT.alert_id,
        files=["demo-app/order_service.py"],
        diff="--- a/demo-app/order_service.py\n+++ b/demo-app/order_service.py\n@@ -1 +1 @@\n-a\n+b\n",
        description="静态验收闸门单测",
        risk=risk,
        model_version="qwen2.5-coder:32b",
        confidence=0.9,
    )


class StaticBlockRuleTest(unittest.TestCase):
    def test_real_tests_pass_high_risk(self) -> None:
        report = TestReport("p", True, verification_mode="UNIT_TEST")
        self.assertEqual(
            workflows.AIOpsFixWorkflow._static_verification_block(
                _patch(), report, True, True
            ),
            "",
        )

    def test_static_only_blocks_protected_path(self) -> None:
        report = TestReport("p", True, verification_mode="STATIC_ONLY")
        reason = workflows.AIOpsFixWorkflow._static_verification_block(
            _patch(risk="低：单文件"), report, True, True
        )
        self.assertIn("发布准入", reason)
        self.assertIn("STATIC_ONLY", reason)
        self.assertIn("受保护目录：是", reason)

    def test_static_only_blocks_high_risk_text(self) -> None:
        report = TestReport("p", True, verification_mode="CONTRACT_TEST")
        reason = workflows.AIOpsFixWorkflow._static_verification_block(
            _patch(risk="high：核心链路"), report, False, True
        )
        self.assertIn("CONTRACT_TEST", reason)
        self.assertIn("受保护目录：否", reason)

    def test_low_risk_static_only_passes(self) -> None:
        report = TestReport("p", True, verification_mode="STATIC_ONLY")
        self.assertEqual(
            workflows.AIOpsFixWorkflow._static_verification_block(
                _patch(risk="低：文案"), report, False, True
            ),
            "",
        )

    def test_disabled_switch_passes(self) -> None:
        report = TestReport("p", True, verification_mode="STATIC_ONLY")
        self.assertEqual(
            workflows.AIOpsFixWorkflow._static_verification_block(
                _patch(), report, True, False
            ),
            "",
        )

    def test_missing_report_or_archived_mode_fails_closed(self) -> None:
        self.assertEqual(
            workflows.AIOpsFixWorkflow._static_verification_block(_patch(), None, True, True),
            "",
        )
        archived = TestReport("p", True)  # 历史归档：无 verification_mode
        reason = workflows.AIOpsFixWorkflow._static_verification_block(
            _patch(), archived, True, True
        )
        self.assertIn("UNKNOWN", reason)


class _StopAfterMR(Exception):
    """放行后中断，避免进入审批等待。"""


class StaticGateWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def _patched(self, enabled: bool):
        def patched(name: str) -> bool:
            return enabled and name == "sandbox-verification-gate-v1"

        return patched

    async def _run(self, report: TestReport, *, enabled: bool = True, policy: dict | None = None):
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
                return _patch()
            if fn is activities.run_tests_in_sandbox:
                return report
            if fn is activities.escalate_to_human:
                return None
            if fn is activities.create_merge_request:
                raise _StopAfterMR()
            return None

        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
            workflows.workflow, "patched", side_effect=self._patched(enabled)
        ), mock.patch.object(workflows.workflow, "now", return_value=NOW), mock.patch.object(
            workflows.workflow, "info", return_value=mock.Mock(workflow_id="wf-m4")
        ):
            try:
                result = await instance.run(ALERT, policy or POLICY)
            except _StopAfterMR:
                result = {"stage": "MR_CREATED", "gate_events": list(instance._gate_events)}
        return result, calls

    async def test_static_only_on_protected_path_escalates(self) -> None:
        report = TestReport("m4-static-p1", True, verification_mode="STATIC_ONLY")
        result, calls = await self._run(report)
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("release:blocked:static-only:high-risk", result["gate_events"])
        self.assertNotIn("create_merge_request", calls)
        self.assertIn("escalate_to_human", calls)

    async def test_real_unit_tests_continue_to_merge_request(self) -> None:
        report = TestReport(
            "m4-static-p1", True, unit_tests="12/12 passed", verification_mode="UNIT_TEST"
        )
        result, calls = await self._run(report)
        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertIn("create_merge_request", calls)
        self.assertNotIn("release:blocked:static-only:high-risk", result["gate_events"])

    async def test_switch_disabled_keeps_legacy_flow(self) -> None:
        report = TestReport("m4-static-p1", True, verification_mode="STATIC_ONLY")
        policy = {
            **POLICY,
            "sandbox": {"require_real_tests_for_high_risk": False},
        }
        result, calls = await self._run(report, policy=policy)
        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertIn("create_merge_request", calls)

    async def test_old_flow_replay_skips_new_gate(self) -> None:
        report = TestReport("m4-static-p1", True, verification_mode="STATIC_ONLY")
        result, calls = await self._run(report, enabled=False)
        self.assertEqual(result["stage"], "MR_CREATED")
        self.assertNotIn("release:blocked:static-only:high-risk", result["gate_events"])


if __name__ == "__main__":
    unittest.main()
