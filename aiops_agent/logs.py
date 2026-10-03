"""日志管道工具（WP2 交付物）：Loki 客户端 + Drain3 模板聚类降噪。

职责（对齐实施计划 WP2）：
    - push_lines：向 Loki 推送日志（本演示中以脚本模拟 Filebeat 采集角色；
      生产环境替换为 Filebeat / Fluent Bit → Loki 的真实管道，查询接口不变）；
    - query_lines / query_metric：Loki 查询（供证据采集与突增检测使用）；
    - cluster_lines：Drain3 模板聚类，同模板海量重复日志聚合为「模板 + 计数」
      （聚类在采集侧完成，压缩后日志量可控，避免淹没 LLM 上下文）；
    - is_surge：错误日志突增判定的纯函数（环比基线 + 绝对门槛，防低流量误报）。

日志字段规范（trace_id 跨服务关联）：
    - stream labels：service、level（error/warn/info）；
    - 日志行内携带 trace_id=<id> 字段，供调用链关联与根因上下文。

环境变量：
    AIOPS_LOKI_URL   Loki 地址（默认 http://localhost:3101，避开该栈 Grafana 的 3100）
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

DEFAULT_LOKI_URL = os.environ.get("AIOPS_LOKI_URL", "http://localhost:3101")

_SAFE_LABEL = re.compile(r"^[\w\-.:]+$")

# 聚类掩码：(正则, 掩码名)，先掩 trace_id，再掩十六进制长串，最后掩纯数字（顺序不可颠倒）
_MASKING = [
    (r"trace_id=[\w\-]+", "TRACEID"),
    (r"\b[0-9a-f]{8,}\b", "HEX"),
    (r"\b\d+\b", "NUM"),
]


def _loki_url(loki_url: str | None = None) -> str:
    return (loki_url or DEFAULT_LOKI_URL).rstrip("/")


def _check_label(value: str, what: str) -> str:
    if not value or not _SAFE_LABEL.match(value):
        raise ValueError(f"非法 {what}: {value!r}（仅允许字母/数字/._-:）")
    return value


def push_lines(
    service: str,
    level: str,
    lines: list[str],
    loki_url: str | None = None,
) -> int:
    """推送一批日志行到 Loki（同一 stream 内时间递增），返回推送行数。"""
    _check_label(service, "service")
    _check_label(level, "level")
    if not lines:
        return 0
    base_ns = time.time_ns()
    values = [[str(base_ns + i), line] for i, line in enumerate(lines)]
    payload = {"streams": [{"stream": {"service": service, "level": level}, "values": values}]}
    req = urllib.request.Request(
        f"{_loki_url(loki_url)}/loki/api/v1/push",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Loki push 失败: HTTP {resp.status}")
    return len(lines)


def query_lines(
    service: str,
    lookback_minutes: int = 10,
    limit: int = 3000,
    level: str | None = None,
    loki_url: str | None = None,
) -> list[str]:
    """查询近 N 分钟该 service 的日志行（时间升序），最多 limit 行。"""
    _check_label(service, "service")
    if level:
        _check_label(level, "level")
    end = time.time_ns()
    start = end - lookback_minutes * 60 * 1_000_000_000
    selector = f'{{service="{service}"' + (f', level="{level}"' if level else "") + "}"
    params = urllib.parse.urlencode(
        {
            "query": selector,
            "start": str(start),
            "end": str(end),
            "limit": str(limit),
            "direction": "backward",
        }
    )
    data = _get_json(f"{_loki_url(loki_url)}/loki/api/v1/query_range?{params}")
    rows: list[tuple[int, str]] = []
    for stream in data.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            rows.append((int(ts), line))
    rows.sort(key=lambda item: item[0])
    return [line for _, line in rows]


def query_metric(query: str, at_ns: int | None = None, loki_url: str | None = None) -> float:
    """Loki instant 指标查询（如 count_over_time），返回各序列求和后的标量（无结果为 0）。"""
    params = {"query": query}
    if at_ns is not None:
        params["time"] = str(at_ns)
    data = _get_json(f"{_loki_url(loki_url)}/loki/api/v1/query?{urllib.parse.urlencode(params)}")
    total = 0.0
    for series in data.get("data", {}).get("result", []):
        value = series.get("value", [None, "0"])
        total += float(value[1])
    return total


def is_surge(recent: float, baseline: float, min_lines: float, factor: float) -> bool:
    """突增判定：绝对门槛（防低流量误报）+ 环比倍数；基线为 0 时过门槛即算突增。"""
    if recent < min_lines:
        return False
    if baseline <= 0:
        return True
    return recent > factor * baseline


def cluster_lines(lines: list[str], depth: int = 4, sim_threshold: float = 0.5) -> dict:
    """Drain3 模板聚类：同模板重复日志聚合为「模板 + 计数」。

    返回 {"templates": [{"id","pattern","count"}...]（按 count 降序）,
          "compression_ratio": 模板数/总行数, "total_lines": N}
    """
    from drain3 import TemplateMiner
    from drain3.masking import MaskingInstruction
    from drain3.template_miner_config import TemplateMinerConfig

    config = TemplateMinerConfig()
    config.drain_depth = depth
    config.drain_sim_th = sim_threshold
    config.masking_instructions = [MaskingInstruction(p, name) for p, name in _MASKING]
    miner = TemplateMiner(config=config)
    for line in lines:
        miner.add_log_message(line)

    clusters = sorted(miner.drain.clusters, key=lambda c: c.size, reverse=True)
    templates = [
        {"id": f"tpl-{i + 1}", "pattern": cluster.get_template(), "count": cluster.size}
        for i, cluster in enumerate(clusters)
    ]
    total = len(lines)
    return {
        "templates": templates,
        "compression_ratio": round(len(templates) / total, 4) if total else 0.0,
        "total_lines": total,
    }


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)
