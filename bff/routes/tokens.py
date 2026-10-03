"""访问令牌域：/api/auth/status、/api/auth/tokens、/api/auth/rotate。

自动轮换由 app.py lifespan 中的后台任务执行；令牌值默认不回显（写入权限 600 的注册表文件，
仅 ``AIOPS_TOKEN_ALLOW_REVEAL=true`` 时经接口回显）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import audit
from .. import auth as auth_store
from ..deps import ApiError, ok

router = APIRouter()


@router.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict:
    """轮换状态 + 调用方自身令牌的有效期（前端据此提示「我的令牌即将失效」）。

    令牌**清单**不在本接口返回（仅管理员经 /api/auth/tokens 可见）。
    """
    try:
        status = auth_store.rotation_status()
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    record = getattr(request.state, "token_record", None) or {}
    status.pop("tokens", None)
    status["self_token"] = {
        "user": record.get("user"),
        "role": record.get("role"),
        "state": record.get("state"),
        "expires_at": record.get("expires_at"),
        "retired_at": record.get("retired_at"),
    }
    return ok(status)


@router.get("/api/auth/tokens")
async def api_auth_tokens() -> dict:
    """令牌清单（仅元数据，**不含令牌值**；管理员）。"""
    try:
        return ok(auth_store.rotation_status())
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc


class RotateBody(BaseModel):
    reason: str = Field(default="manual", max_length=64)


@router.post("/api/auth/rotate")
async def api_auth_rotate(request: Request, body: RotateBody | None = None) -> dict:
    """立即轮换令牌（管理员）。

    轮换后**旧令牌仍在宽限期内有效**，因此当前会话不会被立刻锁出；
    令牌值默认不回显（写在权限 600 的注册表文件里）。
    """
    try:
        result = auth_store.rotate(reason=(body.reason if body else "manual"))
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc

    audit.write_audit(
        actor=request.state.identity.user,
        action="auth:rotate",
        target=",".join(f"{item['user']}/{item['role']}" for item in result["identities"]),
        params={"reason": result["reason"], "grace_seconds": result["grace_seconds"]},
    )

    payload = {
        "rotated_at": result["rotated_at"],
        "reason": result["reason"],
        "identities": result["identities"],
        "grace_seconds": result["grace_seconds"],
        "next_rotation_at": result["next_rotation_at"],
        "reveal_enabled": auth_store.allow_reveal(),
        "registry_path": str(auth_store.registry_path()),
    }
    if auth_store.allow_reveal():
        payload["new_tokens"] = result["new_tokens"]
    else:
        payload["hint"] = (
            f"新令牌已写入 {auth_store.registry_path()}（权限 600），请从文件读取；"
            "如需经接口回显，设置 AIOPS_TOKEN_ALLOW_REVEAL=true"
        )
    return ok(payload)
