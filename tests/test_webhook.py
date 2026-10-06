"""告警接入服务单测（WP1）：Alertmanager 载荷解析、幂等键一致性与接入鉴权（P0-1）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

零外部依赖（不需要 Temporal 服务端）：仅覆盖纯函数解析与鉴权逻辑。
"""

from __future__ import annotations

import hashlib
import hmac
import unittest

import demo_cli
from aiops_agent.models import Alert
from alert_webhook import (
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


if __name__ == "__main__":
    unittest.main()
