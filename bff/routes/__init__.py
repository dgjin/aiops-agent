"""BFF 路由汇总：按域挂载（顺序即注册顺序；SPA catch-all 由 app.py 最后注册）。"""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    approvals,
    audit,
    chatops,
    escalations,
    flows,
    health,
    im_callback,
    monitored_apps,
    ops_metrics,
    requirements,
    system,
    systems,
    tokens,
    users,
)


def include_routers(app: FastAPI) -> None:
    for module in (
        health,
        flows,
        approvals,
        chatops,
        im_callback,
        escalations,
        ops_metrics,
        audit,
        tokens,
        system,
        systems,
        monitored_apps,
        requirements,
        users,
    ):
        app.include_router(module.router)
