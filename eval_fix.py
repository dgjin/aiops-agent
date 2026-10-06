"""Fix 模型对比评估脚本（WP4 增强交付物）：diff 语义正确率 = 校验链 + 真实沙箱单测。

口径（对齐实施计划 WP4 验收「建议 diff 可编译」与增强目标「更大模型提升 diff 语义正确率」）：
    - 样本来源：data/fix_eval_samples.json（评测夹具仓库 data/fix_eval_repo 的真实可修复
      场景，33 个样本；基线由 scripts/gen_fix_eval_samples.py 实时采集）；
    - 每个样本真实调用 fix_agent.run_fix：LLM 生成 → pydantic schema → 应用 → 编译校验；
    - schema_pass = 校验链全过（meta.validated，非降级）；
    - sandbox_pass = 补丁送入真实沙箱（Docker 隔离运行夹具仓库全量单测）：修复目标用例
      转为通过且不引入新的失败用例（夹具基线缺陷为已知失败，其余用例作回归保护）
      ——diff 语义正确率主指标；
    - 支持多模型对比：--model 可重复传入，报告按模型汇总对比。

用法：
    .venv/bin/python eval_fix.py                                        # 默认模型（AIOPS_FIX_MODEL）
    .venv/bin/python eval_fix.py --model qwen3:8b --model qwen3.8:27b-mlx
    .venv/bin/python eval_fix.py --id F01 --model qwen3:8b

输出：
    - 控制台逐样本明细 + 各模型指标对比；
    - data/fix_eval_report.json（评测报告，持久化归档）。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from aiops_agent import fix_agent, sandbox
from aiops_agent.models import Alert, RootCause, TestReport

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_PATH = BASE_DIR / "data" / "fix_eval_samples.json"
REPORT_PATH = BASE_DIR / "data" / "fix_eval_report.json"
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


def evaluate_sample(sample: dict, model: str, timeout: int) -> dict:
    """评测单样本：真实调用修复生成 + 真实沙箱验证，返回明细行。"""
    alert = Alert(**sample["alert"])
    root_cause = RootCause(**sample["root_cause"])
    patch, meta = fix_agent.run_fix(
        alert,
        root_cause,
        sample.get("references", []),
        attempt=0,
        repo_dir=EVAL_REPO_DIR,
        model=model,
        timeout=timeout,
    )
    schema_pass = bool(meta["validated"]) and not meta["degraded"]
    row: dict = {
        "id": sample["id"],
        "note": sample.get("note", ""),
        "model": model,
        "patch_id": patch.patch_id,
        "files": patch.files,
        "expect_file": sample.get("expect_file", ""),
        "file_hit": patch.files == [sample["expect_file"]] if sample.get("expect_file") else None,
        "degraded": bool(meta["degraded"]),
        "validated": bool(meta["validated"]),
        "reason": meta["reason"],
        "elapsed_seconds": meta["elapsed_seconds"],
        "schema_pass": schema_pass,
        "diff": patch.diff,
    }
    if schema_pass:
        report = sandbox.run_patch_tests(
            patch, 0, repo_dir=EVAL_REPO_DIR, sandbox_root=EVAL_SANDBOX_ROOT
        )
        verdict, verdict_info = _sandbox_verdict(report, sample)
        row.update(
            {
                "sandbox_pass": verdict,
                "unit_tests": report.unit_tests,
                "sandbox_details": report.details,
                **verdict_info,
            }
        )
    else:
        row.update(
            {
                "sandbox_pass": False,
                "unit_tests": "未执行（校验链未通过）",
                "sandbox_details": "LLM 输出未通过 schema/应用/编译校验，沙箱按语义错误计",
                "failed_tests": [],
                "target_tests_passed": False,
                "new_failures": [],
            }
        )
    return row


def summarize(rows: list[dict]) -> dict:
    """按模型汇总指标（diff 语义正确率 = 沙箱全绿率）。"""
    total = len(rows) or 1
    return {
        "total": len(rows),
        "schema_pass_rate": round(sum(1 for r in rows if r["schema_pass"]) / total, 4),
        "file_hit_rate": round(sum(1 for r in rows if r["file_hit"]) / total, 4),
        "sandbox_pass_rate": round(sum(1 for r in rows if r["sandbox_pass"]) / total, 4),
        "avg_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in rows) / total, 2),
    }


def _mark(row: dict) -> str:
    """✓ 沙箱全绿 / △ 校验链过但沙箱失败（语义错误）/ ✗ 生成阶段失败。"""
    if row["sandbox_pass"]:
        return "✓"
    return "△" if row["schema_pass"] else "✗"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix 模型对比评估（真实 LLM + 真实沙箱）")
    parser.add_argument("--id", default=None, help="仅评测指定样本 ID（如 F01）")
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help=f"模型（可重复传参做多模型对比；默认 {fix_agent.DEFAULT_FIX_MODEL}）",
    )
    parser.add_argument("--timeout", type=int, default=120, help="单次生成超时秒数（默认 120）")
    args = parser.parse_args()

    payload = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    samples = payload["samples"]
    if args.id:
        samples = [s for s in samples if s["id"] == args.id]
        if not samples:
            raise SystemExit(f"样本不存在: {args.id}")
    models = args.model or [fix_agent.DEFAULT_FIX_MODEL]
    print(f"Fix 模型对比评估：模型={models} 样本数={len(samples)} timeout={args.timeout}s")

    results: dict[str, list[dict]] = {}
    for model in models:
        print(f"[模型] {model}")
        rows: list[dict] = []
        start = time.monotonic()
        for sample in samples:
            row = evaluate_sample(sample, model, args.timeout)
            rows.append(row)
            note = f" sandbox={row['unit_tests']}" if row["sandbox_pass"] else ""
            reason = f" reason={row['reason'][:70]}" if not row["schema_pass"] else ""
            print(
                f"  [{_mark(row)}] {row['id']} files={row['files']} file_hit={row['file_hit']} "
                f"schema={row['schema_pass']} sandbox={row['sandbox_pass']}{note} "
                f"{row['elapsed_seconds']}s{reason}"
            )
        print(f"  耗时 {round(time.monotonic() - start, 2)}s")
        results[model] = rows

    comparison = {model: summarize(rows) for model, rows in results.items()}
    print("模型对比（sandbox_pass_rate = diff 语义正确率）：")
    for model, metrics in comparison.items():
        print(
            f"  {model}: schema={metrics['schema_pass_rate']} sandbox={metrics['sandbox_pass_rate']} "
            f"file_hit={metrics['file_hit_rate']} 平均 {metrics['avg_elapsed_seconds']}s"
        )

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "models": models,
        "samples_path": str(SAMPLES_PATH),
        "repo_dir": str(EVAL_REPO_DIR),
        "sandbox_root": str(EVAL_SANDBOX_ROOT),
        "comparison": comparison,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评测报告已写入：{REPORT_PATH}")


if __name__ == "__main__":
    main()
