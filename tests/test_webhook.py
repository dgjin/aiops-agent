"""告警接入服务单测（WP1）：Alertmanager 载荷解析、幂等键一致性与接入鉴权（P0-1）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

零外部依赖（不需要 Temporal 服务端）：仅覆盖纯函数解析与鉴权逻辑。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import unittest
from unittest import mock

import demo_cli
from sqlalchemy import create_engine, func, select

import alert_webhook
from aiops_agent import alert_store, db
from aiops_agent.models import Alert
from alert_webhook import (
    AggregateWindow,
    AuthConfig,
    RateLimiter,
    check_request_auth,
    load_auth_config,
    parse_alertmanager_payload,
    workflow_id_for,
)


def _item(
    status: str = "firing",
    labels: dict | None = None,
    annotations: dict | None = None,
    fingerprint: str = "ffffffffffffffff",
    starts_at: str | None = None,
) -> dict:
    """构造 Alertmanager v4 单条 alert（starts_at 缺省不写字段，覆盖旧载荷容错）。"""
    item = {
        "status": status,
        "labels": labels or {},
        "annotations": annotations or {},
        "fingerprint": fingerprint,
    }
    if starts_at is not None:
        item["startsAt"] = starts_at
    return item


def _payload(*items: dict) -> dict:
    return {"version": "4", "status": "firing", "alerts": list(items)}


def _alert(alert_id: str, service: str = "nl2sql") -> Alert:
    return Alert(alert_id=alert_id, service=service)


class TestParseAlertmanagerPayload(unittest.TestCase):
    def test_firing_alert_maps_fields(self) -> None:
        payload = _payload(
            _item(
                labels={
                    "alertname": "QuerySuccessRateLow",
                    "severity": "critical",
                    "service": "nl2sql",
                    "aiops_demo": "true",
                    "alert_id": "a-wp1-001",
                },
                annotations={"summary": "S", "description": "D"},
                fingerprint="abcdef0123456789",
            )
        )
        alerts, resolved = parse_alertmanager_payload(payload)
        self.assertEqual(resolved, 0)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.alert_id, "a-wp1-001")  # labels.alert_id 优先于 fingerprint
        self.assertEqual(alert.service, "nl2sql")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.description, "D")

    def test_resolved_ignored_and_counted(self) -> None:
        payload = _payload(
            _item(labels={"alertname": "A", "service": "s"}),
            _item(status="resolved", labels={"alertname": "A", "service": "s"}),
        )
        alerts, resolved = parse_alertmanager_payload(payload)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(resolved, 1)

    def test_fingerprint_fallback_takes_first_12(self) -> None:
        payload = _payload(
            _item(labels={"alertname": "A", "service": "s"}, fingerprint="0123456789abcdef")
        )
        (alert,), _ = parse_alertmanager_payload(payload)
        self.assertEqual(alert.alert_id, "0123456789ab")

    def test_fingerprint_with_starts_at_forms_cycle_key(self) -> None:
        """无 alert_id 标签时幂等键 = fingerprint 前 12 位 + startsAt 秒级时间戳。"""
        payload = _payload(
            _item(
                labels={"alertname": "Nl2sqlAppDown", "service": "nl2sql"},
                fingerprint="0123456789abcdef",
                starts_at="2026-10-04T05:30:00Z",
            )
        )
        (alert,), _ = parse_alertmanager_payload(payload)
        self.assertEqual(alert.alert_id, "0123456789ab-1791091800")

    def test_invalid_starts_at_falls_back_to_fingerprint(self) -> None:
        payload = _payload(
            _item(
                labels={"alertname": "A"},
                fingerprint="0123456789abcdef",
                starts_at="not-a-date",
            )
        )
        (alert,), _ = parse_alertmanager_payload(payload)
        self.assertEqual(alert.alert_id, "0123456789ab")

    def test_same_starts_at_idempotent_new_cycle_new_id(self) -> None:
        """同一故障周期的重复投递保持幂等；跨周期（startsAt 刷新）产出新幂等键。"""
        labels = {"alertname": "Nl2sqlAppDown", "service": "nl2sql"}
        payload = _payload(
            _item(labels=labels, fingerprint="0123456789abcdef", starts_at="2026-10-04T05:30:00Z"),
            _item(labels=labels, fingerprint="0123456789abcdef", starts_at="2026-10-04T05:30:00Z"),
            _item(labels=labels, fingerprint="0123456789abcdef", starts_at="2026-10-04T08:00:00Z"),
        )
        alerts, _ = parse_alertmanager_payload(payload)
        self.assertEqual(alerts[0].alert_id, alerts[1].alert_id)
        self.assertNotEqual(alerts[0].alert_id, alerts[2].alert_id)

    def test_defaults_service_job_severity_summary(self) -> None:
        payload = _payload(
            _item(labels={"alertname": "A", "job": "node"}, annotations={"summary": "S"})
        )
        (alert,), _ = parse_alertmanager_payload(payload)
        self.assertEqual(alert.service, "node")  # job 兜底
        self.assertEqual(alert.severity, "critical")  # severity 缺省
        self.assertEqual(alert.description, "S")  # description 缺省取 summary

    def test_unknown_service_and_alertname_description(self) -> None:
        payload = _payload(_item(labels={"alertname": "OnlyName"}))
        (alert,), _ = parse_alertmanager_payload(payload)
        self.assertEqual(alert.service, "unknown")  # service/job 均缺失
        self.assertEqual(alert.description, "OnlyName")  # 连注解都缺时用 alertname

    def test_empty_payload(self) -> None:
        alerts, resolved = parse_alertmanager_payload({})
        self.assertEqual(alerts, [])
        self.assertEqual(resolved, 0)


class TestWorkflowId(unittest.TestCase):
    def test_matches_demo_cli(self) -> None:
        alert = Alert(alert_id="a-1", service="order", severity="critical", description="")
        self.assertEqual(workflow_id_for(alert), demo_cli._wf_id("order", "a-1"))


class TestAuthConfig(unittest.TestCase):
    def test_defaults_strict_production(self) -> None:
        cfg = load_auth_config(env={})
        self.assertFalse(cfg.configured)
        self.assertFalse(cfg.demo)
        self.assertEqual(cfg.rate_limit_per_min, 120)
        self.assertEqual(cfg.allow_ips, ())

    def test_parse_env_fields(self) -> None:
        cfg = load_auth_config(
            env={
                "AIOPS_WEBHOOK_TOKEN": " t0k ",
                "AIOPS_WEBHOOK_HMAC_SECRET": "s3cr3t",
                "AIOPS_WEBHOOK_ALLOW_IPS": "1.2.3.4, 5.6.7.8 ,",
                "AIOPS_WEBHOOK_RATE_LIMIT": "10",
                "AIOPS_MODE": "demo",
            }
        )
        self.assertEqual(cfg.token, "t0k")
        self.assertEqual(cfg.hmac_secret, "s3cr3t")
        self.assertEqual(cfg.allow_ips, ("1.2.3.4", "5.6.7.8"))
        self.assertEqual(cfg.rate_limit_per_min, 10)
        self.assertTrue(cfg.demo)

    def test_invalid_rate_falls_back_and_negative_clamped(self) -> None:
        cfg = load_auth_config(env={"AIOPS_WEBHOOK_RATE_LIMIT": "abc"})
        self.assertEqual(cfg.rate_limit_per_min, 120)
        cfg = load_auth_config(env={"AIOPS_WEBHOOK_RATE_LIMIT": "-5"})
        self.assertEqual(cfg.rate_limit_per_min, 0)


class TestCheckRequestAuth(unittest.TestCase):
    BODY = b'{"alerts": []}'

    @staticmethod
    def _cfg(**overrides) -> AuthConfig:
        base = {"demo": True}  # 默认给可放行的演示档，用例按需覆盖
        base.update(overrides)
        return AuthConfig(**base)

    def test_unconfigured_demo_allows(self) -> None:
        self.assertIsNone(check_request_auth({}, self.BODY, "127.0.0.1", self._cfg()))

    def test_unconfigured_production_fail_closed(self) -> None:
        status, body = check_request_auth({}, self.BODY, "127.0.0.1", self._cfg(demo=False))
        self.assertEqual(status, 503)
        self.assertIn("fail-closed", body["error"])

    def test_token_via_bearer_and_custom_header(self) -> None:
        cfg = self._cfg(token="s3cret", demo=False)
        self.assertIsNone(
            check_request_auth({"Authorization": "Bearer s3cret"}, self.BODY, "1.1.1.1", cfg)
        )
        self.assertIsNone(check_request_auth({"X-AIOps-Token": "s3cret"}, self.BODY, "1.1.1.1", cfg))

    def test_wrong_or_missing_token_rejected(self) -> None:
        cfg = self._cfg(token="s3cret", demo=False)
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic s3cret"}):
            status, _ = check_request_auth(headers, self.BODY, "1.1.1.1", cfg)
            self.assertEqual(status, 401)

    def test_hmac_signature(self) -> None:
        cfg = self._cfg(hmac_secret="h-sign", demo=False)
        good = hmac.new(b"h-sign", self.BODY, hashlib.sha256).hexdigest()
        self.assertIsNone(
            check_request_auth({"X-AIOps-Signature": f"sha256={good}"}, self.BODY, "1.1.1.1", cfg)
        )
        # 签名与请求体绑定：换体即失效；乱填签名同样拒绝
        status, _ = check_request_auth(
            {"X-AIOps-Signature": f"sha256={good}"}, b'{"alerts":[{"x":1}]}', "1.1.1.1", cfg
        )
        self.assertEqual(status, 401)
        status, _ = check_request_auth(
            {"X-AIOps-Signature": "sha256=deadbeef"}, self.BODY, "1.1.1.1", cfg
        )
        self.assertEqual(status, 401)

    def test_ip_allowlist_blocks_even_with_valid_token(self) -> None:
        cfg = self._cfg(token="t", demo=False, allow_ips=("10.0.0.1",))
        status, _ = check_request_auth({"Authorization": "Bearer t"}, self.BODY, "10.0.0.9", cfg)
        self.assertEqual(status, 403)
        self.assertIsNone(
            check_request_auth({"Authorization": "Bearer t"}, self.BODY, "10.0.0.1", cfg)
        )


class TestRateLimiter(unittest.TestCase):
    def test_fixed_window_per_key(self) -> None:
        lim = RateLimiter(2)
        self.assertTrue(lim.allow("a", now=0.0))
        self.assertTrue(lim.allow("a", now=1.0))
        self.assertFalse(lim.allow("a", now=2.0))
        self.assertTrue(lim.allow("b", now=2.0))  # 按来源独立计数
        self.assertTrue(lim.allow("a", now=61.0))  # 窗口滚动后恢复

    def test_disabled(self) -> None:
        lim = RateLimiter(0)
        for _ in range(5):
            self.assertTrue(lim.allow("a", now=0.0))


class TestAggregateWindow(unittest.TestCase):
    """P2-1 事件聚合窗口：同故障域窗口内归并、过期开新事件、可关闭、可回滚。"""

    @staticmethod
    def _alert(alert_id: str, service: str = "nl2sql") -> Alert:
        return Alert(alert_id=alert_id, service=service)

    def test_same_service_within_window_merges(self) -> None:
        window = AggregateWindow(90.0)
        to_start, merged = window.route([self._alert("a-1")], now=100.0)
        self.assertEqual([a.alert_id for a in to_start], ["a-1"])
        self.assertEqual(merged, [])
        to_start, merged = window.route([self._alert("a-2")], now=130.0)
        self.assertEqual(to_start, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].count, 2)
        self.assertEqual(merged[0].alert_ids, ["a-1", "a-2"])
        self.assertEqual(merged[0].service, "nl2sql")

    def test_window_expiry_starts_new_event(self) -> None:
        window = AggregateWindow(90.0)
        window.route([self._alert("a-1")], now=100.0)
        to_start, merged = window.route([self._alert("a-2")], now=191.0)  # 91s 后：过期
        self.assertEqual([a.alert_id for a in to_start], ["a-2"])
        self.assertEqual(merged, [])

    def test_disabled_window_passes_all(self) -> None:
        window = AggregateWindow(0)
        to_start, merged = window.route([self._alert("a-1"), self._alert("a-2")], now=100.0)
        self.assertEqual(len(to_start), 2)
        self.assertEqual(merged, [])

    def test_different_services_isolated(self) -> None:
        window = AggregateWindow(90.0)
        to_start, _ = window.route(
            [self._alert("a-1"), self._alert("b-1", service="order")], now=100.0
        )
        self.assertEqual(len(to_start), 2)

    def test_same_batch_same_service_merges_into_first(self) -> None:
        window = AggregateWindow(90.0)
        to_start, merged = window.route(
            [self._alert("a-1"), self._alert("a-2"), self._alert("a-3")], now=100.0
        )
        self.assertEqual([a.alert_id for a in to_start], ["a-1"])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].count, 3)
        self.assertEqual(merged[0].alert_ids, ["a-1", "a-2", "a-3"])

    def test_attach_wf_visible_in_later_merge(self) -> None:
        window = AggregateWindow(90.0)
        window.route([self._alert("a-1")], now=100.0)
        window.attach_wf("nl2sql", "aiops-fix-nl2sql-a-1")
        _, merged = window.route([self._alert("a-2")], now=110.0)
        self.assertEqual(merged[0].wf_id, "aiops-fix-nl2sql-a-1")

    def test_discard_rolls_back_unstarted_events_even_merged(self) -> None:
        window = AggregateWindow(90.0)
        window.route([self._alert("a-1")], now=100.0)  # 干净新事件
        window.discard(["nl2sql"])
        # 回滚后同 service 再投递：仍视为首发（启动失败不吞告警）
        to_start, _ = window.route([self._alert("a-2")], now=105.0)
        self.assertEqual([a.alert_id for a in to_start], ["a-2"])
        # 已归并（count>1）但未启动的事件同样整体回滚：重投后必须能重新开流程
        window.route([self._alert("a-3")], now=106.0)
        window.discard(["nl2sql"])
        to_start, merged = window.route([self._alert("a-4")], now=107.0)
        self.assertEqual([a.alert_id for a in to_start], ["a-4"])
        self.assertEqual(merged, [])
        # 已绑定流程 id 的事件不回滚（重复投递仅归并）
        window.attach_wf("nl2sql", "aiops-fix-nl2sql-a-4")
        window.discard(["nl2sql"])
        to_start, merged = window.route([self._alert("a-5")], now=108.0)
        self.assertEqual(to_start, [])
        self.assertEqual(merged[0].count, 2)

    def test_state_boundary_cleanup_drops_expired(self) -> None:
        window = AggregateWindow(90.0)
        for i in range(1024):
            window.route([self._alert(f"s{i}-1", service=f"svc{i}")], now=100.0)
        # 窗口过期后新增第 1025 个域：触发内存兜底清理，过期项被清掉
        to_start, _ = window.route([self._alert("x-1", service="svcX")], now=200.0)
        self.assertEqual(len(to_start), 1)
        # svc0 已被清理：再投递视为新事件（开新流程，不被误吞）
        to_start, _ = window.route([self._alert("s0-2", service="svc0")], now=201.0)
        self.assertEqual(len(to_start), 1)


class DemoHandlerTest(unittest.TestCase):
    """demo 主处理：失败必 5xx 重投、部分成功保留、同批归并失败不吞告警。"""

    def setUp(self) -> None:
        self._orig_window = alert_webhook._AGGREGATE_WINDOW
        alert_webhook._AGGREGATE_WINDOW = alert_webhook.AggregateWindow(90.0)
        self.addCleanup(self._restore_window)

    def _restore_window(self) -> None:
        alert_webhook._AGGREGATE_WINDOW = self._orig_window

    def test_partial_failure_keeps_success_and_retries_failure(self) -> None:
        async def fake(alerts, snapshot):
            ok = [a for a in alerts if a.service == "nl2sql"]
            bad = [a for a in alerts if a.service == "order"]
            return (
                [alert_webhook.workflow_id_for(a) for a in ok],
                [],
                [(a, RuntimeError("boom")) for a in bad],
            )

        with mock.patch.object(alert_webhook, "_start_workflows", side_effect=fake):
            status, body = alert_webhook._handle_alerts(
                [_alert("a-1"), _alert("b-1", service="order")], 0
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["started"], ["aiops-fix-nl2sql-a-1"])
        self.assertEqual(body["failed"], ["b-1"])

        async def fake_ok(alerts, snapshot):
            return [alert_webhook.workflow_id_for(a) for a in alerts], [], []

        # 重投整批：nl2sql 已绑定流程（仅归并），order 补启动
        with mock.patch.object(alert_webhook, "_start_workflows", side_effect=fake_ok):
            status, body = alert_webhook._handle_alerts(
                [_alert("a-1"), _alert("b-1", service="order")], 0
            )
        self.assertEqual(status, 200)
        self.assertIn("aiops-fix-order-b-1", body["started"])

    def test_same_batch_merge_failure_does_not_swallow_alerts(self) -> None:
        """回归（旧 bug）：同批 [a-1, a-2] 归并后代表启动失败——重投必须重新开流程。"""

        async def fail(alerts, snapshot):
            return [], [], [(a, RuntimeError("temporal down")) for a in alerts]

        with mock.patch.object(alert_webhook, "_start_workflows", side_effect=fail):
            status, _ = alert_webhook._handle_alerts([_alert("a-1"), _alert("a-2")], 0)
        self.assertEqual(status, 500)

        async def ok(alerts, snapshot):
            return [alert_webhook.workflow_id_for(a) for a in alerts], [], []

        with mock.patch.object(alert_webhook, "_start_workflows", side_effect=ok):
            status, body = alert_webhook._handle_alerts([_alert("a-1"), _alert("a-2")], 0)
        self.assertEqual(status, 200)
        self.assertEqual(body["started"], ["aiops-fix-nl2sql-a-1"])


async def _confirm_all(engine, events):
    """测试替身：模拟启动成功并如实落库（不触 Temporal）。"""
    for event in events:
        alert_store.mark_started(engine, event.event_id, event.claim_token)
    return [event.wf_id for event in events], [], []


async def _fail_all(engine, events):
    """测试替身：模拟 Temporal 故障（释放租约等重投）。"""
    failures = []
    for event in events:
        alert_store.mark_failed(engine, event.event_id, event.claim_token, "temporal down")
        failures.append((event, RuntimeError("temporal down")))
    return [], [], failures


async def _noop_start(engine, events):
    """测试替身：认领但不回报（模拟启动仍在进行）。"""
    return [], [], []


class ProductionHandlerTest(unittest.TestCase):
    """production 主处理：SQLite 夹具覆盖持久化聚合、busy 重投、失败重投与补偿。"""

    def setUp(self) -> None:
        db.reset_engine()
        self.addCleanup(db.reset_engine)
        self.engine = create_engine("sqlite://")
        db.metadata.create_all(self.engine)
        env = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": "sqlite://"}
        )
        env.start()
        self.addCleanup(env.stop)
        engine_patch = mock.patch.object(alert_webhook.db, "get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        kill_patch = mock.patch.object(alert_webhook, "check_kill_switch", return_value=None)
        kill_patch.start()
        self.addCleanup(kill_patch.stop)

    def _counts(self) -> tuple[int, int]:
        with self.engine.connect() as conn:
            events = conn.execute(select(func.count()).select_from(db.alert_events)).scalar_one()
            members = conn.execute(
                select(func.count()).select_from(db.alert_members)
            ).scalar_one()
        return int(events), int(members)

    def _event_row(self):
        with self.engine.connect() as conn:
            return conn.execute(select(db.alert_events)).mappings().one()

    def test_batch_aggregates_and_starts_representative(self) -> None:
        with mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ):
            status, body = alert_webhook._handle_alerts([_alert("a-1"), _alert("a-2")], 0)
        self.assertEqual(status, 200)
        self.assertEqual(body["started"], ["aiops-fix-nl2sql-a-1"])
        self.assertEqual(body["aggregated"][0]["count"], 2)
        self.assertEqual(self._counts(), (1, 2))

    def test_start_failure_500_then_retry_starts_same_wf(self) -> None:
        with mock.patch.object(alert_webhook, "_start_claimed_events", side_effect=_fail_all):
            status, body = alert_webhook._handle_alerts([_alert("a-1"), _alert("a-2")], 0)
        self.assertEqual(status, 500)
        self.assertEqual(body["failed"], ["aiops-fix-nl2sql-a-1"])
        row = self._event_row()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["claim_token"], "")
        # Alertmanager 重投：同一事件、同一代表 id，成员不重复计数
        with mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ):
            status, body = alert_webhook._handle_alerts([_alert("a-1"), _alert("a-2")], 0)
        self.assertEqual(status, 200)
        self.assertEqual(body["started"], ["aiops-fix-nl2sql-a-1"])
        self.assertEqual(self._counts(), (1, 2))

    def test_live_claim_from_other_instance_returns_503(self) -> None:
        # 第一次只认领不回报（模拟另一实例仍在启动）
        with mock.patch.object(alert_webhook, "_start_claimed_events", side_effect=_noop_start):
            status, _ = alert_webhook._handle_alerts([_alert("a-1")], 0)
        self.assertEqual(status, 200)
        status, body = alert_webhook._handle_alerts([_alert("a-2")], 0)
        self.assertEqual(status, 503)
        self.assertTrue(body["busy"])
        # 成员仍然计入（去重后 2 条），代表 id 不变
        self.assertEqual(self._counts(), (1, 2))

    def test_kill_switch_blocks_before_store(self) -> None:
        with mock.patch.object(
            alert_webhook, "check_kill_switch", return_value={"error": "kill switch 已激活"}
        ):
            status, _ = alert_webhook._handle_alerts([_alert("a-1")], 0)
        self.assertEqual(status, 503)
        self.assertEqual(self._counts(), (0, 0))

    def test_compensate_respects_backoff_then_retries_same_wf(self) -> None:
        outcome = alert_store.ingest(self.engine, [_alert("a-1")], {}, 90.0)
        event = outcome.to_start[0]
        alert_store.mark_failed(self.engine, event.event_id, event.claim_token, "boom")
        with mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ) as fake:
            self.assertEqual(alert_webhook._compensate_once(self.engine), 0)  # 退避未到期
            fake.assert_not_called()
        with self.engine.begin() as conn:  # 让退避到期
            conn.execute(db.alert_events.update().values(retry_at=None))
        with mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ):
            self.assertEqual(alert_webhook._compensate_once(self.engine), 1)
        self.assertEqual(self._event_row()["status"], "started")
        with mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ) as fake:
            self.assertEqual(alert_webhook._compensate_once(self.engine), 0)  # 已启动不重复
            fake.assert_not_called()

    def test_compensate_skips_on_kill_switch(self) -> None:
        with mock.patch.object(
            alert_webhook, "check_kill_switch", return_value={"error": "active"}
        ), mock.patch.object(
            alert_webhook, "_start_claimed_events", side_effect=_confirm_all
        ) as fake:
            self.assertEqual(alert_webhook._compensate_once(self.engine), 0)
            fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
