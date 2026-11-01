"""评测框架 V2（M3）：版本化数据集、可比较报告、分场景指标与门禁判定。

设计目标（对齐闭环实施计划 M3 第 1/4 节）：

- 报告必须自带来源：``dataset_version``（样本内容哈希）、``code_commit``、
  ``prompt_hash``、``model``、``provider``、``rag_index_hash``；缺任何一项都不能
  与基线比较；
- 根因指标：高置信误放行率、分场景准确率、置信度分桶、Brier Score；
- 修复指标：diff 可应用率、首轮/最终沙箱通过率、目标测试通过率、新增回归数；
- 门禁：低证据阻断必须 100%、安全回归集高置信误放行必须为 0、Schema 通过率
  根因 ≥98% / 修复 ≥90%、holdout 沙箱通过率 ≥85%；相对基线准确率下降超过
  2 个百分点、出现新增高风险误放行或新增回归即失败。

本模块只做纯计算（哈希、指标、比较），不调用模型、不启动沙箱。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

SPLITS = ("dev", "holdout")

# 门禁硬阈值（与实施计划一致，不得放宽）
HARD_LIMITS = {
    "triage": {
        "schema_pass_rate": 0.98,
        "low_conf_block_rate": 1.0,
        "high_confidence_false_pass_count": 0,
        "safety_regression_false_pass": 0,
    },
    "fix": {
        "schema_pass_rate": 0.90,
        "holdout_sandbox_pass_rate": 0.85,
        "new_regression_count": 0,
        "abstain_false_pass_count": 0,
    },
}

# 相对基线允许下降的最大百分点（2pp）
MAX_ACCURACY_DROP = 0.02
_COMPARABLE = (
    "top1_hit_rate",
    "auto_proceed_accuracy",
    "route_accuracy",
    "schema_pass_rate",
    "low_conf_block_rate",
    "first_round_sandbox_pass_rate",
    "final_sandbox_pass_rate",
    "target_test_pass_rate",
)


def canonical_hash(value: object) -> str:
    """稳定内容哈希（排序键 + 紧凑分隔符）：用于数据集版本与提示词指纹。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def dataset_meta(payload: dict) -> dict:
    """数据集元信息：版本、规模、切分与场景分布（不含样本正文）。"""
    samples = payload.get("samples") or []
    splits: dict[str, int] = {}
    scenarios: dict[str, int] = {}
    for sample in samples:
        split = sample.get("split", "dev")
        splits[split] = splits.get(split, 0) + 1
        scenario = sample.get("scenario", "unspecified")
        scenarios[scenario] = scenarios.get(scenario, 0) + 1
    return {
        "dataset_version": canonical_hash(samples),
        "sample_count": len(samples),
        "splits": splits,
        "scenarios": scenarios,
    }


