"""速率限制：token / IP 双维度滑动窗口（优化方案 P2-01；production 默认启用）。

设计（按本仓库双模架构落地）：
- **后端**：进程内滑动窗口（默认，零依赖，单副本部署足够）；
  配置 ``AIOPS_REDIS_URL`` 时切换 Redis sorted set——多副本共享配额；
  Redis 故障自动降级内存后端（限速不成为可用性单点）。
- **维度**：
  * 已认证请求按 **token 指纹** 限速（读 / 写两套配额，写更严）；
  * 带凭证但 **认证失败** 的请求按客户端 IP 限速（防令牌暴力破解）；
  * 无凭证请求不计数——由鉴权层直接 401，前端据此引导设置令牌。
- **默认开关**：production 开、demo 关（演示体验优先）；
  ``AIOPS_RATE_LIMIT_ENABLED`` 可显式覆盖。

环境变量：
    AIOPS_RATE_LIMIT_ENABLED             1/0（缺省：production=开、demo=关）
    AIOPS_RATE_LIMIT_READ_PER_MIN        已认证读配额（默认 240；控制台多标签轮询实测所需）
    AIOPS_RATE_LIMIT_WRITE_PER_MIN       已认证写配额（默认 20）
    AIOPS_RATE_LIMIT_AUTH_FAIL_PER_MIN   认证失败按 IP 配额（默认 10）
    AIOPS_REDIS_URL                      配置后使用 Redis 滑动窗口（多副本共享）
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from aiops_agent import mode

# 滑动窗口长度（秒）：所有维度统一按 1 分钟计
WINDOW_SECONDS = 60.0

# 内存后端兜底：窗口 key 数上限（超过后清理过期窗口，防无界增长）
_MAX_KEYS = 4096

_windows: dict[str, deque[float]] = {}
_lock = threading.Lock()

_redis_client = None
_redis_failed = False


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def enabled() -> bool:
    """是否启用限速：显式配置优先，否则 production 默认开、demo 默认关。"""
    raw = os.environ.get("AIOPS_RATE_LIMIT_ENABLED", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return mode.is_production()


def read_limit() -> int:
    return _env_int("AIOPS_RATE_LIMIT_READ_PER_MIN", 240)


def write_limit() -> int:
    return _env_int("AIOPS_RATE_LIMIT_WRITE_PER_MIN", 20)


def auth_fail_limit() -> int:
    return _env_int("AIOPS_RATE_LIMIT_AUTH_FAIL_PER_MIN", 10)


def _hit_memory(key: str, limit: int, window: float) -> tuple[bool, float]:
    """进程内滑动窗口：deque 存时间戳；超限返回建议等待秒数。"""
    now = time.monotonic()
    with _lock:
        if len(_windows) > _MAX_KEYS:  # 兜底清理：窗口内已无记录的 key
            stale = [k for k, dq in _windows.items() if not dq or dq[-1] <= now - window]
            for k in stale:
                _windows.pop(k, None)
        dq = _windows.setdefault(key, deque())
        cutoff = now - window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False, max(0.5, dq[0] + window - now)
        dq.append(now)
        return True, 0.0


def _redis():
    """Redis 客户端（懒初始化）；未配置/初始化失败返回 None（降级内存）。"""
    global _redis_client, _redis_failed
    url = os.environ.get("AIOPS_REDIS_URL", "").strip()
    if not url or _redis_failed:
        return None
    if _redis_client is None:
        try:
            import redis  # 延迟导入：未安装时静默降级

            _redis_client = redis.Redis.from_url(
                url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
            )
        except Exception:  # noqa: BLE001 - 降级内存后端
            _redis_failed = True
            return None
    return _redis_client


def _hit_redis(conn, key: str, limit: int, window: float) -> tuple[bool, float] | None:
    """Redis sorted set 滑动窗口；任何异常返回 None（调用方降级内存）。"""
    now = time.time()
    try:
        pipe = conn.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zadd(key, {f"{now:.6f}:{os.urandom(4).hex()}": now})
        pipe.zcard(key)
        pipe.expire(key, int(window) + 5)
        _, _, count, _ = pipe.execute()
    except Exception:  # noqa: BLE001 - Redis 抖动降级内存
        return None
    if count is not None and int(count) > limit:
        return False, window
    return True, 0.0


def hit(key: str, limit: int, window: float = WINDOW_SECONDS) -> tuple[bool, float]:
    """记一次请求。

    返回 ``(allowed, retry_after)``：放行时 retry_after 为 0；
    超限时 retry_after 为建议等待秒数（用于 Retry-After 头）。
    """
    conn = _redis()
    if conn is not None:
        result = _hit_redis(conn, f"aiops:rl:{key}", limit, window)
        if result is not None:
            return result
    return _hit_memory(key, limit, window)


def peek(key: str, limit: int, window: float = WINDOW_SECONDS) -> tuple[bool, float]:
    """只读检查是否已达上限（**不计数**）。

    用于登录等"先检查再执行昂贵操作"的场景：验证前 peek，失败后才 hit，
    成功尝试不计入失败配额。返回与 ``hit`` 相同语形。
    """
    conn = _redis()
    if conn is not None:
        now = time.time()
        try:
            pipe = conn.pipeline()
            pipe.zremrangebyscore(f"aiops:rl:{key}", 0, now - window)
            pipe.zcard(f"aiops:rl:{key}")
            _, count = pipe.execute()
            if count is not None and int(count) >= limit:
                return False, window
            return True, 0.0
        except Exception:  # noqa: BLE001 - Redis 抖动降级内存
            pass
    now = time.monotonic()
    with _lock:
        dq = _windows.get(key)
        if not dq:
            return True, 0.0
        cutoff = now - window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False, max(0.5, dq[0] + window - now)
        return True, 0.0


def reset() -> None:
    """清空内存窗口（仅测试用）。"""
    with _lock:
        _windows.clear()
