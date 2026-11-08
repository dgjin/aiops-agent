"""生产灰度放行检查（M6）：按上线阶段核对硬条件，只读不改配置。

阶段顺序（计划第九节第 3 步）：

    staging-shadow → production-shadow → single-service → limited-release → expand

输入为 ``/api/metrics/ops?days=7`` 的 JSON 快照；缺少字段一律记 ``unknown``，
不得当作通过。人工项（备份恢复 / Kill Switch / 值班 Runbook）必须显式
``--confirm``，否则结论只能是 ``pending``。

用法：

    curl -s -H "Authorization: Bearer $TOKEN" \
      'http://127.0.0.1:8600/api/metrics/ops?days=7' > /tmp/ops.json
    .venv/bin/python scripts/rollout_gate.py --stage single-service \
      --service demo-app --metrics /tmp/ops.json --quality-bar data/model_quality_bar.json \
      --confirm backup-restore,kill-switch,runbook

退出码：0 = allow；1 = block；2 = pending（或输入不具备）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGES = ("staging-shadow", "production-shadow", "single-service", "limited-release", "expand")
MIN_SAMPLES = 50
MIN_DAYS = 7
MIN_AUTO_FIX_RATE = 0.5
# M5：写命令 outbox 最老等待超过该值即视为积压（与 readiness 降级阈值同口径）
MAX_COMMAND_BACKLOG_SECONDS = 300
MANUAL_ITEMS = {
    "backup-restore": "MySQL 备份恢复演练通过",
    "kill-switch": "Kill Switch 生效演练通过",
    "runbook": "值班 Runbook 已交付并演练",
}


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _check(checks: list[dict], cid: str, label: str, status: str, value, threshold: str, detail: str = "") -> None:
    checks.append(
        {
            "id": cid,
            "label": label,
            "status": status,
            "value": value,
            "threshold": threshold,
            "detail": detail,
        }
    )


def _sample_check(checks: list[dict], metrics: dict) -> None:
    closed = _number(metrics.get("closed_total"))
    days = _number(metrics.get("period_days"))
    if closed is None or days is None:
        _check(checks, "sample", "有效告警样本", "unknown", None, f"≥{MIN_SAMPLES} 条 / ≥{MIN_DAYS} 天", "指标缺少 closed_total 或 period_days")
        return
    ok = closed >= MIN_SAMPLES and days >= MIN_DAYS
    _check(
        checks,
        "sample",
        "有效告警样本",
        "pass" if ok else "fail",
        {"closed_total": int(closed), "period_days": days},
        f"≥{MIN_SAMPLES} 条 / ≥{MIN_DAYS} 天",
        "样本不足必须延长影子期，不以时间到期豁免",
    )


def _evidence_check(checks: list[dict], metrics: dict) -> None:
    evidence = metrics.get("evidence") or {}
    rate = _number(evidence.get("ok_rate"))
    sample = _number(evidence.get("sample")) or 0
    if rate is None or sample <= 0:
        _check(checks, "evidence", "证据质量 OK 覆盖", "unknown", None, "100%", "证据分量不可读或样本为 0")
        return
    _check(
        checks,
        "evidence",
        "证据质量 OK 覆盖",
        "pass" if rate >= 1.0 else "fail",
        round(rate, 4),
        "100%（自动流程）",
        "观测面异常不得计入模型低置信",
    )


def _false_pass_check(checks: list[dict], metrics: dict) -> None:
    sandbox = metrics.get("sandbox") or {}
    value = _number(sandbox.get("high_confidence_false_pass"))
    if value is None:
        _check(checks, "false-pass", "高置信误放行", "unknown", None, "0", "缺少 sandbox.high_confidence_false_pass")
        return
    _check(
        checks,
        "false-pass",
        "高置信误放行",
        "pass" if value == 0 else "fail",
        int(value),
        "0",
        "组合判据放行但最终转人工的流程数",
    )


def _sandbox_metric_check(checks: list[dict], metrics: dict) -> None:
    """真实沙箱通过率与静态验收必须分列，否则口径不可用。"""
    sandbox = metrics.get("sandbox") or {}
    if "real_sample" not in sandbox or "static_count" not in sandbox:
        _check(checks, "sandbox-split", "真实沙箱与静态验收分列", "unknown", None, "分列统计", "缺少 real_sample/static_count")
        return
    real_sample = _number(sandbox.get("real_sample")) or 0
    rate = _number(sandbox.get("real_pass_rate"))
    if real_sample <= 0 or rate is None:
        _check(
            checks,
            "sandbox-split",
            "真实沙箱与静态验收分列",
            "unknown",
            {"real_sample": 0, "static_count": sandbox.get("static_count")},
            "存在 UNIT_TEST 样本",
            "无真实沙箱样本时不能证明测试有效",
        )
        return
    _check(
        checks,
        "sandbox-split",
        "真实沙箱与静态验收分列",
        "pass",
        {"real_sample": int(real_sample), "real_pass_rate": round(rate, 4), "static_count": sandbox.get("static_count")},
        "仅 UNIT_TEST 计入通过率",
        "STATIC_ONLY / CONTRACT_TEST 单列，不并入",
    )


def _auto_fix_check(checks: list[dict], metrics: dict, service: str | None) -> None:
    if service:
        rows = [row for row in metrics.get("by_service") or [] if row.get("service") == service]
        if not rows:
            _check(checks, "auto-fix", f"{service} 自动完成率", "unknown", None, f"≥{MIN_AUTO_FIX_RATE:.0%}", "该服务不在当前窗口的统计维度中")
            return
        row = rows[0]
        rate = _number(row.get("auto_fix_rate"))
        closed = _number(row.get("closed")) or 0
        if rate is None or closed <= 0:
            _check(checks, "auto-fix", f"{service} 自动完成率", "unknown", {"closed": closed}, f"≥{MIN_AUTO_FIX_RATE:.0%}", "样本为 0，比率不可判定")
            return
        _check(
            checks,
            "auto-fix",
            f"{service} 自动完成率",
            "pass" if rate >= MIN_AUTO_FIX_RATE else "fail",
            {"rate": round(rate, 4), "closed": int(closed)},
            f"≥{MIN_AUTO_FIX_RATE:.0%}",
            "不得以增加人工改判、回滚或 SLA 超时为代价",
        )
        return

    rate = _number(metrics.get("auto_fix_rate"))
    sample = _number(metrics.get("auto_fix_sample")) or 0
    if rate is None or sample <= 0:
        _check(checks, "auto-fix", "整体自动完成率", "unknown", None, f"≥{MIN_AUTO_FIX_RATE:.0%}", "缺少分母")
        return
    _check(
        checks,
        "auto-fix",
        "整体自动完成率",
        "pass" if rate >= MIN_AUTO_FIX_RATE else "fail",
        {"rate": round(rate, 4), "done": int(sample)},
        f"≥{MIN_AUTO_FIX_RATE:.0%}",
        "白名单阶段以所选服务为准",
    )


def _baseline_check(checks: list[dict], metrics: dict, baseline: dict | None) -> None:
    """回滚率与人工改判率不得高于基线（未提供基线不能算通过）。"""
    if not baseline:
        _check(checks, "baseline", "回滚/改判不高于基线", "unknown", None, "≤ 基线", "未提供 --baseline 快照")
        return
    current_rollback = _number((metrics.get("rollback") or {}).get("rate"))
    current_override = _number((metrics.get("sandbox") or {}).get("override_rate"))
    base_rollback = _number(baseline.get("rollback_rate"))
    base_override = _number(baseline.get("override_rate"))
    missing: list[str] = []
    worse: list[str] = []
    for name, current, base in (
        ("回滚率", current_rollback, base_rollback),
        ("人工改判率", current_override, base_override),
    ):
        if current is None or base is None:
            missing.append(name)
        elif current > base:
            worse.append(f"{name} {current:.2%} > 基线 {base:.2%}")
    if worse:
        status, detail = "fail", "；".join(worse)
    elif missing:
        status, detail = "unknown", "缺少数据：" + "、".join(missing)
    else:
        status, detail = "pass", "回滚率与人工改判率均不高于基线"
    _check(
        checks,
        "baseline",
        "回滚/改判不高于基线",
        status,
        {"rollback_rate": current_rollback, "override_rate": current_override},
        "≤ 基线",
        detail,
    )


def _quality_bar_check(checks: list[dict], quality_bar: Path | None) -> None:
    if quality_bar is None:
        _check(checks, "quality-bar", "模型质量门槛", "unknown", None, "存在且达标", "未提供 --quality-bar 路径")
        return
    exists = quality_bar.exists()
    _check(
        checks,
        "quality-bar",
        "模型质量门槛",
        "pass" if exists else "fail",
        str(quality_bar),
        "存在且达标（Schema ≥90% / 沙箱 ≥85%）",
        "缺失时 release 策略按设计一律转人工",
    )


def _manual_checks(checks: list[dict], confirmed: set[str]) -> None:
    for key, label in MANUAL_ITEMS.items():
        _check(
            checks,
            key,
            label,
            "pass" if key in confirmed else "unknown",
            key in confirmed,
            "人工确认",
            "必须由演练记录支撑，工具不代替演练",
        )


def _integrity_check(checks: list[dict], metrics: dict) -> None:
    """M5 硬门禁：审计链完整性。不可读不得视为通过。"""
    integrity = metrics.get("audit_integrity") or {}
    if not integrity.get("available"):
        _check(
            checks,
            "audit-integrity",
            "审计链完整性",
            "unknown",
            None,
            "tamper-evident 校验通过",
            "审计链不可读/校验不可用；不得当作未发生篡改",
        )
        return
    ok = bool(integrity.get("ok"))
    _check(
        checks,
        "audit-integrity",
        "审计链完整性",
        "pass" if ok else "fail",
        {"checked": integrity.get("checked"), "legacy": integrity.get("legacy")},
        "tamper-evident 校验通过",
        integrity.get("error") or "链头与归档段均通过 SHA-256 校验",
    )


def _command_backlog_check(checks: list[dict], metrics: dict) -> None:
    """M5 硬门禁：写命令 outbox 无积压、无失败。"""
    commands = metrics.get("commands") or {}
    if not commands.get("available"):
        _check(
            checks,
            "command-backlog",
            "写命令 outbox",
            "unknown",
            None,
            "0 积压 / 0 失败",
            "命令存储不可读；不得当作无积压",
        )
        return
    pending = int(commands.get("pending") or 0)
    oldest = float(commands.get("oldest_pending_seconds") or 0.0)
    failed = int(commands.get("failed_24h") or 0)
    ok = pending == 0 and failed == 0 and oldest < MAX_COMMAND_BACKLOG_SECONDS
    _check(
        checks,
        "command-backlog",
        "写命令 outbox",
        "pass" if ok else "fail",
        {"pending": pending, "oldest_pending_seconds": round(oldest, 1), "failed_24h": failed},
        f"0 积压 / 0 失败 / 最老等待 <{MAX_COMMAND_BACKLOG_SECONDS}s",
        "存在积压或失败时必须先恢复命令投递，再推进灰度",
    )


def _notify_delivery_check(checks: list[dict], metrics: dict) -> None:
    """M5 硬门禁：转人工通知已实际送达（recorded/failed 不算送达）。"""
    detail = metrics.get("escalation_detail") or {}
    value = detail.get("notify_pending")
    if not detail.get("available") or value is None:
        _check(
            checks,
            "notify-delivery",
            "转人工通知送达",
            "unknown",
            value,
            "0 条未送达",
            "待办统计不可读；不得当作已送达",
        )
        return
    pending = int(value)
    _check(
        checks,
        "notify-delivery",
        "转人工通知送达",
        "pass" if pending == 0 else "fail",
        pending,
        "0 条未送达",
        "recorded/failed 且未升级的通知不计入已送达",
    )


def evaluate(
    stage: str,
    metrics: dict,
    *,
    service: str | None = None,
    quality_bar: Path | None = None,
    confirmed: set[str] | None = None,
    baseline: dict | None = None,
) -> dict:
    if stage not in STAGES:
        raise ValueError(f"未知阶段：{stage}（可选：{', '.join(STAGES)}）")
    checks: list[dict] = []
    if stage in ("staging-shadow", "production-shadow"):
        _sample_check(checks, metrics)
        _evidence_check(checks, metrics)
        _false_pass_check(checks, metrics)
    if stage in ("single-service", "limited-release", "expand"):
        _sample_check(checks, metrics)
        _evidence_check(checks, metrics)
        _false_pass_check(checks, metrics)
        _sandbox_metric_check(checks, metrics)
        _auto_fix_check(checks, metrics, service)
        _quality_bar_check(checks, quality_bar)
    if stage in ("staging-shadow", "production-shadow", "single-service", "limited-release"):
        _manual_checks(checks, confirmed or set())
    if stage in ("limited-release", "expand"):
        _baseline_check(checks, metrics, baseline)
    if stage == "expand" and not service:
        _check(checks, "service", "扩展目标服务", "unknown", None, "显式指定服务", "逐服务扩展必须单独验收")
    # M5：证据完整性 / outbox 积压 / SLA 未送达 —— 所有阶段均为硬门禁
    _integrity_check(checks, metrics)
    _command_backlog_check(checks, metrics)
    _notify_delivery_check(checks, metrics)

    statuses = {item["status"] for item in checks}
    decision = "block" if "fail" in statuses else ("pending" if "unknown" in statuses else "allow")
    return {
        "stage": stage,
        "service": service,
        "decision": decision,
        "checks": checks,
        "next": _next_step(stage, decision),
    }


def _next_step(stage: str, decision: str) -> str:
    if decision == "block":
        return "存在硬条件未满足：保持当前阶段，修复后重新取样；不得通过调阈值或忽略告警推进"
    if decision == "pending":
        return "补齐 unknown 项（指标分量 / 基线 / 人工演练）后重跑；未知不等于通过"
    order = list(STAGES)
    if stage == order[-1]:
        return "当前阶段已满足；继续逐服务验收日志、测试、SLO 与回滚"
    return f"满足本阶段条件，可评估进入下一阶段：{order[order.index(stage) + 1]}"


def _load_json(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[rollout-gate] 无法读取 {path}：{exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生产灰度放行检查（只读）")
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--service", default=None, help="单服务白名单/逐服务扩展目标")
    parser.add_argument("--metrics", default=None, help="/api/metrics/ops 的 JSON 快照")
    parser.add_argument("--baseline", default=None, help="基线快照（rollback_rate/override_rate）")
    parser.add_argument("--quality-bar", default=None, help="data/model_quality_bar.json 路径")
    parser.add_argument("--confirm", default="", help="人工演练项，逗号分隔：" + ",".join(MANUAL_ITEMS))
    args = parser.parse_args(argv)

    metrics = _load_json(args.metrics)
    if metrics is None:
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "decision": "pending",
                    "reason": "缺少 --metrics 快照或文件不可读；工具不伪造指标",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    confirmed = {item.strip() for item in args.confirm.split(",") if item.strip()}
    result = evaluate(
        args.stage,
        metrics,
        service=args.service,
        quality_bar=Path(args.quality_bar) if args.quality_bar else None,
        confirmed=confirmed,
        baseline=_load_json(args.baseline),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"allow": 0, "block": 1, "pending": 2}[result["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())
