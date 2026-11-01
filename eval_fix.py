"""Fix 模型评测 V2（M3）：首轮/最终沙箱通过率 + 弃权路由 + 质量门槛证据。

口径（对齐闭环实施计划 M3）：

- 代码修复集：``data/fix_eval_samples.json``（33 条真实夹具缺陷）；每条真实调用
  ``fix_agent.run_fix`` → schema/应用/编译校验 → 真实沙箱单测（目标用例转通过且
  不引入新失败）；``--retries`` 可按工作流回炉策略评测「首轮 vs 最终」通过率；
- 弃权集：``data/fix_eval_abstain_samples.json``（28 条非代码根因/误报/日志异常），
  只经组合放行判据，期望 route=runbook|human，**不得生成代码补丁**；
- 指标：diff 可应用率、首轮/最终沙箱通过率、目标测试通过率、新增回归数、
  弃权准确率与误放行数；
- ``--write-quality-bar`` 仅在 holdout 切分同时满足 Schema ≥90% 与沙箱 ≥85% 时
  写入 ``data/model_quality_bar.json``，供 ``model_routing`` 作为自动修复路由证据；
  未达标不写、不放宽门槛。

用法：
    .venv/bin/python eval_fix.py --split dev
    .venv/bin/python eval_fix.py --split holdout --retries 2 --check --write-quality-bar
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import time
from pathlib import Path

from aiops_agent import evalkit, fix_agent, gate_decision, model_routing, sandbox
from aiops_agent.models import Alert, RootCause, TestReport

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_PATH = BASE_DIR / "data" / "fix_eval_samples.json"
ABSTAIN_PATH = BASE_DIR / "data" / "fix_eval_abstain_samples.json"
REPORT_PATH = BASE_DIR / "data" / "fix_eval_report.json"
BASELINE_PATH = BASE_DIR / "data" / "eval_baseline.json"
QUALITY_BAR_PATH = BASE_DIR / "data" / "model_quality_bar.json"
EVAL_SANDBOX_ROOT = BASE_DIR / "data" / "sandbox-eval"
EVAL_REPO_DIR = BASE_DIR / "data" / "fix_eval_repo"


def _parse_failed_tests(details: str) -> set[str] | None:
    """从沙箱 details 解析失败用例名；非用例失败（超时 / 未发现用例 / 准备失败）返回 None。"""
    match = re.search(r"失败用例：([^；]+)；", details)
    if not match:
        return None
    return {name for name in match.group(1).split("、") if name}


def _sandbox_verdict(report: TestReport, sample: dict) -> tuple[bool, dict]:
    """沙箱判定：目标缺陷用例转为通过，且不引入新的失败用例（允许基线已知失败）。"""
    if report.passed:
        return True, {"failed_tests": [], "target_tests_passed": True, "new_failures": []}
    failed = _parse_failed_tests(report.details)
    if failed is None:
        return False, {"failed_tests": [], "target_tests_passed": False, "new_failures": []}
    targets = set(sample.get("fix_target_tests", []))
    baseline = set(sample.get("baseline_failed_tests", []))
    target_passed = not (targets & failed)
    new_failures = sorted(failed - baseline)
    verdict = target_passed and not new_failures
    return verdict, {
        "failed_tests": sorted(failed),
        "target_tests_passed": target_passed,
        "new_failures": new_failures,
    }


def evaluate_sample(sample: dict, model: str, timeout: int, retries: int = 0) -> dict:
    """评测单条可修复样本：生成 → 校验 → 真实沙箱；记录首轮与最终结果。"""
    alert = Alert(**sample["alert"])
    root_cause = RootCause(**sample["root_cause"])
    attempts: list[dict] = []
    first_schema = False
    first_sandbox = False
    final_verdict: dict = {
        "target_tests_passed": False,
        "new_failures": [],
        "failed_tests": [],
    }
    elapsed = 0.0
    final_patch = None
    final_meta: dict = {}
    for attempt in range(retries + 1):
        patch, meta = fix_agent.run_fix(
            alert,
            root_cause,
            sample.get("references", []),
            attempt=attempt,
            repo_dir=EVAL_REPO_DIR,
            model=model,
            timeout=timeout,
        )
        elapsed += float(meta.get("elapsed_seconds", 0))
        schema_pass = bool(meta["validated"]) and not meta["degraded"]
        record = {"attempt": attempt, "schema_pass": schema_pass, "validated": bool(meta["validated"])}
        if schema_pass:
            report = sandbox.run_patch_tests(
                patch, attempt, repo_dir=EVAL_REPO_DIR, sandbox_root=EVAL_SANDBOX_ROOT
            )
            verdict, info = _sandbox_verdict(report, sample)
            record.update(
                {
                    "sandbox_pass": verdict,
                    "unit_tests": report.unit_tests,
                    "sandbox_details": report.details,
                    **info,
                }
            )
            final_verdict = info
        else:
            verdict = False
            record.update(
                {
                    "sandbox_pass": False,
                    "unit_tests": "未执行（校验链未通过）",
                    "sandbox_details": "LLM 输出未通过 schema/应用/编译校验，沙箱按语义错误计",
                }
            )
        if attempt == 0:
            first_schema = schema_pass
            first_sandbox = verdict
        attempts.append(record)
        final_patch = patch
        final_meta = meta
        if verdict:
            break

    return {
        "id": sample["id"],
        "note": sample.get("note", ""),
        "scenario": sample.get("scenario", "code_fix"),
        "split": sample.get("split", "dev"),
        "model": model,
        "patch_id": final_patch.patch_id if final_patch else "",
        "files": final_patch.files if final_patch else [],
        "expect_file": sample.get("expect_file", ""),
        "file_hit": final_patch.files == [sample["expect_file"]] if sample.get("expect_file") and final_patch else None,
        "degraded": bool(final_meta.get("degraded")),
        "validated": bool(final_meta.get("validated")),
        "routing": final_meta.get("routing", {}),
        "reason": final_meta.get("reason", ""),
        "elapsed_seconds": round(elapsed, 2),
        "schema_pass": any(a["schema_pass"] for a in attempts),
        "first_round_sandbox_pass": first_sandbox,
        "final_sandbox_pass": any(a["sandbox_pass"] for a in attempts),
        "target_tests_passed": final_verdict.get("target_tests_passed", False),
        "new_failures": final_verdict.get("new_failures", []),
        "failed_tests": final_verdict.get("failed_tests", []),
        "attempts": attempts,
        "diff": final_patch.diff if final_patch else "",
    }


def evaluate_abstain(sample: dict, threshold: float = 0.8) -> dict:
    """弃权样本：只经组合判据（不调用模型、不生成补丁）。"""
    root = sample["root_cause"]
    expect_route = str(sample.get("expect_route") or "runbook")
    decision = gate_decision.decide(
        confidence=float(root.get("confidence", 0.9)),
        threshold=threshold,
        error_type=str(root.get("error_type", "")),
        summary=str(root.get("summary", "")),
        suspect_files=root.get("suspect_files") or [],
        evidence_status=sample.get("evidence_status", "OK"),
        files_found=sample.get("files_found", root.get("suspect_files") or []),
        files_missing=sample.get("files_missing", []),
        scope_known=bool(sample.get("scope_known", True)),
        routing=sample.get("routing"),
    )
    return {
        "id": sample["id"],
        "scenario": sample.get("scenario", "abstain"),
        "split": sample.get("split", "dev"),
        "expect_route": expect_route,
        "route": decision.route,
        "route_correct": decision.route == expect_route,
        "released_code_fix": decision.allowed,
        "confidence": root.get("confidence", 0.0),
        "gate_reasons": decision.reasons,
    }


def _prompt_hash() -> str:
    return evalkit.prompt_hash([inspect.getsource(fix_agent.build_fix_prompt)])


def _load_baseline(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if "fix" in payload and isinstance(payload["fix"], dict):
        return payload["fix"]
    return payload if "metrics" in payload else None


def _mark(row: dict) -> str:
    """✓ 沙箱通过 / △ 校验链过但沙箱失败（语义错误）/ ✗ 生成阶段失败。"""
    if row["final_sandbox_pass"]:
        return "✓"
    return "△" if row["schema_pass"] else "✗"


def _write_quality_bar(model: str, report: dict, provider: str) -> dict:
    """仅当 Schema ≥90% 且 holdout 沙箱 ≥85% 时写入质量证据（不达标不写）。"""
    entry = evalkit.quality_bar_entry(report)
    if not (
        entry["schema_pass_rate"] >= model_routing.QUALITY_BAR["schema_pass_rate"]
        and entry["holdout_sandbox_pass_rate"] >= model_routing.QUALITY_BAR["holdout_sandbox_pass_rate"]
    ):
        return {"written": False, "reason": "未达 holdout 门槛", "entry": entry}
    payload: dict = {"models": {}}
    try:
        payload = json.loads(QUALITY_BAR_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    payload.setdefault("models", {})[f"{provider}:{model}"] = entry
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    evalkit.write_report(QUALITY_BAR_PATH, payload)
    return {"written": True, "entry": entry}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix 模型评测 V2（真实 LLM + 真实沙箱）")
    parser.add_argument("--id", default=None, help="仅评测指定样本 ID（如 F01 / AB01）")
    parser.add_argument("--model", action="append", default=None, help="模型（可重复做多模型对比）")
    parser.add_argument("--timeout", type=int, default=120, help="单次生成超时秒数（默认 120）")
    parser.add_argument("--retries", type=int, default=0, help="回炉重试次数（默认 0=只评首轮）")
    parser.add_argument("--split", default="dev", choices=["dev", "holdout", "all"], help="数据切分")
    parser.add_argument("--dataset", default=str(SAMPLES_PATH), help="代码修复集路径")
    parser.add_argument("--abstain-dataset", default=str(ABSTAIN_PATH), help="弃权集路径")
    parser.add_argument("--report", default=str(REPORT_PATH), help="报告输出路径")
    parser.add_argument("--baseline", default=str(BASELINE_PATH), help="批准基线路径")
    parser.add_argument("--write-quality-bar", action="store_true", help="holdout 达标时写质量证据")
    parser.add_argument("--check", action="store_true", help="门禁模式：未达阈值/相对基线退化时退出码 1")
    parser.add_argument("--write-baseline", action="store_true", help="holdout 达标时把本次结果写入批准基线")
    args = parser.parse_args()

    patch_payload = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    abstain_payload = json.loads(Path(args.abstain_dataset).read_text(encoding="utf-8"))
    patch_samples = evalkit.split_samples(patch_payload["samples"], args.split)
    abstain_samples = evalkit.split_samples(abstain_payload["samples"], args.split)
    if args.id:
        patch_samples = [s for s in patch_payload["samples"] if s["id"] == args.id]
        abstain_samples = [s for s in abstain_payload["samples"] if s["id"] == args.id]
        if not patch_samples and not abstain_samples:
            raise SystemExit(f"样本不存在: {args.id}")
    models = args.model or [fix_agent.DEFAULT_FIX_MODEL]
    provider = fix_agent.resolve_provider()
    total = len(patch_samples) + len(abstain_samples)
    print(
        f"Fix 评测 V2：provider={provider} 模型={models} 切分={args.split} "
        f"修复样本={len(patch_samples)} 弃权样本={len(abstain_samples)} 合计={total}"
    )
    if total < 60 and args.split == "all":
        print("[提示] 全量口径样本数 <60，请检查数据集是否完整（应覆盖 33 修复 + ≥27 弃权）")

    abstain_rows = [evaluate_abstain(sample) for sample in abstain_samples]
    abstain = evalkit.abstain_metrics(abstain_rows)
    if not patch_samples:
        # 只评弃权/路由样本（如 --id AB01 调试）：不产生修复指标，只校验误放行
        status = "ok" if abstain["false_pass_count"] == 0 else "hard_fail"
        report = {
            "schema_version": "v2",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "split": args.split,
            "mode": "abstain_only",
            "provider": provider,
            "abstain_metrics": abstain,
            "abstain_samples": abstain_rows,
            "gate_status": status,
        }
        print(
            f"弃权评测：{abstain['total']} 条 路由准确={abstain['abstain_accuracy']} "
            f"误放行={abstain['false_pass_count']}"
        )
        evalkit.write_report(args.report, report)
        print(f"评测报告已写入：{args.report}")
        print(f"门禁结论：{status}")
        if args.check and status != "ok":
            raise SystemExit(1)
        return
    results: dict[str, list[dict]] = {}
    comparisons: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    quality_bar_results: dict[str, dict] = {}

    for model in models:
        print(f"[模型] {model}")
        rows: list[dict] = []
        start = time.monotonic()
        for sample in patch_samples:
            row = evaluate_sample(sample, model, args.timeout, max(0, args.retries))
            rows.append(row)
            print(
                f"  [{_mark(row)}] {row['id']} files={row['files']} schema={row['schema_pass']} "
                f"first={row['first_round_sandbox_pass']} final={row['final_sandbox_pass']} "
                f"{row['elapsed_seconds']}s"
            )
        results[model] = rows
        metrics = evalkit.fix_metrics(rows)
        metrics.update(abstain)
        if args.split == "holdout":
            metrics["holdout_sandbox_pass_rate"] = metrics["final_sandbox_pass_rate"]
        metrics.update(evalkit.dataset_meta({"samples": patch_samples + abstain_samples}))
        comparison = evalkit.compare_baseline(metrics, _load_baseline(Path(args.baseline)), "fix")
        comparisons[model] = {"metrics": metrics, **comparison}
        report = evalkit.build_report(
            kind="fix",
            payload={"samples": patch_samples + abstain_samples},
            model=model,
            provider=provider,
            metrics=metrics,
            rows=rows,
            prompt_hash_value=_prompt_hash(),
            rag_index_hash=evalkit.file_hash(EVAL_REPO_DIR / "requirements.txt"),
            split=args.split,
            extra={
                "abstain_samples": abstain_rows,
                "abstain_metrics": abstain,
                "baseline_comparison": comparison,
                "repo_dir": str(EVAL_REPO_DIR),
                "retries": args.retries,
            },
        )
        reports[model] = report
        print(
            f"  schema={metrics['schema_pass_rate']} diff_apply={metrics['diff_apply_rate']} "
            f"first={metrics['first_round_sandbox_pass_rate']} final={metrics['final_sandbox_pass_rate']} "
            f"target={metrics['target_test_pass_rate']} new_regressions={metrics['new_regression_count']} "
            f"abstain={metrics['abstain_accuracy']} false_pass={metrics['false_pass_count']}"
        )
        print(f"  耗时 {round(time.monotonic() - start, 2)}s 门禁={comparison['status']}")
        for violation in comparison["violations"]:
            print(f"  [违规] {violation}")
        if args.write_quality_bar and args.split == "holdout":
            quality_bar_results[model] = _write_quality_bar(model, report, provider)
            print(f"  质量门槛证据：{quality_bar_results[model]}")

    if args.write_baseline:
        if len(models) > 1:
            raise SystemExit("--write-baseline 仅支持单模型评测，请分模型执行")
        if args.split != "holdout":
            raise SystemExit("--write-baseline 必须使用 --split holdout（质量门槛基于 holdout 切分）")
        model = models[0]
        status = comparisons[model]["status"]
        hard_fail = any(v.get("type") == "hard_limit" for v in comparisons[model]["violations"])
        if hard_fail or status == "regression":
            print("[基线] 存在硬阈值违规或相对基线退化，拒绝写入基线")
        else:
            evalkit.write_baseline(args.baseline, "fix", reports[model])
            print(f"[基线] 已写入批准基线：{args.baseline}")

    worst = "ok"
    for result in comparisons.values():
        if result["status"] == "hard_fail":
            worst = "hard_fail"
            break
        if result["status"] in ("regression", "baseline_incompatible"):
            worst = result["status"]
    report = {
        "schema_version": "v2",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "split": args.split,
        "models": models,
        "provider": provider,
        "dataset_version": evalkit.dataset_meta({"samples": patch_samples + abstain_samples})["dataset_version"],
        "abstain_metrics": abstain,
        "abstain_samples": abstain_rows,
        "comparison": comparisons,
        "key_metrics": {
            model: {
                "schema_pass_rate": m["metrics"]["schema_pass_rate"],
                "first_round_sandbox_pass_rate": m["metrics"]["first_round_sandbox_pass_rate"],
                "final_sandbox_pass_rate": m["metrics"]["final_sandbox_pass_rate"],
                "target_test_pass_rate": m["metrics"]["target_test_pass_rate"],
                "new_regression_count": m["metrics"]["new_regression_count"],
                "abstain_accuracy": m["metrics"]["abstain_accuracy"],
                "gate_status": m["status"],
            }
            for model, m in comparisons.items()
        },
        "quality_bar": quality_bar_results,
        "model_reports": reports,
        "results": results,
    }
    evalkit.write_report(args.report, report)
    print(f"评测报告已写入：{args.report}")
    print(f"门禁结论：{worst}")
    if args.check and worst in ("regression", "hard_fail"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
