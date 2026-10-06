"""Triage 离线评测脚本（WP3 任务 4 交付物）：Top-1 命中率 + 低置信拦截率。

口径（对齐实施计划 WP3 验收指标）：
    - 样本来源：data/triage_eval_samples.json（4 条证据充分 + 2 条证据不足）；
    - 证据充分样本：Top-1 命中（error_type 含期望关键词）且 confidence ≥ 闸门阈值
      才计为「正确自动放行」；
    - 证据不足样本：confidence < 阈值（或 LLM 降级）即计为「正确拦截」（闸门 1 转人工）；
    - schema 通过率 = 非降级（pydantic 校验成功）比例。

用法：
    .venv/bin/python eval_triage.py            # 全部样本（真实调用 Ollama）
    .venv/bin/python eval_triage.py --id S05   # 单样本调试

输出：
    - 控制台逐样本明细 + 汇总指标；
    - data/triage_eval_report.json（评测报告，持久化归档）。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import yaml

from aiops_agent import triage
from aiops_agent.models import Alert

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_PATH = BASE_DIR / "data" / "triage_eval_samples.json"
REPORT_PATH = BASE_DIR / "data" / "triage_eval_report.json"


def load_threshold() -> float:
    """读取闸门 1 置信度阈值（与工作流同一策略文件，保证评测口径一致）。"""
    policy_path = Path(os.environ.get("AIOPS_POLICY_PATH", "demo-policy.yaml"))
    if not policy_path.is_absolute():
        policy_path = BASE_DIR / policy_path
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    return float(policy["release_gate"]["triage"]["confidence_threshold"])


def keyword_hit(error_type: str, keywords: list[str]) -> bool:
    """Top-1 命中判定：error_type 含任一期望关键词（大小写不敏感）。"""
    text = error_type.lower()
    return any(keyword.lower() in text for keyword in keywords)


def evaluate_sample(sample: dict, threshold: float, model: str | None = None) -> dict:
    """评测单样本：真实调用 triage，返回明细行。

    样本可选字段 ``changes``（近发布/配置事件，P1-1 变更关联）同步透传，
    用于验证「故障与变更相关性」的判定质量。
    """
    alert = Alert(**sample["alert"])
    root_cause, meta = triage.run_triage(
        alert,
        sample["clustered"],
        sample.get("trace_ids", []),
        model=model,
        changes=sample.get("changes"),
    )
    hit = keyword_hit(root_cause.error_type, sample.get("expected_keywords", []))
    expect_low = bool(sample.get("expect_low_confidence"))
    blocked = meta["degraded"] or root_cause.confidence < threshold
    gate = "escalate（转人工）" if blocked else "auto_proceed（放行）"
    if expect_low:
        correct = blocked
    else:
        correct = (not meta["degraded"]) and hit and not blocked
    return {
        "id": sample["id"],
        "note": sample.get("note", ""),
        "expect_low_confidence": expect_low,
        "error_type": root_cause.error_type,
        "confidence": root_cause.confidence,
        "suspect_files": root_cause.suspect_files,
        "summary": root_cause.summary,
        "degraded": meta["degraded"],
        "reason": meta.get("reason", ""),
        "elapsed_seconds": meta["elapsed_seconds"],
        "keyword_hit": hit,
        "gate": gate,
        "correct": correct,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage 离线评测（真实调用 Ollama）")
    parser.add_argument("--id", default=None, help="仅评测指定样本 ID（如 S01）")
    parser.add_argument("--model", default=None, help=f"模型（默认 {triage.DEFAULT_MODEL}）")
    args = parser.parse_args()

    threshold = load_threshold()
    payload = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    samples = payload["samples"]
    if args.id:
        samples = [s for s in samples if s["id"] == args.id]
        if not samples:
            raise SystemExit(f"样本不存在: {args.id}")

    used_model = args.model or triage.DEFAULT_MODEL
    print(f"Triage 离线评测：model={used_model} 阈值={threshold} 样本数={len(samples)}")
    rows: list[dict] = []
    start = time.monotonic()
    for sample in samples:
        row = evaluate_sample(sample, threshold, model=args.model)
        rows.append(row)
        mark = "✓" if row["correct"] else "✗"
        degraded_note = f" degraded({row['reason'][:60]})" if row["degraded"] else ""
        print(
            f"  [{mark}] {row['id']} {row['note']} → error_type={row['error_type']!r} "
            f"conf={row['confidence']:.2f} gate={row['gate']} hit={row['keyword_hit']} "
            f"{row['elapsed_seconds']}s{degraded_note}"
        )
    total_elapsed = round(time.monotonic() - start, 2)

    high = [r for r in rows if not r["expect_low_confidence"]]
    low = [r for r in rows if r["expect_low_confidence"]]
    metrics = {
        "total": len(rows),
        "schema_pass_rate": round(sum(0 if r["degraded"] else 1 for r in rows) / len(rows), 4)
        if rows
        else 0.0,
        "high_evidence_samples": len(high),
        "top1_hit_rate": round(sum(1 for r in high if r["keyword_hit"]) / len(high), 4)
        if high
        else 0.0,
        "auto_proceed_accuracy": round(sum(1 for r in high if r["correct"]) / len(high), 4)
        if high
        else 0.0,
        "low_evidence_samples": len(low),
        "low_conf_block_rate": round(sum(1 for r in low if r["correct"]) / len(low), 4)
        if low
        else 0.0,
        "correct_rate": round(sum(1 for r in rows if r["correct"]) / len(rows), 4)
        if rows
        else 0.0,
        "avg_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in rows) / len(rows), 2)
        if rows
        else 0.0,
        "total_elapsed_seconds": total_elapsed,
    }
    print("汇总指标：")
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": used_model,
        "confidence_threshold": threshold,
        "metrics": metrics,
        "samples": rows,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评测报告已写入：{REPORT_PATH}")


if __name__ == "__main__":
    main()
