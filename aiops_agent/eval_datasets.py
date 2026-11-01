"""评测集完整性校验与确定性安全回归夹具（M3）。

PR 阶段不调用模型：先校验数据集门槛（数量、ID 唯一、切分覆盖、场景覆盖、
版本哈希），再用「高置信模型输出」夹具验证组合判据必须拦截非代码根因、
且不得过度拦截代码根因。真实模型评测（eval_triage/eval_fix）在每日任务执行。

用法：
    from aiops_agent import eval_datasets
    result = eval_datasets.run(threshold=0.8)
    result["ok"], result["checks"], result["problems"]
"""

from __future__ import annotations

import json
from pathlib import Path

from . import evalkit, gate_decision

BASE_DIR = Path(__file__).resolve().parent.parent
TRIAGE_PATH = BASE_DIR / "data" / "triage_eval_samples.json"
FIX_PATH = BASE_DIR / "data" / "fix_eval_samples.json"
ABSTAIN_PATH = BASE_DIR / "data" / "fix_eval_abstain_samples.json"

MIN_TRIAGE_SAMPLES = 100
MIN_FIX_SAMPLES = 60
MIN_SAFETY_SAMPLES = 20
MIN_LOW_EVIDENCE_SAMPLES = 10
MIN_ABSTAIN_SAMPLES = 20
MIN_HOLDOUT_RATIO = 0.15
ROUTES = ("code_fix", "runbook", "human")
SPLITS = ("dev", "holdout")

# 计划要求重点回归的非代码根因场景（S08/S11 同类）必须持续有样本覆盖
REQUIRED_SAFETY_SCENARIOS = (
    "db_pool",
    "gateway_502",
    "dns",
    "tls",
    "rate_limit",
    "kafka_lag",
    "disk_full",
    "config_parse",
    "port_in_use",
    "permission",
)


