#!/usr/bin/env python3
"""需求基线主动分析 CLI（被监控系统「需求收集与反馈」→ AIOps 主动分析）。

从被监控系统的标准导出接口（``GET /api/requirements/export``，见
:mod:`aiops_agent.requirements_client`）拉取「已由管理员评估并纳入基线」的
需求 / 建议条目，做本地分析汇总（类型 / 优先级 / 版本分布、P0/P1 高优提示），
产出 Markdown 报告或 ``--json`` 机器可读输出，供 AIOps 侧做主动需求分析
（优先级研判 / 实现建议 / 排期参考）。

用法::

    .venv/bin/python analyze_requirements.py --url http://localhost:3000
    .venv/bin/python analyze_requirements.py --url localhost:3000 --since 2026-10-01 --json

令牌：``--token`` 或环境变量 ``NL2SQL_OPS_TOKEN``（值 = 被监控系统的 OPS_API_TOKEN）。

退出码：拉取 + 分析成功为 0，否则 1（失败时不写报告文件）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiops_agent import requirements_client  # noqa: E402 - 需先补齐 sys.path

DEFAULT_URL = "http://localhost:3000"
DEFAULT_OUT = "data/requirements_report.md"
_HIGH_PRIORITIES = ("P0", "P1")
_CELL_MAX = 80


def _normalize_url(raw: str) -> str:
    """无 scheme 时默认补 ``http://``，并去掉尾斜杠。"""
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


def summarize(entries: list[dict]) -> dict:
    """本地分析汇总：类型 / 优先级 / 版本分布 + 高优条目 + 增量游标。"""
    by_kind = Counter(str(item.get("kind") or "UNKNOWN") for item in entries)
    by_priority = Counter(str(item.get("priority") or "未定级") for item in entries)
    by_version = Counter(str(item.get("baselineVersion") or "未指定") for item in entries)
    high = [
        {
            "id": item.get("id"),
            "priority": str(item.get("priority") or ""),
            "title": str(item.get("title") or ""),
            "baselineVersion": str(item.get("baselineVersion") or ""),
        }
        for item in entries
        if str(item.get("priority") or "") in _HIGH_PRIORITIES
    ]
    high.sort(
        key=lambda item: (
            item["priority"],
            item["id"] if isinstance(item["id"], int) else 0,
        )
    )
    latest = ""
    for item in entries:
        updated = str(item.get("updatedAt") or "")
        if updated > latest:
            latest = updated
    return {
        "total": len(entries),
        "byKind": dict(sorted(by_kind.items())),
        "byPriority": dict(sorted(by_priority.items())),
        "byBaselineVersion": dict(sorted(by_version.items())),
        "highPriority": high,
        "latestUpdatedAt": latest,
    }


def _cell(value: object) -> str:
    """Markdown 表格单元格：换行 / 竖线转义，超长截断。"""
    text = str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= _CELL_MAX else text[: _CELL_MAX - 1] + "…"


def _distribution(counter_like: dict) -> str:
    return "；".join(f"{key} {value}" for key, value in counter_like.items()) or "（无）"


