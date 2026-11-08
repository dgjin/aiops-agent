"""转人工处置闭环单测（bff/escalations，P1-3）：状态机 / 幂等登记 / 存储双模 / SLA 巡检。

覆盖三个层面：
- 纯函数：升级原因翻译（gate_events → 中文）、记录构造、assign/close/retry 状态迁移；
- 存储（demo JSON / production SQLite 注入）：幂等登记、closed 不重开、统计、热生效；
- SLA：到期判定（open/assigned 且超时且未再升级）、sweep_due 每单只升级一次。

另覆盖重试路由（routes/escalations）：需求来源待办（wf ``aiops-req-*`` 或级联
``req-N-retryM``）重试必须重启需求修复流——按告警流重试对无日志的需求必然
在闸门 1 被置信度拦截，永远无法成功（历史故障：req-19-retry1 级联）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiops_agent import db as db_layer
from bff import escalations as esc
from bff.deps import ApiError
from bff.routes import escalations as esc_routes


def _flow(
    wf_id: str = "aiops-fix-order-a1",
    stage: str = "ESCALATED",
    gate_events: list[str] | None = None,
    service: str = "order",
) -> dict:
    return {
        "wf_id": wf_id,
        "run_id": "run-a1",
        "stage": stage,
        "gate_events": gate_events
        if gate_events is not None
        else ["gate1:blocked:confidence-low"],
        "alert": {
            "alert_id": "a1",
            "service": service,
            "severity": "critical",
            "description": "订单错误率激增",
        },
    }


def _resolution_payload(**overrides) -> dict:
    """结构化关闭请求（M2）：默认一条可入知识库的 RESOLVED 记录。"""
    payload = {
        "note": "已扩容连接池并观察 30 分钟",
        "close_reason": "RESOLVED",
        "actual_root_cause": "优惠券服务数据库连接池耗尽",
        "actual_action": "调大连接池上限并滚动重启",
        "verification": "错误率回落至 0.1% 以下，持续 30 分钟",
        "verdict": "CORRECT",
        "knowledge_candidate": True,
        "sensitive_confirmed": True,
    }
    payload.update(overrides)
    return payload


def _close(entry: dict, actor: str = "alice", **overrides) -> dict:
    """按 M3 规则结构化关闭（仅备注关闭已停用）。"""
    resolution = esc.build_resolution(_resolution_payload(**overrides), actor)
    return esc.apply_close(entry, actor, "", resolution)


class PureFunctionTest(unittest.TestCase):
    """纯函数：升级原因翻译 / 记录构造 / 状态迁移。"""

    def test_auto_reason_gate1_blocked(self) -> None:
        reason = esc.auto_reason_from_events(["gate1:blocked:confidence=0.42<0.6"])
        self.assertIn("闸门1", reason)

    def test_auto_reason_takes_last_meaningful_event(self) -> None:
        reason = esc.auto_reason_from_events(["canary:degraded:5xx", "gate2:timeout:900s"])
        self.assertIn("审批超时", reason)

    def test_auto_reason_fallback(self) -> None:
        self.assertIn("转人工", esc.auto_reason_from_events([]))
        self.assertIn("转人工", esc.auto_reason_from_events(None))

    def test_auto_reason_gate2b_tests_canary(self) -> None:
        self.assertIn("二级审批", esc.auto_reason_from_events(["gate2b:rejected"]))
        self.assertIn("回炉", esc.auto_reason_from_events(["tests:failed:3"]))
        self.assertIn("回滚", esc.auto_reason_from_events(["canary:degraded:1"]))

    def test_auto_reason_tests_failed_with_detail(self) -> None:
        """新格式携带单轮失败摘要：升级原因具体到真实原因（如生成环节降级）。"""
        reason = esc.auto_reason_from_events(
            [
                "tests:failed:attempt=0:补丁生成失败（生成环节降级）：QoderFixError: 未产生改动（exit=1）",
                "tests:failed:attempt=2:补丁生成失败（生成环节降级）：QoderFixError: 未产生改动（exit=1）",
            ]
        )
        self.assertIn("回炉重试耗尽", reason)
        self.assertIn("补丁生成失败", reason)
        self.assertIn("exit=1", reason)
        # 中间版本格式（attempt=N 无详情）仍回退概述（前缀兼容）
        self.assertIn("回炉重试耗尽", esc.auto_reason_from_events(["tests:failed:attempt=1"]))

    def test_new_entry_shape(self) -> None:
        entry = esc.new_entry(_flow())
        self.assertEqual(entry["id"], "esc-aiops-fix-order-a1")
        self.assertEqual(entry["status"], "open")
        self.assertEqual(entry["service"], "order")
        self.assertEqual(entry["alert_id"], "a1")
        self.assertEqual(entry["history"][0]["action"], "open")
        self.assertFalse(entry["re_escalated"])
        self.assertIsNone(entry["closed_at"])
        self.assertEqual(entry["retry_wf_id"], "")
        self.assertEqual(entry["notify_attempts"], 0)
        self.assertEqual(entry["version"], 1)

    def test_assign_rejects_blank_and_closed(self) -> None:
        entry = esc.new_entry(_flow())
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_assign(entry, "   ", "alice")
        _close(entry)
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_assign(entry, "bob", "alice")

    def test_assign_marks_assigned_and_supports_reassign(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_assign(entry, "alice", "bob")
        self.assertEqual(entry["status"], "assigned")
        self.assertEqual(entry["assignee"], "alice")
        esc.apply_assign(entry, "carol", "bob")
        self.assertIn("改派", entry["history"][-1]["detail"])
        self.assertEqual(entry["assignee"], "carol")

    def test_close_records_note_and_rejects_repeat(self) -> None:
        entry = esc.new_entry(_flow())
        resolution = esc.build_resolution(_resolution_payload(note="已回滚发布"), "alice")
        esc.apply_close(entry, "alice", "已回滚发布", resolution)
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["note"], "已回滚发布")
        self.assertIsNotNone(entry["closed_at"])
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_close(entry, "alice", "", resolution)

    def test_close_requires_structured_resolution(self) -> None:
        """M3：仅备注关闭已停用（不再产生 resolution=NULL 的弱关闭）。"""
        entry = esc.new_entry(_flow())
        with self.assertRaises(esc.EscalationStoreError) as ctx:
            esc.apply_close(entry, "alice", "手动处置")
        self.assertIn("结构化", str(ctx.exception))
        self.assertEqual(entry["status"], "open")

    def test_retried_enters_retrying_with_parent_chain(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        self.assertEqual(entry["status"], "retrying")
        self.assertEqual(entry["retry_wf_id"], "aiops-fix-order-a1-retry1")
        self.assertEqual(entry["parent_run_id"], "run-a1")
        self.assertIsNotNone(entry["retry_started_at"])
        self.assertIn("retry1", entry["note"])
        self.assertEqual(entry["history"][-1]["action"], "retry")
        # 已在重试中不得重复启动
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_retried(entry, "aiops-fix-order-a1-retry2", "alice")

    def test_retry_outcome_done_auto_closes_with_resolution(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        detail = {
            "exec_status": "COMPLETED",
            "result": {
                "stage": "DONE",
                "root_cause": {"summary": "数据库连接池耗尽"},
                "patch": {"description": "调大连接池并重启"},
            },
        }
        esc.apply_retry_outcome(entry, "DONE", "system:retry-tracking", detail=detail)
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["retry_outcome"], "DONE")
        self.assertEqual(entry["resolution"]["close_reason"], "RESOLVED")
        self.assertEqual(entry["resolution"]["closed_by"], "system:retry-tracking")
        self.assertIn("连接池", entry["resolution"]["actual_root_cause"])
        self.assertEqual(entry["resolution"]["source"], "retry-tracking")
        self.assertEqual(entry["history"][-1]["action"], "retry:done")

    def test_retry_outcome_failure_reopens_and_resets_notify_cycle(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_assign(entry, "alice", "bob")
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "bob")
        entry["re_escalated"] = True
        entry["notify_mode"] = "live"
        esc.apply_retry_outcome(
            entry, "ESCALATED", "system:retry-tracking", reason="闸门2：审批超时"
        )
        self.assertEqual(entry["status"], "assigned")  # 已指派：重开后保持责任人
        self.assertEqual(entry["retry_outcome"], "ESCALATED")
        self.assertFalse(entry["re_escalated"])  # 新一轮升级可重新通知
        self.assertEqual(entry["notify_mode"], "")
        self.assertEqual(entry["notify_attempts"], 0)
        self.assertIn("审批超时", entry["note"])
        self.assertEqual(
            entry["retry_wf_id"], "aiops-fix-order-a1-retry1"
        )  # 父子链保留
        self.assertEqual(entry["history"][-1]["action"], "retry:reopen")

    def test_retry_outcome_running_keeps_tracking(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        before = len(entry["history"])
        esc.apply_retry_outcome(entry, "RUNNING", "system:retry-tracking")
        self.assertEqual(entry["status"], "retrying")
        self.assertEqual(len(entry["history"]), before)

    def test_re_escalate_marks_once(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_re_escalate(entry, "system")
        self.assertTrue(entry["re_escalated"])
        self.assertEqual(entry["history"][-1]["action"], "re-escalate")


class DemoStoreTest(unittest.TestCase):
    """demo 后端：JSON 文件持久化 + 幂等登记。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "escalations.json"
        patcher = mock.patch.object(esc, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_registers_once_and_skips_non_escalated(self) -> None:
        flows = [_flow(), _flow(wf_id="wf-ok", stage="RELEASED")]
        self.assertEqual(esc.sync_from_flows(flows), 1)
        self.assertEqual(esc.sync_from_flows(flows), 0)  # 幂等：不重复登记
        items = esc.list_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["wf_id"], "aiops-fix-order-a1")

    def test_closed_entry_not_reopened_by_sync(self) -> None:
        esc.sync_from_flows([_flow()])
        entry = esc.get("esc-aiops-fix-order-a1")
        _close(entry)
        esc.persist_entry(entry)
        self.assertEqual(esc.sync_from_flows([_flow()]), 0)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "closed")

    def test_retry_flow_not_registered_as_duplicate_todo(self) -> None:
        """重试流程再次 ESCALATED 时由原待办重开，不再另开重复待办。"""
        esc.sync_from_flows([_flow()])
        entry = esc.get("esc-aiops-fix-order-a1")
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        esc.persist_entry(entry)
        flows = [_flow(wf_id="aiops-fix-order-a1-retry1")]
        self.assertEqual(esc.sync_from_flows(flows), 0)
        self.assertEqual(len(esc.list_all()), 1)

    def test_list_filters_and_orders_desc(self) -> None:
        first = esc.new_entry(_flow(wf_id="wf-1"))
        first["escalated_at"] = "2026-10-06T00:00:00+00:00"
        second = esc.new_entry(_flow(wf_id="wf-2"))
        second["escalated_at"] = "2026-10-06T01:00:00+00:00"
        esc.persist_entry(first)
        esc.persist_entry(second)
        self.assertEqual(
            [e["id"] for e in esc.list_all()], ["esc-wf-2", "esc-wf-1"]
        )
        _close(second)
        esc.persist_entry(second)
        self.assertEqual([e["id"] for e in esc.list_all("open")], ["esc-wf-1"])

    def test_stats(self) -> None:
        esc.sync_from_flows([_flow(wf_id="wf-1"), _flow(wf_id="wf-2")])
        entry = esc.get("esc-wf-2")
        esc.apply_assign(entry, "alice", "bob")
        esc.persist_entry(entry)
        s = esc.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["open"], 1)
        self.assertEqual(s["assigned"], 1)
        self.assertEqual(s["retrying"], 0)
        self.assertEqual(s["closed"], 0)
        self.assertEqual(s["sla_minutes"], esc.DEFAULT_SLA_MINUTES)

    def test_write_is_atomic_no_tmp_left(self) -> None:
        esc.sync_from_flows([_flow()])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_demo_cas_rejects_stale_version(self) -> None:
        """并发旧版本写入返回冲突，不覆盖新状态（M3）。"""
        esc.sync_from_flows([_flow()])
        stale = esc.get("esc-aiops-fix-order-a1")
        fresh = esc.get("esc-aiops-fix-order-a1")
        esc.apply_assign(fresh, "alice", "bob")
        esc.persist_entry(fresh)  # version 1 → 2

        esc.apply_assign(stale, "mallory", "bob")
        with self.assertRaises(esc.EscalationConflictError):
            esc.persist_entry(stale, expected_version=1)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["assignee"], "alice")
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["version"], 2)

    def test_version_increments_on_each_write(self) -> None:
        esc.sync_from_flows([_flow()])
        entry = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(entry["version"], 1)
        esc.apply_assign(entry, "alice", "bob")
        esc.persist_entry(entry)
        self.assertEqual(entry["version"], 2)
        reloaded = esc.get("esc-aiops-fix-order-a1")
        esc.apply_plan(reloaded, "观察 30 分钟", "bob")
        esc.persist_entry(reloaded)
        self.assertEqual(reloaded["version"], 3)


