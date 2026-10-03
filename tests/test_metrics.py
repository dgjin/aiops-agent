"""Prometheus 指标与可选 OTel 追踪单测（批次 5 / 优化方案 3.4 GAP-07/08）。

- 指标注册与 observe_* 增量：``REGISTRY.get_sample_value`` 前后差值断言（不依赖初始值）；
- BFF /metrics 端点与请求中间件：路由模板标签（防高基数）、自身不计数、鉴权早退计数；
- runs_store 终态指标：production + SQLite 分支，仅终态首次出现计 workflow_completed；
- tracing：未配置 no-op、初始化失败降级（绝不阻塞服务）。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from aiops_agent import db, metrics, tracing

from bff import runs_store

TOKENS = {
    "tok-adm": {"user": "li", "role": "admin"},
}


def _value(name: str, labels: dict) -> float:
    """读取样本值（不存在时按 0.0 处理，便于差值断言）。"""
    return REGISTRY.get_sample_value(name, labels) or 0.0


class MetricsCoreTest(unittest.TestCase):
    def test_available_with_prometheus_client(self) -> None:
        self.assertTrue(metrics.available())

    def test_render_contains_metric_names(self) -> None:
        payload, content_type = metrics.render()
        self.assertIn("text/plain", content_type)
        text = payload.decode("utf-8")
        for name in (
            "aiops_workflow_started_total",
            "aiops_fix_attempts_total",
            "aiops_sandbox_runs_total",
            "aiops_canary_deploys_total",
            "aiops_bff_requests_total",
            "aiops_gate_events_total",
        ):
            self.assertIn(name, text)

    def test_render_disabled_when_client_missing(self) -> None:
        with mock.patch.object(metrics, "_AVAILABLE", False):
            payload, content_type = metrics.render()
        self.assertIn(b"metrics disabled", payload)
        self.assertIn("text/plain", content_type)

    def test_label_sanitization(self) -> None:
        self.assertEqual(metrics._label(None), "unknown")
        self.assertEqual(metrics._label("   "), "unknown")
        self.assertEqual(metrics._label("a b/c"), "a_b/c")
        self.assertEqual(metrics._label("/api/flows/{wf_id}"), "/api/flows/{wf_id}")
        self.assertEqual(metrics._label("order.v1:blue-2"), "order.v1:blue-2")
        self.assertEqual(len(metrics._label("x" * 100)), 64)
        self.assertEqual(metrics._label("", fallback="GET"), "GET")


class ObserveCountersTest(unittest.TestCase):
    """observe_* 增量断言（conftest 固定 demo 模式标签）。"""

    def test_workflow_started_increment(self) -> None:
        labels = {"service": "order-metric-t", "mode": "demo"}
        before = _value("aiops_workflow_started_total", labels)
        metrics.observe_workflow_started("order-metric-t")
        self.assertEqual(_value("aiops_workflow_started_total", labels) - before, 1.0)

    def test_workflow_completed_stage_normalized_and_duration(self) -> None:
        completed = {"service": "order-metric-t", "stage": "DONE", "mode": "demo"}
        duration = {"service": "order-metric-t", "mode": "demo"}
        before_c = _value("aiops_workflow_completed_total", completed)
        before_d = _value("aiops_workflow_duration_seconds_count", duration)
        metrics.observe_workflow_completed("order-metric-t", "done", 12.5)
        self.assertEqual(_value("aiops_workflow_completed_total", completed) - before_c, 1.0)
        self.assertEqual(
            _value("aiops_workflow_duration_seconds_count", duration) - before_d, 1.0
        )

    def test_fix_attempt_provider_and_degraded_labels(self) -> None:
        ok = {"provider": "qoder", "degraded": "false", "mode": "demo"}
        degraded = {"provider": "qoder", "degraded": "true", "mode": "demo"}
        before_ok = _value("aiops_fix_attempts_total", ok)
        before_deg = _value("aiops_fix_attempts_total", degraded)
        metrics.observe_fix_attempt("qoder", False)
        metrics.observe_fix_attempt("qoder", True, 3.5)
        self.assertEqual(_value("aiops_fix_attempts_total", ok) - before_ok, 1.0)
        self.assertEqual(_value("aiops_fix_attempts_total", degraded) - before_deg, 1.0)

    def test_sandbox_run_pass_fail_labels(self) -> None:
        passed = {"passed": "true", "mode": "demo"}
        failed = {"passed": "false", "mode": "demo"}
        before_p = _value("aiops_sandbox_runs_total", passed)
        before_f = _value("aiops_sandbox_runs_total", failed)
        metrics.observe_sandbox_run(True, 1.2)
        metrics.observe_sandbox_run(False)
        self.assertEqual(_value("aiops_sandbox_runs_total", passed) - before_p, 1.0)
        self.assertEqual(_value("aiops_sandbox_runs_total", failed) - before_f, 1.0)

    def test_canary_deploy_and_error_rate(self) -> None:
        healthy = {"healthy": "true", "mode": "demo"}
        before_h = _value("aiops_canary_deploys_total", healthy)
        before_r = _value("aiops_canary_error_rate_count", {"mode": "demo"})
        metrics.observe_canary_deploy(True, 1.5)
        self.assertEqual(_value("aiops_canary_deploys_total", healthy) - before_h, 1.0)
        self.assertEqual(
            _value("aiops_canary_error_rate_count", {"mode": "demo"}) - before_r, 1.0
        )

    def test_gate_event_increment(self) -> None:
        labels = {"gate": "approval", "decision": "approve", "mode": "demo"}
        before = _value("aiops_gate_events_total", labels)
        metrics.observe_gate_event("approval", "approve")
        self.assertEqual(_value("aiops_gate_events_total", labels) - before, 1.0)

    def test_bff_request_increment(self) -> None:
        labels = {"method": "GET", "path": "/api/unit-metric", "status": "200"}
        before = _value("aiops_bff_requests_total", labels)
        metrics.observe_bff_request("get", "/api/unit-metric", 200, 0.01)
        self.assertEqual(_value("aiops_bff_requests_total", labels) - before, 1.0)


class BffMetricsApiTest(unittest.TestCase):
    """TestClient 集成：/metrics 端点与请求中间件（真实中间件链）。"""

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ,
            {"AIOPS_MODE": "demo", "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS)},
        )
        self._env.start()
        from bff import app as app_module

        self.app_module = app_module
        self.client = TestClient(app_module.app)
        self.admin = {"Authorization": "Bearer tok-adm"}

    def tearDown(self) -> None:
        self._env.stop()

    def test_metrics_endpoint_public_and_prometheus_format(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("aiops_bff_requests_total", response.text)

    def test_metrics_endpoint_not_self_counted(self) -> None:
        self.client.get("/metrics")
        self.assertIsNone(
            REGISTRY.get_sample_value(
                "aiops_bff_requests_total",
                {"method": "GET", "path": "/metrics", "status": "200"},
            )
        )

    def test_request_middleware_counts_api_calls(self) -> None:
        labels = {"method": "GET", "path": "/api/health", "status": "200"}
        before = _value("aiops_bff_requests_total", labels)
        self.assertEqual(self.client.get("/api/health", headers=self.admin).status_code, 200)
        self.assertEqual(_value("aiops_bff_requests_total", labels) - before, 1.0)

    def test_route_template_label_avoids_high_cardinality(self) -> None:
        async def fake_detail(wf_id: str) -> dict:
            return {"wf_id": wf_id, "exec_status": "RUNNING", "status": None, "result": None}

        with mock.patch.object(
            self.app_module.gw, "flow_detail", new=AsyncMock(side_effect=fake_detail)
        ):
            response = self.client.get("/api/flows/wf-cardinality-probe", headers=self.admin)
        self.assertEqual(response.status_code, 200)

        template = {"method": "GET", "path": "/api/flows/{wf_id}", "status": "200"}
        concrete = {"method": "GET", "path": "/api/flows/wf-cardinality-probe", "status": "200"}
        self.assertGreaterEqual(_value("aiops_bff_requests_total", template), 1.0)
        self.assertIsNone(REGISTRY.get_sample_value("aiops_bff_requests_total", concrete))

    def test_auth_rejection_still_counted(self) -> None:
        # 鉴权早退发生在路由匹配前：path 标签可能为路由模板或 unmatched，合计断言增量
        def total_401() -> float:
            return sum(
                _value(
                    "aiops_bff_requests_total",
                    {"method": "GET", "path": path, "status": "401"},
                )
                for path in ("/api/health", "unmatched")
            )

        before = total_401()
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        self.assertEqual(total_401() - before, 1.0)


class RunsStoreTerminalMetricTest(unittest.TestCase):
    """runs_store 终态指标（production + SQLite）：仅终态首次出现计 workflow_completed。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self._tmp.name) / 'metrics.db'}"
        self._env = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": self.url}
        )
        self._env.start()
        db.reset_engine()
        runs_store._clear_cache()

    def tearDown(self) -> None:
        runs_store._clear_cache()
        db.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def _item(self, **overrides) -> dict:
        base = {
            "wf_id": "aiops-fix-order-metric-1",
            "run_id": "run-metric-1",
            "stage": "DONE",
            "exec_status": "COMPLETED",
            "close_time": "2026-10-03T10:05:00+00:00",
            "confidence": 0.9,
            "patch_id": "p-metric-1",
            "alert": {"alert_id": "a-1", "service": "order-metric"},
            "start_time": "2026-10-03T10:00:00+00:00",
            "duration_seconds": 300.0,
        }
        base.update(overrides)
        return base

    def test_terminal_stage_counted_once(self) -> None:
        labels = {"service": "order-metric", "stage": "DONE", "mode": "production"}
        before = _value("aiops_workflow_completed_total", labels)
        self.assertEqual(runs_store.sync_runs([self._item()]), 1)
        self.assertEqual(_value("aiops_workflow_completed_total", labels) - before, 1.0)
        # 签名未变 → 不写库、不重复计数
        self.assertEqual(runs_store.sync_runs([self._item()]), 0)
        self.assertEqual(_value("aiops_workflow_completed_total", labels) - before, 1.0)

    def test_non_terminal_stage_not_counted(self) -> None:
        labels = {"service": "order-metric", "stage": "WAIT_APPROVAL", "mode": "production"}
        before = _value("aiops_workflow_completed_total", labels)
        self.assertEqual(
            runs_store.sync_runs(
                [
                    self._item(
                        stage="WAIT_APPROVAL",
                        exec_status="RUNNING",
                        close_time=None,
                        duration_seconds=None,
                    )
                ]
            ),
            1,
        )
        self.assertEqual(_value("aiops_workflow_completed_total", labels) - before, 0.0)

    def test_stage_change_to_another_terminal_counted(self) -> None:
        runs_store.sync_runs([self._item()])
        labels = {"service": "order-metric", "stage": "FAILED", "mode": "production"}
        before = _value("aiops_workflow_completed_total", labels)
        self.assertEqual(
            runs_store.sync_runs([self._item(stage="FAILED", exec_status="FAILED")]), 1
        )
        self.assertEqual(_value("aiops_workflow_completed_total", labels) - before, 1.0)


