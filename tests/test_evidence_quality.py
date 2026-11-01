"""证据质量预检（M1）：六状态判定 / 阻断分类 / Loki 元数据查询。

背景：日志供给恢复后，「查不到日志」≠「服务没问题」。本模块把证据质量固定为六个
状态（见 ``aiops_agent/evidence_quality.py``），供工作流在调用根因模型前预检、供
前端展示与筛选；此处只测确定性纯函数与查询元数据，不触达真实 Loki。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from aiops_agent import evidence_quality, logs

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def evaluate(**kwargs):
    params = {
        "service": "nl2sql",
        "loki_available": True,
        "lines": ["ERROR NullPointerException: coupon is null"],
        "latest_log_at": (NOW - timedelta(minutes=1)).isoformat(),
        "registered": True,
        "lookback_minutes": 10,
        "stale_after_minutes": 15,
        "min_signal_lines": 1,
        "now": NOW,
    }
    params.update(kwargs)
    return evidence_quality.evaluate(**params)


class EvaluateTest(unittest.TestCase):
    """六状态判定顺序：Loki 不可用 > 未映射（仅无日志）> 无日志 > 过期 > 信号不足 > OK。"""

    def test_ok_with_fresh_error_lines(self) -> None:
        result = evaluate()
        self.assertEqual(result["status"], evidence_quality.OK)
        self.assertEqual(result["signal_lines"], 1)
        self.assertEqual(result["line_count"], 1)
        self.assertEqual(result["service_registered"], True)
        self.assertIn("latest_log_at", result)

    def test_loki_unavailable_wins(self) -> None:
        result = evaluate(loki_available=False, lines=[], latest_log_at="", query_error="HTTP 502")
        self.assertEqual(result["status"], evidence_quality.LOKI_UNAVAILABLE)
        self.assertIn("HTTP 502", result["detail"])

    def test_no_logs_when_service_registered(self) -> None:
        result = evaluate(lines=[], latest_log_at="", registered=True)
        self.assertEqual(result["status"], evidence_quality.NO_LOGS)
        self.assertIn("无服务", result["detail"])

    def test_no_logs_when_registry_unknown(self) -> None:
        # 注册表为空（registered=None）时跳过映射校验，不能凭空判 UNMAPPED
        result = evaluate(lines=[], latest_log_at="", registered=None)
        self.assertEqual(result["status"], evidence_quality.NO_LOGS)

    def test_unmapped_only_when_unregistered_and_no_logs(self) -> None:
        result = evaluate(lines=[], latest_log_at="", registered=False)
        self.assertEqual(result["status"], evidence_quality.UNMAPPED_SERVICE)

    def test_unregistered_service_with_logs_is_not_blocked(self) -> None:
        # 观测面已证明该标签可查询（如 node/redis 等基础设施服务）：不判 UNMAPPED
        result = evaluate(registered=False)
        self.assertEqual(result["status"], evidence_quality.OK)

    def test_missing_service_label_is_unmapped(self) -> None:
        result = evaluate(service="  ")
        self.assertEqual(result["status"], evidence_quality.UNMAPPED_SERVICE)

    def test_stale_logs_threshold(self) -> None:
        latest = (NOW - timedelta(minutes=16)).isoformat()
        result = evaluate(lines=["ERROR boom"], latest_log_at=latest)
        self.assertEqual(result["status"], evidence_quality.STALE_LOGS)

    def test_fresh_at_threshold_boundary_is_ok(self) -> None:
        latest = (NOW - timedelta(minutes=15)).isoformat()
        result = evaluate(lines=["ERROR boom"], latest_log_at=latest)
        self.assertEqual(result["status"], evidence_quality.OK)

    def test_insufficient_signal_for_health_checks(self) -> None:
        result = evaluate(
            lines=["GET /healthz 200", "server started, listening on 8080"],
            latest_log_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(result["status"], evidence_quality.INSUFFICIENT_SIGNAL)
        self.assertEqual(result["signal_lines"], 0)

    def test_min_signal_lines_threshold(self) -> None:
        lines = ["ERROR first", "timeout calling downstream"]
        self.assertEqual(evaluate(lines=lines, min_signal_lines=3)["status"], evidence_quality.INSUFFICIENT_SIGNAL)
        self.assertEqual(evaluate(lines=lines, min_signal_lines=2)["status"], evidence_quality.OK)

    def test_empty_lines_with_latest_timestamp_is_not_no_logs(self) -> None:
        # 历史窗口回看命中了流但行被截断：有最新时间即不判 NO_LOGS，交给陈旧/信号判定
        latest = (NOW - timedelta(minutes=1)).isoformat()
        result = evaluate(lines=[], latest_log_at=latest)
        self.assertEqual(result["status"], evidence_quality.INSUFFICIENT_SIGNAL)


class SignalHelpersTest(unittest.TestCase):
    def test_signal_and_noise_regex(self) -> None:
        self.assertEqual(
            evidence_quality.signal_lines(["ERROR boom", "错误：超时", "GET /ok"]),
            ["ERROR boom", "错误：超时"],
        )
        self.assertTrue(evidence_quality.is_noise_line("health check 200"))
        self.assertFalse(evidence_quality.is_noise_line("ERROR health check failed"))

    def test_blocking_category_and_reason(self) -> None:
        self.assertEqual(evidence_quality.blocking_category(evidence_quality.INSUFFICIENT_SIGNAL), "evidence")
        for status in (
            evidence_quality.LOKI_UNAVAILABLE,
            evidence_quality.NO_LOGS,
            evidence_quality.STALE_LOGS,
            evidence_quality.UNMAPPED_SERVICE,
        ):
            self.assertEqual(evidence_quality.blocking_category(status), "observability")

        reason = evidence_quality.escalation_reason(
            evaluate(lines=[], latest_log_at="", registered=True)
        )
        self.assertIn("观测异常", reason)
        self.assertIn(evidence_quality.NO_LOGS, reason)


class QueryMetaTest(unittest.TestCase):
    """Loki query_range 元数据：时间升序 / 最新时间 / 流数量 / 截断标记。"""

    BASE_NS = 1_700_000_000_000_000_000

    def _payload(self, rows):
        return {"data": {"result": [{"stream": {"service": "nl2sql"}, "values": rows}]}}

    def test_returns_latest_at_and_counts(self) -> None:
        payload = self._payload(
            [
                [str(self.BASE_NS + 2_000_000_000), "third"],
                [str(self.BASE_NS), "first"],
                [str(self.BASE_NS + 1_000_000_000), "second"],
            ]
        )
        with mock.patch.object(logs, "_get_json", return_value=payload):
            meta = logs.query_lines_with_meta("nl2sql", lookback_minutes=10, limit=10)
        self.assertEqual(meta["lines"], ["first", "second", "third"])
        self.assertEqual(meta["latest_ns"], self.BASE_NS + 2_000_000_000)
        self.assertEqual(meta["latest_at"], "2023-11-14T22:13:22+00:00")
        self.assertEqual(meta["stream_count"], 1)
        self.assertFalse(meta["truncated"])
        self.assertEqual(meta["line_count"], 3)

    def test_truncation_keeps_newest_rows(self) -> None:
        payload = self._payload(
            [[str(self.BASE_NS + i), f"line-{i}"] for i in range(4)]
        )
        with mock.patch.object(logs, "_get_json", return_value=payload) as get_json:
            meta = logs.query_lines_with_meta("nl2sql", limit=2)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["lines"], ["line-2", "line-3"])
        self.assertEqual(meta["line_count"], 2)
        # 向 Loki 多取 1 行用于判定截断
        self.assertIn("limit=3", get_json.call_args.args[0])

    def test_empty_result(self) -> None:
        with mock.patch.object(logs, "_get_json", return_value={"data": {"result": []}}):
            meta = logs.query_lines_with_meta("nl2sql")
        self.assertEqual(meta["lines"], [])
        self.assertEqual(meta["latest_at"], "")
        self.assertIsNone(meta["latest_ns"])
        self.assertEqual(meta["stream_count"], 0)

    def test_query_lines_wrapper_compatible(self) -> None:
        payload = self._payload([[str(self.BASE_NS), "only"]])
        with mock.patch.object(logs, "_get_json", return_value=payload):
            self.assertEqual(logs.query_lines("nl2sql"), ["only"])


if __name__ == "__main__":
    unittest.main()