class SlaSweepTest(unittest.TestCase):
    """SLA 到期判定与再升级巡检（每单只升级一次，防轰炸）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "escalations.json"
        patcher = mock.patch.object(esc, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_overdue(self, wf_id: str = "wf-old") -> dict:
        entry = esc.new_entry(_flow(wf_id=wf_id))
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
        entry["escalated_at"] = old
        esc.persist_entry(entry)
        return entry

    def test_due_only_overdue_open_entries(self) -> None:
        self._seed_overdue()
        fresh = esc.new_entry(_flow(wf_id="wf-fresh"))  # 刚登记：未超时
        esc.persist_entry(fresh)
        self.assertEqual(
            [e["id"] for e in esc.due_for_re_escalation()], ["esc-wf-old"]
        )

    def test_closed_and_re_escalated_excluded(self) -> None:
        entry = self._seed_overdue()
        entry["re_escalated"] = True  # 已升级过：不再重复
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])

        entry["re_escalated"] = False
        _close(entry)
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])  # 已关闭：不再巡检

    def test_retrying_pauses_sla(self) -> None:
        """重试运行中：原待办不再重复升级（等新流程终态）。"""
        entry = self._seed_overdue()
        esc.apply_retried(entry, "wf-old-retry1", "alice")
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])

    def test_sweep_sends_once_after_live_delivery(self) -> None:
        self._seed_overdue()
        with mock.patch.object(
            esc, "_send_re_escalation", return_value={"mode": "live"}
        ) as sender:
            first = esc.sweep_due()
            second = esc.sweep_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        sender.assert_called_once()
        entry = esc.get("esc-wf-old")
        self.assertTrue(entry["re_escalated"])
        self.assertEqual(entry["notify_mode"], "live")
        self.assertEqual(entry["notify_attempts"], 1)
        self.assertIsNone(entry["notify_next_retry_at"])

    def test_recorded_is_not_marked_notified_and_retries_after_backoff(self) -> None:
        """仅留档不算送达：保持 overdue，按退避重试，后续 live 才完成升级。"""
        self._seed_overdue()
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            esc, "_send_re_escalation", return_value={"mode": "recorded"}
        ):
            first = esc.sweep_due(now)
            in_backoff = esc.sweep_due(now + timedelta(seconds=30))
        self.assertEqual(len(first), 1)
        self.assertEqual(in_backoff, [])  # 退避窗口内不轰炸
        entry = esc.get("esc-wf-old")
        self.assertFalse(entry["re_escalated"])
        self.assertEqual(entry["notify_mode"], "recorded")
        self.assertEqual(entry["notify_attempts"], 1)
        self.assertIsNotNone(entry["notify_next_retry_at"])
        self.assertEqual(esc.stats()["notify_pending"], 1)

        with mock.patch.object(
            esc, "_send_re_escalation", return_value={"mode": "live"}
        ) as sender:
            third = esc.sweep_due(now + timedelta(seconds=61))
        self.assertEqual(len(third), 1)
        sender.assert_called_once()
        entry = esc.get("esc-wf-old")
        self.assertTrue(entry["re_escalated"])
        self.assertEqual(entry["notify_mode"], "live")
        self.assertEqual(entry["notify_attempts"], 2)
        self.assertEqual(esc.stats()["notify_pending"], 0)

    def test_failed_delivery_records_error_and_keeps_overdue(self) -> None:
        self._seed_overdue()
        with mock.patch.object(
            esc,
            "_send_re_escalation",
            return_value={"mode": "failed", "error": "机器人应答非零 code=19001"},
        ):
            processed = esc.sweep_due()
        self.assertEqual(len(processed), 1)
        entry = esc.get("esc-wf-old")
        self.assertFalse(entry["re_escalated"])
        self.assertEqual(entry["notify_mode"], "failed")
        self.assertIn("19001", entry["notify_error"])
        self.assertIsNotNone(entry["notify_next_retry_at"])
        self.assertTrue(esc_routes._decorate(entry)["overdue"])  # 未送达仍保持超时

    def test_notify_backoff_caps_at_one_hour(self) -> None:
        self.assertEqual(esc.notify_backoff_seconds(1), 60)
        self.assertEqual(esc.notify_backoff_seconds(2), 300)
        self.assertEqual(esc.notify_backoff_seconds(3), 900)
        self.assertEqual(esc.notify_backoff_seconds(4), 1800)
        self.assertEqual(esc.notify_backoff_seconds(5), 3600)
        self.assertEqual(esc.notify_backoff_seconds(99), 3600)

    def test_sla_minutes_env_override_and_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "5"}):
            self.assertEqual(esc.sla_minutes(), 5)
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "abc"}):
            self.assertEqual(esc.sla_minutes(), esc.DEFAULT_SLA_MINUTES)
        with mock.patch.dict(os.environ, {"AIOPS_ESCALATION_SLA_MINUTES": "-3"}):
            self.assertEqual(esc.sla_minutes(), esc.DEFAULT_SLA_MINUTES)


class ProductionDbBackendTest(unittest.TestCase):
    """production（SQLite 注入）：upsert / 读库 / 幂等登记同源。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{Path(self._tmp.name) / 'aiops.sqlite3'}",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db_layer.reset_engine)

    def test_roundtrip_and_idempotent_sync(self) -> None:
        self.assertEqual(esc.sync_from_flows([_flow()]), 1)
        entry = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(entry["service"], "order")
        self.assertEqual(entry["auto_reason"], "闸门1：根因置信度不足（confidence-low）")

        esc.apply_assign(entry, "alice", "bob")
        esc.persist_entry(entry)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["assignee"], "alice")

        self.assertEqual(esc.sync_from_flows([_flow()]), 0)  # 幂等仍在（读库）
        self.assertEqual(esc.stats()["assigned"], 1)

    def test_sla_due_reads_from_db(self) -> None:
        entry = esc.new_entry(_flow())
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
        entry["escalated_at"] = old
        esc.persist_entry(entry)
        due = esc.due_for_re_escalation()
        self.assertEqual([e["id"] for e in due], ["esc-aiops-fix-order-a1"])

    def test_resolution_and_plan_roundtrip_from_db(self) -> None:
        """M2：resolution(JSON) / plan(VARCHAR) 在 production（SQLite）后端完整往返。"""
        entry = esc.new_entry(_flow())
        esc.apply_plan(entry, "先核对 Loki 标签映射", "alice")
        resolution = esc.build_resolution(_resolution_payload(), "alice")
        esc.apply_close(entry, "alice", "", resolution)
        esc.persist_entry(entry)

        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["plan"], "先核对 Loki 标签映射")
        self.assertEqual(loaded["resolution"]["close_reason"], "RESOLVED")
        self.assertEqual(loaded["resolution"]["verdict"], "CORRECT")
        self.assertEqual(loaded["resolution"]["knowledge_status"], "DRAFT")
        self.assertEqual(esc.knowledge_status(loaded), "DRAFT")

    def test_retry_notify_and_version_roundtrip(self) -> None:
        """M3：重试跟踪/通知送达/版本号在 production 后端完整往返。"""
        entry = esc.new_entry(_flow())
        esc.persist_entry(entry)
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        now = datetime.now(timezone.utc)
        esc.apply_notify_result(
            entry, {"mode": "failed", "error": "connect timeout"}, 30, now
        )
        esc.persist_entry(entry)

        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "retrying")
        self.assertEqual(loaded["retry_wf_id"], "aiops-fix-order-a1-retry1")
        self.assertEqual(loaded["parent_run_id"], "run-a1")
        self.assertTrue(loaded["retry_started_at"])
        self.assertEqual(loaded["notify_mode"], "failed")
        self.assertEqual(loaded["notify_attempts"], 1)
        self.assertEqual(loaded["notify_error"], "connect timeout")
        self.assertTrue(loaded["notify_next_retry_at"])
        self.assertEqual(loaded["version"], 2)

    def test_cas_rejects_stale_version(self) -> None:
        entry = esc.new_entry(_flow())
        esc.persist_entry(entry)
        stale = esc.get("esc-aiops-fix-order-a1")
        fresh = esc.get("esc-aiops-fix-order-a1")
        esc.apply_assign(fresh, "alice", "bob")
        esc.persist_entry(fresh)

        esc.apply_assign(stale, "mallory", "bob")
        with self.assertRaises(esc.EscalationConflictError):
            esc.persist_entry(stale, expected_version=1)
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["assignee"], "alice")
        self.assertEqual(loaded["version"], 2)


