"""健康与自省：/metrics（Prometheus 抓取）、/api/health、/api/subsystems。"""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from aiops_agent import metrics, readiness
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


@router.get("/livez", include_in_schema=False)
async def livez(request: Request) -> JSONResponse:
    _loopback_only(request)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    _loopback_only(request)
    report = await readiness.check(gw)
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
    subsystems = {**report["checks"], **report["degradations"]}
    degraded = sorted(name for name, info in subsystems.items() if not info["ok"])
    return JSONResponse(ok({"ok": report["ok"], "subsystems": subsystems, "degraded": degraded,
                            "degraded_mode": bool(degraded), "configuration": report["configuration"]}),
                        status_code=200 if report["ok"] else 503, headers={"Cache-Control": "no-store"})
