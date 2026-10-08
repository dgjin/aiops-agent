"""BFF 中间件：缓存策略 / 集中式鉴权（会话+静态令牌 / 限速 / kill switch）/ 安全头 / 指标。

``register_middlewares`` 的注册顺序即洋葱顺序（Starlette：后注册者更外层）：
    metrics → security → auth → cache → 路由
含义：鉴权早退（401/403/429/503）响应同样经过外层安全头与指标采集。
"""

from __future__ import annotations

import hashlib
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aiops_agent import kill_switch, metrics

from . import ratelimit
from . import auth as auth_store
from .deps import ok

# 写接口所需的最低角色（读接口默认 viewer）。未列出的写接口按最高要求（安全默认）。
_WRITE_ROLE_RULES: list[tuple[str, str]] = [
    ("/api/monitored-apps", "admin"),   # 配置维护
    ("/api/requirements/analyses", "admin"),  # 需求智能分析：发起 / 反馈 / 批准（批准即启动修复工作流）
    ("/api/flows/", "operator"),        # 审批 / 发布指令 / 排队补丁
    ("/api/escalations", "operator"),   # 转人工待办：指派 / 关闭 / 重试修复
    ("/api/users", "admin"),            # 用户管理（CRUD / 重置密码 / 强制下线）
    ("/api/auth/logout", "viewer"),     # 登出：任何已认证角色
    ("/api/auth/password", "viewer"),   # 本人改密：任何已认证角色（需原密码）
]

# 读接口里需要更高角色的少数路径（令牌清单 / 用户清单属敏感信息）
_READ_ROLE_OVERRIDES: list[tuple[str, str]] = [
    ("/api/auth/tokens", "admin"),
    ("/api/users", "admin"),
]

# 认证前的唯一合法入口（登录换取会话令牌；失败限速在路由内，防口令爆破）
_PUBLIC_ENDPOINTS = {"/api/auth/login"}

# kill switch 激活时仍可调用的写端点（否则激活后无法关闭/登出；角色校验仍生效）
_KILL_SWITCH_EXEMPT = {
    "/api/system/kill-switch",
    "/api/auth/logout",
    "/api/auth/password",
}


