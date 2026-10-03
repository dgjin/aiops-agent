"""BFF 共享基础：网关单例、统一响应与业务错误（供 routes 各域复用）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .temporal_gateway import TemporalGateway

log = logging.getLogger("aiops.bff")

# 共享网关单例：连接预热（lifespan）/ 缓存失效 / 各域路由共用同一实例
gw = TemporalGateway()

# 前端构建产物目录（静态托管，SPA fallback 兜底）
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class ApiError(Exception):
    """业务错误：由异常处理器统一转 JSON（设计方案 6.3）。"""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message


def ok(data: dict) -> dict:
    """统一响应包装：所有响应携带服务端时间（前端倒计时校准）。"""
    return {"server_time": datetime.now(timezone.utc).isoformat(), **data}


def close_dt(item: dict) -> datetime | None:
    """流程项的 close_time ISO → datetime（坏值返回 None；overview / audit 共用）。"""
    try:
        return datetime.fromisoformat(item["close_time"]) if item.get("close_time") else None
    except ValueError:
        return None
