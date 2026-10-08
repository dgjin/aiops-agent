"""需求基线域：/api/requirements（被监控系统「需求收集与反馈」→ AIOps 主动分析）。

用户在**被监控系统**提交的需求 / 建议 / 缺陷，经其管理员评估、纳入「需求基线」后，
由标准导出接口 ``GET /api/requirements/export`` 开放给 AIOps（契约 v1.0，客户端实现见
:mod:`aiops_agent.requirements_client`）。本域把该接口包装成控制台读接口：

- 数据源（被监控应用）沿用「被监控应用」清单：``app_id`` 显式指定优先，缺省取第一个
  启用项；响应同时回带清单摘要（``apps``），供前端下拉切换；
- 令牌取环境变量 ``NL2SQL_OPS_TOKEN``（值 = 被监控系统的 ``OPS_API_TOKEN``）；
  未配置时**不发请求**、直接返回中文配置指引；
- 拉取失败是常态（未部署 / 未配置令牌 / 网络不可达），一律 ``ok=False + error``
  透传（HTTP 仍为 200），由前端展示原因。
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Query

from aiops_agent import requirements_client

from .. import monitored_apps as store
from ..deps import ApiError, ok

router = APIRouter()

# 回带前端的应用清单字段（其余字段属维护面，不需要）
_APP_FIELDS = ("id", "name", "url", "service", "enabled")


def _app_summary(app: dict) -> dict:
    return {key: app.get(key) for key in _APP_FIELDS}


def _empty(error: str, *, apps: list[dict], app: dict | None) -> dict:
    """失败/空态响应骨架：ok=False + 中文原因（前端直接展示）。"""
    return ok(
        {
            "ok": False,
            "error": error,
            "warnings": [],
            "apps": apps,
            "app": app,
            "url": "",
            "service": "",
            "system": "",
            "exported_at": "",
            "filter": {},
            "returned": 0,
            "entries": [],
        }
    )


@router.get("/api/requirements")
async def api_requirements(
    app_id: str = Query("", max_length=64, description="被监控应用 ID；缺省取清单中第一个启用项"),
    status: str = Query("BASELINED", pattern="^(BASELINED|PENDING|REJECTED|ALL)$"),
    kind: str = Query("", max_length=32),
    since: str = Query("", max_length=40, description="增量游标（上次导出的 exportedAt，>= 语义）"),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    """拉取需求基线条目（读接口：viewer 起可访问）。"""
    apps = [_app_summary(app) for app in store.list_all()]
    if app_id:
        target = next((app for app in apps if app.get("id") == app_id), None)
        if target is None:
            raise ApiError(404, f"未找到被监控应用：{app_id}")
    else:
        target = next((app for app in apps if app.get("enabled")), apps[0] if apps else None)
    if target is None:
        return _empty(
            "被监控应用清单为空：请先在「被监控应用」页添加应用（该应用需部署需求收集与反馈能力）",
            apps=[],
            app=None,
        )

    token = (os.environ.get(requirements_client.TOKEN_ENV) or "").strip()
    if not token:
        return _empty(
            f"未配置令牌：请设置环境变量 {requirements_client.TOKEN_ENV}"
            "（值 = 被监控系统的 OPS_API_TOKEN）并重启 BFF",
            apps=apps,
            app=target,
        )

    result = await asyncio.to_thread(
        requirements_client.fetch_requirements,
        str(target.get("url") or ""),
        status=status,
        kind=kind or None,
        since=since or None,
        limit=limit,
        token=token,
    )
    return ok(
        {
            "ok": result["ok"],
            "error": result["error"],
            "warnings": result["warnings"],
            "apps": apps,
            "app": target,
            "url": result["url"],
            "service": result["service"],
            "system": result["system"],
            "exported_at": result["exported_at"],
            "filter": result["filter"],
            "returned": result["returned"],
            "entries": result["entries"],
        }
    )