def load(path: Path) -> dict:
    """读取数据集；缺文件或非法 JSON 时抛出带路径的 ValueError。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"数据集不可读: {path} ({exc})") from exc
    except ValueError as exc:
        raise ValueError(f"数据集不是合法 JSON: {path} ({exc})") from exc


def _check_version(payload: dict, problems: list[str], label: str) -> None:
    samples = payload.get("samples") or []
    if payload.get("schema_version") != "v2":
        problems.append(f"{label}: schema_version 应为 v2，实际 {payload.get('schema_version')!r}")
    expected = evalkit.canonical_hash(samples)
    if payload.get("dataset_version") != expected:
        problems.append(
            f"{label}: dataset_version 与内容不一致（{payload.get('dataset_version')} != {expected}），"
            "请运行 scripts/gen_eval_v2_datasets.py 重新生成"
        )


def _require(sample: dict, fields: tuple[str, ...], problems: list[str], label: str) -> None:
    """字段必须存在；空列表是合法值（低证据样本的 expected_keywords 允许为空）。"""
    for field in fields:
        if field not in sample:
            problems.append(f"{label}: 缺少字段 {field}")


def _require_text(sample: dict, fields: tuple[str, ...], problems: list[str], label: str) -> None:
    for field in fields:
        if not str(sample.get(field) or "").strip():
            problems.append(f"{label}: 字段 {field} 为空")


def validate_triage(payload: dict) -> list[str]:
    """根因集门槛：≥100 条、ID 唯一、切分与路由覆盖、安全回归场景齐全。"""
    problems: list[str] = []
    samples = payload.get("samples") or []
    _check_version(payload, problems, "triage")
    if len(samples) < MIN_TRIAGE_SAMPLES:
        problems.append(f"triage: 样本数 {len(samples)} < {MIN_TRIAGE_SAMPLES}")

    ids = [str(s.get("id", "")) for s in samples]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        problems.append(f"triage: 样本 ID 重复 {duplicates[:5]}")

    for sample in samples:
        label = f"triage/{sample.get('id', '?')}"
        _require(
            sample,
            ("alert", "clustered", "trace_ids", "expected_keywords", "scenario", "signal"),
            problems,
            label,
        )
        _require_text(sample, ("id", "scenario"), problems, label)
        if not isinstance(sample.get("trace_ids"), list) or not isinstance(sample.get("expected_keywords"), list):
            problems.append(f"{label}: trace_ids/expected_keywords 必须为列表")
        if sample.get("expect_route") not in ROUTES:
            problems.append(f"{label}: expect_route 非法 {sample.get('expect_route')!r}")
        if sample.get("split") not in SPLITS:
            problems.append(f"{label}: split 非法 {sample.get('split')!r}")
        signal = sample.get("signal") or {}
        if not signal.get("error_type"):
            problems.append(f"{label}: signal.error_type 为空")
        if sample.get("safety_regression") and sample.get("expect_route") != "runbook":
            problems.append(f"{label}: 安全回归样本的 expect_route 必须为 runbook")

    routes = {route: sum(1 for s in samples if s.get("expect_route") == route) for route in ROUTES}
    for route, count in routes.items():
        if count == 0:
            problems.append(f"triage: expect_route={route} 无样本（三类路由必须都有覆盖）")

    holdout = sum(1 for s in samples if s.get("split") == "holdout")
    if holdout == 0 or holdout < len(samples) * MIN_HOLDOUT_RATIO:
        problems.append(
            f"triage: holdout 样本 {holdout} 不足（要求 ≥{MIN_HOLDOUT_RATIO:.0%}，共 {len(samples)}）"
        )

    safety = [s for s in samples if s.get("safety_regression")]
    if len(safety) < MIN_SAFETY_SAMPLES:
        problems.append(f"triage: 安全回归样本 {len(safety)} < {MIN_SAFETY_SAMPLES}")
    scenarios = {str(s.get("scenario")) for s in safety}
    missing = [scenario for scenario in REQUIRED_SAFETY_SCENARIOS if scenario not in scenarios]
    if missing:
        problems.append(f"triage: 安全回归缺少场景 {missing}")
    for split in SPLITS:
        if not any(s.get("split") == split for s in safety):
            problems.append(f"triage: 安全回归样本未覆盖 {split} 切分")

    # 低证据阻断率是硬阈值（必须 100%）；样本不足时必须显式失败而不是跳过
    low = [s for s in samples if s.get("expect_low_confidence")]
    if len(low) < MIN_LOW_EVIDENCE_SAMPLES:
        problems.append(f"triage: 低证据样本 {len(low)} < {MIN_LOW_EVIDENCE_SAMPLES}")
    if not any(s.get("split") == "holdout" for s in low):
        problems.append("triage: 低证据样本未覆盖 holdout 切分")
    return problems


def validate_fix(patch_payload: dict, abstain_payload: dict) -> list[str]:
    """修复集门槛：可修复 + 弃权合计 ≥60 条，弃权样本路由与字段齐全。"""
    problems: list[str] = []
    patch_samples = patch_payload.get("samples") or []
    abstain_samples = abstain_payload.get("samples") or []
    _check_version(patch_payload, problems, "fix")
    _check_version(abstain_payload, problems, "fix_abstain")

    total = len(patch_samples) + len(abstain_samples)
    if total < MIN_FIX_SAMPLES:
        problems.append(f"fix: 修复+弃权样本合计 {total} < {MIN_FIX_SAMPLES}")
    if len(abstain_samples) < MIN_ABSTAIN_SAMPLES:
        problems.append(f"fix: 弃权样本 {len(abstain_samples)} < {MIN_ABSTAIN_SAMPLES}")

    ids = [str(s.get("id", "")) for s in patch_samples + abstain_samples]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        problems.append(f"fix: 样本 ID 重复 {duplicates[:5]}")

    for sample in patch_samples:
        label = f"fix/{sample.get('id', '?')}"
        _require(sample, ("alert", "root_cause", "expect_file", "fix_target_tests"), problems, label)
        _require_text(sample, ("id", "expect_file"), problems, label)
        if sample.get("split") not in SPLITS:
            problems.append(f"{label}: split 非法 {sample.get('split')!r}")

    abstain_routes: dict[str, int] = {}
    for sample in abstain_samples:
        label = f"fix_abstain/{sample.get('id', '?')}"
        root = sample.get("root_cause") or {}
        _require(root, ("error_type", "summary"), problems, f"{label}.root_cause")
        _require_text(root, ("error_type", "summary"), problems, f"{label}.root_cause")
        if not isinstance(root.get("confidence"), (int, float)):
            problems.append(f"{label}: root_cause.confidence 缺失")
        route = sample.get("expect_route")
        if route not in ("runbook", "human"):
            problems.append(f"{label}: expect_route 必须为 runbook|human，实际 {route!r}")
        else:
            abstain_routes[route] = abstain_routes.get(route, 0) + 1
        if sample.get("split") not in SPLITS:
            problems.append(f"{label}: split 非法 {sample.get('split')!r}")
    for route in ("runbook", "human"):
        if abstain_routes.get(route, 0) == 0:
            problems.append(f"fix: 弃权集缺少 expect_route={route} 的样本")
    return problems


def safety_fixtures(triage_payload: dict, abstain_payload: dict) -> list[dict]:
    """构造「高置信模型输出」夹具：安全回归样本必须被组合判据拦截。"""
    fixtures: list[dict] = []
    for sample in triage_payload.get("samples") or []:
        if not sample.get("safety_regression"):
            continue
        signal = sample.get("signal") or {}
        fixtures.append(
            {
                "source": f"triage:{sample.get('id')}",
                "confidence": 0.99,
                "error_type": str(signal.get("error_type", "")),
                "summary": str(signal.get("summary", "")),
                "suspect_files": ["app/service.py"],
                "files_found": ["app/service.py"],
                "evidence_status": "OK",
                "expect_route": gate_decision.ROUTE_RUNBOOK,
            }
        )
    for sample in abstain_payload.get("samples") or []:
        root = sample.get("root_cause") or {}
        suspect = list(root.get("suspect_files") or [])
        fixtures.append(
            {
                "source": f"abstain:{sample.get('id')}",
                "confidence": max(0.9, float(root.get("confidence") or 0)),
                "error_type": str(root.get("error_type", "")),
                "summary": str(root.get("summary", "")),
                "suspect_files": suspect,
                "files_found": list(sample.get("files_found") or suspect),
                "evidence_status": sample.get("evidence_status", "OK"),
                "expect_route": str(sample.get("expect_route", "")),
            }
        )
    return fixtures


def code_fixtures(triage_payload: dict) -> list[dict]:
    """代码根因夹具：证据充分且文件可定位时不得被过度拦截。"""
    fixtures: list[dict] = []
    for sample in triage_payload.get("samples") or []:
        if sample.get("expect_route") != "code_fix":
            continue
        signal = sample.get("signal") or {}
        fixtures.append(
            {
                "source": f"triage:{sample.get('id')}",
                "confidence": 0.95,
                "error_type": str(signal.get("error_type", "")),
                "summary": str(signal.get("summary", "")),
                "suspect_files": ["app/service.py"],
                "files_found": ["app/service.py"],
                "evidence_status": "OK",
                "expect_route": gate_decision.ROUTE_CODE_FIX,
            }
        )
    return fixtures


def check_fixtures(fixtures: list[dict], threshold: float = 0.8) -> list[str]:
    """对夹具逐一跑组合判据，返回问题列表（空列表=全部符合预期）。"""
    problems: list[str] = []
    for fixture in fixtures:
        decision = gate_decision.decide(
            confidence=fixture["confidence"],
            threshold=threshold,
            error_type=fixture["error_type"],
            summary=fixture["summary"],
            suspect_files=fixture["suspect_files"],
            evidence_status=fixture["evidence_status"],
            files_found=fixture["files_found"],
        )
        if decision.route != fixture["expect_route"]:
            problems.append(
                f"{fixture['source']}: 期望 route={fixture['expect_route']}，实际 {decision.route}"
                f"（allowed={decision.allowed}, reasons={decision.blocked_codes}）"
            )
        elif fixture["expect_route"] == gate_decision.ROUTE_CODE_FIX and not decision.allowed:
            problems.append(f"{fixture['source']}: 代码根因被过度拦截 {decision.blocked_codes}")
    return problems


def run(
    *,
    threshold: float = 0.8,
    triage_path: Path = TRIAGE_PATH,
    fix_path: Path = FIX_PATH,
    abstain_path: Path = ABSTAIN_PATH,
) -> dict:
    """执行全部门禁检查；任何一项失败即 ``ok=False``。"""
    checks: list[dict] = []
    problems: list[str] = []

    def record(name: str, detail: str, errors: list[str]) -> None:
        checks.append({"name": name, "ok": not errors, "detail": detail, "problems": errors})
        problems.extend(errors)

    try:
        triage = load(triage_path)
        fix = load(fix_path)
        abstain = load(abstain_path)
    except ValueError as exc:
        record("数据集加载", "读取三个评测数据集", [str(exc)])
        return {"ok": False, "checks": checks, "problems": problems}

    triage_problems = validate_triage(triage)
    record(
        "根因集完整性",
        f"{len(triage.get('samples') or [])} 条",
        triage_problems,
    )
    fix_problems = validate_fix(fix, abstain)
    record(
        "修复集完整性",
        f"{len(fix.get('samples') or [])} 修复 + {len(abstain.get('samples') or [])} 弃权",
        fix_problems,
    )

    safety = safety_fixtures(triage, abstain)
    record("安全回归夹具", f"{len(safety)} 条高置信输入必须拦截", check_fixtures(safety, threshold))
    code = code_fixtures(triage)
    record("代码根因放行", f"{len(code)} 条代码根因不得过度拦截", check_fixtures(code, threshold))
    return {"ok": not problems, "checks": checks, "problems": problems}
