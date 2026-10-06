"""变更关联服务（P1-1）：把「最近发布/配置变更」注入 triage 上下文。

设计（对齐评估报告 P1-1「变更关联：发布事件注入 triage 上下文」）：

双通道变更源，合并后按时间倒序返回：

1. **外部变更 feed**（生产语义）：JSONL 追加式事件流，每行
   ``{"ts": ISO8601, "service": "order", "version": "v1.0.601", "kind": "release|config", "summary": "..."}``；
   路径由 ``AIOPS_CHANGE_FEED`` 指定，缺省 ``data/changes.jsonl``——
   生产环境接入 CI/CD 流水线（Jenkins/GitLab CI/ArgoCD 事件）写入即可；
2. **本系统发布留痕**（演示自动闭环）：解析 ``data/argocd/*.json``（ArgoCD Application
   manifest 留痕），取 ``aiops.service`` 标签 + ``aiops.version`` 注解 + 文件 mtime——
   本系统每次真实发布自动成为下一次 triage 可见的变更事件。

读取侧全部优雅降级：文件缺失/坏行/时间格式非法一律跳过，**绝不阻断修复链路**。
窗口默认 120 分钟（``AIOPS_CHANGE_LOOKBACK_MINUTES`` 可覆盖），最多返回 5 条。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FEED = BASE_DIR / "data" / "changes.jsonl"
ARGOCD_DIR = BASE_DIR / "data" / "argocd"

DEFAULT_LOOKBACK_MINUTES = 120
MAX_ITEMS = 5


def feed_path() -> Path:
    override = os.environ.get("AIOPS_CHANGE_FEED", "").strip()
    return Path(override) if override else DEFAULT_FEED


def _default_lookback() -> int:
    try:
        value = int(os.environ.get("AIOPS_CHANGE_LOOKBACK_MINUTES", ""))
        return value if value > 0 else DEFAULT_LOOKBACK_MINUTES
    except ValueError:
        return DEFAULT_LOOKBACK_MINUTES


def _parse_ts(value: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _feed_events(service: str) -> list[dict]:
    """通道 1：外部 JSONL feed（坏行跳过）。"""
    path = feed_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("service") != service:
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        events.append(
            {
                "ts": ts,
                "version": str(record.get("version") or ""),
                "kind": str(record.get("kind") or "release"),
                "summary": str(record.get("summary") or ""),
                "source": "feed",
            }
        )
    return events


def _argocd_events(service: str) -> list[dict]:
    """通道 2：本系统发布留痕（文件 mtime 即发布时间）。"""
    if not ARGOCD_DIR.is_dir():
        return []
    events: list[dict] = []
    for path in ARGOCD_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except (OSError, json.JSONDecodeError):
            continue
        labels = ((raw.get("metadata") or {}).get("labels")) or {}
        if labels.get("aiops.service") != service:
            continue
        version = (((raw.get("metadata") or {}).get("annotations")) or {}).get("aiops.version") or ""
        events.append(
            {
                "ts": mtime,
                "version": str(version),
                "kind": "release",
                "summary": f"本系统滚动发布 {version or '（版本未知）'} 成功（补丁 {labels.get('aiops.patch-id') or '--'}）",
                "source": "argocd",
            }
        )
    return events


def recent_changes(
    service: str,
    lookback_minutes: int | None = None,
    limit: int = MAX_ITEMS,
    now: datetime | None = None,
) -> list[dict]:
    """近 lookback 分钟内该服务的变更事件（双通道合并，时间倒序，最多 limit 条）。

    返回条目：``{ts, version, kind, summary, source}``；任何通道故障均降级为
    缺少该通道数据，异常不外抛。
    """
    lookback = lookback_minutes if lookback_minutes is not None else _default_lookback()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    threshold = current - timedelta(minutes=lookback)

    events: list[dict] = []
    for loader in (_feed_events, _argocd_events):
        try:
            events.extend(loader(service))
        except Exception:  # noqa: BLE001 - 变更源故障绝不阻断修复链路
            continue
    recent = [e for e in events if threshold <= e["ts"] <= current]
    recent.sort(key=lambda e: e["ts"], reverse=True)
    return [
        {
            "ts": e["ts"].isoformat(timespec="seconds"),
            "version": e["version"],
            "kind": e["kind"],
            "summary": e["summary"],
            "source": e["source"],
        }
        for e in recent[:limit]
    ]
