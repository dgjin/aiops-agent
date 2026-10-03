"""data/ 产物只读聚合：索引 stats / 留痕关联 / 发布记录 / stable 探测（设计方案 6.1）。

数据源约定（与主工程一致）：
- data/code_index.json     代码索引 stats（chunks/dim/model/backend）
- data/historical_tickets.json  历史工单（tickets 计数）
- data/notify/*.json       通知留痕（审批/公告/升级卡片）
- data/argocd/*.json       ArgoCD Application manifest 留痕
- data/sandbox/<patch_id>/ 沙箱工作区
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_VERSION_RE = re.compile(r"v[\d.]+")


def index_stats() -> dict | None:
    path = DATA_DIR / "code_index.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    chunks = raw.get("chunks") or []
    files = sorted({c.get("file") for c in chunks if c.get("file")})
    tickets = 0
    tickets_path = DATA_DIR / "historical_tickets.json"
    if tickets_path.is_file():
        try:
            loaded = json.loads(tickets_path.read_text(encoding="utf-8"))
            tickets = len(loaded) if isinstance(loaded, list) else 0
        except (OSError, json.JSONDecodeError):
            tickets = 0
    return {
        "model": raw.get("model"),
        "dim": raw.get("dim"),
        "backend": raw.get("backend"),
        "created_at": raw.get("created_at"),
        "repo": raw.get("repo"),
        "chunks": len(chunks),
        "files": len(files),
        "tickets": tickets,
        "faiss_file": (DATA_DIR / "code_index.faiss").is_file(),
    }


def artifacts_for_patch(patch_id: str) -> dict:
    """按 patch_id 关联留痕文件与沙箱目录。"""

    def _glob(sub: str) -> list[str]:
        directory = DATA_DIR / sub
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.glob(f"*{patch_id}*") if p.is_file())

    sandbox = DATA_DIR / "sandbox" / patch_id
    return {
        "notify": _glob("notify"),
        "argocd": _glob("argocd"),
        "sandbox_dir": str(sandbox) if sandbox.is_dir() else None,
    }


def recent_releases(limit: int = 5) -> list[dict]:
    """最近发布记录（按文件修改时间倒序，解析 manifest 关键标签）。"""
    directory = DATA_DIR / "argocd"
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = raw.get("metadata") or {}
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        out.append(
            {
                "file": path.name,
                "name": meta.get("name"),
                "version": annotations.get("aiops.version"),
                "service": labels.get("aiops.service"),
                "alert_id": labels.get("aiops.alert-id"),
                "patch_id": labels.get("aiops.patch-id"),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return out


def broadcast_version(patch_id: str | None) -> str | None:
    """从公告卡片留痕（broadcast-p-xxx.json）提取发布版本号（如 v1.0.514）。"""
    if not patch_id:
        return None
    path = DATA_DIR / "notify" / f"broadcast-{patch_id}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    title = (((raw.get("card") or {}).get("header") or {}).get("title") or {}).get("content") or ""
    match = _VERSION_RE.search(str(title))
    return match.group(0) if match else None


def probe_stable(port: str, timeout: float = 1.5) -> dict:
    """探测稳定版容器 /health（同步函数，调用方用 asyncio.to_thread 包装）。"""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 固定本机地址
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return {"running": True, "port": port, "target": url, "body": body}
    except Exception as exc:  # noqa: BLE001 - 探测类接口允许失败返回
        return {"running": False, "port": port, "target": url, "error": str(exc)}
