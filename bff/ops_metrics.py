"""运营度量聚合（P1-4 / M5）：口径唯一、空样本一律 ``null``。

数据面全部为既有读侧数据，不新增写路径：
    - 流程列表（``gw.list_flows`` / production 的 ``workflow_runs`` 表）：终态、时长、
      ``kind``（告警/需求）、``patch.risk``、``test_report`` 与 ``gate_events`` 事件链；
    - 操作审计（``audit.read_audit`` / ``audit_events`` 表）：审批、发布指令、排队、转人工处置；
    - 转人工待办（``escalations.stats`` 与待办条目）：处置闭环、SLA、关闭原因、知识状态；
    - 证据归档（``log_evidence`` 表 / 文件后端）：六状态证据质量；
    - 采集心跳（``aiops_agent.log_health``）：按服务的采集新鲜度与覆盖率。

口径（前端提示与本模块注释保持同一份定义）：
    - 窗口：终态流程的 ``close_time`` 落在最近 ``days`` 天（默认 7，1–30 可调）；
    - MTTR：DONE 流程 ``duration_seconds`` 均值与 P95（告警触发 → 发布完成）；
    - MTTA：人工介入的终态流程，``start_time`` → 首个介入动作（审批/指令/排队/处置）的均值与 P95；
    - 自动完成率：DONE / 同类型（kind）全部终态；
    - 人工干预率：被人类写操作触及的终态流程占比；
    - 闸门拦截率：``gate_events`` 命中拦截族的终态流程占比；
    - 回滚率：命中 ``canary:degraded`` 的流程 / 进入金丝雀（healthy 或 degraded）的流程；
    - 真实沙箱通过率：仅 ``UNIT_TEST``；``CONTRACT_TEST`` 与 ``STATIC_ONLY`` 单列，不并入；
    - 证据充分率：证据质量为 ``OK`` 的归档流程占比；观测类异常单独统计，
      不得计入「模型低置信」；
    - 空样本（分母为 0）的比率一律返回 ``null``，由前端显示「暂无数据」，
      不得返回会被渲染成 0% 或 100% 的值。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

# 闸门拦截事件族（前缀 → 展示标签）：
# 计数按「流程」去重（一条流程命中多族时各族分别计），与拦截率分子口径一致。
GATE_FAMILIES: list[tuple[str, str]] = [
    ("gate1:blocked", "闸门1：低置信度转人工"),
    ("gate1:composite", "闸门1：组合判据拦截"),
    ("evidence:blocked", "证据预检：观测/证据不足转人工"),
    ("tests:failed", "沙箱：测试拦截回炉"),
    ("release:blocked:static-only", "发布准入：静态验收不足以放行"),
    ("gate2:reject", "闸门2：一级审批驳回"),
    ("gate2:timeout", "闸门2：一级审批超时"),
    ("gate2b:reject", "闸门2：二级审批驳回"),
    ("gate2b:timeout", "闸门2：二级审批超时"),
    ("canary:degraded", "金丝雀：劣化自动回滚"),
]

# 人工写操作（审计 action 前缀）——触及即计入「人工干预」。
# 与 bff/routes/*.py 的 write_audit action 命名逐一对齐：
#   approval:* / second-approval:* / deploy-command:* / queue-patch / escalation:*
# 需求流的批准发生在流程创建前，单独计入需求类干预。
INTERVENTION_PREFIXES = (
    "approval:",
    "second-approval:",
    "deploy-command:",
    "queue-patch",
    "escalation:",
    "requirement:approve",
    "requirement:resume-shadow",
)

KINDS = (("alert", "告警修复"), ("requirement", "需求修复"))
KIND_LABELS = dict(KINDS)

RISK_BUCKETS = (("high", "高风险"), ("medium", "中风险"), ("low", "低风险"), ("unknown", "未知"))
RISK_LABELS = dict(RISK_BUCKETS)

# 证据质量六状态（与 aiops_agent/evidence_quality.py 一致；UNKNOWN=旧归档/未采集）
EVIDENCE_STATUSES = (
    "OK",
    "LOKI_UNAVAILABLE",
    "NO_LOGS",
    "STALE_LOGS",
    "UNMAPPED_SERVICE",
    "INSUFFICIENT_SIGNAL",
    "UNKNOWN",
)
EVIDENCE_LABELS = {
    "OK": "证据充分",
    "LOKI_UNAVAILABLE": "Loki 不可用",
    "NO_LOGS": "窗口内无日志",
    "STALE_LOGS": "日志过期",
    "UNMAPPED_SERVICE": "服务标签未映射",
    "INSUFFICIENT_SIGNAL": "日志信号不足",
    "UNKNOWN": "未采集/历史归档",
}
# 观测面异常：不是模型判断错误，不得计入模型低置信指标
OBSERVABILITY_STATUSES = frozenset({"LOKI_UNAVAILABLE", "NO_LOGS", "STALE_LOGS", "UNMAPPED_SERVICE"})

SANDBOX_MODES = ("UNIT_TEST", "CONTRACT_TEST", "STATIC_ONLY", "UNKNOWN")
SANDBOX_MODE_LABELS = {
    "UNIT_TEST": "真实单元测试",
    "CONTRACT_TEST": "声明契约（静态）",
    "STATIC_ONLY": "无套件静态验收",
    "UNKNOWN": "历史/未知口径",
}

# 连续失败/回滚的信号阈值（值班告警入口，不改业务流程判定）
DUTY_STREAK_THRESHOLD = 3
DUTY_STREAK_WINDOW = 5
RUNBOOK_PATH = "docs/AIOps 值班 Runbook.md"

CLOSE_REASON_LABELS = {
    "RESOLVED": "已解决",
    "FALSE_POSITIVE": "误报",
    "OBSERVABILITY_FIXED": "观测修复",
    "DUPLICATE": "重复",
    "NO_CODE_CHANGE": "无需改代码",
    "OTHER": "其他",
    "unstructured": "未结构化关闭",
}


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


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    """分母为 0 时返回 None（前端显示「暂无数据」），不返回误导性的 0.0。"""
    return round(numerator / denominator, 4) if denominator else None


def _kind_of(item: dict) -> str:
    return "requirement" if item.get("kind") == "requirement" else "alert"


def _risk_text(item: dict) -> str:
    # SQL 数据面把风险镜像在 ``risk`` 列（历史行可能没有 result.patch），优先取列值
    if item.get("risk"):
        return str(item["risk"]).strip()
    patch = item.get("patch") or {}
    return str(patch.get("risk") or "").strip()


def _risk_bucket(text: str) -> str:
    lowered = text.lower()
    for token, bucket in (("高", "high"), ("high", "high"), ("中", "medium"), ("medium", "medium"),
                          ("低", "low"), ("low", "low")):
        if token in text or token in lowered:
            return bucket
    return "unknown"


def _events(item: dict) -> list[str]:
    return [str(event) for event in item.get("gate_events") or []]


def _has_family(item: dict, prefix: str) -> bool:
    return any(event.startswith(prefix) for event in _events(item))


def _intervention_set(audit_rows: list[dict], closed_ids: set[str]) -> set[str]:
    return {
        str(row.get("wf_id"))
        for row in audit_rows or []
        if str(row.get("wf_id") or "") in closed_ids
        and str(row.get("action") or "").startswith(INTERVENTION_PREFIXES)
    }


def _window(items: list[dict], days: int, now: datetime) -> list[dict]:
    current = now
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
    return closed


def _block(closed: list[dict], intervened: set[str]) -> dict:
    """单一口径块：自动完成率 / 人工干预率 / MTTR。"""
    total = len(closed)
    done = [item for item in closed if item.get("stage") == "DONE"]
    durations = [
        float(item["duration_seconds"])
        for item in done
        if isinstance(item.get("duration_seconds"), (int, float))
    ]
    touched = [item for item in closed if str(item.get("wf_id")) in intervened]
    return {
        "closed": total,
        "done": len(done),
        "auto_fix_rate": _rate(len(done), total),
        "human_intervention_rate": _rate(len(touched), total),
        "human_intervention_sample": len(touched),
        "mttr": {
            "avg_seconds": _avg(durations),
            "p95_seconds": _p95(durations),
            "sample": len(durations),
        },
    }


def _mtta(closed: list[dict], audit_rows: list[dict]) -> dict:
    """MTTA：告警触发 → 首个人工动作（仅统计有介入且时间可解析的流程）。"""
    started = {
        str(item.get("wf_id")): _parse_ts(item.get("start_time")) for item in closed
    }
    first: dict[str, datetime] = {}
    for row in audit_rows or []:
        if not str(row.get("action") or "").startswith(INTERVENTION_PREFIXES):
            continue
        wf_id = str(row.get("wf_id") or "")
        ts = _parse_ts(row.get("ts"))
        if wf_id not in started or started[wf_id] is None or ts is None:
            continue
        if wf_id not in first or ts < first[wf_id]:
            first[wf_id] = ts
    deltas = [
        max(0.0, (stamp - started[wf_id]).total_seconds())  # type: ignore[operator]
        for wf_id, stamp in first.items()
        if started.get(wf_id) is not None and stamp >= started[wf_id]  # type: ignore[operator]
    ]
    return {"avg_seconds": _avg(deltas), "p95_seconds": _p95(deltas), "sample": len(deltas)}


def _rollback(closed: list[dict]) -> dict:
    degraded = sum(1 for item in closed if _has_family(item, "canary:degraded"))
    released = sum(
        1
        for item in closed
        if _has_family(item, "canary:degraded") or _has_family(item, "canary:healthy")
    )
    return {"rate": _rate(degraded, released), "sample": degraded, "released": released}


def _by_service(closed: list[dict], intervened: set[str]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for item in closed:
        service = str((item.get("alert") or {}).get("service") or "unknown")
        buckets.setdefault(service, []).append(item)
    rows = []
    for service, items in buckets.items():
        done = sum(1 for item in items if item.get("stage") == "DONE")
        touched = sum(1 for item in items if str(item.get("wf_id")) in intervened)
        rows.append(
            {
                "service": service,
                "closed": len(items),
                "done": done,
                "auto_fix_rate": _rate(done, len(items)),
                "human_intervention_rate": _rate(touched, len(items)),
            }
        )
    rows.sort(key=lambda row: (-row["closed"], row["service"]))
    return rows[:10]


def _evidence(evidence_rows: list[dict] | None) -> dict:
    """证据质量分布：只看归档事实，缺数据时 available=False 而不是零值。"""
    if evidence_rows is None:
        return {"available": False, "sample": 0, "ok_rate": None, "by_status": [], "observability_share": None}
    statuses = Counter()
    for row in evidence_rows:
        quality = row.get("quality") if isinstance(row, dict) else None
        status = (
            row.get("quality_status")
            if isinstance(row, dict) and row.get("quality_status")
            else (quality or {}).get("status") if isinstance(quality, dict) else None
        )
        statuses[str(status or "UNKNOWN")] += 1
    total = sum(statuses.values())
    ok = statuses.get("OK", 0)
    observability = sum(statuses.get(status, 0) for status in OBSERVABILITY_STATUSES)
    return {
        "available": True,
        "sample": total,
        "ok_rate": _rate(ok, total),
        "observability_share": _rate(observability, total),
        "by_status": [
            {
                "key": status,
                "label": EVIDENCE_LABELS.get(status, status),
                "count": statuses.get(status, 0),
                "observability": status in OBSERVABILITY_STATUSES,
            }
            for status in EVIDENCE_STATUSES
        ],
    }


def _logship(log_ship: list[dict] | None, managed_services: list[str] | None) -> dict:
    """采集新鲜度与覆盖率：无心跳时 available=False（不得显示「正常」）。"""
    if not log_ship:
        return {
            "available": False,
            "services": 0,
            "healthy": 0,
            "unhealthy": 0,
            "freshness_p95_seconds": None,
            "coverage": None,
            "unhealthy_services": [],
        }
    ages = [float(row["age_seconds"]) for row in log_ship if isinstance(row.get("age_seconds"), (int, float))]
    bad = [row for row in log_ship if not row.get("healthy")]
    covered = {str(row.get("service") or "") for row in log_ship} - {""}
    if managed_services is None:
        coverage = None
    else:
        total = len({str(service) for service in managed_services if str(service).strip()})
        hit = len(covered & {str(service) for service in managed_services})
        coverage = {"covered": hit, "total": total, "rate": _rate(hit, total)}
    return {
        "available": True,
        "services": len(log_ship),
        "healthy": len(log_ship) - len(bad),
        "unhealthy": len(bad),
        "freshness_p95_seconds": _p95(ages),
        "coverage": coverage,
        "unhealthy_services": sorted(str(row.get("service") or "unknown") for row in bad)[:10],
    }


def _sandbox(closed: list[dict], esc_entries: list[dict] | None) -> dict:
    reports = [item.get("test_report") or {} for item in closed if item.get("test_report")]
    modes = Counter()
    passed = Counter()
    for report in reports:
        mode = str(report.get("verification_mode") or "UNKNOWN").upper()
        if mode not in SANDBOX_MODES:
            mode = "UNKNOWN"
        modes[mode] += 1
        if report.get("passed"):
            passed[mode] += 1
    real_total = modes.get("UNIT_TEST", 0)
    real_pass = passed.get("UNIT_TEST", 0)

    patches = [item.get("patch") for item in closed if item.get("patch")]
    applicable = sum(
        1
        for patch in patches
        if str((patch or {}).get("diff") or "").strip() and (patch or {}).get("files")
    )

    verdicts = Counter()
    for entry in esc_entries or []:
        resolution = entry.get("resolution") or {}
        verdict = str(resolution.get("verdict") or "")
        if verdict:
            verdicts[verdict] += 1
    judged = sum(verdicts.values())

    # 高置信误放行：曾通过组合判据（gate1:composite:pass）且置信度 ≥0.8，最终仍转人工
    false_pass = sum(
        1
        for item in closed
        if item.get("stage") == "ESCALATED"
        and isinstance(item.get("confidence"), (int, float))
        and float(item["confidence"]) >= 0.8
        and any(event.startswith("gate1:composite:pass") for event in _events(item))
    )

    return {
        "available": bool(reports),
        "sample": len(reports),
        "by_mode": [
            {
                "key": mode,
                "label": SANDBOX_MODE_LABELS[mode],
                "count": modes.get(mode, 0),
                "passed": passed.get(mode, 0),
                "is_real": mode == "UNIT_TEST",
            }
            for mode in SANDBOX_MODES
        ],
        "real_pass_rate": _rate(real_pass, real_total),
        "real_sample": real_total,
        "static_count": modes.get("STATIC_ONLY", 0),
        "contract_count": modes.get("CONTRACT_TEST", 0),
        "unknown_count": modes.get("UNKNOWN", 0),
        # 结构可应用率：patch 具备 diff 与文件清单；真实 git apply 结果由发布阶段判定
        "patch_structural_rate": _rate(applicable, len(patches)),
        "patch_structural_sample": len(patches),
        "override_rate": _rate(verdicts.get("INCORRECT", 0), judged),
        "override_sample": judged,
        "high_confidence_false_pass": false_pass,
    }


def _escalation_detail(
    esc_stats: dict | None, esc_entries: list[dict] | None, *, now: datetime
) -> dict:
    entries = esc_entries
    if entries is None:
        return {
            "available": False,
            "open_sample": 0,
            "open_avg_seconds": None,
            "open_p95_seconds": None,
            "handle_avg_seconds": None,
            "sla_minutes": (esc_stats or {}).get("sla_minutes"),
            "sla_breach": None,
            "close_reasons": [],
            "knowledge": (esc_stats or {}).get("knowledge"),
        }
    sla_minutes = int((esc_stats or {}).get("sla_minutes") or 0)
    open_ages: list[float] = []
    handle: list[float] = []
    breach = 0
    reasons = Counter()
    for entry in entries:
        escalated = _parse_ts(entry.get("escalated_at"))
        status = str(entry.get("status") or "open")
        if status == "closed":
            closed_at = _parse_ts(entry.get("closed_at"))
            if escalated and closed_at and closed_at >= escalated:
                handle.append((closed_at - escalated).total_seconds())
            resolution = entry.get("resolution") or {}
            reasons[str(resolution.get("close_reason") or "unstructured")] += 1
        elif escalated:
            age = max(0.0, (now - escalated).total_seconds())
            open_ages.append(age)
            if sla_minutes and age > sla_minutes * 60:
                breach += 1
    knowledge = (esc_stats or {}).get("knowledge") or {}
    return {
        "available": True,
        "open_sample": len(open_ages),
        "open_avg_seconds": _avg(open_ages),
        "open_p95_seconds": _p95(open_ages),
        "handle_avg_seconds": _avg(handle),
        "sla_minutes": sla_minutes,
        "sla_breach": breach,
        "close_reasons": [
            {"key": key, "label": CLOSE_REASON_LABELS.get(key, key), "count": count}
            for key, count in reasons.most_common()
        ],
        "knowledge": {
            "draft": int(knowledge.get("draft") or 0),
            "approved": int(knowledge.get("approved") or 0),
            "rejected": int(knowledge.get("rejected") or 0),
            # 采用情况需索引重建后回填，数据面暂无信号：返回 None 而不是 0
            "adopted": None,
        },
    }


def _consecutive(closed: list[dict], prefix: str, limit: int = DUTY_STREAK_WINDOW) -> int:
    """按 close_time 倒序统计连续命中某事件族的流程数（用于值班告警）。"""
    ordered = sorted(
        closed,
        key=lambda item: _parse_ts(item.get("close_time")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:limit]
    streak = 0
    for item in ordered:
        if _has_family(item, prefix):
            streak += 1
        else:
            break
    return streak


def _duty_alerts(
    *,
    logship: dict,
    escalation_detail: dict,
    sandbox: dict,
    rollback: dict,
    closed: list[dict],
) -> list[dict]:
    """值班告警入口：数据不足时给 unknown，不给「正常」。"""
    alerts: list[dict] = []

    def add(code: str, label: str, level: str, value, threshold, note: str) -> None:
        alerts.append(
            {
                "code": code,
                "label": label,
                "level": level,
                "value": value,
                "threshold": threshold,
                "detail": note,
                "runbook": f"{RUNBOOK_PATH}#{code.replace('_', '-')}",
            }
        )

    if not logship["available"]:
        add("log-stall", "日志采集停滞", "unknown", None, "心跳新鲜度", "无采集心跳数据，无法判断")
    elif logship["unhealthy"]:
        add(
            "log-stall",
            "日志采集停滞",
            "danger" if logship["unhealthy"] >= DUTY_STREAK_THRESHOLD else "warn",
            logship["unhealthy"],
            f"不健康服务数 ≥{DUTY_STREAK_THRESHOLD}",
            "；".join(logship["unhealthy_services"]) or "存在不健康服务",
        )

    if escalation_detail["sla_breach"] is None:
        add("escalation-backlog", "待办积压", "unknown", None, "SLA 超时数", "待办存储不可读")
    elif escalation_detail["sla_breach"]:
        add(
            "escalation-backlog",
            "待办积压",
            "danger",
            escalation_detail["sla_breach"],
            "SLA 超时 0",
            f"超过 {escalation_detail['sla_minutes']} 分钟未处置",
        )

    streak = _consecutive(closed, "tests:failed")
    if streak >= DUTY_STREAK_THRESHOLD:
        add("sandbox-failure-streak", "沙箱连续失败", "danger", streak, f"连续 ≥{DUTY_STREAK_THRESHOLD}", "最近流程沙箱连续回炉")

    streak = _consecutive(closed, "gate1:blocked:confidence")
    if streak >= DUTY_STREAK_THRESHOLD:
        add("model-quality-streak", "模型连续低置信", "warn", streak, f"连续 ≥{DUTY_STREAK_THRESHOLD}", "最近流程连续触发置信度闸门")

    if rollback["sample"]:
        add(
            "rollback",
            "发布回滚",
            "warn",
            rollback["sample"],
            "窗口内 0",
            f"金丝雀劣化回滚 {rollback['sample']}/{rollback['released']}",
        )

    if sandbox["high_confidence_false_pass"]:
        add(
            "high-confidence-false-pass",
            "高置信误放行",
            "danger",
            sandbox["high_confidence_false_pass"],
            "0",
            "曾被组合判据放行但最终转人工",
        )
    return alerts


def compute(
    items: list[dict],
    audit_rows: list[dict],
    esc_stats: dict | None = None,
    *,
    days: int = 7,
    now: datetime | None = None,
    esc_entries: list[dict] | None = None,
    evidence_rows: list[dict] | None = None,
    log_ship: list[dict] | None = None,
    managed_services: list[str] | None = None,
    source: str = "temporal",
) -> dict:
    """聚合运营度量（纯函数，便于单测）。

    ``items``：流程列表（含终态 stage/kind/duration/gate_events/test_report/patch）；
    ``audit_rows``：审计行（最新在前）；
    ``esc_stats``：待办统计（不可读时 None）；
    ``esc_entries``：待办条目（用于 SLA / 关闭原因 / 改判率；None=不可读）；
    ``evidence_rows``：证据归档行（None=不可读，不是 0 条）；
    ``log_ship``：采集心跳快照；``managed_services``：受管服务清单（覆盖率分母）。
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    closed = _window(items, days, current)
    total = len(closed)
    by_stage = Counter(str(item.get("stage") or "UNKNOWN") for item in closed)
    intervened = _intervention_set(audit_rows, {str(item.get("wf_id")) for item in closed})

    overall = _block(closed, intervened)

    family_counts: Counter = Counter()
    blocked_ids: set[str] = set()
    for item in closed:
        hit = False
        for prefix, _label in GATE_FAMILIES:
            if _has_family(item, prefix):
                family_counts[prefix] += 1
                hit = True
        if hit:
            blocked_ids.add(str(item.get("wf_id")))

    by_kind = {}
    for kind, _label in KINDS:
        subset = [item for item in closed if _kind_of(item) == kind]
        by_kind[kind] = _block(subset, intervened)

    risk_counts = Counter(_risk_bucket(_risk_text(item)) for item in closed)
    risk_done = Counter(
        _risk_bucket(_risk_text(item)) for item in closed if item.get("stage") == "DONE"
    )
    by_risk = [
        {
            "key": bucket,
            "label": label,
            "closed": risk_counts.get(bucket, 0),
            "done": risk_done.get(bucket, 0),
            "auto_fix_rate": _rate(risk_done.get(bucket, 0), risk_counts.get(bucket, 0)),
        }
        for bucket, label in RISK_BUCKETS
    ]

    evidence = _evidence(evidence_rows)
    logship = _logship(log_ship, managed_services)
    sandbox = _sandbox(closed, esc_entries)
    escalation_detail = _escalation_detail(esc_stats, esc_entries, now=current)
    rollback = _rollback(closed)

    return {
        "period_days": days,
        "source": source,
        "closed_total": total,
        "by_stage": dict(by_stage),
        "mttr": overall["mttr"],
        "auto_fix_rate": overall["auto_fix_rate"],
        "auto_fix_sample": overall["done"],
        "human_intervention_rate": overall["human_intervention_rate"],
        "human_intervention_sample": overall["human_intervention_sample"],
        "mtta": _mtta(closed, audit_rows),
        "gate_block_rate": _rate(len(blocked_ids), total),
        "gate_block_sample": len(blocked_ids),
        "gate_breakdown": [
            {"key": prefix, "label": label, "count": family_counts.get(prefix, 0)}
            for prefix, label in GATE_FAMILIES
        ],
        "by_kind": by_kind,
        "by_risk": by_risk,
        "by_service": _by_service(closed, intervened),
        "rollback": rollback,
        "evidence": evidence,
        "logship": logship,
        "sandbox": sandbox,
        "escalations": esc_stats or None,
        "escalation_detail": escalation_detail,
        # 模型成本：未接入真实计量，返回 null（不得显示 0）
        "cost": {
            "available": False,
            "model_cost_usd": None,
            "note": "未接入模型成本计量；接入前不得以 0 展示",
        },
        "duty_alerts": _duty_alerts(
            logship=logship,
            escalation_detail=escalation_detail,
            sandbox=sandbox,
            rollback=rollback,
            closed=closed,
        ),
    }
