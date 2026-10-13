"""离线SDK history replay：合成旧版发布历史，不连接/导出真实Temporal。"""
from __future__ import annotations

import tests  # replay同样必须处于隔离钩子内
import asyncio
from datetime import datetime, timezone
import unittest
from unittest import mock

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory
from temporalio.converter import PayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Replayer
from aiops_agent import activities, workflows
from aiops_agent.models import Alert, CanaryResult, Patch, ReleaseResult, RequirementTask, RootCause, TestReport

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
QUEUE = {"name": "aiops-tasks", "kind": 1}
POLICY = {"triage": {"confidence_threshold": .7, "max_fix_retries": 0},
          "approval": {"timeout_seconds": 30, "second_approval_dirs": []},
          "notify_window": {"countdown_seconds": 30},
          "canary": {"traffic_percent": 5, "observe_seconds": 10, "auto_rollback": True}}
ALERT = Alert("replay-alert", "isolated")
CAUSE = RootCause("error", ["page.txt"], 1.0, "test")
PATCH = Patch("replay-patch", ALERT.alert_id, ["page.txt"], "test diff", "test", "low", "test", 1.0)
REPORT = TestReport(PATCH.patch_id, True)
CANARY = CanaryResult(PATCH.patch_id, 5, True, 0, 1, "healthy")
TASK = RequirementTask("analysis", "app", "entry", "isolated", "title", "requirement",
                       "plan", [], ["page.txt"], 1, "operator")


def payloads(*values):
    return {"payloads": PayloadConverter.default.to_payloads(values)}


def legacy_history(requirement=False, degraded=False, incompatible=False, failed=None):
    events = []
    def add(kind, **attrs):
        fields = {kind + "_event_attributes": attrs}
        event = HistoryEvent(event_id=len(events) + 1, event_time=NOW,
                             event_type=EventType.Value("EVENT_TYPE_" + kind.upper()), **fields)
        events.append(event)
        return event.event_id

    workflow_type = "AIOpsRequirementWorkflow" if requirement else "AIOpsFixWorkflow"
    add("workflow_execution_started", workflow_type={"name": workflow_type}, task_queue=QUEUE,
        input=payloads(TASK if requirement else ALERT, POLICY),
        workflow_task_timeout={"seconds": 10},
        original_execution_run_id="00000000-0000-4000-8000-000000000001",
        first_execution_run_id="00000000-0000-4000-8000-000000000001")

    def task():
        scheduled = add("workflow_task_scheduled", task_queue=QUEUE, start_to_close_timeout={"seconds": 10})
        started = add("workflow_task_started", scheduled_event_id=scheduled, identity="test", request_id="test")
        return scheduled, started

    pending = task()
    aid = 0
    def activity(name, result, timeout=60, signal=None):
        nonlocal pending, aid
        done = add("workflow_task_completed", scheduled_event_id=pending[0], started_event_id=pending[1], identity="test")
        aid += 1
        if incompatible and name == "deploy_canary":
            timer = add("timer_started", timer_id="incompatible-release-timer",
                        workflow_task_completed_event_id=done, start_to_fire_timeout={"seconds": 1})
            add("timer_fired", timer_id="incompatible-release-timer", started_event_id=timer)
            pending = task()
            return
        scheduled = add("activity_task_scheduled", activity_id=str(aid), activity_type={"name": name},
                        task_queue=QUEUE, workflow_task_completed_event_id=done,
                        start_to_close_timeout={"seconds": timeout},
                        retry_policy={"initial_interval": {"seconds": 1}, "backoff_coefficient": 2,
                                      "maximum_attempts": 3})
        started = add("activity_task_started", scheduled_event_id=scheduled, identity="test", attempt=1)
        if name == failed:
            add("activity_task_failed", scheduled_event_id=scheduled, started_event_id=started,
                failure={"message": "legacy release failure", "application_failure_info": {
                    "type": "LegacyReleaseFailure", "non_retryable": True}}, retry_state=2, identity="test")
        else:
            add("activity_task_completed", scheduled_event_id=scheduled, started_event_id=started,
                result=payloads(result), identity="test")
        if signal:
            add("workflow_execution_signaled", signal_name=signal[0], input=payloads(signal[1]), identity="test")
        pending = task()

    if not requirement:
        activity("collect_evidence", {"trace_ids": []})
        activity("cluster_logs", {})
        activity("analyze_root_cause", CAUSE)
    activity("retrieve_similar_fixes", [], 300)
    activity("generate_patch", PATCH, 960)
    activity("run_tests_in_sandbox", REPORT)
    activity("create_merge_request", {"mr_id": "test"})
    activity("notify_approvers", {}, signal=("submit_approval", "approve"))
    activity("notify_users", {"version": "v1"}, signal=("submit_deploy_command", "deploy_now"))
    canary = CanaryResult(PATCH.patch_id, 5, not degraded, 50 if degraded else 0, 1, "test")
    activity("deploy_canary", canary, 600)
    if failed != "deploy_canary":
        activity("finalize_release", ReleaseResult("v1", degraded, "test"), 600)
    if failed:
        done = add("workflow_task_completed", scheduled_event_id=pending[0], started_event_id=pending[1], identity="test")
        add("workflow_execution_failed", workflow_task_completed_event_id=done,
            failure={"message": "Activity task failed"})
        return WorkflowHistory("isolated-replay-failure", events)
    if degraded:
        activity("escalate_to_human", None)
    # 最后一个WFT仍未完成：复现旧运行历史，验证到finalize及回滚分支，而不伪造线上证据。
    return WorkflowHistory("isolated-replay", events)


class ReleaseHistoryReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_histories_without_patch_marker_replay(self):
        replayer = Replayer(workflows=[workflows.AIOpsFixWorkflow, workflows.AIOpsRequirementWorkflow])
        for requirement in (False, True):
            for degraded in (False, True):
                with self.subTest(requirement=requirement, degraded=degraded):
                    result = await replayer.replay_workflow(legacy_history(requirement, degraded))
                    self.assertIsNone(result.replay_failure)

    async def test_old_failed_histories_keep_legacy_failure_commands(self):
        replayer = Replayer(workflows=[workflows.AIOpsFixWorkflow, workflows.AIOpsRequirementWorkflow])
        for requirement in (False, True):
            for failed in ("deploy_canary", "finalize_release"):
                with self.subTest(requirement=requirement, failed=failed):
                    result = await replayer.replay_workflow(legacy_history(requirement, failed=failed))
                    self.assertIsNone(result.replay_failure)

    async def test_replay_rejects_incompatible_release_command(self):
        history = legacy_history(incompatible=True)
        replayer = Replayer(workflows=[workflows.AIOpsFixWorkflow])
        with self.assertRaises(workflows.workflow.NondeterminismError):
            await replayer.replay_workflow(history)

    async def test_new_marker_branch_escalates_nonretryable_failure(self):
        for failure_at in (activities.deploy_canary, activities.finalize_release):
            with self.subTest(activity=failure_at.__name__):
                await self._assert_new_failure(failure_at)

    async def test_old_pending_activity_new_failure_escalates_with_failure_marker(self):
        for failure_at in (activities.deploy_canary, activities.finalize_release):
            with self.subTest(activity=failure_at.__name__):
                await self._assert_new_failure(failure_at, old_attempt=True)

    async def _assert_new_failure(self, failure_at, old_attempt=False):
        instance = workflows.AIOpsFixWorkflow()
        instance._policy = POLICY
        instance._alert = ALERT
        calls = []
        async def call(fn, *args, **kwargs):
            calls.append((fn.__name__, kwargs))
            if fn is activities.retrieve_similar_fixes:
                return []
            if fn is activities.generate_patch:
                return PATCH
            if fn is activities.run_tests_in_sandbox:
                return REPORT
            if fn is activities.create_merge_request:
                return {"mr_id": "test"}
            if fn is activities.notify_approvers:
                instance._approval = "approve"
                return {}
            if fn is activities.notify_users:
                instance._deploy_command = "deploy_now"
                return {"version": "v1"}
            if fn is failure_at:
                error = ActivityError("safety", scheduled_event_id=1, started_event_id=2,
                    identity="test", activity_type=fn.__name__, activity_id="1", retry_state=None)
                error.__cause__ = ApplicationError("conflict", type="ReleaseSafetyError", non_retryable=True)
                raise error
            if fn is activities.deploy_canary:
                return CANARY
            return None
        with mock.patch.object(instance, "_call", side_effect=call), mock.patch.object(
                workflows.workflow, "patched", side_effect=lambda name: not old_attempt or
                name == "release-safety-failure-g1") as marker, mock.patch.object(
                workflows.workflow, "now", return_value=NOW), mock.patch.object(
                workflows.workflow, "info", return_value=mock.Mock(workflow_id="test")), mock.patch.object(
                workflows.workflow, "wait_condition", new=mock.AsyncMock()):
            result = await instance._fix_to_release(ALERT, CAUSE, NOW)
        self.assertEqual(marker.call_args_list, [mock.call("fix-start-notify-g1"), mock.call("release-safety-g1")] + (
            [mock.call("release-safety-failure-g1")] if old_attempt else []))
        self.assertEqual(result["stage"], "ESCALATED")
        if failure_at is activities.deploy_canary:
            self.assertNotIn("finalize_release", [name for name, _ in calls])
        self.assertIsNone(result["deploy_result"])
        options = {} if old_attempt else {"release_safety": True}
        self.assertIn(("deploy_canary", {"timeout": workflows._CANARY_ACTIVITY_TIMEOUT, **options}), calls)

    async def test_heartbeat_and_wait_for_cancellation_only_new_version(self):
        instance = workflows.AIOpsFixWorkflow()
        with mock.patch.object(workflows.workflow, "execute_activity", new=mock.AsyncMock()) as execute:
            await instance._call(activities.deploy_canary)
            self.assertNotIn("heartbeat_timeout", execute.call_args.kwargs)
            await instance._call(activities.deploy_canary, release_safety=True)
            self.assertEqual(execute.call_args.kwargs["heartbeat_timeout"].total_seconds(), 15)
            self.assertEqual(execute.call_args.kwargs["cancellation_type"],
                             workflows.workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)


if __name__ == "__main__":
    unittest.main()
