"""OpenTelemetry 追踪（优化方案 3.4，GAP-08；可选启用，默认 no-op）。

启用条件：配置 ``AIOPS_OTEL_ENDPOINT``（OTLP **HTTP** 端点，如 ``http://localhost:4318``；
依赖清单使用 ``opentelemetry-exporter-otlp-proto-http``，无需 gRPC 依赖）。
未配置时完全 no-op（零依赖路径）；依赖缺失/初始化失败仅告警降级，不阻塞主流程。

- BFF：``setup_tracing("aiops-bff", instrument_fastapi_app=app)`` —— HTTP 请求自动成 span；
- Worker：``temporal_interceptors()`` —— Temporal SDK 的 OTel interceptor，
  workflow/activity 执行自动成 span（导出至 Jaeger / Tempo / OTLP 收集器）。

环境变量：
    AIOPS_OTEL_ENDPOINT   OTLP HTTP 端点（缺省未启用）
    AIOPS_OTEL_REQUIRED   production 门禁：true 时未配置/初始化失败即未就绪
                          （真实 Collector 联通仍是外部验收，本机不伪造通过）
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("aiops.tracing")

_initialized = False
_last_error = ""


def enabled() -> bool:
    """是否配置了 OTLP 端点（未配置则 no-op）。"""
    return bool(os.environ.get("AIOPS_OTEL_ENDPOINT", "").strip())


def required() -> bool:
    """是否要求追踪必须可用（生产门禁开关）。"""
    return os.environ.get("AIOPS_OTEL_REQUIRED", "").strip().lower() in {"1", "true", "yes", "on"}


def state() -> dict:
    """追踪配置/初始化状态（readiness / 指标使用，不返回 endpoint 与异常详情）。"""
    endpoint_configured = enabled()
    if not endpoint_configured:
        detail = "otlp_not_configured"
        ok = not required()
    elif _initialized:
        detail = "otlp_initialized"
        ok = True
    elif _last_error:
        detail = "otlp_init_failed"
        ok = not required()
    else:
        # 已配置但尚未初始化（例如 readiness 先于启动钩子执行）：不算失败
        detail = "otlp_configured_not_initialized"
        ok = True
    return {
        "enabled": endpoint_configured and _initialized,
        "configured": endpoint_configured,
        "required": required(),
        "ok": ok,
        "detail": detail,
    }


def _build_exporter(endpoint: str):
    """构建 OTLP span exporter：HTTP 优先（与依赖清单一致），gRPC 兜底。

    HTTP 版 endpoint 支持基础地址（自动补 ``/v1/traces``）或完整路径。
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint)
    except ImportError:  # pragma: no cover - 装了 grpc 版依赖的部署
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint)


def setup_tracing(service_name: str, *, instrument_fastapi_app=None) -> bool:
    """初始化 OTLP 导出（幂等）；返回是否启用。

    - 未配置 ``AIOPS_OTEL_ENDPOINT``：no-op 返回 False；
    - 依赖缺失/初始化失败：告警 + 返回 False（追踪绝不阻塞服务启动）。
    """
    global _initialized, _last_error
    endpoint = os.environ.get("AIOPS_OTEL_ENDPOINT", "").strip()
    if not endpoint:
        return False
    if _initialized:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(_build_exporter(endpoint)))
        trace.set_tracer_provider(provider)

        if instrument_fastapi_app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(instrument_fastapi_app)

        _initialized = True
        _last_error = ""
        log.info("[tracing] OTel 已启用：service=%s endpoint=%s", service_name, endpoint)
        return True
    except Exception as exc:  # noqa: BLE001 - 追踪初始化失败不阻塞服务
        _last_error = type(exc).__name__
        log.warning("[tracing] OTel 初始化失败（降级为无追踪）：%s", exc)
        return False


def temporal_interceptors() -> list:
    """Temporal worker 的 OTel interceptor 列表（未启用/不可用时返回空列表）。"""
    if not setup_tracing("aiops-worker"):
        return []
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor

        return [TracingInterceptor()]
    except Exception as exc:  # noqa: BLE001 - 版本差异等场景降级
        log.warning("[tracing] Temporal OTel interceptor 不可用：%s", exc)
        return []