_REQUIREMENT_SESSION = {
    "id": "ra-app-19",
    "app_id": "app-19",
    "service": "nl2sql",
    "entry_id": "19",
    "entry_title": "用户信息维护功能优化。",
    "entry_snapshot": {"title": "用户信息维护功能优化。", "content": "点击用户名进入用户信息维护"},
    "status": "approved",
    "current_version": 1,
    "versions": [
        {
            "version": 1,
            "analysis": {
                "plan": ["在首页增加用户信息入口", "补充跳转测试"],
                "acceptance": ["首页可见入口"],
                "suspect_files": ["web/src/App.tsx"],
            },
        }
    ],
    "approved": {
        "version": 1,
        "wf_id": "aiops-req-ra-app-19-v1",
        "at": "2026-10-10T05:49:50+00:00",
        "actor": "admin",
    },
}


def _req_flow(wf_id: str, alert_id: str, service: str = "nl2sql") -> dict:
    """需求来源的升级流程（如闸门 2 超时升级；alert_id 为 req-N 形态）。"""
    return {
        "wf_id": wf_id,
        "run_id": "run-req-19",
        "stage": "ESCALATED",
        "gate_events": ["gate2:timeout:300s"],
        "alert": {"alert_id": alert_id, "service": service, "severity": "critical"},
    }