def _client_ip(request: Request) -> str:
    """客户端 IP：优先 X-Forwarded-For 首段（Ingress 场景），否则直连地址。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _rate_limited(retry_after: float) -> JSONResponse:
    """429 响应：统一 JSON 信封 + Retry-After（前端可提示等待秒数）。"""
    response = JSONResponse(status_code=429, content=ok({"error": "请求过于频繁，请稍后再试"}))
    response.headers["Retry-After"] = str(max(1, int(retry_after + 0.5)))
    return response


def required_role(method: str, path: str) -> str:
    """返回访问该接口所需的最低角色（纯函数，便于单测覆盖）。"""
    if method.upper() == "GET":
        for prefix, role in _READ_ROLE_OVERRIDES:
            if path.startswith(prefix):
                return role
        return "viewer"
    for prefix, role in _WRITE_ROLE_RULES:
        if path.startswith(prefix):
            return role
    return "admin"  # 未知写接口：安全默认（宁可要求更高）


async def _cache_policy(request: Request, call_next):
    """静态资源缓存策略：**带哈希的产物长期缓存、SPA 入口不缓存、/api 不缓存**。

    背景：此前 index.html 无 Cache-Control，浏览器按启发式缓存，重新构建后仍跑旧包，
    表现为「页面/面板行为与新代码不一致」，只能硬刷新——排查成本很高。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif not path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _auth_guard(request: Request, call_next):
    """集中式鉴权：/api 全部需 Bearer 凭证（登录端点除外）。

    校验链（**会话优先，静态回落**）：
        1. 会话令牌（用户名密码登录签发；身份以用户表为准，可即时吊销/禁用）；
        2. 未命中回落静态令牌（自动化/应急通道；注册表缺失不阻断会话通道）。
    双通道均不可用（注册表/用户表损坏）时 fail-closed：503。
    """
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)  # 静态资源与 SPA fallback 不鉴权
    if path in _PUBLIC_ENDPOINTS:
        return await call_next(request)  # 登录：认证前唯一合法入口

    # 静态令牌通道可用性（不可用不立即 503：会话通道可能仍工作，反之亦然）
    try:
        tokens: dict | None = auth_store.registry_tokens()
        registry_error: str | None = None
    except auth_store.AuthConfigError as exc:
        tokens, registry_error = None, str(exc)

    # 通道 1：会话令牌
    try:
        resolved = auth_store.resolve_session(request.headers.get("authorization"))
        session_error: str | None = None
    except auth_store.AuthConfigError as exc:
        resolved, session_error = None, str(exc)

    if resolved is not None:
        identity, token_record = resolved
    else:
        if tokens is None and session_error is not None:
            # 双通道均不可用：fail-closed（宁可拦住，也不在不可判定时放行）
            return JSONResponse(
                status_code=503, content=ok({"error": session_error or registry_error})
            )
        # 通道 2：静态令牌（注册表不可用时按空表校验 → 统一 401）
        try:
            identity, token_record = auth_store.resolve_token(
                request.headers.get("authorization"), tokens=tokens or {}
            )
        except HTTPException as exc:  # noqa: PERF203 - 统一转 JSON 信封
            # 速率限制：带凭证但认证失败 → 按 IP 计数（防令牌/口令暴力破解）；
            # 无凭证的 401 不计数（前端首次打开页面的正常引导路径）
            if (
                exc.status_code == 401
                and request.headers.get("authorization")
                and ratelimit.enabled()
            ):
                allowed, retry_after = ratelimit.hit(
                    f"authfail:{_client_ip(request)}", ratelimit.auth_fail_limit()
                )
                if not allowed:
                    return _rate_limited(retry_after)
            return JSONResponse(
                status_code=exc.status_code,
                content=ok({"error": exc.detail}),
                headers=exc.headers or None,
            )
        token_record = {**(token_record or {}), "source": "static"}

    # 速率限制：已认证请求按 token 指纹（读 / 写两套配额；写更严）
    if ratelimit.enabled():
        kind = (
            "write" if request.method.upper() in ("POST", "PUT", "DELETE", "PATCH") else "read"
        )
        limit = ratelimit.write_limit() if kind == "write" else ratelimit.read_limit()
        fingerprint = hashlib.sha256(
            (request.headers.get("authorization") or "").encode("utf-8")
        ).hexdigest()[:24]
        allowed, retry_after = ratelimit.hit(f"tok:{fingerprint}:{kind}", limit)
        if not allowed:
            return _rate_limited(retry_after)

    need = required_role(request.method, path)
    if identity.level < auth_store.ROLE_ORDER[need]:
        return JSONResponse(
            status_code=403,
            content=ok({"error": f"角色 {identity.role} 无权执行该操作（要求 {need}）"}),
        )

    request.state.identity = identity  # 供各写接口取真实操作者（审计 actor）
    request.state.token_record = token_record  # 供 /api/auth/status 告知调用方自身令牌有效期

    # kill switch：激活后拒绝一切写操作（管理端点自身除外，否则激活后无法关闭）；
    # 状态不可读 fail-closed（503），宁可拦住也不放行
    if request.method.upper() in ("POST", "PUT", "DELETE", "PATCH") and path not in _KILL_SWITCH_EXEMPT:
        try:
            state = kill_switch.get_state()
        except kill_switch.KillSwitchError as exc:
            return JSONResponse(status_code=503, content=ok({"error": str(exc)}))
        if state.get("active"):
            return JSONResponse(
                status_code=503,
                content=ok(
                    {
                        "error": "kill switch 已激活，写操作被拒绝"
                        f"（原因：{state.get('reason') or '未注明'}；操作者：{state.get('actor') or '未知'}）",
                        "kill_switch": state,
                    }
                ),
            )

    return await call_next(request)


# 安全响应头（生产安全基线，P2-05）：
# - CSP：脚本仅同源（构建产物无 inline script）；style 放行 inline（入口 index.html 自带
#   一行内联 <style> 背景色）；img 放行 data:（favicon 为内联 SVG）；
# - HSTS 仅在 HTTPS（或经 TLS 终结代理识别到 X-Forwarded-Proto: https）时下发，
#   否则 HTTP 明文部署会误导浏览器强制升级；
# - CORS 不启用：控制台与 BFF 同域部署（静态托管），无需跨域访问。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    ),
}


async def _security_headers(request: Request, call_next):
    """安全头（最外层中间件：鉴权 401/限速 429 等早退响应同样携带）。"""
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


async def _metrics_middleware(request: Request, call_next):
    """Prometheus 请求指标（优化方案 3.4，GAP-07）。

    仅采集 /api（静态资源与控制台轮询无关）；``path`` 标签取**路由模板**
    （``/api/flows/{wf_id}``），避免 wf_id 造成高基数。
    """
    if not request.url.path.startswith("/api"):
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        route = request.scope.get("route")
        metrics.observe_bff_request(
            request.method,
            getattr(route, "path", "unmatched"),
            500,
            time.perf_counter() - started,
        )
        raise
    route = request.scope.get("route")
    metrics.observe_bff_request(
        request.method,
        getattr(route, "path", "unmatched"),
        response.status_code,
        time.perf_counter() - started,
    )
    return response


def register_middlewares(app) -> None:  # noqa: ANN001 - FastAPI 实例（避免循环导入不引类型）
    """注册全部 HTTP 中间件（顺序即洋葱顺序：后者更外层）。"""
    app.middleware("http")(_cache_policy)
    app.middleware("http")(_auth_guard)
    app.middleware("http")(_security_headers)
    app.middleware("http")(_metrics_middleware)
