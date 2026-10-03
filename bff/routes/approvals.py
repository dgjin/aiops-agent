"""审批与发布窗口：/api/approvals、/api/window。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from aiops_agent.config import load_policy

from .. import aggregator
from ..deps import gw, ok

router = APIRouter()


@router.get("/api/approvals")
async def api_approvals() -> dict:
    items = await gw.list_flows(limit=200)
    pending = []
    for item in items:
        if item["exec_status"] != "RUNNING" or item["stage"] != "WAIT_APPROVAL":
            continue
        if item["approval"] is None:
            pending.append({**item, "pending": "approval"})
        elif item["needs_second"] and item["approval"] == "approve" and item["second_approval"] is None:
            pending.append({**item, "pending": "second_approval"})
    pending.sort(key=lambda i: (i.get("deadline") or {}).get("at") or "9999")
    return ok({"items": pending})


@router.get("/api/window")
async def api_window() -> dict:
    items = await gw.list_flows(limit=200)
    notifying = [i for i in items if i["exec_status"] == "RUNNING" and i["stage"] == "NOTIFYING"]
    notifying.sort(key=lambda i: (i.get("deadline") or {}).get("at") or "9999")
    versions = await asyncio.gather(
        *(asyncio.to_thread(aggregator.broadcast_version, i.get("patch_id")) for i in notifying)
    )
    for item, version in zip(notifying, versions):
        item["version"] = version
    # 公告窗口总长度（策略快照）：供前端画倒计时环形进度（无此值只能显示剩余秒数）
    try:
        countdown_seconds = int(
            load_policy().release_gate.notify_window.countdown.total_seconds()
        )
    except Exception:  # noqa: BLE001 - 策略不可读时降级为不画进度环
        countdown_seconds = None
    return ok({"items": notifying, "countdown_seconds": countdown_seconds})
