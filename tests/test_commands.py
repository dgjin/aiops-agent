"""M2 写命令 outbox 与窗口命令信封：契约 / 存储 / 投递 / 重试 / 回执（全部离线）。

覆盖三层：
- ``aiops_agent.command_contract``：workflow-command-v1 结构校验与内容指纹；
- ``bff.commands``：意图审计 + 命令入库（同一事务语义）、退避重试、回执收敛；
- ``aiops_agent.workflows``：deploy/queue 信封的 command_id 幂等与拒收留痕。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from aiops_agent import command_contract, kill_switch, workflows
from aiops_agent import db as db_layer
from aiops_agent.models import Alert
from bff import audit, commands
from bff.deps import gw
from bff.routes import commands as commands_routes

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _approval_payload(command_id: str, decision: str = "approve", comment: str = "") -> dict:
    return {
        "contract": "approval-evidence-v1",
        "command_id": command_id,
        "gate": "approval",
        "decision": decision,
        "actor": "alice",
        "channel": "console",
        "comment": comment,
        "submitted_at": NOW.isoformat(),
        "case_id": "c-1",
        "patch_id": "p-1",
        "diff_hash": "a" * 64,
        "test_report_hash": "b" * 64,
        "policy_sha256": "c" * 64,
        "run_id": "run-1",
    }


def _deploy_envelope(command_id: str = "cmd-deploy-1", command: str = "deploy_now") -> dict:
    return command_contract.build_envelope(
        command_type="deploy-command",
        command_id=command_id,
        actor="alice",
        channel="console",
        submitted_at=command_contract.utc_now_iso(),
        payload=command_contract.deploy_payload(command),
    )


def _queue_envelope(command_id: str = "cmd-queue-1") -> dict:
    return command_contract.build_envelope(
        command_type="queue-patch",
        command_id=command_id,
        actor="alice",
        channel="console",
        submitted_at=command_contract.utc_now_iso(),
        payload=command_contract.queue_payload(
            new_wf_id="aiops-fix-order-a-2", service="order", alert_id="a-2"
        ),
    )


def _detail(
    *,
    wf_id: str = "wf-1",
    exec_status: str = "RUNNING",
    stage: str = "WAIT_APPROVAL",
    extra: dict | None = None,
) -> dict:
    status = {"stage": stage, **(extra or {})}
    return {"wf_id": wf_id, "exec_status": exec_status, "status": status, "errors": []}


class _StoreCase(unittest.IsolatedAsyncioTestCase):
    """每个用例独立数据目录，避免 spool / 审计互相污染。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(audit, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        commands.reset_for_tests()

    def _record(self, **overrides) -> dict:
        params = {
            "command_type": "approval",
            "wf_id": "wf-1",
            "actor": "alice",
            "channel": "console",
            "payload": _approval_payload("cmd-a1"),
            "audit_action": "approval:approve",
            "command_id": "cmd-a1",
            "run_id": "run-1",
            "audit_params": {"decision": "approve", "comment": "同意"},
        }
        params.update(overrides)
        return commands.record_intent(**params)


class CommandContractTest(unittest.TestCase):
    def test_new_command_id_prefix_and_length(self) -> None:
        command_id = command_contract.new_command_id()
        self.assertTrue(command_id.startswith("cmd-"))
        self.assertLessEqual(len(command_id), 64)

    def test_valid_envelopes(self) -> None:
        self.assertIsNone(command_contract.envelope_problem(_deploy_envelope(), "deploy-command"))
        self.assertIsNone(command_contract.envelope_problem(_queue_envelope(), "queue-patch"))

    def test_structure_problems(self) -> None:
        cases = [
            ("legacy", "deploy-command", "missing-envelope"),
            ({**_deploy_envelope(), "actor": ""}, "deploy-command", "missing-actor"),
            ({**_deploy_envelope(), "channel": "sms"}, "deploy-command", "invalid-channel"),
            (
                {**_deploy_envelope(), "payload": {"command": "reboot"}},
                "deploy-command",
                "invalid-command",
            ),
            (
                {**_queue_envelope(), "payload": {"new_wf_id": ""}},
                "queue-patch",
                "missing-workflow-id",
            ),
            ({**_deploy_envelope(), "type": "queue-patch"}, "deploy-command", "wrong-type"),
        ]
        for envelope, expected_type, code in cases:
            problem = command_contract.envelope_problem(envelope, expected_type)
            self.assertIsNotNone(problem, envelope)
            self.assertEqual(problem[0], code)

    def test_digest_stable_and_content_sensitive(self) -> None:
        envelope = _deploy_envelope()
        self.assertEqual(
            command_contract.envelope_digest(envelope),
            command_contract.envelope_digest(dict(envelope)),
        )
        changed = {**envelope, "payload": {"command": "cancel"}}
        self.assertNotEqual(
            command_contract.envelope_digest(envelope),
            command_contract.envelope_digest(changed),
        )


class CommandStoreTest(_StoreCase):
    def test_record_intent_writes_pending_audit_and_spool(self) -> None:
        record = self._record()
        self.assertEqual(record["command_id"], "cmd-a1")
        self.assertEqual(record["status"], "PENDING")
        self.assertEqual(record["audit_params"]["comment"], "同意")

        stored = commands.get("cmd-a1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["wf_id"], "wf-1")

        rows = audit.read_audit()
        self.assertEqual(rows[0]["result"], "pending")
        self.assertEqual(rows[0]["params"]["delivery"], "PENDING")
        self.assertEqual(rows[0]["params"]["command_id"], "cmd-a1")
        self.assertEqual(rows[0]["params"]["comment"], "同意")

    def test_record_intent_duplicate_id_fails_closed(self) -> None:
        self._record()
        with self.assertRaises(commands.CommandStoreError):
            self._record()

    def test_record_intent_does_not_signal(self) -> None:
        with mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal:
            self._record()
        signal.assert_not_awaited()

    def test_corrupted_spool_fails_closed(self) -> None:
        (Path(self.tmp.name) / commands.SPOOL_NAME).write_text("{not-json", encoding="utf-8")
        with self.assertRaises(commands.CommandStoreError):
            commands.list_commands()
        with self.assertRaises(commands.CommandStoreError):
            self._record(command_id="cmd-a2")

    def test_list_filters_and_oldest_first(self) -> None:
        self._record(command_id="cmd-a1")
        self._record(
            command_id="cmd-a2",
            command_type="deploy-command",
            payload=_deploy_envelope("cmd-a2"),
            audit_action="deploy-command:deploy_now",
        )
        commands._update("cmd-a2", status="FAILED", completed_at=commands._now_iso())

        pending = commands.list_commands(status="PENDING")
        self.assertEqual([item["command_id"] for item in pending], ["cmd-a1"])
        by_wf = commands.list_commands(wf_id="wf-1", limit=10)
        self.assertEqual(len(by_wf), 2)
        oldest = commands.list_commands(status="PENDING", oldest_first=True)
        self.assertEqual(oldest[0]["command_id"], "cmd-a1")

    def test_stats_reports_backlog_and_recent_failures(self) -> None:
        self._record(command_id="cmd-a1")
        self._record(command_id="cmd-a2")
        commands._update("cmd-a2", status="FAILED", completed_at=commands._now_iso())

        stats = commands.stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["failed_24h"], 1)
        self.assertGreaterEqual(stats["oldest_pending_seconds"], 0.0)


class CommandDeliveryTest(_StoreCase):
    async def test_approval_accepted(self) -> None:
        self._record()
        accepted = {"command_id": "cmd-a1", "gate": "approval", "decision": "approve"}
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal, \
             mock.patch.object(
                 gw, "approval_receipt", mock.AsyncMock(return_value=("accepted", accepted))
             ) as receipt, \
             mock.patch.object(gw, "invalidate_cache") as invalidate, \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "ACCEPTED")
        signal.assert_awaited_once_with("wf-1", _approval_payload("cmd-a1"))
        receipt.assert_awaited_once_with("wf-1", "cmd-a1")
        invalidate.assert_called_once()
        rows = audit.read_audit()
        self.assertEqual(rows[0]["result"], "signaled")
        self.assertEqual(rows[0]["params"]["delivery"], "accepted")

    async def test_approval_rejected_by_workflow(self) -> None:
        self._record()
        rejection = {"command_id": "cmd-a1", "reason": "证据绑定已变化"}
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()), \
             mock.patch.object(
                 gw, "approval_receipt", mock.AsyncMock(return_value=("rejected", rejection))
             ), \
             mock.patch.object(gw, "invalidate_cache"), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("证据绑定已变化", result["last_error"])
        rows = audit.read_audit()
        self.assertEqual(rows[0]["result"], "rejected")
        self.assertEqual(rows[0]["params"]["delivery"], "rejected")

    async def test_temporal_unreachable_schedules_retry_without_signal(self) -> None:
        self._record()
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(side_effect=RuntimeError("down"))), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal, \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(result["next_retry_at"])
        self.assertIn("Temporal", result["last_error"])
        signal.assert_not_awaited()

    async def test_signal_failure_retries_then_fails(self) -> None:
        self._record(max_attempts=1)
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock(side_effect=RuntimeError("boom"))), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Temporal 投递失败", result["last_error"])

    async def test_kill_switch_active_fails_closed(self) -> None:
        self._record()
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal, \
             mock.patch.object(
                 kill_switch, "get_state", return_value={"active": True, "reason": "演练"}
             ):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("kill switch", result["last_error"])
        signal.assert_not_awaited()

    async def test_stage_changed_fails_without_signal(self) -> None:
        self._record()
        with mock.patch.object(
            gw, "flow_detail", mock.AsyncMock(return_value=_detail(stage="FIXING"))
        ), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal, \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("流程阶段已变化", result["last_error"])
        signal.assert_not_awaited()

    async def test_replay_window_uses_receipt_without_resending(self) -> None:
        """signal 已送达但进程在回写前崩溃：重试先查回执，不重复发送。"""
        self._record()
        commands._update("cmd-a1", attempts=1)
        accepted = {"command_id": "cmd-a1", "gate": "approval", "decision": "approve"}
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()) as signal, \
             mock.patch.object(
                 gw, "approval_receipt", mock.AsyncMock(return_value=("accepted", accepted))
             ), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-a1")

        self.assertEqual(result["status"], "ACCEPTED")
        signal.assert_not_awaited()

    async def test_deploy_receipt_uses_command_ids(self) -> None:
        envelope = _deploy_envelope()
        self._record(
            command_type="deploy-command",
            command_id="cmd-deploy-1",
            payload=envelope,
            audit_action="deploy-command:deploy_now",
        )
        after = _detail(stage="NOTIFYING", extra={"command_ids": ["cmd-deploy-1"]})
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=after)), \
             mock.patch.object(gw, "signal_deploy_command", mock.AsyncMock()) as signal, \
             mock.patch.object(gw, "invalidate_cache"), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-deploy-1")

        self.assertEqual(result["status"], "ACCEPTED")
        signal.assert_awaited_once_with("wf-1", envelope)

    async def test_queue_receipt_uses_command_ids(self) -> None:
        envelope = _queue_envelope()
        self._record(
            command_type="queue-patch",
            command_id="cmd-queue-1",
            payload=envelope,
            audit_action="queue-patch",
        )
        after = _detail(stage="NOTIFYING", extra={"command_ids": ["cmd-queue-1"]})
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=after)), \
             mock.patch.object(gw, "signal_queue_patch", mock.AsyncMock()) as signal, \
             mock.patch.object(gw, "invalidate_cache"), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-queue-1")

        self.assertEqual(result["status"], "ACCEPTED")
        signal.assert_awaited_once_with("wf-1", envelope)

    async def test_workflow_command_rejection_is_reported(self) -> None:
        self._record(
            command_type="deploy-command",
            command_id="cmd-deploy-1",
            payload=_deploy_envelope(),
            audit_action="deploy-command:deploy_now",
        )
        rejection = {"command_id": "cmd-deploy-1", "code": "wrong-stage", "reason": "阶段已变化"}
        after = _detail(
            stage="NOTIFYING",
            extra={"command_rejections": [rejection], "command_ids": []},
        )
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=after)), \
             mock.patch.object(gw, "signal_deploy_command", mock.AsyncMock()), \
             mock.patch.object(gw, "invalidate_cache"), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-deploy-1")

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("阶段已变化", result["last_error"])

    async def test_refresh_receipt_only_for_submitted(self) -> None:
        self._record()
        commands._update("cmd-a1", status="SUBMITTED", attempts=1, submitted_at=commands._now_iso())
        accepted = {"command_id": "cmd-a1"}
        with mock.patch.object(gw, "approval_receipt", mock.AsyncMock(return_value=("accepted", accepted))):
            result = await commands.refresh_receipt("cmd-a1")
        self.assertEqual(result["status"], "ACCEPTED")

        with mock.patch.object(gw, "approval_receipt", mock.AsyncMock()) as receipt:
            again = await commands.refresh_receipt("cmd-a1")
        self.assertEqual(again["status"], "ACCEPTED")
        receipt.assert_not_awaited()

    async def test_dispatch_due_respects_backoff(self) -> None:
        self._record(command_id="cmd-a1")
        commands._update(
            "cmd-a1",
            next_retry_at=(datetime.now(timezone.utc).replace(year=2100)).isoformat(),
        )
        with mock.patch.object(commands, "deliver", mock.AsyncMock(return_value={})) as deliver:
            self.assertEqual(await commands.dispatch_due(), [])
        deliver.assert_not_awaited()

        commands._update("cmd-a1", next_retry_at="2000-01-01T00:00:00+00:00")
        with mock.patch.object(commands, "deliver", mock.AsyncMock(return_value={"status": "ACCEPTED"})):
            self.assertEqual(len(await commands.dispatch_due()), 1)


