"""BFF 中间件：缓存策略 / 集中式鉴权（会话+静态令牌 / 限速 / kill switch）/ 安全头 / 指标。

``register_middlewares`` 的注册顺序即洋葱顺序（Starlette：后注册者更外层）：
    metrics → security → auth → cache → 路由
含义：鉴权早退（401/403/429/503）响应同样经过外层安全头与指标采集。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aiops_agent import kill_switch, metrics, mode as runtime_mode

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
    # IM 通道（ChatOps）：通道身份固定 viewer，命令级权限（发送者角色）在路由内判定
    ("/api/im/command", "viewer"),
]

# 读接口里需要更高角色的少数路径（令牌清单 / 用户清单属敏感信息）
_READ_ROLE_OVERRIDES: list[tuple[str, str]] = [
    ("/api/auth/tokens", "admin"),
    ("/api/users", "admin"),
]

# 认证前的唯一合法入口（登录换取会话令牌；失败限速在路由内，防口令爆破）
_PUBLIC_ENDPOINTS = {"/api/auth/login"}

# 服务令牌旁路（面向被监控系统的只读集成通道）：(精确路径, 环境变量名)。
# 用途：被监控系统（如智能问数）在自己的「需求反馈」界面展示其需求在本系统的
# 处理进度——该通道独立于控制台会话/注册表（不受令牌轮换与注册表故障影响），
# 但仅限 GET 且路径精确匹配；env 未配置＝通道关闭（fail-closed 到常规鉴权）。
_SERVICE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/api/requirements/track", "AIOPS_TRACK_SERVICE_TOKEN"),
)

# 服务身份角色：只读通道，与读接口默认要求一致
_SERVICE_ROLE = "viewer"
_SERVICE_USER = "service:nl2sql"

# IM 通道旁路（ChatOps）：(精确路径, 环境变量名)。IM 网桥（飞书/钉钉机器人出口）
# 把群消息 POST 到 /api/im/command——**专用通道，任何失败都不回落常规鉴权**：
# 未配置（含 production 未接入可信网桥）＝通道关闭（503）；凭证缺失/无效＝认证
# 失败（401）；普通会话/静态/服务令牌一律不能进入该通道。通道凭证也不得与控制台
# 或服务令牌复用（防止宽权限凭证借道 ChatOps 绕过命令级权限）。通道身份固定
# viewer，发送者角色（AIOPS_IM_USERS）与命令级权限在路由内判定。
_IM_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/api/im/command", "AIOPS_IM_TOKEN"),
)
_IM_ROLE = "viewer"
_IM_USER = "im:gateway"


class ImChannelError(RuntimeError):
    """IM 通道关闭 / 认证失败：携带 HTTP 状态码，绝不回落常规鉴权。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message

# kill switch 激活时仍可调用的写端点（否则激活后无法关闭/登出；角色校验仍生效）
# /api/im/command：单端点承载读+写命令（middleware 无法区分），写命令在路由内做
# kill switch 细粒度校验——查询命令保留可用，与控制台 GET 行为对齐。
_KILL_SWITCH_EXEMPT = {
    "/api/system/kill-switch",
    "/api/auth/logout",
    "/api/auth/password",
    "/api/im/command",
}


# 受信代理（默认仅本机回环：TLS 终结代理 Caddy 经 loopback 转发）。只有来自这些
# 直连对端的请求才采信 X-Forwarded-For / X-Forwarded-Proto；可用
# AIOPS_TRUSTED_PROXIES（逗号分隔）显式覆盖。
_TRUSTED_PROXIES = frozenset(
    item.strip()
    for item in (os.environ.get("AIOPS_TRUSTED_PROXIES") or "127.0.0.1,::1").split(",")
    if item.strip()
)


def _trusted_proxy_peer(request: Request) -> bool:
    """直连对端是否为受信本机代理（进程启动时读取一次的成员集）。"""
    return bool(request.client) and request.client.host in _TRUSTED_PROXIES


def _client_ip(request: Request) -> str:
    """客户端 IP：仅受信代理可提供 X-Forwarded-For，且取**末段**（最近一跳写入的真实来源）。

    Caddy 覆盖 XFF 时单值即真实客户端；追加模式时客户端伪造值在前、真实值在末段。
    非受信对端的 XFF 一律忽略，防止伪造头绕过限速与审计归因。
    """
    peer = request.client.host if request.client else "unknown"
    if peer in _TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip() or peer
    return peer


