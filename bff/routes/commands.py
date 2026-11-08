"""写命令 outbox 查询：``/api/commands`` 与 ``/api/commands/{command_id}``（M2）。

写链路（审批 / 窗口指令 / 排队）在 ``bff.commands`` 中先落「意图审计 + 命令」，
再尝试投递；本端点让运维可以：

- 看到积压（PENDING）、已送达（SUBMITTED）、已受理（ACCEPTED）与失败（FAILED）；
- 按工作流过滤，核对某次操作是否真的到达了 Temporal；
- 对 SUBMITTED 命令补查一次业务回执（不重发信号）。

读取本身不产生写操作；``refresh_receipt`` 只更新命令状态与结果审计。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from .. import commands
from ..deps import ApiError, ok

router = APIRouter()


def _store_unavailable(exc: Exception) -> ApiError:
    return ApiError(503, f"命令存储不可用：{exc}")


@router.get("/api/commands")
async def api_commands(
    status: str = Query("", pattern="^(|PENDING|SUBMITTED|ACCEPTED|FAILED)$"),
    wf_id: str = "",
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """命令列表（按创建时间倒序）+ 积压统计。"""
    try:
        items = await asyncio.to_thread(
            commands.list_commands,
            status=status or None,
            wf_id=wf_id or None,
            limit=limit,
        )
        stats = await asyncio.to_thread(commands.stats)
    except commands.CommandStoreError as exc:
        raise _store_unavailable(exc) from exc
    return ok({"items": items, "stats": stats})


@router.get("/api/commands/{command_id}")
async def api_command(command_id: str) -> dict:
    """命令详情：SUBMITTED 时补查一次业务回执（accepted / rejected）。"""
    try:
        record = await commands.refresh_receipt(command_id)
    except commands.CommandNotFound as exc:
        raise ApiError(404, f"命令不存在：{command_id}") from exc
    except commands.CommandStoreError as exc:
        raise _store_unavailable(exc) from exc
    return ok({"command": record})
