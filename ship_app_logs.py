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
import json
import os
import re
import time
from pathlib import Path

from aiops_agent import logs

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_POSITIONS = BASE_DIR / "data" / "log_ship_positions.json"
DEFAULT_SERVICE = os.environ.get("AIOPS_MONITOR_SERVICE", "nl2sql")

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


def ship_once(
    file_path: Path,
    service: str = DEFAULT_SERVICE,
    positions_path: Path | None = None,
    loki_url: str | None = None,
    from_start: bool = False,
) -> dict:
    """增量采集一次：读新增 → 按级别分组 → 推送到 Loki → 落位点。返回统计。"""
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

    pushed = 0
    for level, batch in grouped.items():
        pushed += logs.push_lines(service, level, batch, loki_url=loki_url)

    if raw_lines:
        positions[key] = new_offset
        save_positions(positions_path, positions)

    return {
        "file": key,
        "service": service,
        "read": len(raw_lines),
        "pushed": pushed,
        "by_level": {level: len(batch) for level, batch in grouped.items()},
        "offset": new_offset,
        "pending_partial": bool(remainder),
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
        if poll_once:
            return
        time.sleep(interval)


def main() -> None:
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
    args = parser.parse_args()

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