class ProductionStoreTest(unittest.IsolatedAsyncioTestCase):
    """production 后端：``audit_events`` 与 ``workflow_commands`` 同库同事务。

    以 SQLite 临时库替代 MySQL（同一 SQLAlchemy 元数据与事务语义），
    验证意图审计与命令写入要么同时成功、要么同时不存在。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        url = f"sqlite:///{Path(self.tmp.name) / 'commands.db'}"
        env = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": url}
        )
        env.start()
        self.addCleanup(env.stop)
        db_layer.reset_engine()
        self.addCleanup(db_layer.reset_engine)
        commands.reset_for_tests()

    def _record(self, command_id: str = "cmd-p1") -> dict:
        return commands.record_intent(
            command_type="approval",
            wf_id="wf-1",
            actor="alice",
            channel="console",
            payload=_approval_payload(command_id),
            audit_action="approval:approve",
            command_id=command_id,
            run_id="run-1",
            audit_params={"decision": "approve", "comment": "同意"},
        )

    def _all(self, table) -> list[dict]:
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(table)).mappings()]

    def test_record_intent_writes_audit_and_command(self) -> None:
        record = self._record()
        self.assertEqual(record["status"], "PENDING")

        audit_rows = self._all(db_layer.audit_events)
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["result"], "pending")
        self.assertEqual(audit_rows[0]["detail"]["command_id"], "cmd-p1")
        self.assertEqual(audit_rows[0]["detail"]["comment"], "同意")
        self.assertEqual(audit_rows[0]["detail"]["delivery"], "PENDING")

        command_rows = self._all(db_layer.workflow_commands)
        self.assertEqual(len(command_rows), 1)
        self.assertEqual(command_rows[0]["status"], "PENDING")
        self.assertEqual(
            command_rows[0]["audit_params"], {"decision": "approve", "comment": "同意"}
        )
        stored = commands.get("cmd-p1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["audit_params"]["comment"], "同意")

    def test_duplicate_id_writes_nothing_extra(self) -> None:
        self._record()
        with self.assertRaises(commands.CommandStoreError):
            self._record()
        self.assertEqual(len(self._all(db_layer.audit_events)), 1)
        self.assertEqual(len(self._all(db_layer.workflow_commands)), 1)

    def test_audit_failure_rolls_back_command(self) -> None:
        """审计表不可写时命令不得落库——同事务保证「无审计推进」不可能出现。"""
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE audit_events")
        with self.assertRaises(commands.CommandStoreError):
            self._record("cmd-p2")
        self.assertEqual(self._all(db_layer.workflow_commands), [])

    async def test_delivery_persists_status_and_outcome_audit(self) -> None:
        self._record()
        accepted = {"command_id": "cmd-p1", "gate": "approval", "decision": "approve"}
        with mock.patch.object(gw, "flow_detail", mock.AsyncMock(return_value=_detail())), \
             mock.patch.object(gw, "signal_approval", mock.AsyncMock()), \
             mock.patch.object(
                 gw, "approval_receipt", mock.AsyncMock(return_value=("accepted", accepted))
             ), \
             mock.patch.object(gw, "invalidate_cache"), \
             mock.patch.object(kill_switch, "get_state", return_value={"active": False}):
            result = await commands.deliver("cmd-p1")

        self.assertEqual(result["status"], "ACCEPTED")
        stored = commands.get("cmd-p1")
        self.assertEqual(stored["status"], "ACCEPTED")
        self.assertTrue(stored["completed_at"])
        rows = audit.read_audit()
        self.assertEqual(sorted(row["result"] for row in rows), ["pending", "signaled"])
        outcome = next(row for row in rows if row["result"] == "signaled")
        self.assertEqual(outcome["params"]["delivery"], "accepted")
        self.assertEqual(outcome["params"]["command_id"], "cmd-p1")

    def test_list_and_stats_read_from_db(self) -> None:
        self._record("cmd-p1")
        self._record("cmd-p2")
        commands._update("cmd-p2", status="FAILED", completed_at=commands._now_iso())

        pending = commands.list_commands(status="PENDING")
        self.assertEqual([item["command_id"] for item in pending], ["cmd-p1"])
        stats = commands.stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["failed_24h"], 1)
        self.assertGreaterEqual(stats["oldest_pending_seconds"], 0.0)


class CommandRoutesTest(_StoreCase):
    async def test_list_and_stats(self) -> None:
        self._record()
        payload = await commands_routes.api_commands(status="", wf_id="", limit=50)
        self.assertIn("server_time", payload)
        self.assertEqual(payload["items"][0]["command_id"], "cmd-a1")
        self.assertEqual(payload["stats"]["pending"], 1)

    async def test_detail_not_found(self) -> None:
        from bff.deps import ApiError

        with self.assertRaises(ApiError) as ctx:
            await commands_routes.api_command("cmd-missing")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_detail_returns_record(self) -> None:
        self._record()
        payload = await commands_routes.api_command("cmd-a1")
        self.assertEqual(payload["command"]["status"], "PENDING")


class WorkflowCommandSignalTest(unittest.IsolatedAsyncioTestCase):
    """工作流侧：信封按 command_id 幂等，非法命令只留痕不推进。"""

    def setUp(self) -> None:
        self.workflow = workflows.AIOpsFixWorkflow()
        self.workflow.stage = "NOTIFYING"

    def test_deploy_envelope_applies_once(self) -> None:
        envelope = _deploy_envelope()
        self.workflow.submit_deploy_command(envelope)
        self.assertEqual(self.workflow._deploy_command, "deploy_now")
        self.assertIn("cmd-deploy-1", self.workflow._command_ids)

        before = len(self.workflow._gate_events)
        self.workflow.submit_deploy_command(dict(envelope))
        self.assertEqual(len(self.workflow._gate_events), before + 1)
        self.assertIn("command:duplicate:deploy-command:cmd-deploy-1", self.workflow._gate_events[-1])

    def test_same_id_different_content_rejected(self) -> None:
        self.workflow.submit_deploy_command(_deploy_envelope())
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command(_deploy_envelope(command="cancel"))

        self.assertEqual(self.workflow._deploy_command, "deploy_now")
        self.assertEqual(self.workflow._command_rejections[-1]["code"], "command-id-conflict")

    def test_wrong_stage_records_rejection(self) -> None:
        self.workflow.stage = "WAIT_APPROVAL"
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command(_deploy_envelope())

        self.assertIsNone(self.workflow._deploy_command)
        self.assertEqual(self.workflow._command_rejections[-1]["code"], "wrong-stage")
        self.assertNotIn("cmd-deploy-1", self.workflow._command_ids)

    def test_invalid_envelope_records_rejection(self) -> None:
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command({**_deploy_envelope(), "actor": ""})
        self.assertEqual(self.workflow._command_rejections[-1]["code"], "missing-actor")

    def test_queue_envelope_applies(self) -> None:
        self.workflow.submit_patch(_queue_envelope())
        self.assertEqual(len(self.workflow._queued_patches), 1)
        workflow_id, alert = self.workflow._queued_patches[0]
        self.assertEqual(workflow_id, "aiops-fix-order-a-2")
        self.assertEqual(alert.alert_id, "a-2")
        self.assertIn("cmd-queue-1", self.workflow._command_ids)

    def test_legacy_signal_shapes_still_work(self) -> None:
        self.workflow.submit_deploy_command("cancel")
        self.assertEqual(self.workflow._deploy_command, "cancel")

        self.workflow.submit_patch("aiops-fix-order-a-3", Alert("a-3", "order"))
        self.assertEqual(len(self.workflow._queued_patches), 1)

    def test_status_exposes_command_trace(self) -> None:
        self.workflow.submit_deploy_command(_deploy_envelope())
        with mock.patch.object(workflows.workflow, "now", return_value=NOW):
            self.workflow.submit_deploy_command(_deploy_envelope(command="cancel"))
        status = self.workflow.status()
        self.assertEqual(status["command_ids"], ["cmd-deploy-1"])
        self.assertEqual(status["command_rejections"][-1]["code"], "command-id-conflict")
        self.assertEqual(status["command_error"]["code"], "command-id-conflict")


if __name__ == "__main__":
    unittest.main()
