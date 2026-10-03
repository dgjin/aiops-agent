"""审计域：/api/audit（终态流程审计 + 操作留痕）、/api/audit/summary（报表聚合 P3-07）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from .. import audit
from ..deps import close_dt, gw, ok

router = APIRouter()


@router.get("/api/audit")
async def api_audit(
    q: str = "",
    stage: str = "",
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    items = await gw.list_flows(limit=200)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    needle = q.strip().lower()
    rows = []
    for item in items:
        if item["exec_status"] == "RUNNING":
            continue
        closed_at = close_dt(item)
        if closed_at is None or closed_at < cutoff:
            continue
        if stage and item["stage"] != stage:
            continue
        if needle:
            alert = item["alert"] or {}
            haystack = " ".join(
                str(v) for v in [item["wf_id"], alert.get("alert_id"), alert.get("service"), item.get("patch_id")]
            ).lower()
            if needle not in haystack:
                continue
        rows.append(item)
    rows.sort(key=lambda i: i.get("close_time") or "", reverse=True)
    return ok({"items": rows[:limit], "ops": audit.read_audit(limit=100)})


@router.get("/api/audit/summary")
async def api_audit_summary(
    days: int = Query(30, ge=1, le=365),
    top: int = Query(10, ge=1, le=50),
) -> dict:
    """操作审计报表聚合（P3-07）：按操作者 / 动作 / 日期 / 结果聚合计数。

    控制台「操作审计」页据此渲染汇总条与日趋势；production 全量 SQL 聚合、
    demo 采样最近 5000 条（truncated 标记）。
    """
    summary = await asyncio.to_thread(audit.aggregate_audit, days=days, top=top)
    return ok(summary)