class TracingTest(unittest.TestCase):
    """OTel 追踪：未配置 no-op；初始化失败降级（绝不阻塞服务）。"""

    def tearDown(self) -> None:
        tracing._initialized = False  # 恢复模块级全局（防跨测试污染）

    def test_disabled_without_endpoint(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_OTEL_ENDPOINT": ""}):
            self.assertFalse(tracing.enabled())
            self.assertFalse(tracing.setup_tracing("aiops-bff"))
            self.assertEqual(tracing.temporal_interceptors(), [])

    def test_enabled_flag_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_OTEL_ENDPOINT": "http://localhost:4318"}):
            self.assertTrue(tracing.enabled())

    def test_setup_failure_degrades(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_OTEL_ENDPOINT": "http://localhost:4318"}):
            with mock.patch.object(
                tracing, "_build_exporter", side_effect=RuntimeError("boom")
            ):
                self.assertFalse(tracing.setup_tracing("aiops-bff"))
            self.assertFalse(tracing._initialized)


class WorkerMetricsServerTest(unittest.TestCase):
    def test_port_zero_disables(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_METRICS_PORT": "0"}):
            self.assertEqual(metrics.start_worker_server(), 0)

    def test_port_conflict_degrades(self) -> None:
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 0))
            port = sock.getsockname()[1]
            with mock.patch.dict(os.environ, {"AIOPS_METRICS_PORT": str(port)}):
                self.assertEqual(metrics.start_worker_server(), 0)

    def test_disabled_when_client_missing(self) -> None:
        with mock.patch.object(metrics, "_AVAILABLE", False):
            self.assertEqual(metrics.start_worker_server(), 0)

    def test_success_returns_port(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_METRICS_PORT": "19399"}):
            with mock.patch("prometheus_client.start_http_server") as fake_server:
                self.assertEqual(metrics.start_worker_server(), 19399)
        fake_server.assert_called_once_with(19399)


if __name__ == "__main__":
    unittest.main()
