"""日志管道与 LLM triage 单测（WP2/WP3）：离线零外部依赖。

覆盖：
    - cluster_lines：Drain3 聚类（同模板合并、异模板分离、掩码、空输入）；
    - is_surge：突增判定（绝对门槛、环比倍数、基线为 0）；
    - triage schema：pydantic 强校验（合法/越界/缺字段/extra 忽略）；
    - strip_code_fence / build_triage_prompt / fallback / run_triage 降级与成功路径（mock Ollama）；
    - build_surge_alert：Alertmanager 载荷标签完整性；
    - resolve_services：显式参数解析 / 清单动态去重 / 清单不可读容错。

运行：
    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from pydantic import ValidationError

from aiops_agent import logs, triage
from aiops_agent.models import Alert
from log_surge_detector import build_surge_alert, resolve_services

ALERT = Alert(alert_id="test-001", service="nl2sql", description="错误日志突增")


class TestClusterLines(unittest.TestCase):
    def test_similar_lines_merge_to_one_template(self) -> None:
        lines = [
            "ERROR [trace_id=trace-{:08x}-{:03d}] java.lang.NullPointerException: coupon is null"
            " at com.example.OrderService.submit(OrderService.java:{})".format(i, i % 100, i)
            for i in range(20)
        ]
        result = logs.cluster_lines(lines)
        self.assertEqual(result["total_lines"], 20)
        self.assertEqual(len(result["templates"]), 1)
        self.assertEqual(result["templates"][0]["count"], 20)
        pattern = result["templates"][0]["pattern"]
        self.assertIn("NullPointerException", pattern)
        self.assertIn("<TRACEID>", pattern)  # trace_id 已掩码
        self.assertNotIn("trace-", pattern)

    def test_distinct_lines_split_templates(self) -> None:
        lines = [
            "ERROR [trace_id=aaa] java.lang.NullPointerException: coupon is null",
            "ERROR [trace_id=bbb] connection timeout service=coupon-mock elapsed=3000",
        ]
        result = logs.cluster_lines(lines)
        self.assertEqual(len(result["templates"]), 2)

    def test_empty_input(self) -> None:
        result = logs.cluster_lines([])
        self.assertEqual(result["total_lines"], 0)
        self.assertEqual(result["templates"], [])
        self.assertEqual(result["compression_ratio"], 0.0)


class TestIsSurge(unittest.TestCase):
    def test_below_min_lines_never_surge(self) -> None:
        self.assertFalse(logs.is_surge(99, 0, min_lines=100, factor=3))
        self.assertFalse(logs.is_surge(50, 10, min_lines=100, factor=3))

    def test_zero_baseline_over_min_is_surge(self) -> None:
        self.assertTrue(logs.is_surge(100, 0, min_lines=100, factor=3))

    def test_factor_comparison_strict(self) -> None:
        self.assertTrue(logs.is_surge(360, 100, min_lines=100, factor=3))
        self.assertFalse(logs.is_surge(300, 100, min_lines=100, factor=3))  # 等于倍数不算突增


class TestTriageSchema(unittest.TestCase):
    def test_valid_payload(self) -> None:
        out = triage.TriageOutput.model_validate(
            {"error_type": "TimeoutError", "suspect_files": ["a.py"], "confidence": 0.7, "summary": "s"}
        )
        self.assertEqual(out.error_type, "TimeoutError")

    def test_confidence_out_of_range_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            triage.TriageOutput.model_validate({"error_type": "X", "confidence": 1.5, "summary": "s"})
        with self.assertRaises(ValidationError):
            triage.TriageOutput.model_validate({"error_type": "X", "confidence": -0.1, "summary": "s"})

    def test_empty_fields_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            triage.TriageOutput.model_validate({"error_type": "", "confidence": 0.5, "summary": "s"})
        with self.assertRaises(ValidationError):
            triage.TriageOutput.model_validate({"error_type": "X", "confidence": 0.5})

    def test_extra_fields_ignored(self) -> None:
        out = triage.TriageOutput.model_validate(
            {"error_type": "X", "confidence": 0.5, "summary": "s", "unknown_extra": 1}
        )
        self.assertEqual(out.error_type, "X")


class TestStripCodeFence(unittest.TestCase):
    def test_fenced_json_stripped(self) -> None:
        self.assertEqual(triage.strip_code_fence('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_bare_json_unchanged(self) -> None:
        self.assertEqual(triage.strip_code_fence('  {"a": 1}  '), '{"a": 1}')


class TestBuildTriagePrompt(unittest.TestCase):
    def test_contains_alert_and_templates(self) -> None:
        clustered = {
            "templates": [
                {"id": "tpl-1", "pattern": "ERROR [<TRACEID>] NullPointerException ...", "count": 760}
            ],
            "compression_ratio": 0.001,
            "total_lines": 800,
        }
        prompt = triage.build_triage_prompt(ALERT, clustered, ["trace-abc"])
        self.assertIn("nl2sql", prompt)
        self.assertIn("[760x]", prompt)
        self.assertIn("NullPointerException", prompt)
        self.assertIn("trace-abc", prompt)

    def test_no_evidence_placeholder(self) -> None:
        prompt = triage.build_triage_prompt(ALERT, {"templates": [], "total_lines": 0})
        self.assertIn("（无日志证据）", prompt)


class TestRunTriage(unittest.TestCase):
    def test_success_path_parses_and_truncates(self) -> None:
        payload = json.dumps(
            {
                "error_type": "NullPointerException",
                "suspect_files": ["a.java", "b.java", "c.java", "d.java"],
                "confidence": 0.86,
                "summary": "优惠券空指针",
            }
        )
        with mock.patch("aiops_agent.triage.call_ollama", return_value=f"```json\n{payload}\n```"):
            root_cause, meta = triage.run_triage(ALERT, {"templates": []})
        self.assertFalse(meta["degraded"])
        self.assertEqual(root_cause.error_type, "NullPointerException")
        self.assertEqual(len(root_cause.suspect_files), 3)  # 截断为最多 3 个
        self.assertAlmostEqual(root_cause.confidence, 0.86)
        self.assertGreaterEqual(meta["elapsed_seconds"], 0)

    def test_llm_unavailable_degrades_safely(self) -> None:
        with mock.patch("aiops_agent.triage.call_ollama", side_effect=TimeoutError("ollama down")):
            root_cause, meta = triage.run_triage(ALERT, {"templates": []})
        self.assertTrue(meta["degraded"])
        self.assertEqual(root_cause.error_type, "Unknown")
        self.assertEqual(root_cause.confidence, 0.0)  # 闸门 1 将拦截转人工

    def test_invalid_llm_json_degrades_safely(self) -> None:
        with mock.patch("aiops_agent.triage.call_ollama", return_value="not a json at all"):
            root_cause, meta = triage.run_triage(ALERT, {"templates": []})
        self.assertTrue(meta["degraded"])
        self.assertEqual(root_cause.confidence, 0.0)


class TestBuildSurgeAlert(unittest.TestCase):
    def test_labels_and_annotations(self) -> None:
        payload = build_surge_alert("nl2sql", 800, 40)
        self.assertEqual(len(payload), 1)
        labels = payload[0]["labels"]
        self.assertEqual(labels["alertname"], "LogErrorSurge")
        self.assertEqual(labels["service"], "nl2sql")
        self.assertEqual(labels["severity"], "critical")
        self.assertEqual(labels["aiops_demo"], "true")
        self.assertTrue(labels["alert_id"].startswith("logsurge-nl2sql-"))
        description = payload[0]["annotations"]["description"]
        self.assertIn("800", description)
        self.assertIn("40", description)


class TestResolveServices(unittest.TestCase):
    def test_explicit_list_parsed(self) -> None:
        self.assertEqual(resolve_services("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(resolve_services(" , ,"), [])

    def test_manifest_mode_dedups_and_sorts(self) -> None:
        apps = [
            {"service": "nl2sql", "log_path": "x.log"},
            {"service": "order", "log_path": "y.log"},
            {"service": "nl2sql", "log_path": "z.log"},
            {"log_path": "no-service.log"},  # 无 service 的条目跳过
        ]
        with mock.patch("bff.monitored_apps.log_targets", return_value=apps):
            self.assertEqual(resolve_services(None), ["nl2sql", "order"])

    def test_manifest_unavailable_returns_empty(self) -> None:
        with mock.patch("bff.monitored_apps.log_targets", side_effect=RuntimeError("boom")):
            self.assertEqual(resolve_services(None), [])


class TestHistoricalTickets(unittest.TestCase):
    def test_load_samples(self) -> None:
        tickets = triage.load_historical_tickets()
        self.assertGreaterEqual(len(tickets), 6)
        self.assertTrue(all(t["error_type"] and t["root_cause"] for t in tickets))


if __name__ == "__main__":
    unittest.main()