class RetryRouteTest(unittest.TestCase):
    """重试修复路由：需求来源重启需求流（防「按告警流重试必然闸门1拦截」回归）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(esc, "DATA_PATH", Path(self._tmp.name) / "escalations.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        sessions_patcher = mock.patch.object(
            esc_routes.requirement_analyses, "list_all", return_value=[]
        )
        self.sessions = sessions_patcher.start()
        self.addCleanup(sessions_patcher.stop)

        audit_patcher = mock.patch.object(esc_routes.audit, "write_audit")
        self.audit = audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        start_patcher = mock.patch.object(
            esc_routes.gw, "start_requirement_flow", new_callable=mock.AsyncMock
        )
        self.start_req = start_patcher.start()
        self.addCleanup(start_patcher.stop)

        alert_patcher = mock.patch.object(esc_routes.gw, "start_flow", new_callable=mock.AsyncMock)
        self.start_alert = alert_patcher.start()
        self.addCleanup(alert_patcher.stop)

    def _request(self, user: str = "admin1"):
        request = mock.Mock()
        request.state.identity.user = user
        return request

    def _retry(self, esc_id: str, body=None) -> dict:
        return asyncio.run(esc_routes.api_escalation_retry(self._request(), esc_id, body))

    def test_requirement_flow_entry_restarts_requirement_workflow(self) -> None:
        """① 流程本身即需求流（aiops-req-*）→ 按 approved.wf_id 精确反查后重启需求流。"""
        self.sessions.return_value = [dict(_REQUIREMENT_SESSION)]
        self.start_req.return_value = "aiops-req-ra-app-19-v1-req-19-retry1"
        esc.persist_entry(esc.new_entry(_req_flow("aiops-req-ra-app-19-v1", "req-19")))

        data = self._retry("esc-aiops-req-ra-app-19-v1")

        self.assertEqual(data["mode"], "requirement")
        self.assertFalse(self.start_alert.called)
        task, wf_id = self.start_req.call_args.args
        self.assertEqual(wf_id, "aiops-req-ra-app-19-v1-req-19-retry1")
        self.assertEqual(task.analysis_id, "ra-app-19")
        self.assertEqual(task.approved_by, "admin1")
        self.assertIn("首页增加用户信息入口", task.plan)
        self.assertEqual(task.acceptance, ["首页可见入口"])
        entry = esc.get("esc-aiops-req-ra-app-19-v1")
        # M3：启动重试不立即关闭，进入 retrying 等待新流程终态
        self.assertEqual(entry["status"], "retrying")
        self.assertEqual(entry["retry_wf_id"], "aiops-req-ra-app-19-v1-req-19-retry1")
        self.assertEqual(entry["parent_run_id"], "run-req-19")
        self.assertTrue(entry["retry_started_at"])
        self.assertIn("aiops-req-ra-app-19-v1-req-19-retry1", entry["note"])
        self.assertEqual(data["retry_wf_id"], "aiops-req-ra-app-19-v1-req-19-retry1")
        self.assertEqual(self.audit.call_args.kwargs["params"]["mode"], "requirement")
        context = self.start_req.call_args.kwargs["evidence_context"]
        self.assertEqual(context["case_id"], "ra-app-19")
        self.assertEqual(context["requested_by"], "admin1")
        self.assertEqual(context["parent_run_id"], "run-req-19")
        self.assertEqual(context["parent_wf_id"], "aiops-req-ra-app-19-v1")
        self.assertEqual(context["retry_wf_id"], "aiops-req-ra-app-19-v1-req-19-retry1")

    def test_cascaded_generic_entry_resolves_by_alert_id(self) -> None:
        """② 旧逻辑级联的告警流待办（req-N-retryM）→ 剥离重试后缀反查会话。"""
        self.sessions.return_value = [dict(_REQUIREMENT_SESSION)]
        self.start_req.return_value = "wf-new"
        esc.persist_entry(esc.new_entry(_req_flow("aiops-fix-nl2sql-req-19-retry1", "req-19-retry1")))

        data = self._retry("esc-aiops-fix-nl2sql-req-19-retry1")

        self.assertEqual(data["mode"], "requirement")
        self.assertFalse(self.start_alert.called)
        _, wf_id = self.start_req.call_args.args
        self.assertEqual(wf_id, "aiops-req-ra-app-19-v1-req-19-retry1-retry1")

    def test_plain_alert_entry_keeps_generic_retry(self) -> None:
        """普通告警来源行为不变：仍以告警流（幂等新键）重启。"""
        self.sessions.return_value = [dict(_REQUIREMENT_SESSION)]
        self.start_alert.return_value = "aiops-fix-order-a1-retry1"
        esc.persist_entry(esc.new_entry(_flow(wf_id="aiops-fix-order-a1")))

        data = self._retry("esc-aiops-fix-order-a1")

        self.assertEqual(data["mode"], "alert")
        self.assertFalse(self.start_req.called)
        alert, wf_id = self.start_alert.call_args.args
        self.assertEqual(wf_id, "aiops-fix-order-a1-retry1")
        self.assertEqual(alert.alert_id, "a1-retry1")
        context = self.start_alert.call_args.kwargs["evidence_context"]
        self.assertEqual(context["case_id"], "a1")
        self.assertEqual(context["requested_by"], "admin1")
        self.assertEqual(context["parent_run_id"], "run-a1")
        self.assertEqual(context["parent_wf_id"], "aiops-fix-order-a1")
        self.assertEqual(context["retry_wf_id"], "aiops-fix-order-a1-retry1")

    def test_retry_while_retrying_conflicts(self) -> None:
        """M3：重试跟踪中再次重试返回 409，不重复启动新流程。"""
        self.sessions.return_value = [dict(_REQUIREMENT_SESSION)]
        self.start_req.return_value = "aiops-req-ra-app-19-v1-req-19-retry1"
        esc.persist_entry(esc.new_entry(_req_flow("aiops-req-ra-app-19-v1", "req-19")))
        self._retry("esc-aiops-req-ra-app-19-v1")

        with self.assertRaises(ApiError) as ctx:
            self._retry("esc-aiops-req-ra-app-19-v1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("重试跟踪", ctx.exception.message)
        self.assertEqual(self.start_req.call_count, 1)

    def test_retry_stale_version_409(self) -> None:
        """M3 CAS：携带旧版本重试返回 409，且新流程不会被错误绑定。"""
        self.sessions.return_value = []
        self.start_alert.return_value = "aiops-fix-order-a1-retry1"
        esc.persist_entry(esc.new_entry(_flow(wf_id="aiops-fix-order-a1")))
        # 并发写入把版本推进到 2
        fresh = esc.get("esc-aiops-fix-order-a1")
        esc.apply_assign(fresh, "bob", "alice")
        esc.persist_entry(fresh)

        with self.assertRaises(ApiError) as ctx:
            self._retry("esc-aiops-fix-order-a1", esc_routes.RetryBody(version=1))
        self.assertEqual(ctx.exception.status_code, 409)
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "assigned")
        self.assertEqual(loaded["retry_wf_id"], "")

    def test_requirement_like_without_session_guides_human(self) -> None:
        """疑似需求来源但会话缺失：409 引导（不再按告警流空转）。"""
        self.sessions.return_value = []
        esc.persist_entry(
            esc.new_entry(_req_flow("aiops-fix-nl2sql-req-19-retry1-retry1", "req-19-retry1-retry1"))
        )
        with self.assertRaises(ApiError) as ctx:
            self._retry("esc-aiops-fix-nl2sql-req-19-retry1-retry1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("需求反馈", ctx.exception.message)
        self.assertFalse(self.start_alert.called)
        self.assertFalse(self.start_req.called)

    def test_requirement_not_approved_409(self) -> None:
        self.sessions.return_value = [dict(_REQUIREMENT_SESSION, status="analyzed")]
        esc.persist_entry(esc.new_entry(_req_flow("aiops-req-ra-app-19-v1", "req-19")))
        with self.assertRaises(ApiError) as ctx:
            self._retry("esc-aiops-req-ra-app-19-v1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("批准", ctx.exception.message)

    def test_requirement_degraded_analysis_409(self) -> None:
        session = dict(_REQUIREMENT_SESSION)
        session["versions"] = [
            {
                "version": 1,
                "analysis": {
                    **_REQUIREMENT_SESSION["versions"][0]["analysis"],
                    "degraded": True,
                },
            }
        ]
        self.sessions.return_value = [session]
        esc.persist_entry(esc.new_entry(_req_flow("aiops-req-ra-app-19-v1", "req-19")))
        with self.assertRaises(ApiError) as ctx:
            self._retry("esc-aiops-req-ra-app-19-v1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("重试分析", ctx.exception.message)

    def test_decorate_marks_requirement_origin(self) -> None:
        entry = esc.new_entry(_req_flow("aiops-fix-nl2sql-req-19-retry1", "req-19-retry1"))
        self.assertTrue(esc_routes._decorate(entry)["requirement"])
        plain = esc.new_entry(_flow(wf_id="aiops-fix-order-a1"))
        self.assertFalse(esc_routes._decorate(plain)["requirement"])

    def test_resolve_prefers_service_match(self) -> None:
        """同 entry_id 多应用：优先服务名匹配的会话。"""
        other = dict(_REQUIREMENT_SESSION, id="ra-app-x-19", app_id="app-x", service="other")
        self.sessions.return_value = [other, dict(_REQUIREMENT_SESSION)]
        resolved = esc_routes.resolve_requirement_session(
            {"wf_id": "aiops-fix-nl2sql-req-19", "alert_id": "req-19", "service": "nl2sql"}
        )
        self.assertEqual(resolved["id"], "ra-app-19")


class RetryTrackingTest(unittest.TestCase):
    """M3 后台重试终态跟踪：DONE 自动关闭 / 失败重开 / RUNNING 保持 / 并发冲突跳过。"""

    def setUp(self) -> None:
        from bff import app as bff_app

        self.bff_app = bff_app
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path_patcher = mock.patch.object(
            esc, "DATA_PATH", Path(self._tmp.name) / "escalations.json"
        )
        path_patcher.start()
        self.addCleanup(path_patcher.stop)
        audit_patcher = mock.patch.object(bff_app.audit, "write_audit")
        self.audit = audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

    def _seed_retrying(self, assignee: str = "alice") -> dict:
        entry = esc.new_entry(_flow(wf_id="aiops-fix-order-a1"))
        if assignee:
            esc.apply_assign(entry, assignee, "bob")
        esc.persist_entry(entry)
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "bob")
        esc.persist_entry(entry)
        return entry

    def _refresh(self, detail: dict, fetch=None) -> list[dict]:
        async def default_fetch(wf_id: str) -> dict:
            self.assertEqual(wf_id, "aiops-fix-order-a1-retry1")
            return detail

        return asyncio.run(self.bff_app.refresh_retry_outcomes(fetch or default_fetch))

    def test_done_auto_closes_with_structured_resolution(self) -> None:
        self._seed_retrying()
        detail = {
            "exec_status": "COMPLETED",
            "result": {
                "stage": "DONE",
                "root_cause": {"summary": "连接池耗尽"},
                "patch": {"description": "扩大连接池上限"},
            },
        }
        changed = self._refresh(detail)

        self.assertEqual(len(changed), 1)
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "closed")
        self.assertEqual(loaded["retry_outcome"], "DONE")
        resolution = loaded["resolution"]
        self.assertEqual(resolution["close_reason"], "RESOLVED")
        self.assertEqual(resolution["source"], "retry-tracking")
        self.assertEqual(resolution["closed_by"], "system:retry-tracking")
        self.assertEqual(resolution["actual_root_cause"], "连接池耗尽")
        self.assertIn("扩大连接池上限", resolution["actual_action"])
        self.assertFalse(resolution["knowledge_candidate"])
        self.assertEqual(loaded["history"][-1]["action"], "retry:done")
        self.assertEqual(self.audit.call_args.kwargs["action"], "escalation:retry-outcome")

    def test_failed_outcome_reopens_and_resets_notify_cycle(self) -> None:
        entry = self._seed_retrying()
        esc.apply_notify_result(entry, {"mode": "failed", "error": "timeout"}, 30)
        esc.persist_entry(entry)

        changed = self._refresh(
            {"exec_status": "COMPLETED", "result": {"stage": "ESCALATED", "gate_events": ["gate1:blocked:low"]}}
        )

        self.assertEqual(len(changed), 1)
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "assigned")  # 有责任人则回到 assigned
        self.assertEqual(loaded["retry_outcome"], "ESCALATED")
        self.assertIn("重试流程", loaded["note"])
        self.assertFalse(loaded["re_escalated"])
        self.assertEqual(loaded["notify_mode"], "")
        self.assertEqual(loaded["notify_attempts"], 0)
        self.assertIsNone(loaded["notify_next_retry_at"])
        self.assertEqual(loaded["history"][-1]["action"], "retry:reopen")

    def test_running_keeps_tracking_without_write(self) -> None:
        self._seed_retrying()
        changed = self._refresh({"exec_status": "RUNNING", "result": {"stage": "FIXING"}})

        self.assertEqual(changed, [])
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "retrying")
        self.assertEqual(loaded["retry_wf_id"], "aiops-fix-order-a1-retry1")
        self.assertFalse(self.audit.called)

    def test_concurrent_update_is_not_overwritten(self) -> None:
        self._seed_retrying()

        async def racing_fetch(wf_id: str) -> dict:
            fresh = esc.get("esc-aiops-fix-order-a1")
            esc.apply_plan(fresh, "并发人工修改", "carol")
            esc.persist_entry(fresh)
            return {"exec_status": "COMPLETED", "result": {"stage": "DONE"}}

        changed = self._refresh({}, fetch=racing_fetch)

        self.assertEqual(changed, [])
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "retrying")  # 不被旧快照回写覆盖
        self.assertEqual(loaded["plan"], "并发人工修改")
        self.assertFalse(self.audit.called)


class CategoryTest(unittest.TestCase):
    """处置分类（M1）：事件/原因确定性推导 + 历史回填只分类不关闭。

    分类目的：把「观测异常 / 证据不足」与模型低置信、审批、测试等业务闸门分开统计，
    避免日志供给故障污染模型质量指标；历史待办只回填分类，不改变状态、不批量关闭。
    """

    def test_category_from_events(self) -> None:
        cases = {
            "evidence:blocked:NO_LOGS": "observability",
            "evidence:blocked:STALE_LOGS": "observability",
            "evidence:blocked:LOKI_UNAVAILABLE": "observability",
            "evidence:blocked:UNMAPPED_SERVICE": "observability",
            "evidence:blocked:INSUFFICIENT_SIGNAL": "evidence",
            "gate1:blocked:confidence=0.4<0.8": "model",
            "gate2:rejected": "approval",
            "gate2b:timeout": "approval",
            "tests:failed:attempt=2:boom": "sandbox",
            "canary:degraded:5xx": "release",
        }
        for event, expected in cases.items():
            with self.subTest(event=event):
                self.assertEqual(esc.category_from_events([event]), expected)

    def test_category_takes_last_meaningful_event(self) -> None:
        events = ["gate1:blocked:low", "evidence:blocked:NO_LOGS"]
        self.assertEqual(esc.category_from_events(events), "observability")
        self.assertEqual(
            esc.category_from_events(["evidence:blocked:NO_LOGS", "gate2:rejected"]), "approval"
        )

    def test_category_from_reason_backfill(self) -> None:
        self.assertEqual(esc.category_from_reason("证据预检：Loki 不可用（LOKI_UNAVAILABLE）"), "observability")
        self.assertEqual(
            esc.category_from_reason("证据预检：日志无可分析信号（INSUFFICIENT_SIGNAL）"), "evidence"
        )
        self.assertEqual(esc.category_from_reason("闸门1：根因置信度不足（0.4）"), "model")
        self.assertEqual(esc.category_from_reason("闸门2：人工审批未通过（reject）"), "approval")
        self.assertEqual(esc.category_from_reason("沙箱测试回炉重试耗尽"), "sandbox")
        self.assertEqual(esc.category_from_reason("金丝雀劣化自动回滚"), "release")
        self.assertEqual(esc.category_from_reason("转人工（原因见流程详情）"), "other")
        self.assertEqual(esc.category_from_reason("", requirement=True), "requirement")

    def test_new_entry_writes_evidence_category(self) -> None:
        entry = esc.new_entry(_flow(gate_events=["evidence:blocked:NO_LOGS"]))
        self.assertEqual(entry["category"], "observability")
        self.assertIn("无服务日志", entry["auto_reason"])

    def test_entry_category_legacy_fallback(self) -> None:
        legacy = {
            "wf_id": "aiops-fix-x-1",
            "alert_id": "a1",
            "auto_reason": "闸门1：根因置信度不足（0.4）",
        }
        self.assertEqual(esc.entry_category(legacy), "model")
        requirement = {
            "wf_id": "aiops-req-ra-1-v1",
            "alert_id": "req-1",
            "auto_reason": "转人工",
        }
        self.assertEqual(esc.entry_category(requirement), "requirement")
        self.assertEqual(esc.entry_category({"category": "observability"}), "observability")
        self.assertEqual(esc.category_label("observability"), "观测异常")
        self.assertEqual(esc.category_label("unknown"), "未分类")


class CategoryBackfillTest(unittest.TestCase):
    """历史待办分类回填：默认 dry-run；落库只写 category，不改状态、不关闭。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "escalations.json"
        patcher = mock.patch.object(esc, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_legacy(self, wf_id: str, reason: str, status: str = "open") -> dict:
        entry = esc.new_entry(_flow(wf_id=wf_id))
        entry.pop("category", None)
        entry["auto_reason"] = reason
        entry["status"] = status
        esc.persist_entry(entry)
        return entry

    def test_backfill_defaults_to_dry_run(self) -> None:
        self._seed_legacy("wf-a", "闸门1：根因置信度不足（0.4）")
        report = esc.backfill_categories()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["changed"], 1)
        self.assertEqual(report["items"][0]["to"], "model")
        self.assertNotIn("category", esc.get("esc-wf-a"))

    def test_backfill_apply_persists_category_without_status_change(self) -> None:
        self._seed_legacy("wf-a", "证据预检：Loki 不可用（LOKI_UNAVAILABLE）")
        self._seed_legacy("wf-b", "闸门1：根因置信度不足（0.4）", status="assigned")
        report = esc.backfill_categories(dry_run=False)
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["changed"], 2)
        first = esc.get("esc-wf-a")
        second = esc.get("esc-wf-b")
        self.assertEqual(first["category"], "observability")
        self.assertEqual(first["status"], "open")
        self.assertEqual(second["category"], "model")
        self.assertEqual(second["status"], "assigned")
        self.assertEqual(second["closed_at"], None)
        self.assertEqual(esc.backfill_categories(dry_run=False)["changed"], 0)

    def test_stats_counts_categories(self) -> None:
        self._seed_legacy("wf-a", "闸门1：根因置信度不足（0.4）")
        self._seed_legacy("wf-b", "证据预检：窗口内无服务日志（NO_LOGS）")
        categories = esc.stats()["categories"]
        self.assertEqual(categories.get("model"), 1)
        self.assertEqual(categories.get("observability"), 1)


