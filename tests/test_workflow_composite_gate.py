"""M3 组合放行判据的工作流接入：放行/拦截事件、非代码根因与旧流程兼容。

用 mock 替换 workflow 运行时 API，只验证命令序列与终态，不启动 Temporal；
``assess_fix_scope`` 活动以桩返回，覆盖文件可定位、路由可达与模型未达标三种情况。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from aiops_agent import activities, gate_decision, model_routing, workflows
from aiops_agent.models import Alert, RootCause

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ALERT = Alert("composite-alert", "nl2sql")
POLICY = {
    "triage": {
        "confidence_threshold": 0.8,
        "max_fix_retries": 0,
        "composite_gate": {"enabled": True, "require_suspect_files": True},
        "fix_routing": {
            "require_quality_bar": True,
            "quality_bar_path": "./data/model_quality_bar.json",
            "prefer_qoder_for_multi_file": True,
        },
    },
    "approval": {"timeout_seconds": 30, "second_approval_dirs": []},
    "notify_window": {"countdown_seconds": 30},
    "canary": {"traffic_percent": 5, "observe_seconds": 10, "auto_rollback": True},
}


class _StopAfterGate(Exception):
    """通过组合判据后中断，避免执行完整修复链路。"""


def _evidence() -> dict:
    return {
        "alert_id": ALERT.alert_id,
        "service": ALERT.service,
        "lines": ["ERROR boom"],
        "trace_ids": ["trace-1"],
        "loki_available": True,
        "quality": {"status": "OK", "detail": "", "escalation_reason": ""},
    }


def _scope(*, found=("order_service.py",), missing=(), route="code_fix") -> dict:
    routing = model_routing.route_fix(
        list(found) + list(missing),
        explicit_provider="ollama",
        qoder_available=False,
        require_quality_bar=route == "human",
    )
    return {
        "repo_dir": "/repo",
        "scope_known": True,
        "files_found": list(found),
        "files_missing": list(missing),
        "routing": {**routing.to_dict(), "route": route},
    }


class CompositeGateWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def _patched(self, enabled: bool):
        def patched(name: str) -> bool:
            if name in ("evidence-quality-g1", "evidence-archive-g1", "composite-gate-v1"):
                return enabled
            return False

        return patched

    async def _run(
        self,
        root_cause: RootCause,
        scope: dict,
        *,
        patched: bool = True,
        policy: dict | None = None,
    ):
        instance = workflows.AIOpsFixWorkflow()
        calls: list[tuple[str, tuple]] = []

        async def call(fn, *args, **kwargs):
            calls.append((fn.__name__, args))
            if fn is activities.collect_evidence:
                return _evidence()
            if fn is activities.cluster_logs:
                return {"templates": [], "compression_ratio": 0.0}
            if fn is activities.archive_evidence:
                return {"stored_lines": 1}
            if fn is activities.analyze_root_cause:
                return root_cause
            if fn is activities.assess_fix_scope:
                return scope
            if fn is activities.escalate_to_human:
                return None
            if fn is activities.retrieve_similar_fixes:
                raise _StopAfterGate()
            return None

        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
            workflows.workflow, "patched", side_effect=self._patched(patched)
        ), mock.patch.object(workflows.workflow, "now", return_value=NOW), mock.patch.object(
            workflows.workflow, "info", return_value=mock.Mock(workflow_id="wf-composite")
        ):
            try:
                result = await instance.run(ALERT, policy or POLICY)
            except _StopAfterGate:
                result = {"stage": "PASSED_GATE", "gate_events": list(instance._gate_events)}
        return result, calls

    async def test_code_root_cause_with_scope_passes_gate(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        result, calls = await self._run(root, _scope())
        self.assertEqual(result["stage"], "PASSED_GATE")
        names = [name for name, _ in calls]
        self.assertIn("assess_fix_scope", names)
        self.assertIn("gate1:composite:pass", result["gate_events"])

    async def test_assess_scope_receives_routing_options(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        _, calls = await self._run(root, _scope())
        options = next(args[2] for name, args in calls if name == "assess_fix_scope")
        self.assertTrue(options["require_quality_bar"])
        self.assertEqual(options["quality_bar_path"], "./data/model_quality_bar.json")
        self.assertTrue(options["prefer_qoder_for_multi_file"])

    async def test_high_confidence_infra_root_cause_escalates_to_runbook(self) -> None:
        root = RootCause(
            "DatabaseConnectionPoolExhausted",
            ["src/db/pool.py"],
            0.95,
            "Hikari 连接池耗尽，等待超时",
        )
        result, calls = await self._run(root, _scope(found=("src/db/pool.py",)))
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("gate1:composite:blocked:non_code_root_cause:infrastructure", result["gate_events"])
        escalate = next(args for name, args in calls if name == "escalate_to_human")
        self.assertIn("Runbook", escalate[1])
        self.assertNotIn("retrieve_similar_fixes", [name for name, _ in calls])

    async def test_missing_suspect_file_blocks(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        result, calls = await self._run(root, _scope(found=(), missing=("order_service.py",)))
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("gate1:composite:blocked:suspect_file_not_found", result["gate_events"])
        self.assertNotIn("retrieve_similar_fixes", [name for name, _ in calls])

    async def test_model_not_qualified_blocks(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        result, _ = await self._run(root, _scope(route="human"))
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("gate1:composite:blocked:model_not_qualified", result["gate_events"])

    async def test_disabled_gate_keeps_legacy_behavior(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        policy = {**POLICY, "triage": {**POLICY["triage"], "composite_gate": {"enabled": False}}}
        result, calls = await self._run(root, _scope(), policy=policy)
        self.assertEqual(result["stage"], "PASSED_GATE")
        self.assertNotIn("assess_fix_scope", [name for name, _ in calls])

    async def test_old_flow_history_replay_skips_scope(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.92, "空值未防护")
        result, calls = await self._run(root, _scope(), patched=False)
        self.assertEqual(result["stage"], "PASSED_GATE")
        self.assertNotIn("assess_fix_scope", [name for name, _ in calls])

    async def test_low_confidence_still_uses_legacy_event(self) -> None:
        root = RootCause("NullPointerException", ["order_service.py"], 0.42, "空值未防护")
        result, calls = await self._run(root, _scope())
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("gate1:blocked:confidence=0.42<0.8", result["gate_events"])
        self.assertNotIn("assess_fix_scope", [name for name, _ in calls])


class ReasonLabelTest(unittest.TestCase):
    def test_known_and_unknown_codes(self) -> None:
        self.assertIn("Runbook", gate_decision.reason_label("non_code_root_cause:infrastructure"))
        self.assertEqual(gate_decision.reason_label("custom_code"), "custom_code")


if __name__ == "__main__":
    unittest.main()
