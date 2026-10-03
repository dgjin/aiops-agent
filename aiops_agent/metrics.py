"""Prometheus 指标（优化方案 3.4，GAP-07）。

- 指标口径与优化方案对齐：工作流 / 修复 / 沙箱 / 发布 / BFF 请求 / 闸门事件；
- 暴露方式：BFF ``GET /metrics``；worker 独立 HTTP 服务（``start_worker_server``，默认 9090）；
- ``prometheus_client`` 缺失时全部指标降级为 no-op（零依赖兜底）；
- 所有 ``observe_*`` 入口均为纯旁路：不改变业务返回值，异常绝不影响主流程。

环境变量：
    AIOPS_METRICS_PORT   worker 指标服务端口（默认 9090；0 = 关闭）
"""

from __future__ import annotations

import logging
import os
import re

from . import mode

log = logging.getLogger("aiops.metrics")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover - 未安装时全量降级
    _AVAILABLE = False


class _NoopMetric:
    """no-op 指标：接口与 prometheus_client 对齐（labels/inc/observe）。"""

    def labels(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def inc(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass

    def observe(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass


if _AVAILABLE:
    WORKFLOW_STARTED = Counter(
        "aiops_workflow_started_total", "工作流启动数", ["service", "mode"]
    )
    WORKFLOW_COMPLETED = Counter(
        "aiops_workflow_completed_total", "工作流完成数", ["service", "stage", "mode"]
    )
    WORKFLOW_DURATION = Histogram(
        "aiops_workflow_duration_seconds",
        "工作流耗时",
        ["service", "mode"],
        buckets=[10, 30, 60, 120, 300, 600],
    )
    FIX_ATTEMPTS = Counter(
        "aiops_fix_attempts_total", "修复生成尝试数", ["provider", "degraded", "mode"]
    )
    FIX_DURATION = Histogram(
        "aiops_fix_duration_seconds", "修复生成耗时", ["provider", "mode"]
    )
    SANDBOX_RUNS = Counter("aiops_sandbox_runs_total", "沙箱执行数", ["passed", "mode"])
    SANDBOX_DURATION = Histogram("aiops_sandbox_duration_seconds", "沙箱执行耗时", ["mode"])
    CANARY_DEPLOYS = Counter("aiops_canary_deploys_total", "金丝雀发布数", ["healthy", "mode"])
    CANARY_ERROR_RATE = Histogram(
        "aiops_canary_error_rate", "金丝雀观测错误率（%）", ["mode"], buckets=[0.5, 1, 2, 5, 10, 50, 100]
    )
    BFF_REQUESTS = Counter(
        "aiops_bff_requests_total", "BFF 请求数", ["method", "path", "status"]
    )
    BFF_REQUEST_DURATION = Histogram(
        "aiops_bff_request_duration_seconds", "BFF 请求耗时", ["method", "path"]
    )
    GATE_EVENTS = Counter("aiops_gate_events_total", "闸门事件数", ["gate", "decision", "mode"])
else:  # pragma: no cover - 降级分支
    WORKFLOW_STARTED = _NoopMetric()
    WORKFLOW_COMPLETED = _NoopMetric()
    WORKFLOW_DURATION = _NoopMetric()
    FIX_ATTEMPTS = _NoopMetric()
    FIX_DURATION = _NoopMetric()
    SANDBOX_RUNS = _NoopMetric()
    SANDBOX_DURATION = _NoopMetric()
    CANARY_DEPLOYS = _NoopMetric()
    CANARY_ERROR_RATE = _NoopMetric()
    BFF_REQUESTS = _NoopMetric()
    BFF_REQUEST_DURATION = _NoopMetric()
    GATE_EVENTS = _NoopMetric()


def available() -> bool:
    """prometheus_client 是否可用（测试/自检用）。"""
    return _AVAILABLE


def current_mode() -> str:
    try:
        return mode.mode()
    except Exception:  # noqa: BLE001 - 指标打点不得干扰主流程
        return "unknown"


# 标签洗白：外部输入（service 等）可能带高基数/异常字符，统一清洗并截断。
# 保留 / {} 等路由模板字符（Prometheus 标签值无字符集限制，保持 /api/flows/{wf_id} 可读）
_LABEL_UNSAFE = re.compile(r"[^a-zA-Z0-9_.:/{}-]")


def _label(value: str | None, *, fallback: str = "unknown") -> str:
    """标签洗白：外部输入（service 等）可能带高基数/异常字符，统一清洗并截断。"""
    text = str(value or "").strip()
    if not text:
        return fallback
    return _LABEL_UNSAFE.sub("_", text)[:64]


def observe_workflow_started(service: str) -> None:
    WORKFLOW_STARTED.labels(service=_label(service), mode=current_mode()).inc()


def observe_workflow_completed(service: str, stage: str, duration: float | None = None) -> None:
    normalized = _label(stage, fallback="UNKNOWN").upper()
    WORKFLOW_COMPLETED.labels(service=_label(service), stage=normalized, mode=current_mode()).inc()
    if duration is not None and duration >= 0:
        WORKFLOW_DURATION.labels(service=_label(service), mode=current_mode()).observe(duration)


def observe_fix_attempt(provider: str, degraded: bool, duration: float | None = None) -> None:
    provider_label = _label(provider, fallback="unknown")
    FIX_ATTEMPTS.labels(
        provider=provider_label, degraded=str(bool(degraded)).lower(), mode=current_mode()
    ).inc()
    if duration is not None and duration >= 0:
        FIX_DURATION.labels(provider=provider_label, mode=current_mode()).observe(duration)


def observe_sandbox_run(passed: bool, duration: float | None = None) -> None:
    SANDBOX_RUNS.labels(passed=str(bool(passed)).lower(), mode=current_mode()).inc()
    if duration is not None and duration >= 0:
        SANDBOX_DURATION.labels(mode=current_mode()).observe(duration)


def observe_canary_deploy(healthy: bool, error_rate: float | None = None) -> None:
    CANARY_DEPLOYS.labels(healthy=str(bool(healthy)).lower(), mode=current_mode()).inc()
    if error_rate is not None and error_rate >= 0:
        CANARY_ERROR_RATE.labels(mode=current_mode()).observe(error_rate)


def observe_gate_event(gate: str, decision: str) -> None:
    GATE_EVENTS.labels(
        gate=_label(gate), decision=_label(decision), mode=current_mode()
    ).inc()


def observe_bff_request(method: str, path: str, status: int, duration: float) -> None:
    method_label = _label(method, fallback="GET").upper()
    path_label = _label(path, fallback="unmatched")
    BFF_REQUESTS.labels(method=method_label, path=path_label, status=str(int(status))).inc()
    BFF_REQUEST_DURATION.labels(method=method_label, path=path_label).observe(max(0.0, duration))


def render() -> tuple[bytes, str]:
    """渲染指标文本，返回 (payload, content_type)。"""
    if not _AVAILABLE:
        return (
            b"# prometheus_client not installed; metrics disabled\n",
            "text/plain; charset=utf-8",
        )
    return generate_latest(), CONTENT_TYPE_LATEST


def start_worker_server() -> int:
    """worker 进程独立指标服务；返回实际端口（0 = 未启动/失败，不影响 worker 主流程）。"""
    if not _AVAILABLE:
        return 0
    try:
        port = int(os.environ.get("AIOPS_METRICS_PORT", "9090") or 0)
    except ValueError:
        port = 9090
    if port <= 0:
        return 0
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
        log.info("[metrics] worker 指标服务已启动：:%d/metrics", port)
        return port
    except Exception as exc:  # noqa: BLE001 - 端口占用等不阻塞 worker
        log.warning("[metrics] worker 指标服务启动失败（端口 %d）：%s", port, exc)
        return 0
