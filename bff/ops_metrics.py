"""运营度量聚合（P1-4）：MTTR / 自动修复率 / 人工干预率 / 闸门拦截率。

数据面全部为既有读侧数据，不新增写路径：
    - Temporal 流程列表（``gw.list_flows``）：终态、时长（``duration_seconds``）、
      ``gate_events`` 闸门事件链（P1-3 起由 ``_item_from_result`` 透出）；
    - 操作审计（``audit.read_audit``）：审批 / 发布指令 / 排队补丁 / 转人工处置；
    - 转人工待办（``escalations.stats``）：处置闭环现状（P1-3）。

口径（前端提示与本模块注释保持同一份定义）：
    - 窗口：终态流程的 ``close_time`` 落在最近 ``days`` 天（默认 7，1–30 可调）；
    - MTTR：DONE 流程 ``duration_seconds`` 均值与 P95（告警触发 → 发布完成）；
    - 自动修复率：DONE / 全部终态；
    - 人工干预率：被人类写操作（审批 / 指令 / 排队 / 转人工处置）触及的终态流程占比；
    - 闸门拦截率：``gate_events`` 命中拦截族（低置信度 / 测试回炉 / 审批驳回超时 /
      金丝雀回滚）的终态流程占比。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

# 闸门拦截事件族（前缀 → 展示标签）：
# 计数按「流程」去重（一条流程命中多族时各族分别计），与拦截率分子口径一致。
GATE_FAMILIES: list[tuple[str, str]] = [
    ("gate1:blocked", "闸门1：低置信度转人工"),
    ("tests:failed", "沙箱：测试拦截回炉"),
    ("gate2:reject", "闸门2：一级审批驳回"),
    ("gate2:timeout", "闸门2：一级审批超时"),
    ("gate2b:reject", "闸门2：二级审批驳回"),
    ("gate2b:timeout", "闸门2：二级审批超时"),
    ("canary:degraded", "金丝雀：劣化自动回滚"),
]

# 人工写操作（审计 action 前缀）——触及即计入「人工干预」。
# 与 bff/routes/*.py 的 write_audit action 命名逐一对齐：
#   approval:* / second-approval:* / deploy-command:* / queue-patch / escalation:*
INTERVENTION_PREFIXES = (
    "approval:",
    "second-approval:",
    "deploy-command:",
    "queue-patch",
    "escalation:",
)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _p95(values: list[float]) -> float | None:
    """最近秩法 P95（样本足够小，不做插值）。"""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return round(ordered[index], 1)


def compute(
    items: list[dict],
    audit_rows: list[dict],
    esc_stats: dict | None = None,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> dict:
    """聚合运营度量（纯函数，便于单测）。

    ``items``：``gw.list_flows`` 输出（含已关闭流程的 stage/duration/gate_events）；
    ``audit_rows``：``audit.read_audit`` 输出（最新在前）；
    ``esc_stats``：``escalations.stats`` 输出（不可读时传 None）。
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current - timedelta(days=days)

    closed: list[dict] = []
    for item in items or []:
        if item.get("exec_status") == "RUNNING":
            continue
        close_time = _parse_ts(item.get("close_time"))
        if close_time is None or close_time < cutoff:
            continue
        closed.append(item)

    total = len(closed)
    by_stage = Counter(str(i.get("stage") or "UNKNOWN") for i in closed)

    # ---- MTTR：DONE 流程时长均值与 P95 ----
    durations = [
        float(i["duration_seconds"])
        for i in closed
        if i.get("stage") == "DONE"
        and isinstance(i.get("duration_seconds"), (int, float))
    ]
    mttr = {
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "p95_seconds": _p95(durations),
        "sample": len(durations),
    }

    # ---- 自动修复率 ----
    auto_fix_sample = by_stage.get("DONE", 0)
    auto_fix_rate = round(auto_fix_sample / total, 4) if total else 0.0

    # ---- 人工干预率：审计动作触及的窗口终态流程 ----
    closed_ids = {str(i.get("wf_id")) for i in closed}
    intervened = {
        str(row.get("wf_id"))
        for row in audit_rows or []
        if str(row.get("wf_id") or "") in closed_ids
        and str(row.get("action") or "").startswith(INTERVENTION_PREFIXES)
    }
    human_intervention_rate = round(len(intervened) / total, 4) if total else 0.0

    # ---- 闸门拦截率与分档 ----
    blocked_ids: set[str] = set()
    family_counts: Counter = Counter()
    for item in closed:
        events = [str(e) for e in item.get("gate_events") or []]
        hit = False
        for prefix, _label in GATE_FAMILIES:
            if any(e.startswith(prefix) for e in events):
                family_counts[prefix] += 1
                hit = True
        if hit:
            blocked_ids.add(str(item.get("wf_id")))
    gate_block_rate = round(len(blocked_ids) / total, 4) if total else 0.0

    return {
        "period_days": days,
        "closed_total": total,
        "by_stage": dict(by_stage),
        "mttr": mttr,
        "auto_fix_rate": auto_fix_rate,
        "auto_fix_sample": auto_fix_sample,
        "human_intervention_rate": human_intervention_rate,
        "human_intervention_sample": len(intervened),
        "gate_block_rate": gate_block_rate,
        "gate_block_sample": len(blocked_ids),
        "gate_breakdown": [
            {"key": prefix, "label": label, "count": family_counts.get(prefix, 0)}
            for prefix, label in GATE_FAMILIES
        ],
        "escalations": esc_stats or None,
    }
