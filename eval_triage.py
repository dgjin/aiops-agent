"""Triage 离线评测 V2（M3）：组合判据 + 版本化报告 + 可复跑门禁。

口径（对齐闭环实施计划 M3）：

- 样本：``data/triage_eval_samples.json``（S01–S32 人工构造 + 场景化变体，≥100 条），
  按 ``dev`` / ``holdout`` 分层；holdout 不用于提示词调参；
- 每条样本真实调用 triage（Ollama），再经**组合放行判据**（证据/结构/根因类型/
  嫌疑文件/模型路由）决定是否允许代码修复：
    - 期望 ``code_fix``：非降级 + 关键词命中 + 组合判据放行；
    - 期望 ``runbook``：组合判据必须拦截（基础设施/配置/依赖类根因）；
    - 期望 ``human``：低置信或证据不足，必须转人工；
- 指标：Top-1 命中率、高置信误放行率、分场景准确率、置信度分桶、Brier Score、
  低证据阻断率（必须 100%）与安全回归误放行（必须 0）；
- 报告自带 ``dataset_version/code_commit/prompt_hash/model/provider/rag_index_hash``，
  可与批准基线比较；``--check`` 在未达硬阈值或相对基线退化时以非零码退出（CI 门禁）。

用法：
    .venv/bin/python eval_triage.py                          # dev 切分
    .venv/bin/python eval_triage.py --split holdout --check   # holdout + 门禁
    .venv/bin/python eval_triage.py --id S08                  # 单样本调试
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import time
from pathlib import Path

import yaml

from aiops_agent import evalkit, gate_decision, triage
from aiops_agent.models import Alert

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_PATH = BASE_DIR / "data" / "triage_eval_samples.json"
REPORT_PATH = BASE_DIR / "data" / "triage_eval_report.json"
BASELINE_PATH = BASE_DIR / "data" / "eval_baseline.json"


def load_threshold() -> float:
    """读取闸门 1 置信度阈值（与工作流同一策略文件，保证评测口径一致）。"""
    policy_path = Path(os.environ.get("AIOPS_POLICY_PATH", "demo-policy.yaml"))
    if not policy_path.is_absolute():
        policy_path = BASE_DIR / policy_path
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    return float(policy["release_gate"]["triage"]["confidence_threshold"])


def keyword_hit(error_type: str, keywords: list[str]) -> bool:
    """Top-1 命中判定：error_type 含任一期望关键词（大小写不敏感）。"""
    text = str(error_type or "").lower()
    return any(str(keyword).lower() in text for keyword in keywords)


def _failure_category(row: dict) -> str:
    """失败分类：优先高置信误放行，其次证据/路由/命中问题。"""
    if row["degraded"]:
        return "degraded"
    if row["released_code_fix"] and not row["keyword_hit"]:
        return "high_confidence_false_pass"
    if row["expect_low_confidence"] and not row["blocked"]:
        return "low_evidence_not_blocked"
    if not row["keyword_hit"]:
        return "keyword_miss"
    if row["expect_route"] != row["route"]:
        return "route_mismatch"
    if row["expect_route"] == "code_fix" and not row["released_code_fix"]:
        return "over_blocked"
    return ""


def evaluate_sample(sample: dict, threshold: float, model: str | None = None) -> dict:
    """评测单样本：真实调用 triage + 组合判据 + 路由判定。"""
    alert = Alert(**sample["alert"])
    root_cause, meta = triage.run_triage(
        alert,
        sample["clustered"],
        sample.get("trace_ids", []),
        model=model,
        changes=sample.get("changes"),
    )
    hit = keyword_hit(root_cause.error_type, sample.get("expected_keywords", []))
    expect_route = str(sample.get("expect_route") or "code_fix")
    decision = gate_decision.decide(
        confidence=root_cause.confidence,
        threshold=threshold,
        error_type=root_cause.error_type,
        summary=root_cause.summary,
        suspect_files=root_cause.suspect_files,
        evidence_status=sample.get("evidence_status", "OK"),
        schema_valid=not meta["degraded"],
        files_found=sample.get("files_found", root_cause.suspect_files),
        files_missing=sample.get("files_missing", []),
        scope_known=bool(sample.get("scope_known", True)),
        require_suspect_files=bool(sample.get("require_suspect_files", True)),
        routing=sample.get("routing"),
    )
    blocked = not decision.allowed
    route_correct = decision.route == expect_route
    if sample.get("expect_low_confidence"):
        correct = bool(blocked and route_correct)
    else:
        correct = bool((not meta["degraded"]) and hit and route_correct)
    row = {
        "id": sample["id"],
        "note": sample.get("note", ""),
        "scenario": sample.get("scenario", "unspecified"),
        "split": sample.get("split", "dev"),
        "safety_regression": bool(sample.get("safety_regression", False)),
        "expect_route": expect_route,
        "expect_low_confidence": bool(sample.get("expect_low_confidence")),
        "error_type": root_cause.error_type,
        "confidence": root_cause.confidence,
        "label": int(hit),
        "suspect_files": root_cause.suspect_files,
        "summary": root_cause.summary,
        "degraded": meta["degraded"],
        "reason": meta.get("reason", ""),
        "elapsed_seconds": meta["elapsed_seconds"],
        "keyword_hit": hit,
        "route": decision.route,
        "blocked": blocked,
        "released_code_fix": decision.allowed,
        "route_correct": route_correct,
        "correct": correct,
        "gate_events": decision.events(),
        "gate_reasons": decision.reasons,
        "gate_labels": decision.labels,
    }
    row["failure_category"] = _failure_category(row)
    return row


def _prompt_hash() -> str:
    return evalkit.prompt_hash([inspect.getsource(triage.build_triage_prompt)])


def _load_baseline(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if "triage" in payload and isinstance(payload["triage"], dict):
        return payload["triage"]
    return payload if "metrics" in payload else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage 离线评测 V2（真实调用 Ollama）")
    parser.add_argument("--id", default=None, help="仅评测指定样本 ID（如 S08）")
    parser.add_argument("--model", default=None, help=f"模型（默认 {triage.DEFAULT_MODEL}）")
    parser.add_argument("--split", default="dev", choices=["dev", "holdout", "all"], help="数据切分（默认 dev）")
    parser.add_argument("--dataset", default=str(SAMPLES_PATH), help="数据集路径")
    parser.add_argument("--report", default=str(REPORT_PATH), help="报告输出路径")
    parser.add_argument("--baseline", default=str(BASELINE_PATH), help="批准基线路径")
    parser.add_argument("--check", action="store_true", help="门禁模式：未达阈值/相对基线退化时退出码 1")
    parser.add_argument("--write-baseline", action="store_true", help="硬阈值全部通过时把本次结果写入批准基线")
    args = parser.parse_args()

    threshold = load_threshold()
    dataset_path = Path(args.dataset)
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    samples = payload["samples"]
    if args.id:
        samples = [s for s in samples if s["id"] == args.id]
        if not samples:
            raise SystemExit(f"样本不存在: {args.id}")
    else:
        samples = evalkit.split_samples(samples, args.split)
    if not samples:
        raise SystemExit(f"切分 {args.split} 无样本")

    used_model = args.model or triage.DEFAULT_MODEL
    print(f"Triage 评测 V2：model={used_model} 阈值={threshold} 切分={args.split} 样本数={len(samples)}")
    rows: list[dict] = []
    start = time.monotonic()
    for sample in samples:
        row = evaluate_sample(sample, threshold, model=args.model)
        rows.append(row)
        mark = "✓" if row["correct"] else "✗"
        degraded_note = f" degraded({row['reason'][:60]})" if row["degraded"] else ""
        print(
            f"  [{mark}] {row['id']} {row['note']} → error_type={row['error_type']!r} "
            f"conf={row['confidence']:.2f} route={row['route']} hit={row['keyword_hit']} "
            f"{row['elapsed_seconds']}s{degraded_note}"
        )
    total_elapsed = round(time.monotonic() - start, 2)

    metrics = evalkit.triage_metrics(rows)
    metrics.update(evalkit.dataset_meta(payload))
    metrics["total_elapsed_seconds"] = total_elapsed
    print("汇总指标：")
    for key, value in metrics.items():
        if isinstance(value, dict):
            continue
        print(f"  {key}: {value}")

    failures: dict[str, int] = {}
    for row in rows:
        if row["failure_category"]:
            failures[row["failure_category"]] = failures.get(row["failure_category"], 0) + 1

    report = evalkit.build_report(
        kind="triage",
        payload=payload,
        model=used_model,
        provider="ollama",
        metrics=metrics,
        rows=rows,
        prompt_hash_value=_prompt_hash(),
        rag_index_hash=evalkit.file_hash(
            os.environ.get("AIOPS_CODE_INDEX", str(BASE_DIR / "data" / "code_index.json"))
        ),
        split=args.split,
        extra={
            "confidence_threshold": threshold,
            "total_elapsed_seconds": total_elapsed,
            "failure_categories": failures,
        },
    )
    comparison = evalkit.compare_baseline(metrics, _load_baseline(Path(args.baseline)), "triage")
    report["baseline_comparison"] = comparison
    evalkit.write_report(args.report, report)
    print(f"评测报告已写入：{args.report}")
    print(f"门禁结论：{comparison['status']}")
    for violation in comparison["violations"]:
        print(f"  [违规] {violation}")
    for warning in comparison["warnings"]:
        print(f"  [提示] {warning}")
    if args.write_baseline:
        hard_fail = any(v.get("type") == "hard_limit" for v in comparison["violations"])
        if hard_fail or comparison["status"] == "regression":
            print("[基线] 存在硬阈值违规或相对基线退化，拒绝写入基线")
        else:
            evalkit.write_baseline(args.baseline, "triage", report)
            print(f"[基线] 已写入批准基线：{args.baseline}")
    if args.check and comparison["status"] in ("regression", "hard_fail"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
