"""健康与自省：/metrics（Prometheus 抓取）、/api/health、/api/subsystems。"""

from __future__ import annotations

import asyncio
import ipaddress
import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from aiops_agent import metrics, readiness
from .. import commands
from ..deps import gw, ok

router = APIRouter()


def _loopback_only(request: Request) -> None:
    # 不信任转发头；代理请求即使 socket peer 是 loopback 也不得探测内部端点。
    try:
        local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    if not local or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-real-ip")):
        raise HTTPException(404, "Not Found")


def _authenticated(request: Request) -> None:
    # 防止单独挂载 router 时意外绕过集中式鉴权。
    if getattr(request.state, "identity", None) is None:
        raise HTTPException(401, "Authentication required")


def _otel_enabled(report: dict) -> bool:
    """readyz/subsystems 只读取 readiness 已算好的 tracing 结果；缺项按未启用。"""
    tracing = (report.get("checks") or {}).get("tracing") or {}
    return bool(tracing.get("enabled"))


@router.get("/livez", include_in_schema=False)
async def livez(request: Request) -> JSONResponse:
    _loopback_only(request)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    _loopback_only(request)
    report = await readiness.check(gw)
    metrics.set_otel_state(_otel_enabled(report))
    return JSONResponse({"ok": report["ok"]}, status_code=200 if report["ok"] else 503,
                        headers={"Cache-Control": "no-store"})


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    """原始 BFF 指标仅本机可抓取，非 readiness 证据。"""
    _loopback_only(request)
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@router.get("/api/health")
async def api_health(request: Request) -> JSONResponse:
    _authenticated(request)
    report = await readiness.check(gw)
    temporal = report["checks"]["temporal"]
    return JSONResponse(ok({"ok": report["ok"], "temporal": {
        "connected": temporal["ok"], "latency_ms": temporal.get("latency_ms", 0.0),
        "error": None if temporal["ok"] else temporal["detail"],
    }}), status_code=200 if report["ok"] else 503, headers={"Cache-Control": "no-store"})


@router.get("/api/subsystems")
async def api_subsystems(request: Request) -> JSONResponse:
    """认证后的只读详情；LLM/Loki/通知未实测或未接线明确降级。"""
    _authenticated(request)
    report = await readiness.check(gw)
    metrics.set_otel_state(_otel_enabled(report))
    subsystems = {**report["checks"], **report["degradations"]}
    subsystems["commands"] = await _command_subsystem()
    degraded = sorted(name for name, info in subsystems.items() if not info["ok"])
    return JSONResponse(ok({"ok": report["ok"], "subsystems": subsystems, "degraded": degraded,
                            "degraded_mode": bool(degraded), "configuration": report["configuration"]}),
                        status_code=200 if report["ok"] else 503, headers={"Cache-Control": "no-store"})


def _command_backlog_seconds() -> int:
    try:
        return max(30, int(os.environ.get("AIOPS_COMMAND_BACKLOG_ALERT_SECONDS", "300")))
    except ValueError:
        return 300


async def _command_subsystem() -> dict:
    """写命令 outbox 健康：积压过老或近 24h 有失败即降级（不翻转整体 HTTP 状态）。"""
    try:
        values = await asyncio.to_thread(commands.stats)
        metrics.set_command_state(
            pending=values["pending"],
            oldest_seconds=values["oldest_pending_seconds"],
            failed_24h=values["failed_24h"],
        )
    except Exception as exc:  # noqa: BLE001 - 存储故障本身就是降级信号
        return {"ok": False, "detail": f"命令存储不可读：{exc}"}
    threshold = _command_backlog_seconds()
    backlog = values["oldest_pending_seconds"] >= threshold
    detail = (
        f"积压 {values['pending']} 条（最老 {values['oldest_pending_seconds']:.0f}s），"
        f"近 24h 失败 {values['failed_24h']} 条"
    )
    if backlog:
        detail += f"；积压超过阈值 {threshold}s"
    return {
        "ok": not backlog and values["failed_24h"] == 0,
        "detail": detail,
        **values,
    }