def _service_identity(request: Request) -> "auth_store.Identity | None":
    """服务令牌旁路判定（纯函数）：路径/方法/令牌三重精确命中才返回服务身份。

    - 仅 GET，且路径必须在 ``_SERVICE_ENDPOINTS`` 白名单中；
    - 环境变量未配置、无 Bearer 或令牌不符（常数时间比较）→ None（走常规鉴权）。
    """
    if request.method.upper() != "GET":
        return None
    env_name = next(
        (name for endpoint, name in _SERVICE_ENDPOINTS if endpoint == request.url.path), None
    )
    if env_name is None:
        return None
    expected = (os.environ.get(env_name) or "").strip()
    if not expected:
        return None
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, expected):
        return None
    return auth_store.Identity(user=_SERVICE_USER, role=_SERVICE_ROLE)


def _im_channel(request: Request, tokens: dict | None) -> "auth_store.Identity | None":
    """IM 通道专用认证（fail-closed）：非 IM 路径返回 None；IM 路径失败即抛错。

    - 仅 POST；路径必须精确命中 ``_IM_ENDPOINTS``；
    - production 在未接入可信网桥（平台签名 / 消息时效 / 防重放）前保持关闭；
    - 未配置通道环境变量＝通道关闭（503）；凭证缺失/不符＝认证失败（401）；
    - 通道凭证与控制台令牌（明文或指纹键）或服务令牌复用＝通道关闭（503）；
    - 本函数仅通道级（viewer）；命令级权限由路由按 AIOPS_IM_USERS 判定。
    """
    env_name = next(
        (name for endpoint, name in _IM_ENDPOINTS if endpoint == request.url.path), None
    )
    if env_name is None:
        return None
    if request.method.upper() != "POST":
        raise ImChannelError(405, "IM 通道仅接受 POST")
    if runtime_mode.is_production():
        raise ImChannelError(503, "IM 通道在 production 未接入可信网桥，通道关闭")
    expected = (os.environ.get(env_name) or "").strip()
    if not expected:
        raise ImChannelError(503, f"IM 通道未配置（{env_name}=空），通道关闭")
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        raise ImChannelError(401, "IM 通道认证失败：缺少通道 Bearer 凭证")
    token = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, expected):
        raise ImChannelError(401, "IM 通道认证失败：通道凭证无效")
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if tokens and (token in tokens or fingerprint in tokens):
        raise ImChannelError(503, "IM 通道凭证与控制台令牌复用，通道关闭（请更换专用凭证）")
    service_token = (os.environ.get("AIOPS_TRACK_SERVICE_TOKEN") or "").strip()
    if service_token and hmac.compare_digest(token, service_token):
        raise ImChannelError(503, "IM 通道凭证与服务令牌复用，通道关闭（请更换专用凭证）")
    return auth_store.Identity(user=_IM_USER, role=_IM_ROLE)


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

    # 通道 0a+0b：服务令牌 / IM 通道旁路（只读集成 / ChatOps；命中后仍走限速与角色检查，
    # 独立于注册表状态）
    service_identity = _service_identity(request)
    try:
        im_identity = _im_channel(request, tokens)
    except ImChannelError as exc:
        # IM 专用通道：关闭/认证失败在此终结，普通凭证一律不能回退进入该通道
        return JSONResponse(status_code=exc.status_code, content=ok({"error": exc.message}))
    if im_identity is not None:
        identity, token_record = im_identity, {"source": "im"}
    elif service_identity is not None:
        identity, token_record = service_identity, {"source": "service"}
    elif resolved is not None:
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

# 交互式查看器（archify 自包含产物）路径白名单：脚本全部内联、PNG 导出经 blob: 图片中转、
# 字体为内嵌 base64 data:，帮助中心以同源 iframe 页内内嵌；全局严格的安全头会阻断其
# 交互、渲染与内嵌。仅对这四条精确路径放行：内联脚本 / blob: 图片 / data: 字体 /
# 同源内嵌（frame-ancestors 'self' 且 X-Frame-Options 改 SAMEORIGIN），其余安全基线与
# 普通页面完全一致（查询参数不影响命中）。新增图时需同步追加白名单。
_DIAGRAM_CSP_PATHS = frozenset(
    {
        "/diagrams/aiops-workflow.html",
        "/diagrams/aiops-architecture.html",
        "/diagrams/aiops-dataflow.html",
        "/diagrams/aiops-tech-architecture.html",
    }
)
_DIAGRAM_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
)


async def _security_headers(request: Request, call_next):
    """安全头（最外层中间件：鉴权 401/限速 429 等早退响应同样携带）。"""
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path in _DIAGRAM_CSP_PATHS:
        response.headers["Content-Security-Policy"] = _DIAGRAM_CSP
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip()
    if request.url.scheme == "https" or (
        forwarded_proto == "https" and _trusted_proxy_peer(request)
    ):
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
