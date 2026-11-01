"""确定性证据质量判定（M1）：只看观测事实，不引入第二个模型置信分。

状态固定为以下六种（工作流与前端共用，不得自由扩展）：

- ``OK``：日志新鲜、服务映射正确且存在可分析信号；
- ``LOKI_UNAVAILABLE``：查询失败（观测面不可用）；
- ``NO_LOGS``：Loki 可用但窗口内无该服务日志；
- ``STALE_LOGS``：存在历史流但最新日志超出策略阈值；
- ``UNMAPPED_SERVICE``：告警服务与采集/注册标签无法对应；
- ``INSUFFICIENT_SIGNAL``：有新鲜日志但只有健康检查、启动信息或噪声。

判定顺序即优先级：Loki 不可用 > 服务未映射（仅无日志时）> 无日志 >
过期 > 信号不足 > OK。未注册服务若仍有日志，不判 ``UNMAPPED_SERVICE``——
观测面已经证明该标签可查询，避免基础设施类告警被误拦截。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

OK = "OK"
LOKI_UNAVAILABLE = "LOKI_UNAVAILABLE"
NO_LOGS = "NO_LOGS"
STALE_LOGS = "STALE_LOGS"
UNMAPPED_SERVICE = "UNMAPPED_SERVICE"
INSUFFICIENT_SIGNAL = "INSUFFICIENT_SIGNAL"

STATUSES = (
    OK,
    LOKI_UNAVAILABLE,
    NO_LOGS,
    STALE_LOGS,
    UNMAPPED_SERVICE,
    INSUFFICIENT_SIGNAL,
)

# 未通过预检的状态：工作流不调用根因模型，直接转人工。
BLOCKING_STATUSES = frozenset(STATUSES) - {OK}

# 错误/异常信号（中英文）：命中任意一条即视为可分析信号。
_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:error|err|exception|fatal|panic|traceback|failed|failure|"
    r"timeout|timed\s*out|refused|unavailable|unhealthy|oom|killed|crash(?:ed)?|"
    r"5\d\d)\b|错误|异常|失败|超时|拒绝|不可用|崩溃|中断|宕机|熔断|限流)"
)

# 健康检查 / 启动 / 噪声：单独出现不足以支撑根因。
_NOISE_RE = re.compile(
    r"(?i)(?:\b(?:health(?:z|check)?|readiness|liveness|metrics|started|starting|"
    r"listening|ready|boot(?:ed|ing)?|shutdown|version)\b|健康检查|启动完成|开始监听|就绪)"
)


def _parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else ""


def _clip(text: str, limit: int = 240) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def signal_lines(lines: list[str] | None) -> list[str]:
    """命中错误/异常关键字的日志行（确定性规则，不做语义猜测）。"""
    return [str(line) for line in (lines or []) if _SIGNAL_RE.search(str(line))]


def is_noise_line(line: str) -> bool:
    """健康检查 / 启动信息 / 普通噪声行（无错误信号时不计为可分析信号）。"""
    text = str(line or "")
    return bool(_NOISE_RE.search(text)) and not _SIGNAL_RE.search(text)


def evaluate(
    *,
    service: str,
    loki_available: bool,
    lines: list[str] | None = None,
    latest_log_at: str | datetime | None = None,
    registered: bool | None = None,
    lookback_minutes: int = 10,
    stale_after_minutes: int = 15,
    min_signal_lines: int = 1,
    now: datetime | None = None,
    query_error: str = "",
) -> dict:
    """返回证据质量元数据（纯函数，便于单测与工作流确定性使用）。

    ``registered``：``True`` 已登记、``False`` 未登记、``None`` 未知（注册表为空时跳过映射校验）。
    """
    lines = [str(line) for line in (lines or [])]
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    latest = _parse_ts(latest_log_at)
    threshold = max(1, int(stale_after_minutes))

    status = OK
    detail = ""
    if not loki_available:
        status = LOKI_UNAVAILABLE
        detail = f"Loki 查询失败，无法取证：{_clip(query_error) or '观测面不可用'}"
    elif not str(service or "").strip():
        status = UNMAPPED_SERVICE
        detail = "告警未携带 service 标签，无法映射到日志流"
    elif not lines and latest is None:
        if registered is False:
            status = UNMAPPED_SERVICE
            detail = f"服务 {service} 未在应用注册表登记，且未查到对应日志标签"
        else:
            status = NO_LOGS
            detail = f"Loki 可用，但近 {max(1, int(lookback_minutes))} 分钟无服务 {service} 的日志"
    elif latest is not None and current - latest > timedelta(minutes=threshold):
        status = STALE_LOGS
        detail = (
            f"最新日志 {_iso(latest)} 距当前超过 {threshold} 分钟"
            f"（策略阈值），历史流存在但已不新鲜"
        )
    else:
        hits = signal_lines(lines)
        minimum = max(1, int(min_signal_lines))
        if len(hits) < minimum:
            status = INSUFFICIENT_SIGNAL
            detail = (
                f"有 {len(lines)} 行新鲜日志，但仅命中 {len(hits)} 行错误/异常信号"
                f"（最低要求 {minimum} 行）；健康检查、启动信息或普通访问日志不足以支撑根因"
            )
        else:
            detail = f"近 {max(1, int(lookback_minutes))} 分钟日志新鲜，命中 {len(hits)} 行可分析信号"

    return {
        "status": status,
        "reason_code": status,
        "detail": detail,
        "latest_log_at": _iso(latest),
        "line_count": len(lines),
        "signal_lines": len(signal_lines(lines)),
        "lookback_minutes": max(1, int(lookback_minutes)),
        "stale_after_minutes": threshold,
        "service_registered": registered,
    }


def blocking_category(status: str) -> str:
    """阻断状态的转人工分类：观测异常 / 证据不足。"""
    return "evidence" if status == INSUFFICIENT_SIGNAL else "observability"


def escalation_reason(quality: dict | None) -> str:
    """证据预检阻断时的人类可读升级原因（供通知与待办摘要）。"""
    quality = quality or {}
    status = str(quality.get("status") or "UNKNOWN")
    detail = str(quality.get("detail") or "").strip()
    prefix = {
        LOKI_UNAVAILABLE: "观测异常：Loki 不可用",
        NO_LOGS: "观测异常：窗口内无服务日志",
        STALE_LOGS: "观测异常：日志已过期",
        UNMAPPED_SERVICE: "观测异常：服务标签无法映射",
        INSUFFICIENT_SIGNAL: "证据不足：日志无可分析信号",
    }.get(status, "证据预检未通过")
    return f"{prefix}（{status}）" + (f"；{detail}" if detail else "")