class ResolutionTest(unittest.TestCase):
    """结构化关闭（M2）：RESOLVED 三必填 / 其他原因需说明 / 候选需敏感确认 / 状态机。"""

    def test_resolved_requires_root_cause_action_and_verification(self) -> None:
        for key, label in (
            ("actual_root_cause", "实际根因"),
            ("actual_action", "实际处置"),
            ("verification", "验证结果"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(esc.EscalationStoreError) as ctx:
                    esc.build_resolution(_resolution_payload(**{key: " "}), "alice")
                self.assertIn(label, str(ctx.exception))

    def test_other_reason_requires_explanation(self) -> None:
        with self.assertRaises(esc.EscalationStoreError):
            esc.build_resolution({"close_reason": "OTHER"}, "alice")
        resolution = esc.build_resolution(
            {"close_reason": "FALSE_POSITIVE", "note": "阈值配置错误，已修正"}, "alice"
        )
        self.assertEqual(resolution["close_reason"], "FALSE_POSITIVE")
        self.assertEqual(resolution["knowledge_status"], "")  # 非候选不进入审核队列
        self.assertEqual(resolution["verdict"], "UNKNOWN")

    def test_candidate_requires_sensitive_confirmation(self) -> None:
        with self.assertRaises(esc.EscalationStoreError) as ctx:
            esc.build_resolution(_resolution_payload(sensitive_confirmed=False), "alice")
        self.assertIn("敏感", str(ctx.exception))

    def test_candidate_starts_as_draft_with_actor_and_timestamp(self) -> None:
        resolution = esc.build_resolution(_resolution_payload(), "alice")
        self.assertEqual(resolution["knowledge_status"], "DRAFT")
        self.assertEqual(resolution["closed_by"], "alice")
        self.assertEqual(resolution["knowledge_reviewed_by"], "")
        self.assertTrue(resolution["closed_at"])

    def test_invalid_reason_or_verdict_rejected(self) -> None:
        with self.assertRaises(esc.EscalationStoreError):
            esc.build_resolution(_resolution_payload(close_reason="DONE"), "alice")
        with self.assertRaises(esc.EscalationStoreError):
            esc.build_resolution(_resolution_payload(verdict="MAYBE"), "alice")

    def test_apply_close_with_resolution_stores_structure(self) -> None:
        entry = esc.new_entry(_flow())
        resolution = esc.build_resolution(_resolution_payload(), "alice")
        esc.apply_close(entry, "alice", "", resolution)
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["resolution"]["actual_root_cause"], "优惠券服务数据库连接池耗尽")
        self.assertEqual(entry["note"], "已扩容连接池并观察 30 分钟")
        self.assertIn("已修复", entry["history"][-1]["detail"])
        self.assertEqual(entry["history"][-1]["action"], "close")

    def test_legacy_closed_record_without_resolution_stays_readable(self) -> None:
        """M3：历史已关闭记录（仅备注）保持原样可读，但不参与知识审核。"""
        entry = esc.new_entry(_flow())
        entry["status"] = "closed"
        entry["note"] = "历史手动处理完毕"
        entry["closed_at"] = entry["escalated_at"]
        self.assertIsNone(entry["resolution"])
        self.assertEqual(esc.knowledge_status(entry), "")
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_knowledge_review(entry, "approve", "admin")

    def test_plan_allows_open_entry_and_rejects_closed(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_plan(entry, "值班核对日志后再重试", "alice")
        self.assertEqual(entry["plan"], "值班核对日志后再重试")
        self.assertEqual(entry["status"], "open")
        self.assertEqual(entry["history"][-1]["action"], "plan")
        esc.apply_close(
            entry,
            "alice",
            "",
            esc.build_resolution(
                _resolution_payload(close_reason="FALSE_POSITIVE", note="误报"), "alice"
            ),
        )
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_plan(entry, "x", "alice")

    def test_approve_then_revoke_state_machine(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_close(
            entry, "alice", "", esc.build_resolution(_resolution_payload(), "alice")
        )
        esc.apply_knowledge_review(entry, "approve", "admin")
        self.assertEqual(esc.knowledge_status(entry), "APPROVED")
        self.assertEqual(entry["resolution"]["knowledge_reviewed_by"], "admin")
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_knowledge_review(entry, "approve", "admin")

        esc.apply_knowledge_review(entry, "revoke", "admin", "根因描述不完整")
        self.assertEqual(esc.knowledge_status(entry), "REJECTED")
        self.assertEqual(entry["resolution"]["knowledge_reject_reason"], "根因描述不完整")
        self.assertEqual(entry["history"][-1]["action"], "knowledge:revoke")
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_knowledge_review(entry, "revoke", "admin")

    def test_approve_rejects_unresolved_close(self) -> None:
        """FALSE_POSITIVE / DUPLICATE 等未解决记录不得进入代码知识库。"""
        entry = esc.new_entry(_flow())
        resolution = esc.build_resolution(
            _resolution_payload(close_reason="DUPLICATE", note="与 esc-1 重复"), "alice"
        )
        esc.apply_close(entry, "alice", "", resolution)
        with self.assertRaises(esc.EscalationStoreError) as ctx:
            esc.apply_knowledge_review(entry, "approve", "admin")
        self.assertIn("已修复", str(ctx.exception))

    def test_reject_records_reason(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_close(
            entry, "alice", "", esc.build_resolution(_resolution_payload(), "alice")
        )
        esc.apply_knowledge_review(entry, "reject", "admin", "含内部系统名")
        self.assertEqual(esc.knowledge_status(entry), "REJECTED")
        self.assertEqual(entry["resolution"]["knowledge_reject_reason"], "含内部系统名")

    def test_decorate_exposes_labels(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_close(
            entry, "alice", "", esc.build_resolution(_resolution_payload(), "alice")
        )
        decorated = esc_routes._decorate(entry)
        self.assertEqual(decorated["close_reason"], "RESOLVED")
        self.assertEqual(decorated["close_reason_label"], "已修复")
        self.assertEqual(decorated["knowledge_status"], "DRAFT")
        self.assertEqual(decorated["knowledge_label"], "待审核")
        legacy = esc_routes._decorate(esc.new_entry(_flow(wf_id="wf-legacy")))
        self.assertIsNone(legacy["resolution"])
        self.assertEqual(legacy["knowledge_label"], "未提交")

    def test_structured_close_detection(self) -> None:
        self.assertFalse(esc_routes._is_structured_close({"note": "仅备注"}))
        self.assertTrue(esc_routes._is_structured_close({"close_reason": "RESOLVED"}))
        self.assertTrue(esc_routes._is_structured_close({"knowledge_candidate": True}))


class CloseReviewRouteTest(unittest.TestCase):
    """关闭 / 计划 / 知识审核路由（M2）：结构化关闭 → 审核 → 导出全链路。

    索引重建不在请求内触发（显式动作），这里只验证导出语料随审核状态增删。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.knowledge_path = Path(self._tmp.name) / "knowledge_records.json"
        patchers = [
            mock.patch.object(esc, "DATA_PATH", Path(self._tmp.name) / "escalations.json"),
            mock.patch.object(esc_routes.knowledge, "export_path", return_value=self.knowledge_path),
            mock.patch.object(esc_routes.audit, "write_audit"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        esc.persist_entry(esc.new_entry(_flow()))

    def _request(self, user: str = "alice"):
        request = mock.Mock()
        request.state.identity.user = user
        return request

    def _close(self, body: dict | None = None) -> dict:
        return asyncio.run(
            esc_routes.api_escalation_close(
                self._request(), "esc-aiops-fix-order-a1", esc_routes.CloseBody(**(body or {}))
            )
        )

    def _review(self, decision: str, reason: str = "") -> dict:
        return asyncio.run(
            esc_routes.api_escalation_knowledge_review(
                self._request("admin"),
                "esc-aiops-fix-order-a1",
                esc_routes.KnowledgeReviewBody(decision=decision, reason=reason),
            )
        )

    def test_structured_close_applies_sensitive_guard(self) -> None:
        payload = _resolution_payload(sensitive_confirmed=False)
        with self.assertRaises(ApiError) as ctx:
            self._close(payload)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("敏感", ctx.exception.message)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "open")

    def test_open_close_approve_export_revoke_chain(self) -> None:
        response = self._close(_resolution_payload())
        self.assertTrue(response["ok"])
        self.assertEqual(response["escalation"]["knowledge_label"], "待审核")
        # 待审核不导出（一次导出会覆盖文件，因此此时导出为空）
        self.assertEqual(response["knowledge_export"]["exported"], 0)

        approved = self._review("approve")
        self.assertEqual(approved["escalation"]["knowledge_status"], "APPROVED")
        self.assertEqual(approved["knowledge_export"]["exported"], 1)
        records = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        self.assertEqual([r["ticket_id"] for r in records], ["esc-aiops-fix-order-a1"])

        revoked = self._review("revoke", "根因描述需补充")
        self.assertEqual(revoked["escalation"]["knowledge_status"], "REJECTED")
        self.assertEqual(revoked["knowledge_export"]["exported"], 0)
        self.assertEqual(json.loads(self.knowledge_path.read_text(encoding="utf-8")), [])

    def test_close_without_body_rejected_with_migration_error(self) -> None:
        """M3：仅备注弱关闭已停用，旧客户端收到明确迁移错误（400）。"""
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(
                esc_routes.api_escalation_close(
                    self._request(), "esc-aiops-fix-order-a1", None
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("结构化", ctx.exception.message)
        self.assertIn("仅备注", ctx.exception.message)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "open")

    def test_close_stale_version_conflicts_without_overwrite(self) -> None:
        """M3 CAS：旧版本关闭返回 409，不覆盖并发写入的新状态。"""
        fresh = esc.get("esc-aiops-fix-order-a1")
        esc.apply_assign(fresh, "bob", "alice")
        esc.persist_entry(fresh)
        with self.assertRaises(ApiError) as ctx:
            self._close({**_resolution_payload(), "version": 1})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("版本", ctx.exception.message)
        loaded = esc.get("esc-aiops-fix-order-a1")
        self.assertEqual(loaded["status"], "assigned")
        self.assertIsNone(loaded["resolution"])

    def test_plan_route_updates_open_entry(self) -> None:
        response = asyncio.run(
            esc_routes.api_escalation_plan(
                self._request(), "esc-aiops-fix-order-a1", esc_routes.PlanBody(plan="周五前核对标签映射")
            )
        )
        self.assertEqual(response["escalation"]["plan"], "周五前核对标签映射")
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "open")


class KnowledgeStatsTest(unittest.TestCase):
    """知识审核计数（M2）：stats 暴露 draft/approved/rejected，供前端徽标。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(esc, "DATA_PATH", Path(self._tmp.name) / "escalations.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _closed(self, wf_id: str, decision: str | None = None) -> dict:
        entry = esc.new_entry(_flow(wf_id=wf_id))
        esc.apply_close(
            entry, "alice", "", esc.build_resolution(_resolution_payload(), "alice")
        )
        if decision:
            esc.apply_knowledge_review(entry, decision, "admin")
        esc.persist_entry(entry)
        return entry

    def test_stats_counts_knowledge_states(self) -> None:
        self._closed("wf-draft")
        self._closed("wf-approved", "approve")
        self._closed("wf-rejected", "reject")
        esc.persist_entry(esc.new_entry(_flow(wf_id="wf-open")))
        knowledge = esc.stats()["knowledge"]
        self.assertEqual(knowledge, {"draft": 1, "approved": 1, "rejected": 1})


class WiringTest(unittest.TestCase):
    """接线冒烟：路由挂载与角色规则（防遗漏注册导致 404 / 权限错配）。"""

    def test_escalation_routes_registered(self) -> None:
        from bff.routes import escalations as routes

        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/escalations", paths)
        self.assertIn("/api/escalations/{esc_id}/assign", paths)
        self.assertIn("/api/escalations/{esc_id}/close", paths)
        self.assertIn("/api/escalations/{esc_id}/plan", paths)
        self.assertIn("/api/escalations/{esc_id}/knowledge/review", paths)
        self.assertIn("/api/escalations/{esc_id}/retry", paths)

    def test_write_role_rule_present(self) -> None:
        from bff.middleware import _WRITE_ROLE_RULES

        self.assertIn(("/api/escalations", "operator"), _WRITE_ROLE_RULES)

    def test_knowledge_review_requires_admin(self) -> None:
        """知识入库是受控动作：审核端点必须高于 operator 前缀规则。"""
        from bff.middleware import required_role

        self.assertEqual(
            required_role("POST", "/api/escalations/esc-x/knowledge/review"), "admin"
        )
        self.assertEqual(required_role("POST", "/api/escalations/esc-x/plan"), "operator")
        self.assertEqual(required_role("POST", "/api/escalations/esc-x/close"), "operator")
        self.assertEqual(required_role("GET", "/api/escalations"), "viewer")


if __name__ == "__main__":
    unittest.main()
