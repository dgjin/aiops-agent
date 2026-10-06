"""BFF 路由汇总：按域挂载（顺序即注册顺序；SPA catch-all 由 app.py 最后注册）。"""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    approvals,
    audit,
    escalations,
    flows,
    health,
    monitored_apps,
    ops_metrics,
    system,
    tokens,
    users,
)


def include_routers(app: FastAPI) -> None:
    for module in (
        health,
        flows,
        approvals,
        escalations,
        ops_metrics,
        audit,
        tokens,
        system,
        monitored_apps,
        users,
    ):
        app.include_router(module.router)