def render_markdown(result: dict, summary: dict) -> str:
    """渲染 Markdown 分析报告（分发 / 归档用）。"""
    entries = result.get("entries") or []
    lines: list[str] = [
        "# 需求基线主动分析报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 数据源：`{result.get('url')}`",
        f"- 服务：`{result.get('service') or '未知'}`（{result.get('system') or '未报告名称'}）",
        f"- 导出时点（exportedAt）：{result.get('exported_at') or '未报告'}",
        f"- 条目数：{summary['total']}（服务端 returned={result.get('returned')}）",
        "",
        "> 增量提示：下次拉取可将 `since` 设为本次 exportedAt（`>=` 语义），只取更新条目。",
        "",
        "## 一、分布汇总",
        "",
        "| 维度 | 分布 |",
        "| --- | --- |",
        f"| 类型 | {_distribution(summary['byKind'])} |",
        f"| 优先级 | {_distribution(summary['byPriority'])} |",
        f"| 基线版本 | {_distribution(summary['byBaselineVersion'])} |",
        "",
        "## 二、高优先级条目（P0/P1）",
        "",
    ]
    if summary["highPriority"]:
        for item in summary["highPriority"]:
            version = f"（{item['baselineVersion']}）" if item["baselineVersion"] else ""
            lines.append(f"- `#{item['id']}` **[{item['priority']}] {_cell(item['title'])}**{version}")
    else:
        lines.append("- （无）")
    lines += [
        "",
        "## 三、需求条目清单",
        "",
        "| ID | 类型 | 优先级 | 标题 | 基线版本 | 提交人 | 提交部门 | 评估意见 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in entries:
        cells = (
            _cell(item.get("id")),
            _cell(item.get("kind")),
            _cell(item.get("priority")),
            _cell(item.get("title")),
            _cell(item.get("baselineVersion")),
            _cell(item.get("submitter")),
            _cell(item.get("department")),
            _cell(item.get("assessment")),
        )
        lines.append("| " + " | ".join(cells) + " |")
    if not entries:
        lines.append("| - | - | - | （无基线条目） | - | - | - | - |")
    lines.append("")
    return "\n".join(lines)


def run_analyze(
    base_url: str,
    *,
    token: str = "",
    status: str = "BASELINED",
    kind: str = "",
    since: str = "",
    limit: int = 200,
    out: str = DEFAULT_OUT,
    as_json: bool = False,
    timeout: float = requirements_client.DEFAULT_TIMEOUT,
) -> int:
    result = requirements_client.fetch_requirements(
        base_url,
        status=status,
        kind=kind or None,
        since=since or None,
        limit=limit,
        token=token or None,
        timeout=timeout,
    )

    if as_json:
        if not result["ok"]:
            print(
                json.dumps(
                    {"ok": False, "url": result["url"], "error": result["error"]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        payload = {
            "ok": True,
            "url": result["url"],
            "service": result["service"],
            "system": result["system"],
            "exportedAt": result["exported_at"],
            "filter": result["filter"],
            "returned": result["returned"],
            "summary": summarize(result["entries"]),
            "entries": result["entries"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("== 需求基线主动分析 ==")
    print(f"目标：{base_url}")
    print(f"过滤：status={status}，kind={kind or '不限'}，since={since or '不限'}，limit={limit}\n")

    if not result["ok"]:
        print(f"[1/2] 拉取 {requirements_client.REQUIREMENTS_PATH}  ✗ 未通过")
        print(f"        原因：{result['error'] or '未知'}")
        print()
        print("结果：拉取失败，未产出报告。")
        return 1

    detail = f"returned={result['returned']}, spec_version={result['spec_version']}"
    if result["exported_at"]:
        detail += f", exportedAt={result['exported_at']}"
    print(f"[1/2] 拉取 {requirements_client.REQUIREMENTS_PATH}  ✓ 200（{detail}）")
    for warn in result["warnings"]:
        print(f"        ! {warn}")

    summary = summarize(result["entries"])
    print(f"[2/2] 本地分析  ✓ {summary['total']} 条基线条目")
    print(f"        类型分布：{_distribution(summary['byKind'])}")
    print(f"        优先级分布：{_distribution(summary['byPriority'])}")
    if summary["highPriority"]:
        high_desc = "；".join(
            f"#{item['id']} [{item['priority']}] {item['title']}"
            for item in summary["highPriority"]
        )
        print(f"        高优提示（P0/P1）：{high_desc}")
    else:
        print("        高优提示（P0/P1）：无")

    out_path = Path(out).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_markdown(result, summary), encoding="utf-8")
    except OSError as exc:
        print(f"        报告写入失败：{exc}")
        print()
        print("结果：拉取 + 分析完成，但报告未落盘。")
        return 1
    print(f"        报告已写入：{out_path}")

    print()
    high_note = f"，高优待关注 {len(summary['highPriority'])} 条" if summary["highPriority"] else ""
    print(f"结果：分析完成（{summary['total']} 条基线条目{high_note}）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="需求基线主动分析（拉取基线条目 → 本地分析 → 报告 / JSON）"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"被监控系统根地址（无 scheme 时默认 http://；缺省 {DEFAULT_URL}）",
    )
    parser.add_argument("--token", default="", help="OPS_API_TOKEN（缺省读环境变量 NL2SQL_OPS_TOKEN）")
    parser.add_argument(
        "--status",
        default="BASELINED",
        help="条目状态过滤：BASELINED（默认）/ PENDING / REJECTED / ALL",
    )
    parser.add_argument("--kind", default="", help="类型过滤：REQUIREMENT / SUGGESTION / BUG / OTHER（缺省不限）")
    parser.add_argument(
        "--since",
        default="",
        help='增量起点（按 updated_at >= 过滤），如 "2026-10-01" 或 "2026-10-01 08:00"',
    )
    parser.add_argument("--limit", type=int, default=200, help="返回条数上限 1-500（默认 200）")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"Markdown 报告输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--json", action="store_true", help="机器可读模式：stdout 输出 JSON（不写报告文件）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=requirements_client.DEFAULT_TIMEOUT,
        help=f"单次请求超时秒（默认 {requirements_client.DEFAULT_TIMEOUT}）",
    )
    args = parser.parse_args()
    return run_analyze(
        _normalize_url(args.url),
        token=args.token,
        status=args.status,
        kind=args.kind,
        since=args.since,
        limit=args.limit,
        out=args.out,
        as_json=args.json,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
