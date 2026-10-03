"""健康与自省：/metrics（Prometheus 抓取）、/api/health、/api/subsystems。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response

from aiops_agent import metrics

from .. import aggregator
from ..deps import gw, ok

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Prometheus 抓取端点（文本格式）。

    不含鉴权（Prometheus 抓取约定）；生产以 NetworkPolicy / Ingress 白名单限制访问。
    """
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@router.get("/api/health")
async def api_health() -> dict:
    connected, latency_ms, error = await gw.ping()
    return ok(
        {
            "ok": True,
            "temporal": {"connected": connected, "latency_ms": round(latency_ms, 1), "error": error},
        }
    )


@router.get("/api/subsystems")
async def api_subsystems() -> dict:
    """修复链路依赖自检（轻量接口，供全局「降级模式」横幅轮询）。

    任一依赖不可用都意味着修复链路会**静默降级**，因此在控制台显性提示（评估报告 C4）。
    """
    connected, latency_ms, error = await gw.ping()
    deps = await asyncio.to_thread(aggregator.probe_dependencies)
    subsystems = {
        "temporal": {
            "ok": connected,
            "detail": f"{round(latency_ms, 1)}ms" if connected else (error or "未连接"),
        },
        **deps,
    }
    degraded = sorted(name for name, info in subsystems.items() if not info["ok"])
    return ok({"subsystems": subsystems, "degraded": degraded, "degraded_mode": bool(degraded)})
