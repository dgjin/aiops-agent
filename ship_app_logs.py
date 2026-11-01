"""应用日志接入 Loki（生产形态：扮演 Filebeat / Fluent Bit 的采集角色）。

背景：被监控应用（smart-data-analytics / nl2sql）把结构化日志写到
``logs/app_server*.log``（每行一个 JSON：``{"ts","level","msg","module","requestId"}``），
其中还夹杂 Vite 等非 JSON 行。AIOps 的日志契约要求：

    - stream labels：``service`` / ``level``（error / warn / info）；
    - 行内携带 ``trace_id=<id>`` 供调用链关联与根因上下文。

本采集器把应用日志规范化后推送到 **AIOps Loki**（默认 http://localhost:3101），
从而让 ``aiops_agent.logs.query_lines("nl2sql")`` 与突增检测无需任何改动即可工作。

用法：
    # 【清单模式·推荐】按「被监控应用」清单采集（每轮重读清单 → 控制台改动热生效）
    .venv/bin/python ship_app_logs.py --once
    .venv/bin/python ship_app_logs.py --from-start --once     # 首次全量
    .venv/bin/python ship_app_logs.py --follow                # 持续采集

    # 【单文件模式】显式指定文件与 service（兼容既有用法）
    .venv/bin/python ship_app_logs.py --file <app>/logs/app_server.log --service nl2sql --once

清单来源：``data/monitored_apps.json``（与「被监控应用」管理页同一份数据），
每条取 ``enabled`` 为真且配置了 ``log_path`` 的条目；``log_path`` 支持通配（如
``app_server*.log``，覆盖轮转归档）。**每轮重新读盘**，因此运行期新增/停用条目自动生效。

位点：``data/log_ship_positions.json``（按文件绝对路径记录已读字节偏移），保证重启不重复推送。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from aiops_agent import config as _config  # noqa: F401 - 触发工程根 .env 加载（与其他入口一致）
from aiops_agent import log_health, logs

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_POSITIONS = BASE_DIR / "data" / "log_ship_positions.json"
DEFAULT_SERVICE = os.environ.get("AIOPS_MONITOR_SERVICE", "nl2sql")

# 推送失败的有界重试：仅在 Loki 未确认写入时重试，退避指数增长（上限 5s）。
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("AIOPS_LOG_SHIP_MAX_ATTEMPTS", "3") or 3)
DEFAULT_BACKOFF_SECONDS = float(os.environ.get("AIOPS_LOG_SHIP_BACKOFF_SECONDS", "0.5") or 0.5)

# 非 JSON 行（如 Vite 输出）的级别识别
_LEVEL_RE = re.compile(r"\b(ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b", re.IGNORECASE)

# 级别归一化到 AIOps 约定的 error / warn / info 三档
_LEVEL_MAP = {
    "error": "error", "err": "error", "fatal": "error", "critical": "error",
    "warn": "warn", "warning": "warn",
    "info": "info", "log": "info", "notice": "info",
    "debug": "info", "trace": "info",  # 演示契约只有三档，低级别并入 info
}


def normalize_level(raw: object) -> str:
    """把任意来源的级别归一化为 error / warn / info。"""
    text = str(raw or "").strip().lower()
    return _LEVEL_MAP.get(text, "info")


def parse_line(raw: str) -> tuple[str, str]:
    """解析一行应用日志，返回 ``(level, 规范化行)``。

    - JSON 行：取 ``level``；行首注入 ``trace_id=<requestId>``（便于调用链关联），
      正文取 ``msg``（带 ``module`` 前缀）；
    - 非 JSON 行：正则识别级别，无法识别按 info；原文保留。
    """
    line = raw.rstrip("\n")
    if not line.strip():
        return "", ""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        obj = None

    if isinstance(obj, dict):
        level = normalize_level(obj.get("level"))
        module = str(obj.get("module") or "").strip()
        msg = str(obj.get("msg") or line).strip()
        request_id = obj.get("requestId") or obj.get("request_id") or obj.get("trace_id")
        prefix = f"trace_id={request_id} " if request_id else ""
        body = f"[{module}] {msg}" if module else msg
        return level, f"{prefix}{body}"

    match = _LEVEL_RE.search(line)
    return normalize_level(match.group(1) if match else "info"), line


def load_positions(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_positions(path: Path, positions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")


def read_new_lines(
    file_path: Path, offset: int, from_start: bool = False
) -> tuple[list[str], int, str]:
    """从 ``offset`` 读取新增内容，返回 ``(完整行列表, 新偏移, 残留半行)``。

    只返回以换行结尾的完整行；文件尾部未写完的半行会通过新偏移「退回」保留，
    下次读取时再拼上（避免把写了一半的 JSON 当成坏行推送）。
    """
    start = 0 if from_start else offset
    if not file_path.is_file():
        return [], start, ""
    size = file_path.stat().st_size
    if start > size:  # 文件被轮转/截断 → 从头读
        start = 0
    with file_path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read()
    if not chunk:
        return [], start, ""

    text = chunk.decode("utf-8", "replace")
    lines = text.split("\n")
    remainder = lines.pop()  # 末段：无换行则视为半行
    new_offset = start + len(chunk) - len(remainder.encode("utf-8"))
    return [ln for ln in lines if ln is not None], new_offset, remainder


def _push_batch(
    service: str,
    level: str,
    batch: list[str],
    loki_url: str | None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> tuple[int, int]:
    """推送一个级别批次；返回 (确认行数, 实际尝试次数)。

    只有 Loki 确认全部行写入才算成功；失败按指数退避重试，超过上限抛错——
    调用方据此保持位置游标不前进（下一轮重推，至少一次语义）。
    """
    attempts = max(1, int(max_attempts))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            confirmed = int(logs.push_lines(service, level, batch, loki_url=loki_url))
            if confirmed != len(batch):
                raise RuntimeError(f"Loki 仅确认 {confirmed}/{len(batch)} 行")
            return confirmed, attempt
        except Exception as exc:  # noqa: BLE001 - 瞬态网络/5xx 统一重试
            last_error = exc
            if attempt < attempts and backoff_seconds > 0:
                time.sleep(min(5.0, backoff_seconds * (2 ** (attempt - 1))))
    raise RuntimeError(
        f"Loki 推送失败（{attempts} 次尝试）：{type(last_error).__name__}: {last_error}"
    ) from last_error


def ship_once(
    file_path: Path,
    service: str = DEFAULT_SERVICE,
    positions_path: Path | None = None,
    loki_url: str | None = None,
    from_start: bool = False,
    health_path: Path | None = None,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> dict:
    """增量采集一次：读新增 → 按级别分组 → 推送到 Loki → 落位点。返回统计。

    位置游标仅在**所有级别批次均被 Loki 确认后**推进；任何失败都保持原位点并在
    心跳中记录错误，下一轮重推（可能重复，但不会丢日志）。
    """
    positions_path = Path(positions_path) if positions_path else DEFAULT_POSITIONS
    key = str(file_path.resolve())
    positions = load_positions(positions_path)
    offset = int(positions.get(key, 0))

    raw_lines, new_offset, remainder = read_new_lines(file_path, offset, from_start=from_start)

    grouped: dict[str, list[str]] = {}
    for raw in raw_lines:
        level, line = parse_line(raw)
        if not line:
            continue
        grouped.setdefault(level, []).append(line)

    attempts_limit = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max(1, int(max_attempts))
    backoff = DEFAULT_BACKOFF_SECONDS if backoff_seconds is None else max(0.0, float(backoff_seconds))
    pushed = 0
    attempts = 0
    warning = "" if file_path.is_file() else f"日志文件不存在：{file_path}"
    try:
        for level, batch in grouped.items():
            confirmed, tries = _push_batch(
                service, level, batch, loki_url, attempts_limit, backoff
            )
            pushed += confirmed
            attempts += tries
        if raw_lines:
            positions[key] = new_offset
            save_positions(positions_path, positions)
    except Exception as exc:  # noqa: BLE001 - 记录心跳后原样抛出，由 follow/调用方决定是否继续
        log_health.record(
            service,
            success=False,
            pushed=0,
            read_lines=len(raw_lines),
            error=f"{type(exc).__name__}: {exc}",
            path=health_path,
        )
        raise

    log_health.record(
        service,
        success=True,
        pushed=pushed,
        read_lines=len(raw_lines),
        warning=warning,
        path=health_path,
    )
    return {
        "file": key,
        "service": service,
        "read": len(raw_lines),
        "pushed": pushed,
        "by_level": {level: len(batch) for level, batch in grouped.items()},
        "offset": new_offset,
        "pending_partial": bool(remainder),
        "attempts": attempts,
        "warning": warning,
    }


_GLOB_CHARS = re.compile(r"[*?\[]")


def resolve_log_files(log_path: str) -> list[Path]:
    """把清单里的 log_path 解析为文件列表；支持通配（如 ``app_server*.log`` 覆盖轮转归档）。"""
    path = Path(log_path).expanduser()
    if not _GLOB_CHARS.search(path.name):
        return [path]
    return sorted(p for p in path.parent.glob(path.name) if p.is_file())


def ship_app(
    app: dict,
    positions_path: Path | None = None,
    loki_url: str | None = None,
    from_start: bool = False,
) -> dict:
    """按清单条目采集：展开 log_path（支持通配）→ 逐文件增量推送，service 取该条目的值。"""
    service = app.get("service") or DEFAULT_SERVICE
    files = resolve_log_files(app["log_path"])
    per_file: list[dict] = []
    by_level: dict[str, int] = {}
    total_read = total_pushed = 0
    for file_path in files:
        stats = ship_once(file_path, service, positions_path, loki_url, from_start=from_start)
        per_file.append(stats)
        total_read += stats["read"]
        total_pushed += stats["pushed"]
        for level, count in stats["by_level"].items():
            by_level[level] = by_level.get(level, 0) + count
    return {
        "app_id": app.get("id"),
        "name": app.get("name"),
        "service": service,
        "log_path": app["log_path"],
        "files": len(files),
        "read": total_read,
        "pushed": total_pushed,
        "by_level": by_level,
        "per_file": per_file,
    }


def ship_list(
    apps: list[dict],
    positions_path: Path | None = None,
    loki_url: str | None = None,
    from_start: bool = False,
) -> dict:
    """按清单批量采集。调用方负责传入「启用且配置了 log_path」的条目。"""
    results = [ship_app(app, positions_path, loki_url, from_start=from_start) for app in apps]
    return {
        "apps": len(results),
        "read": sum(item["read"] for item in results),
        "pushed": sum(item["pushed"] for item in results),
        "results": results,
    }


def load_apps() -> list[dict]:
    """读取采集清单（**每次读盘 → 控制台改动热生效**）。

    复用 bff 的清单存储（与「被监控应用」管理页同一份数据），因此新增/停用/改路径
    在采集器下一轮即生效，无需重启采集进程。
    """
    try:
        from bff import monitored_apps

        return monitored_apps.log_targets()
    except Exception as exc:  # noqa: BLE001 - 清单不可读时不中断采集循环
        print(f"[ship] 读取被监控应用清单失败：{type(exc).__name__}: {exc}", flush=True)
        return []


def _registry_apps() -> list[dict]:
    try:
        from aiops_agent import app_registry

        return app_registry.load_apps()
    except Exception:  # noqa: BLE001 - 注册表不可用不阻断采集
        return []


def _norm_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _typo_suggestion(service: str, candidates: set[str]) -> str:
    """确定性相似度：归一化后 >= 0.8 视为疑似拼写错误（不做模糊猜测）。"""
    wanted = _norm_label(service)
    if not wanted:
        return ""
    best = ""
    best_ratio = 0.0
    for candidate in candidates:
        ratio = difflib.SequenceMatcher(None, wanted, _norm_label(candidate)).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best if best_ratio >= 0.8 and best != service else ""


def audit_service_labels(
    apps: list[dict] | None = None,
    registry_apps: list[dict] | None = None,
    check_loki: bool = False,
    loki_url: str | None = None,
) -> dict:
    """按注册表核对采集 service 标签，输出具体服务清单（M1）。

    发现问题不修改任何配置，只返回 ``issues`` 供值班/运维修复：
    ``unregistered_service`` / ``missing_collection`` / ``missing_log_path`` /
    ``missing_files`` / ``duplicate_service`` / ``suspected_typo`` / ``no_recent_logs``。
    """
    targets = list(apps) if apps is not None else load_apps()
    registry = list(registry_apps) if registry_apps is not None else _registry_apps()
    registry_services = {
        str(app.get("service") or "").strip()
        for app in registry
        if str(app.get("service") or "").strip()
    }
    target_services = {
        str(app.get("service") or "").strip()
        for app in targets
        if str(app.get("service") or "").strip()
    }
    issues: list[dict] = []

    seen: dict[str, int] = {}
    for app in targets:
        service = str(app.get("service") or "").strip()
        seen[service] = seen.get(service, 0) + 1
        if not service:
            issues.append(
                {"service": "", "code": "missing_service", "detail": "采集条目缺少 service 标签", "suggestion": "补齐 service 后重启采集"}
            )
            continue
        if registry_services and service not in registry_services:
            suggestion = _typo_suggestion(service, registry_services)
            issues.append(
                {
                    "service": service,
                    "code": "suspected_typo" if suggestion else "unregistered_service",
                    "detail": (
                        f"采集标签 {service} 未在应用注册表登记"
                        + (f"，疑似 {suggestion} 的拼写变体" if suggestion else "")
                    ),
                    "suggestion": f"核对并统一为 {suggestion}" if suggestion else "在「被监控应用」登记该 service，或修正采集标签",
                }
            )
        files = resolve_log_files(str(app.get("log_path") or ""))
        if not files:
            issues.append(
                {
                    "service": service,
                    "code": "missing_files",
                    "detail": f"log_path 未匹配到任何文件：{app.get('log_path')}",
                    "suggestion": "核对日志路径/通配符，确认应用已产生日志",
                }
            )
        if check_loki:
            try:
                probe = logs.query_lines_with_meta(service, lookback_minutes=60, limit=1, loki_url=loki_url)
            except Exception as exc:  # noqa: BLE001 - 审计报告保留错误，不阻断其他检查
                issues.append(
                    {
                        "service": service,
                        "code": "loki_unreachable",
                        "detail": f"Loki 查询失败：{type(exc).__name__}: {exc}",
                        "suggestion": "先修复 Loki/采集管道，再判断日志标签",
                    }
                )
                break
            if not probe["lines"]:
                issues.append(
                    {
                        "service": service,
                        "code": "no_recent_logs",
                        "detail": "近 60 分钟 Loki 无该 service 日志",
                        "suggestion": "检查采集进程位点、Loki 写入与应用日志轮转",
                    }
                )

    for service, count in sorted(seen.items()):
        if count > 1:
            issues.append(
                {
                    "service": service,
                    "code": "duplicate_service",
                    "detail": f"{count} 个采集条目共用同一 service 标签",
                    "suggestion": "合并采集目标或为应用分配唯一 service（避免日志流混淆）",
                }
            )

    for app in registry:
        service = str(app.get("service") or "").strip()
        if not service:
            continue
        if not app.get("enabled", True):
            continue
        if not str(app.get("log_path") or "").strip():
            issues.append(
                {
                    "service": service,
                    "code": "missing_log_path",
                    "detail": "注册应用已启用但未配置 log_path",
                    "suggestion": "在「被监控应用」补充日志路径，否则该服务永远没有日志证据",
                }
            )
        elif service not in target_services:
            issues.append(
                {
                    "service": service,
                    "code": "missing_collection",
                    "detail": "注册应用已启用且配置了日志路径，但不在当前采集目标中",
                    "suggestion": "确认应用已启用、日志路径非空，并检查采集器清单读取",
                }
            )

    return {
        "checked_at": log_health.now_iso(),
        "targets": len(targets),
        "registry": len(registry),
        "check_loki": bool(check_loki),
        "issues": issues,
        "ok": not issues,
    }


def follow(
    source: Path | None,
    service: str | None = None,
    positions_path: Path | None = None,
    loki_url: str | None = None,
    interval: float = 2.0,
    poll_once: bool = False,
    from_start: bool = False,
) -> None:
    """持续采集（``poll_once`` 供测试只跑一轮）。

    ``source=None`` → **清单模式**：每一轮都重新读取清单，因此运行期新增/停用条目会自动生效；
    否则为单文件模式（``--file``，兼容既有用法）。
    """
    first = True
    while True:
        use_from_start = from_start and first
        first = False
        try:
            if source is None:
                apps = load_apps()
                stats = ship_list(apps, positions_path, loki_url, from_start=use_from_start)
                if stats["pushed"]:
                    print(f"[ship] 清单采集 {stats['apps']} 个应用 → 推送 {stats['pushed']} 行", flush=True)
                elif poll_once:
                    print(f"[ship] 清单采集 {stats['apps']} 个应用 → 无新增日志", flush=True)
            else:
                used = service or DEFAULT_SERVICE
                stats = ship_once(source, used, positions_path, loki_url, from_start=use_from_start)
                if stats["pushed"]:
                    print(f"[ship] {used} 推送 {stats['pushed']} 行 {stats['by_level']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 单轮推送故障（如 Loki 瞬态 5xx）不中断采集循环
            if poll_once:
                raise  # 测试模式保留异常传播，便于调用方感知失败
            print(f"[ship] 本轮采集失败（{type(exc).__name__}: {exc}），{interval}s 后重试", flush=True)
        if poll_once:
            return
        time.sleep(interval)


def main() -> None:
    # 禁用 urllib 系统代理快照（同 watchdog/mcp_server 的 ProxyHandler({}) 惯例）：
    # 采集只推送本机 Loki；若系统代理在首次 urlopen 时开启，全局 opener 会永久
    # 缓存该代理，代理关闭后仍走死端口 → 推送全部失败。
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    parser = argparse.ArgumentParser(
        description="应用日志接入 Loki（Filebeat 角色）：单文件模式或按被监控应用清单热采集"
    )
    parser.add_argument("--file", default=None, help="单文件模式：应用日志文件；不传则走清单模式")
    parser.add_argument("--service", default=None, help=f"单文件模式的 service 标签（默认 {DEFAULT_SERVICE}）")
    parser.add_argument("--positions", default=None, help=f"位点文件（默认 {DEFAULT_POSITIONS}）")
    parser.add_argument("--loki-url", default=None, help="AIOps Loki 地址（默认取 AIOPS_LOKI_URL）")
    parser.add_argument("--once", action="store_true", help="只采集一次后退出")
    parser.add_argument("--follow", action="store_true", help="持续采集（默认）")
    parser.add_argument("--from-start", action="store_true", help="忽略位点，从头读取（follow 下仅首轮）")
    parser.add_argument("--interval", type=float, default=2.0, help="follow 轮询间隔秒（默认 2.0）")
    parser.add_argument(
        "--audit-labels",
        action="store_true",
        help="按应用注册表核对采集 service 标签并输出 JSON（有问题时退出码 1）",
    )
    parser.add_argument(
        "--check-loki",
        action="store_true",
        help="配合 --audit-labels：额外检查近 60 分钟 Loki 是否有对应日志",
    )
    args = parser.parse_args()

    if args.audit_labels:
        report = audit_service_labels(
            check_loki=args.check_loki, loki_url=args.loki_url
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["ok"] else 1)

    positions = Path(args.positions) if args.positions else None
    source = Path(args.file).expanduser() if args.file else None

    if args.once:
        if source is not None:
            stats = ship_once(source, args.service or DEFAULT_SERVICE, positions, args.loki_url, from_start=args.from_start)
            print(
                f"采集完成：read={stats['read']} pushed={stats['pushed']} "
                f"by_level={stats['by_level']} offset={stats['offset']}"
            )
            return
        apps = load_apps()
        stats = ship_list(apps, positions, args.loki_url, from_start=args.from_start)
        print(f"清单采集完成：apps={stats['apps']} read={stats['read']} pushed={stats['pushed']}")
        for item in stats["results"]:
            print(
                f"  - {item['name']}（service={item['service']}）files={item['files']} "
                f"pushed={item['pushed']} {item['by_level']}"
            )
        return

    if source is None:
        print("持续采集（清单模式，每轮重读清单 → 改动热生效；Ctrl-C 退出）", flush=True)
    else:
        print(f"持续采集 {source} → service={args.service or DEFAULT_SERVICE}（Ctrl-C 退出）", flush=True)
    follow(source, args.service, positions, args.loki_url, interval=args.interval, from_start=args.from_start)


if __name__ == "__main__":
    main()
