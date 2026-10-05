"""用户与会话域：登录 / 登出 / 本人改密 / 用户管理（管理员）/ 会话查看与吊销。

- 认证方式：用户名+密码换取**会话令牌**（``Authorization: Bearer <token>``），
  与静态令牌**并存**（校验链见 middleware：会话优先、静态回落）；
- 公开端点：``POST /api/auth/login``（失败按 IP 限速，防口令爆破；成功不计入配额）；
- 角色规则（middleware 集中定义）：``/api/users`` 全部需要 admin；
  ``logout`` 与本人改密任何已认证角色可用；
- 自我保护：不能删除/降级/禁用自己；不能移除最后一个启用管理员（auth 层 409）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import audit
from .. import auth as auth_store
from .. import ratelimit
from ..deps import ApiError, ok
from ..middleware import _client_ip, _rate_limited

router = APIRouter()


# ----------------------------------------------------------------------
# 登录 / 登出 / 本人改密（认证域）
# ----------------------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/api/auth/login")
async def api_auth_login(request: Request, body: LoginBody) -> dict:
    """用户名+密码登录 → 会话令牌（滑动续期，默认 12 小时）。

    限速：先 peek 检查（只读不计数），验证失败才 hit 计数——成功尝试不受配额影响。
    """
    key = f"login:{_client_ip(request)}"
    if ratelimit.enabled():
        allowed, retry_after = ratelimit.peek(key, ratelimit.auth_fail_limit())
        if not allowed:
            return _rate_limited(retry_after)
    username = body.username.strip()
    try:
        session = auth_store.login(username, body.password)
    except auth_store.AuthError as exc:
        if ratelimit.enabled():
            ratelimit.hit(key, ratelimit.auth_fail_limit())
        audit.write_audit(
            actor=username or "unknown",
            action="auth:login",
            target="denied",
            params={"reason": exc.message},
            result="denied",
        )
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=session["user"],
        action="auth:login",
        target="success",
        params={"role": session["role"], "expires_at": session["expires_at"]},
    )
    return ok(
        {
            "token": session["token"],
            "user": session["user"],
            "role": session["role"],
            "expires_at": session["expires_at"],
            "ttl_seconds": session["ttl_seconds"],
        }
    )


@router.post("/api/auth/logout")
async def api_auth_logout(request: Request) -> dict:
    """登出：吊销当前会话令牌（静态令牌无会话可吊销，返回提示）。"""
    identity = request.state.identity
    try:
        revoked = auth_store.revoke_session(request.headers.get("authorization"))
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=identity.user,
        action="auth:logout",
        target="session" if revoked else "static-token",
        params={},
    )
    payload: dict = {"revoked": revoked}
    if not revoked:
        payload["hint"] = "当前为静态令牌（长期有效）；如需使其失效，请管理员执行令牌轮换"
    return ok(payload)


class PasswordChangeBody(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


@router.post("/api/auth/password")
async def api_auth_password(request: Request, body: PasswordChangeBody) -> dict:
    """本人修改密码（需原密码）。

    成功后吊销该用户**全部会话**（其他设备强制下线）；若当前凭证为会话
    （``relogin_required=true``），前端应引导重新登录。
    """
    identity = request.state.identity
    try:
        auth_store.change_own_password(identity.user, body.old_password, body.new_password)
        revoked = auth_store.revoke_user_sessions(identity.user)
    except auth_store.AuthError as exc:
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    record = getattr(request.state, "token_record", None) or {}
    relogin = record.get("source") == "session"
    audit.write_audit(
        actor=identity.user,
        action="auth:password",
        target="self",
        params={"revoked_sessions": revoked, "relogin_required": relogin},
    )
    return ok({"updated": True, "revoked_sessions": revoked, "relogin_required": relogin})


# ----------------------------------------------------------------------
# 用户管理（管理员；middleware 规则：/api/users 全部需要 admin）
# ----------------------------------------------------------------------


class UserCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(pattern="^(viewer|operator|admin)$")


class UserUpdateBody(BaseModel):
    role: str | None = Field(default=None, pattern="^(viewer|operator|admin)$")
    state: str | None = Field(default=None, pattern="^(active|disabled)$")


class PasswordResetBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


@router.get("/api/users")
async def api_list_users() -> dict:
    """用户清单（管理员；不含密码哈希）。"""
    try:
        return ok({"users": auth_store.list_users()})
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc


@router.post("/api/users")
async def api_create_user(request: Request, body: UserCreateBody) -> dict:
    """创建用户（管理员）。"""
    try:
        user = auth_store.create_user(body.username.strip(), body.password, body.role)
    except auth_store.AuthError as exc:
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="users:create",
        target=user["username"],
        params={"role": user["role"]},
    )
    return ok({"user": user})


@router.put("/api/users/{username}")
async def api_update_user(request: Request, username: str, body: UserUpdateBody) -> dict:
    """更新用户角色/状态（管理员）。

    自我保护：不能降低或禁用自己的账户（避免把自己锁在门外）；
    store 层规则：不能移除最后一个启用管理员（409）。
    """
    actor = request.state.identity.user
    if username == actor:
        if body.role is not None and body.role != "admin":
            raise ApiError(409, "不能降低自己的角色，请让其他管理员操作")
        if body.state == "disabled":
            raise ApiError(409, "不能禁用自己的账户")
    try:
        user = auth_store.update_user(username, role=body.role, state=body.state)
    except auth_store.AuthError as exc:
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=actor,
        action="users:update",
        target=username,
        params={"role": body.role, "state": body.state},
    )
    return ok({"user": user})


@router.delete("/api/users/{username}")
async def api_delete_user(request: Request, username: str) -> dict:
    """删除用户（管理员；不能删除自己）；同时吊销其全部会话。"""
    actor = request.state.identity.user
    if username == actor:
        raise ApiError(409, "不能删除自己的账户")
    try:
        auth_store.delete_user(username)
        revoked = auth_store.revoke_user_sessions(username)
    except auth_store.AuthError as exc:
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=actor,
        action="users:delete",
        target=username,
        params={"revoked_sessions": revoked},
    )
    return ok({"deleted": True, "revoked_sessions": revoked})


@router.post("/api/users/{username}/password")
async def api_reset_password(
    request: Request, username: str, body: PasswordResetBody
) -> dict:
    """重置用户密码（管理员；无需原密码）；成功后吊销其全部会话（强制下线）。"""
    try:
        auth_store.set_password(username, body.password)
        revoked = auth_store.revoke_user_sessions(username)
    except auth_store.AuthError as exc:
        raise ApiError(exc.status_code, exc.message) from exc
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="users:password-reset",
        target=username,
        params={"revoked_sessions": revoked},
    )
    return ok({"updated": True, "revoked_sessions": revoked})


@router.get("/api/users/{username}/sessions")
async def api_list_sessions(username: str) -> dict:
    """用户在线会话（管理员；仅脱敏指纹，不含令牌值）。"""
    try:
        return ok({"sessions": auth_store.list_sessions(username)})
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc


@router.delete("/api/users/{username}/sessions")
async def api_revoke_sessions(request: Request, username: str) -> dict:
    """强制下线（管理员）：吊销用户全部有效会话。"""
    try:
        revoked = auth_store.revoke_user_sessions(username)
    except auth_store.AuthConfigError as exc:
        raise ApiError(503, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="users:revoke-sessions",
        target=username,
        params={"revoked_sessions": revoked},
    )
    return ok({"revoked_sessions": revoked})