def code_commit(repo_dir: str | Path | None = None) -> str:
    """当前代码提交（取不到时返回空串，报告仍可生成但不可与基线比较）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir or Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()[:12] if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def file_hash(path: str | Path | None) -> str:
    """文件内容哈希（RAG 索引 / 质量门槛证据）；缺失返回空串。"""
    if not path:
        return ""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def prompt_hash(texts: list[str] | tuple[str, ...]) -> str:
    """提示词指纹：所有提示词模板拼接后的稳定哈希。"""
    return canonical_hash(list(texts))


def split_samples(samples: list[dict], split: str = "dev") -> list[dict]:
    """按切分筛选样本；holdout 只能显式选择，避免调参污染。"""
    if split not in SPLITS + ("all",):
        raise ValueError(f"未知切分 {split!r}，允许 {SPLITS + ('all',)}")
    if split == "all":
        return list(samples)
    return [s for s in samples if s.get("split", "dev") == split]


def brier_score(rows: list[dict]) -> float:
    """Brier Score：以自报 confidence 为「根因正确」概率，label=关键词命中。"""
    usable = [r for r in rows if r.get("label") in (0, 1, False, True)]
    if not usable:
        return 0.0
    total = sum((float(r["confidence"]) - float(r["label"])) ** 2 for r in usable)
    return round(total / len(usable), 4)


def confidence_buckets(rows: list[dict]) -> dict:
    """置信度分桶：每桶样本数与命中率（用于校准分析）。"""
    buckets: dict[str, dict] = {}
    for row in rows:
        low = min(0.9, max(0.0, float(row["confidence"]) // 0.1 * 0.1))
        key = f"{low:.1f}-{low + 0.1:.1f}"
        item = buckets.setdefault(key, {"total": 0, "hits": 0, "hit_rate": 0.0})
        item["total"] += 1
        item["hits"] += int(bool(row.get("label")))
    for item in buckets.values():
        item["hit_rate"] = round(item["hits"] / item["total"], 4) if item["total"] else 0.0
    return dict(sorted(buckets.items()))


def scenario_accuracy(rows: list[dict], key: str = "correct") -> dict:
    """分场景准确率：按 sample.scenario 分层。"""
    stats: dict[str, dict] = {}
    for row in rows:
        scenario = row.get("scenario") or "unspecified"
        item = stats.setdefault(scenario, {"total": 0, "correct": 0, "accuracy": 0.0})
        item["total"] += 1
        item["correct"] += int(bool(row.get(key)))
    for item in stats.values():
        item["accuracy"] = round(item["correct"] / item["total"], 4) if item["total"] else 0.0
    return dict(sorted(stats.items()))


def _rate(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _rate_or_none(part: int, total: int) -> float | None:
    """分母为 0 时返回 None：单样本调试或切分缺样本不能被误判为「0%」。"""
    return round(part / total, 4) if total else None


def triage_metrics(rows: list[dict]) -> dict:
    """根因评测指标（行内约定字段见 eval_triage.py）。"""
    high = [r for r in rows if not r.get("expect_low_confidence")]
    low = [r for r in rows if r.get("expect_low_confidence")]
    released = [r for r in rows if r.get("released_code_fix")]
    false_pass = [r for r in released if not r.get("keyword_hit")]
    safety_false_pass = [r for r in released if r.get("safety_regression")]
    return {
        "total": len(rows),
        "schema_pass_rate": _rate(sum(1 for r in rows if not r.get("degraded")), len(rows)),
        "high_evidence_samples": len(high),
        "top1_hit_rate": _rate_or_none(sum(1 for r in high if r.get("keyword_hit")), len(high)),
        "auto_proceed_accuracy": _rate_or_none(sum(1 for r in high if r.get("correct")), len(high)),
        "route_accuracy": _rate(sum(1 for r in rows if r.get("route_correct")), len(rows)),
        "low_evidence_samples": len(low),
        "low_conf_block_rate": _rate_or_none(sum(1 for r in low if r.get("blocked")), len(low)),
        "high_confidence_false_pass_count": len(false_pass),
        "high_confidence_false_pass_rate": _rate_or_none(len(false_pass), len(released)),
        "safety_regression_false_pass": len(safety_false_pass),
        "brier_score": brier_score(rows),
        "confidence_buckets": confidence_buckets(rows),
        "scenarios": scenario_accuracy(rows),
        "correct_rate": _rate(sum(1 for r in rows if r.get("correct")), len(rows)),
    }


def fix_metrics(rows: list[dict]) -> dict:
    """修复评测指标（仅可修复样本；弃权样本单独统计）。"""
    total = len(rows)
    new_regressions = sum(len(r.get("new_failures") or []) for r in rows)
    return {
        "total": total,
        "schema_pass_rate": _rate(sum(1 for r in rows if r.get("schema_pass")), total),
        "diff_apply_rate": _rate(sum(1 for r in rows if r.get("validated")), total),
        "first_round_sandbox_pass_rate": _rate(
            sum(1 for r in rows if r.get("first_round_sandbox_pass")), total
        ),
        "final_sandbox_pass_rate": _rate(
            sum(1 for r in rows if r.get("final_sandbox_pass")), total
        ),
        "target_test_pass_rate": _rate(sum(1 for r in rows if r.get("target_tests_passed")), total),
        "new_regression_count": new_regressions,
        "file_hit_rate": _rate(sum(1 for r in rows if r.get("file_hit")), total),
        "avg_elapsed_seconds": round(
            sum(float(r.get("elapsed_seconds", 0)) for r in rows) / total, 2
        )
        if total
        else 0.0,
    }


def abstain_metrics(rows: list[dict]) -> dict:
    """弃权/路由样本指标：非代码根因必须被组合判据拦截。"""
    total = len(rows)
    false_pass = [r for r in rows if r.get("released_code_fix")]
    return {
        "total": total,
        "abstain_accuracy": _rate(sum(1 for r in rows if r.get("route_correct")), total),
        "false_pass_count": len(false_pass),
        "expected_routes": {
            route: sum(1 for r in rows if r.get("expect_route") == route)
            for route in ("runbook", "human")
        },
    }


def compare_baseline(metrics: dict, baseline: dict | None, kind: str) -> dict:
    """与批准基线比较 + 硬阈值检查。

    返回 ``status``：
      - ``ok``：全部门禁通过；
      - ``regression``：相对基线下降/新增误放行；
      - ``hard_fail``：未达硬阈值（即使无基线也失败）；
      - ``baseline_missing`` / ``baseline_incompatible``：无可比基线（硬阈值仍生效）。
    """
    if kind not in HARD_LIMITS:
        raise ValueError(f"未知评测类型 {kind!r}")
    violations: list[dict] = []
    warnings: list[dict] = []

    for name, limit in HARD_LIMITS[kind].items():
        value = metrics.get(name)
        if value is None:
            continue
        if name.endswith("_count") or name == "safety_regression_false_pass":
            if float(value) > float(limit):
                violations.append({"type": "hard_limit", "metric": name, "value": value, "limit": limit})
        elif float(value) < float(limit):
            violations.append({"type": "hard_limit", "metric": name, "value": value, "limit": limit})

    status = "ok"
    if baseline is None:
        status = "baseline_missing"
    elif baseline.get("dataset_version") != metrics.get("dataset_version"):
        status = "baseline_incompatible"
        warnings.append(
            {
                "type": "baseline_incompatible",
                "baseline_version": baseline.get("dataset_version"),
                "dataset_version": metrics.get("dataset_version"),
            }
        )
    else:
        base_metrics = baseline.get("metrics") or {}
        for name in _COMPARABLE:
            if metrics.get(name) is None or base_metrics.get(name) is None:
                continue
            drop = float(base_metrics[name]) - float(metrics[name])
            if drop > MAX_ACCURACY_DROP + 1e-9:
                violations.append(
                    {
                        "type": "accuracy_drop",
                        "metric": name,
                        "baseline": base_metrics[name],
                        "value": metrics[name],
                        "drop": round(drop, 4),
                    }
                )
        # 新增高风险误放行：高置信错误放行的绝对数增加即失败
        base_false = int(base_metrics.get("high_confidence_false_pass_count", 0) or 0)
        if int(metrics.get("high_confidence_false_pass_count", 0) or 0) > base_false:
            violations.append(
                {
                    "type": "new_high_confidence_false_pass",
                    "baseline": base_false,
                    "value": metrics.get("high_confidence_false_pass_count"),
                }
            )
        base_regressions = int(base_metrics.get("new_regression_count", 0) or 0)
        if int(metrics.get("new_regression_count", 0) or 0) > base_regressions:
            violations.append(
                {
                    "type": "new_regressions",
                    "baseline": base_regressions,
                    "value": metrics.get("new_regression_count"),
                }
            )

    if violations:
        hard = [v for v in violations if v["type"] in ("hard_limit", "new_high_confidence_false_pass", "new_regressions")]
        status = "hard_fail" if hard else "regression"
    return {"status": status, "violations": violations, "warnings": warnings}


def build_report(
    *,
    kind: str,
    payload: dict,
    model: str,
    provider: str,
    metrics: dict,
    rows: list[dict],
    prompt_hash_value: str = "",
    rag_index_hash: str = "",
    split: str = "dev",
    extra: dict | None = None,
) -> dict:
    """组装 V2 报告：来源齐全 + 逐样本明细 + 失败分类。"""
    meta = dataset_meta(payload)
    report = {
        "schema_version": "v2",
        "kind": kind,
        "generated_at": _now(),
        "split": split,
        "dataset_version": meta["dataset_version"],
        "dataset": meta,
        "code_commit": code_commit(),
        "prompt_hash": prompt_hash_value,
        "model": model,
        "provider": provider,
        "rag_index_hash": rag_index_hash,
        "metrics": {**metrics, "dataset_version": meta["dataset_version"]},
        "samples": rows,
    }
    if extra:
        report.update(extra)
    return report


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")


def write_report(path: str | Path, report: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def quality_bar_entry(report: dict) -> dict:
    """从 holdout 修复报告提取质量门槛条目（仅当两项门槛均达标时用于写入）。"""
    metrics = report.get("metrics") or {}
    return {
        "schema_pass_rate": metrics.get("schema_pass_rate", 0.0),
        "holdout_sandbox_pass_rate": metrics.get("final_sandbox_pass_rate", 0.0),
        "dataset_version": report.get("dataset_version", ""),
        "code_commit": report.get("code_commit", ""),
        "generated_at": report.get("generated_at", ""),
    }


def baseline_entry(report: dict) -> dict:
    """从报告提取基线条目（只保留比较所需字段，避免整份报告膨胀）。"""
    return {
        "dataset_version": report.get("dataset_version", ""),
        "metrics": report.get("metrics") or {},
        "model": report.get("model", ""),
        "provider": report.get("provider", ""),
        "code_commit": report.get("code_commit", ""),
        "generated_at": report.get("generated_at", ""),
        "split": report.get("split", ""),
    }


def write_baseline(path: str | Path, kind: str, report: dict) -> dict:
    """把通过硬阈值的报告写入批准基线（按 ``triage`` / ``fix`` 分键，保留另一类）。"""
    if kind not in HARD_LIMITS:
        raise ValueError(f"未知评测类型 {kind!r}")
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    entry = baseline_entry(report)
    payload.setdefault("schema_version", "v1")
    payload[kind] = entry
    payload[f"{kind}_updated_at"] = entry["generated_at"]
    write_report(target, payload)
    return entry
