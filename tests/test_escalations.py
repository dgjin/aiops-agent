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

    def test_assign_rejects_blank_and_closed(self) -> None:
        entry = esc.new_entry(_flow())
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_assign(entry, "   ", "alice")
        esc.apply_close(entry, "alice", "手动处置")
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
        esc.apply_close(entry, "alice", "已回滚发布")
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["note"], "已回滚发布")
        self.assertIsNotNone(entry["closed_at"])
        with self.assertRaises(esc.EscalationStoreError):
            esc.apply_close(entry, "alice", "")

    def test_retried_closes_with_new_wf_id(self) -> None:
        entry = esc.new_entry(_flow())
        esc.apply_retried(entry, "aiops-fix-order-a1-retry1", "alice")
        self.assertEqual(entry["status"], "closed")
        self.assertIn("retry1", entry["note"])
        self.assertEqual(entry["history"][-1]["action"], "retry")

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
        esc.apply_close(entry, "alice", "")
        esc.persist_entry(entry)
        self.assertEqual(esc.sync_from_flows([_flow()]), 0)
        self.assertEqual(esc.get("esc-aiops-fix-order-a1")["status"], "closed")

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
        esc.apply_close(second, "alice", "")
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
        self.assertEqual(s["closed"], 0)
        self.assertEqual(s["sla_minutes"], esc.DEFAULT_SLA_MINUTES)

    def test_write_is_atomic_no_tmp_left(self) -> None:
        esc.sync_from_flows([_flow()])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


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
        esc.apply_close(entry, "alice", "")
        esc.persist_entry(entry)
        self.assertEqual(esc.due_for_re_escalation(), [])  # 已关闭：不再巡检

    def test_sweep_sends_once_per_entry(self) -> None:
        self._seed_overdue()
        with mock.patch.object(
            esc, "_send_re_escalation", return_value={"mode": "demo"}
        ) as sender:
            first = esc.sweep_due()
            second = esc.sweep_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        sender.assert_called_once()
        self.assertTrue(esc.get("esc-wf-old")["re_escalated"])

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

    def _retry(self, esc_id: str) -> dict:
        return asyncio.run(esc_routes.api_escalation_retry(self._request(), esc_id))

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
        self.assertEqual(entry["status"], "closed")
        self.assertIn("aiops-req-ra-app-19-v1-req-19-retry1", entry["note"])
        self.assertEqual(self.audit.call_args.kwargs["params"]["mode"], "requirement")

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


class WiringTest(unittest.TestCase):
    """接线冒烟：路由挂载与角色规则（防遗漏注册导致 404 / 权限错配）。"""

    def test_escalation_routes_registered(self) -> None:
        from bff.routes import escalations as routes

        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/escalations", paths)
        self.assertIn("/api/escalations/{esc_id}/assign", paths)
        self.assertIn("/api/escalations/{esc_id}/close", paths)
        self.assertIn("/api/escalations/{esc_id}/retry", paths)

    def test_write_role_rule_present(self) -> None:
        from bff.middleware import _WRITE_ROLE_RULES

        self.assertIn(("/api/escalations", "operator"), _WRITE_ROLE_RULES)


if __name__ == "__main__":
    unittest.main()
