"""证据质量预检的工作流接入（M1）：阻断转人工 / 放行根因 / 旧流程单参数兼容。

工作流分支由 ``workflow.patched("evidence-quality-g1")`` 守护：
- 新流程：collect_evidence 携带策略选项，quality 非 OK 时直接转人工，不调用根因模型；
- 在途旧流程：保持单参数调用（历史重放不产生额外命令）。

此处用 mock 替换 workflow 运行时 API，只验证命令序列与终态，不启动 Temporal。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from aiops_agent import activities, workflows
from aiops_agent.models import Alert, RootCause

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ALERT = Alert("gate-alert", "nl2sql")
POLICY = {
    "triage": {
        "confidence_threshold": 0.8,
        "max_fix_retries": 0,
        "evidence_lookback_minutes": 12,
        "evidence_stale_minutes": 20,
        "evidence_min_signal_lines": 2,
        "evidence_insufficient_escalate": True,
    },
    "approval": {"timeout_seconds": 30, "second_approval_dirs": []},
    "notify_window": {"countdown_seconds": 30},
    "canary": {"traffic_percent": 5, "observe_seconds": 10, "auto_rollback": True},
}


class _StopAfterEvidence(Exception):
    """旧流程用例：取证后中断，避免进入完整修复链路。"""


def _evidence(status: str, detail: str = "", reason: str = "") -> dict:
    return {
        "alert_id": ALERT.alert_id,
        "service": ALERT.service,
        "lines": ["ERROR boom"] if status == "OK" else [],
        "trace_ids": ["trace-1"],
        "loki_available": True,
        "latest_log_at": "",
        "quality": {
            "status": status,
            "detail": detail,
            "escalation_reason": reason,
        },
    }


class EvidenceGateTest(unittest.IsolatedAsyncioTestCase):
    def _patched(self, enabled: bool):
        def patched(name: str) -> bool:
            if name in ("evidence-quality-g1", "evidence-archive-g1"):
                return enabled
            return False

        return patched

    async def _run(self, evidence: dict, patched: bool = True, policy: dict | None = None):
        instance = workflows.AIOpsFixWorkflow()
        calls: list[tuple[str, tuple]] = []
        archived = {"stored_lines": 1, "quality_status": evidence["quality"]["status"]}

        async def call(fn, *args, **kwargs):
            calls.append((fn.__name__, args))
            if fn is activities.collect_evidence:
                return evidence
            if fn is activities.cluster_logs:
                return {"templates": [], "compression_ratio": 0.0}
            if fn is activities.archive_evidence:
                return archived
            if fn is activities.escalate_to_human:
                return None
            if fn is activities.analyze_root_cause:
                return RootCause("NullPointerException", ["page.txt"], 0.2, "mock")
            return None

        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
            workflows.workflow, "patched", side_effect=self._patched(patched)
        ), mock.patch.object(workflows.workflow, "now", return_value=NOW), mock.patch.object(
            workflows.workflow, "info", return_value=mock.Mock(workflow_id="wf-gate")
        ):
            result = await instance.run(ALERT, policy or POLICY)
        return result, calls, archived

    async def test_no_logs_blocks_before_root_cause(self) -> None:
        evidence = _evidence(
            "NO_LOGS",
            detail="Loki 可用，但近 12 分钟无服务 nl2sql 的日志",
            reason="观测异常：窗口内无服务日志（NO_LOGS）；Loki 可用，但近 12 分钟无服务 nl2sql 的日志",
        )
        result, calls, archived = await self._run(evidence)
        names = [name for name, _ in calls]
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("evidence:blocked:NO_LOGS", result["gate_events"])
        self.assertNotIn("analyze_root_cause", names)
        self.assertIn("escalate_to_human", names)
        self.assertEqual(result["evidence"], archived)

        # 取证携带策略选项（而非旧流程的单参数调用）
        collect = next(args for name, args in calls if name == "collect_evidence")
        self.assertEqual(len(collect), 2)
        self.assertEqual(collect[1]["lookback_minutes"], 12)
        self.assertEqual(collect[1]["stale_after_minutes"], 20)
        self.assertEqual(collect[1]["min_signal_lines"], 2)

        escalate = next(args for name, args in calls if name == "escalate_to_human")
        self.assertIn("观测异常", escalate[1])

    async def test_loki_unavailable_blocks(self) -> None:
        evidence = _evidence("LOKI_UNAVAILABLE", detail="Loki 查询失败", reason="观测异常：Loki 不可用")
        result, calls, _ = await self._run(evidence)
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("evidence:blocked:LOKI_UNAVAILABLE", result["gate_events"])
        self.assertNotIn("analyze_root_cause", [name for name, _ in calls])

    async def test_ok_passes_gate_and_reaches_root_cause(self) -> None:
        evidence = _evidence("OK")
        policy = {**POLICY, "triage": {**POLICY["triage"], "confidence_threshold": 0.8}}
        result, calls, _ = await self._run(evidence, policy=policy)
        names = [name for name, _ in calls]
        self.assertIn("analyze_root_cause", names)
        # 根因置信度 0.2 < 0.8：闸门 1 拦截（证据预检未追加事件）
        self.assertEqual(result["stage"], "ESCALATED")
        self.assertIn("gate1:blocked:confidence=0.20<0.8", result["gate_events"])
        self.assertFalse(any(event.startswith("evidence:blocked:") for event in result["gate_events"]))

    async def test_insufficient_signal_can_be_allowed_by_config(self) -> None:
        evidence = _evidence("INSUFFICIENT_SIGNAL", detail="仅健康检查行")
        policy = {
            **POLICY,
            "triage": {
                **POLICY["triage"],
                "evidence_insufficient_escalate": False,
                "confidence_threshold": 0.8,
            },
        }
        result, calls, _ = await self._run(evidence, policy=policy)
        self.assertIn("analyze_root_cause", [name for name, _ in calls])
        self.assertFalse(any(event.startswith("evidence:blocked:") for event in result["gate_events"]))

    async def test_insufficient_signal_blocks_by_default(self) -> None:
        evidence = _evidence("INSUFFICIENT_SIGNAL", detail="仅健康检查行", reason="证据不足：日志无可分析信号")
        result, calls, _ = await self._run(evidence)
        self.assertIn("evidence:blocked:INSUFFICIENT_SIGNAL", result["gate_events"])
        self.assertNotIn("analyze_root_cause", [name for name, _ in calls])
        escalate = next(args for name, args in calls if name == "escalate_to_human")
        self.assertIn("证据不足", escalate[1])

    async def test_old_flow_keeps_single_arg_collect_and_no_gate(self) -> None:
        instance = workflows.AIOpsFixWorkflow()
        calls: list[tuple[str, tuple]] = []

        async def call(fn, *args, **kwargs):
            calls.append((fn.__name__, args))
            if fn is activities.collect_evidence:
                return {"lines": [], "trace_ids": [], "loki_available": False}
            raise _StopAfterEvidence()

        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
            workflows.workflow, "patched", side_effect=self._patched(False)
        ), mock.patch.object(workflows.workflow, "now", return_value=NOW):
            with self.assertRaises(_StopAfterEvidence):
                await instance.run(ALERT, POLICY)
        collect = next(args for name, args in calls if name == "collect_evidence")
        self.assertEqual(len(collect), 1)
        self.assertIs(collect[0], ALERT)


if __name__ == "__main__":
    unittest.main()
