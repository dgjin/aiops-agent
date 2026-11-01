"""日志采集健康心跳（M1）：按受管服务记录最近成功/失败与推送行数。

文件位置：``<data_root>/log_ship_health.json``（demo 为 ``data/``，production 为
``data/prod/``；``AIOPS_LOG_HEALTH_PATH`` 可覆盖，测试指向临时文件）。

使用方：
    - ``ship_app_logs.py``：每轮采集后写入（原子替换，单进程）；
    - ``scripts/aiops-watchdog.py``：``status`` 输出采集新鲜度，不再把「进程活着」
      当成「日志供给正常」；
    - ``aiops_agent/metrics.py``：``/metrics`` 抓取时读取，暴露每服务时间戳/行数。

健康口径（不做「正常」措辞）：
    - 心跳超过 ``max_age_seconds`` → 采集停摆；
    - ``consecutive_failures > 0`` → 最近推送失败，需看 ``last_error``；
    - 文件缺失/无采集目标 → ``last_warning`` 明示，不以 0 行冒充健康。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiops_agent import mode

ENV_PATH = "AIOPS_LOG_HEALTH_PATH"
DEFAULT_MAX_AGE_SECONDS = 300


def health_path() -> Path:
    override = (os.environ.get(ENV_PATH) or "").strip()
    return Path(override).expanduser() if override else mode.data_root() / "log_ship_health.json"


def _now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso(now: datetime | None = None) -> str:
    """公开的 UTC ISO 时间（采集审计等调用方复用同一格式）。"""
    return _now_iso(now)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def read(path: Path | None = None) -> dict:
    """读取心跳文件；缺失/损坏返回空结构（不抛错，避免拖垮采集或守护）。"""
    target = path or health_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"services": {}}
    services = data.get("services") if isinstance(data, dict) else None
    return {"services": services if isinstance(services, dict) else {}}


def _write(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".log-health-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record(
    service: str,
    *,
    success: bool,
    pushed: int = 0,
    read_lines: int = 0,
    error: str = "",
    warning: str = "",
    files: int = 1,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """记录一次采集结果，返回该服务的最新心跳。"""
    service = str(service or "").strip() or "unknown"
    target = path or health_path()
    payload = read(target)
    services = payload.setdefault("services", {})
    previous = services.get(service) or {}
    stamp = _now_iso(now)
    entry = {
        "service": service,
        "last_attempt_at": stamp,
        "last_success_at": previous.get("last_success_at") or "",
        "last_pushed_lines": int(pushed),
        "last_read_lines": int(read_lines),
        "files": max(0, int(files)),
        "consecutive_failures": int(previous.get("consecutive_failures") or 0),
        "last_error": str(previous.get("last_error") or ""),
        "last_error_at": previous.get("last_error_at") or "",
        "last_warning": str(warning or ""),
        "last_warning_at": stamp if warning else (previous.get("last_warning_at") or ""),
    }
    if success:
        entry["last_success_at"] = stamp
        entry["consecutive_failures"] = 0
        entry["last_error"] = ""
        entry["last_error_at"] = ""
    else:
        entry["consecutive_failures"] += 1
        entry["last_error"] = str(error or "未知采集错误")
        entry["last_error_at"] = stamp
    services[service] = entry
    payload["updated_at"] = stamp
    _write(payload, target)
    return entry


def snapshot(
    path: Path | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> list[dict]:
    """返回按服务排序的健康视图（供守护 status / Prometheus 渲染）。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    items: list[dict] = []
    for service, entry in sorted(read(path).get("services", {}).items()):
        last_success = _parse_iso(entry.get("last_success_at"))
        age = (current - last_success).total_seconds() if last_success else None
        reasons: list[str] = []
        if last_success is None:
            reasons.append("从未成功采集")
        elif age is not None and age > max_age_seconds:
            reasons.append(f"心跳已停摆 {int(age)}s（阈值 {max_age_seconds}s）")
        if int(entry.get("consecutive_failures") or 0) > 0:
            reasons.append(f"连续失败 {int(entry.get('consecutive_failures') or 0)} 次：{entry.get('last_error') or '未知错误'}")
        if entry.get("last_warning"):
            reasons.append(str(entry["last_warning"]))
        items.append(
            {
                **entry,
                "age_seconds": age,
                "healthy": not reasons,
                "detail": "；".join(reasons) if reasons else (
                    f"心跳新鲜（{int(age or 0)}s），最近一次推送 {int(entry.get('last_pushed_lines') or 0)} 行"
                ),
            }
        )
    return items
