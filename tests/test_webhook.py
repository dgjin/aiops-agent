"""告警接入服务单测（WP1）：Alertmanager 载荷解析与幂等键一致性。

运行：
    .venv/bin/python -m unittest discover -s tests -v

零外部依赖（不需要 Temporal 服务端）：仅覆盖纯函数解析逻辑。
"""

from __future__ import annotations

import unittest

import demo_cli
from aiops_agent.models import Alert
from alert_webhook import parse_alertmanager_payload, workflow_id_for


def _item(
    status: str = "firing",
    labels: dict | None = None,
    annotations: dict | None = None,
    fingerprint: str = "ffffffffffffffff",
) -> dict:
    """构造 Alertmanager v4 单条 alert。"""
    return {
        "status": status,
        "labels": labels or {},
        "annotations": annotations or {},
        "fingerprint": fingerprint,
    }


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


if __name__ == "__main__":
    unittest.main()
