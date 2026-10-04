"""data/ 产物只读聚合：索引 stats / 留痕关联 / 发布记录 / stable 与被监控应用探测（设计方案 6.1）。

数据源约定（与主工程一致）：
- data/code_index.json     代码索引 stats（chunks/dim/model/backend）
- data/historical_tickets.json  历史工单（tickets 计数）
- data/notify/*.json       通知留痕（审批/公告/升级卡片）
- data/argocd/*.json       ArgoCD Application manifest 留痕
- data/sandbox/<patch_id>/ 沙箱工作区

探测约定：
- probe_stable：本系统发布出的稳定版服务（/health，解析 JSON）
- probe_monitored_app：**被监控应用**根地址（AIOPS_MONITOR_URL，默认 http://localhost:3000/），
  根路径通常返回 HTML，故不解析 JSON，只回报可达性与 HTTP 状态码；
  可选关键字校验——响应内容须包含指定关键字才算在线（覆盖「端口活着但页面白屏」类故障）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_VERSION_RE = re.compile(r"v[\d.]+")

# 被监控应用根地址默认值（AIOPS_MONITOR_URL 可覆盖）
DEFAULT_MONITOR_URL = "http://localhost:3000/"


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


def _http_reachable(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """HTTP 可达性：有响应（含 4xx/5xx）即视为可达。返回 (ok, detail)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本机依赖地址
            status = getattr(resp, "status", None) or resp.getcode()
        return True, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 探测类允许失败返回
        return False, f"{type(exc).__name__}: {exc}"


def probe_docker(timeout: float = 3.0) -> tuple[bool, str]:
    """Docker 守护进程可用性（沙箱测试与发布依赖）。"""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "未安装 docker CLI"
    except subprocess.TimeoutExpired:
        return False, f"docker info 超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip() or "ok"
    tail = (proc.stderr or "").strip().splitlines()
    return False, (tail[-1][:100] if tail else "docker info 失败")


def probe_dependencies() -> dict:
    """修复链路依赖自检（供全局「降级模式」横幅）。

    这些依赖任一不可用，修复链路都会**静默降级**（日志证据缺失 → 置信度不足；
    沙箱不可用 → 测试失败 → 转人工），但控制台此前毫无提示（评估报告 C4）。
    """
    loki_url = os.environ.get("AIOPS_LOKI_URL", "http://localhost:3101").rstrip("/")
    ollama_url = os.environ.get("AIOPS_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    loki_ok, loki_detail = _http_reachable(f"{loki_url}/ready")
    ollama_ok, ollama_detail = _http_reachable(f"{ollama_url}/api/tags")
    docker_ok, docker_detail = probe_docker()
    return {
        "loki": {"ok": loki_ok, "detail": loki_detail},
        "ollama": {"ok": ollama_ok, "detail": ollama_detail},
        "docker": {"ok": docker_ok, "detail": docker_detail},
    }


def _read_head(stream, limit: int = 65536) -> bytes:
    """读取响应片段供关键字校验（HTML 首屏足够；读取失败按「无内容」处理）。"""
    try:
        return stream.read(limit) or b""
    except Exception:  # noqa: BLE001 - 探测类允许失败返回
        return b""


def probe_monitored_app(url: str | None = None, timeout: float = 2.0, keyword: str = "") -> dict:
    """探测**被监控应用**根地址的可用性（同步函数，调用方用 asyncio.to_thread 包装）。

    地址优先级：显式 url > 环境变量 AIOPS_MONITOR_URL > DEFAULT_MONITOR_URL。
    与 probe_stable 的差异：被监控应用是外部系统，根路径通常返回 HTML，故**不解析 JSON**，
    只回报可达性、HTTP 状态码与延迟。

    说明：能收到任何 HTTP 响应（含 4xx/5xx）都视为「在线」——那表示端口确实有服务在响应；
    只有连接失败/超时才判定为不可达。

    关键字校验（可选）：配置 keyword 时，响应内容须包含该关键字才算在线——
    覆盖「端口活着但页面白屏/异常」类故障（如前端挂载点被改坏：HTTP 200 但内容失效）。
    """
    target = url or os.environ.get("AIOPS_MONITOR_URL") or DEFAULT_MONITOR_URL
    check = (keyword or "").strip()
    started = time.monotonic()

    def _elapsed_ms() -> float:
        return round((time.monotonic() - started) * 1000, 1)

    def _keyword_ok(body: bytes) -> bool:
        return not check or check in body.decode("utf-8", errors="ignore")

    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:  # noqa: S310 - 用户配置的监控地址
            status = getattr(resp, "status", None) or resp.getcode()
            body = _read_head(resp) if check else b""
        if not _keyword_ok(body):
            return {
                "running": False,
                "target": target,
                "status_code": status,
                "latency_ms": _elapsed_ms(),
                "error": f"HTTP {status} 但响应内容缺少关键字 {check!r}（页面可能白屏/异常）",
            }
        return {"running": True, "target": target, "status_code": status, "latency_ms": _elapsed_ms()}
    except urllib.error.HTTPError as exc:
        # 有 HTTP 响应即在线（如根路径 404 也说明服务在跑）；关键字校验同样适用
        body = _read_head(exc) if check else b""
        if not _keyword_ok(body):
            return {
                "running": False,
                "target": target,
                "status_code": exc.code,
                "latency_ms": _elapsed_ms(),
                "error": f"HTTP {exc.code} 且响应内容缺少关键字 {check!r}（页面可能白屏/异常）",
            }
        return {
            "running": True,
            "target": target,
            "status_code": exc.code,
            "latency_ms": _elapsed_ms(),
            "note": f"服务在线但根路径返回 HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001 - 探测类接口允许失败返回
        return {
            "running": False,
            "target": target,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": _elapsed_ms(),
        }
