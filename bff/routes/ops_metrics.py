"""运营度量域：/api/metrics/ops（P1-4 面板数据源）。

读侧聚合，无写路径；数据源任一分量不可读时降级（该分量样本清零并置
``degraded`` 标记）——派生统计失败不应让面板整体 500。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from .. import audit, escalations, ops_metrics
from ..deps import gw, ok

router = APIRouter()


@router.get("/api/metrics/ops")
async def api_ops_metrics(days: int = Query(7, ge=1, le=30)) -> dict:
    """近 N 天运营度量：MTTR / 自动修复率 / 人工干预率 / 闸门拦截率。"""
    degraded: list[str] = []

    try:
        items = await gw.list_flows(limit=500)
    except Exception:  # noqa: BLE001 - Temporal 不可达：空窗口展示
        items = []
        degraded.append("temporal")

    try:
        audit_rows = await asyncio.to_thread(audit.read_audit, 2000)
    except Exception:  # noqa: BLE001 - 审计不可读：人工干预率退化为 0
        audit_rows = []
        degraded.append("audit")

    try:
        esc_stats = await asyncio.to_thread(escalations.stats)
    except Exception:  # noqa: BLE001 - 待办存储不可读：仅隐藏转人工现状
        esc_stats = None
        degraded.append("escalations")

    data = ops_metrics.compute(items, audit_rows, esc_stats, days=days)
    data["degraded"] = degraded
    return ok(data)
