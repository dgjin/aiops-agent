"""模型路由（M3）：把「用哪个模型修」变成显式、可评测、可拦截的决策。

口径（对齐闭环实施计划 M3 第 3 节）：

- 快速分诊/摘要/候选定位保持 triage 模型（`qwen3:8b` 一类小模型），不参与代码修复路由；
- 自动修复默认路由到 **通过 holdout 质量门槛** 的 27B 本地模型或 Qoder 提供者；
- 8B 不再作为无人值守修复的默认模型：显式指定可复现评测，但生产策略
  （`fix_routing.require_quality_bar=true`）下未达标模型一律返回人工/排队，
  **不静默降级** 到未达标模型；
- 复杂多文件任务优先 Qoder；生成失败仍由 `fix_agent` 的确定性安全降级兜底。

本模块不直接生成补丁，只回答「谁来做、是否达标、为什么」；纯函数之外仅有一次
可缓存的 Qoder 可用性探测（`qoder_ready`），便于单测注入。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 质量门槛（与实施计划一致，验收不得放宽）
QUALITY_BAR = {
    "schema_pass_rate": 0.90,
    "holdout_sandbox_pass_rate": 0.85,
}

# 自动修复默认模型（27B 本地模型；Qoder 由 provider 切换）
DEFAULT_FIX_MODEL = "qwen3.8:27b-mlx"
DEFAULT_PROVIDER = "ollama"
MIN_FIX_PARAM_B = 27.0
MULTI_FILE_THRESHOLD = 2

ROUTE_CODE_FIX = "code_fix"
ROUTE_HUMAN = "human"

_TIER_SMALL = "small"
_TIER_LARGE = "large"
_TIER_EXTERNAL = "external"

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b")
_QODER_CACHE: bool | None = None


def param_size(model: str) -> float:
    """从模型名提取参数量（B）：qwen3:8b → 8，qwen3.8:27b-mlx → 27；未知返回 0。"""
    sizes = [float(m) for m in _PARAM_RE.findall(str(model or "").lower())]
    return max(sizes) if sizes else 0.0


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def qoder_ready(bin_path: str | None = None) -> bool:
    """Qoder CLI 可用性探测（带进程内缓存；AIOPS_QODER_SCOPE_CHECK=0 可关闭）。"""
    global _QODER_CACHE
    if not _env_flag("AIOPS_QODER_SCOPE_CHECK", True):
        return False
    if _QODER_CACHE is not None:
        return _QODER_CACHE
    try:
        from . import qoder_fix

        ok, _reason = qoder_fix.qoder_available(bin_path)
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理（安全侧）
        ok = False
    _QODER_CACHE = ok
    return ok


def reset_qoder_cache() -> None:
    """测试用：清空 Qoder 可用性缓存。"""
    global _QODER_CACHE
    _QODER_CACHE = None


def load_quality_bar(path: str | os.PathLike | None = None) -> dict | None:
    """读取质量门槛证据文件（由 eval_fix --write-quality-bar 在达标时生成）。

    缺失/损坏 → None（视为无证据，require_quality_bar 时路由人工）。
    """
    target = path if path is not None else os.environ.get("AIOPS_MODEL_QUALITY_BAR")
    if not target:
        return None
    try:
        payload = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def bar_entry(quality_bar: dict | None, provider: str, model: str) -> dict | None:
    """在证据文件中查找 provider/model 条目（兼容 ollama:model 键与 models 子表）。"""
    if not quality_bar:
        return None
    models = quality_bar.get("models") if isinstance(quality_bar.get("models"), dict) else {}
    for key in (f"{provider}:{model}", model, provider):
        entry = models.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def meets_quality_bar(entry: dict | None, dataset_version: str | None = None) -> bool:
    """条目是否同时满足 Schema 与 holdout 沙箱门槛（可选校验数据集版本）。"""
    if not entry:
        return False
    if dataset_version and entry.get("dataset_version") not in (None, "", dataset_version):
        return False
    try:
        schema = float(entry.get("schema_pass_rate", 0))
        sandbox = float(entry.get("holdout_sandbox_pass_rate", 0))
    except (TypeError, ValueError):
        return False
    return schema >= QUALITY_BAR["schema_pass_rate"] and sandbox >= QUALITY_BAR["holdout_sandbox_pass_rate"]


@dataclass
class RouteDecision:
    """一次修复路由结论（labels 直接写入 patch meta 与评测报告）。"""

    provider: str
    model: str
    route: str
    tier: str
    qualified: bool
    reason: str
    multi_file: bool = False
    labels: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "route": self.route,
            "tier": self.tier,
            "qualified": self.qualified,
            "reason": self.reason,
            "multi_file": self.multi_file,
        }


def _tier_of(provider: str, model: str) -> str:
    if provider == "qoder":
        return _TIER_EXTERNAL
    return _TIER_LARGE if param_size(model) >= MIN_FIX_PARAM_B else _TIER_SMALL


def route_fix(
    suspect_files: list[str] | None = None,
    *,
    explicit_provider: str | None = None,
    explicit_model: str | None = None,
    qoder_available: bool | None = None,
    quality_bar: dict | None = None,
    require_quality_bar: bool = False,
    dataset_version: str | None = None,
    prefer_qoder_for_multi_file: bool = True,
    multi_file_threshold: int = MULTI_FILE_THRESHOLD,
) -> RouteDecision:
    """决定本次修复的 provider/model。

    ``require_quality_bar=True``（生产策略）时，未在质量证据文件中达标的模型
    返回 ``route=human`` —— 由工作流转人工，不静默降级到小模型。
    """
    files = {f for f in (suspect_files or []) if f}
    multi_file = len(files) >= max(1, int(multi_file_threshold))
    ready = qoder_ready() if qoder_available is None else bool(qoder_available)

    provider = (explicit_provider or os.environ.get("AIOPS_FIX_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in ("ollama", "qoder"):
        provider = DEFAULT_PROVIDER
    model = (
        explicit_model
        or os.environ.get("AIOPS_FIX_MODEL")
        or (os.environ.get("AIOPS_TRIAGE_MODEL") if provider == "ollama" else None)
        or (os.environ.get("AIOPS_QODER_MODEL") if provider == "qoder" else None)
        or (DEFAULT_FIX_MODEL if provider == "ollama" else "qoder")
    )

    reason = "explicit_override" if (explicit_provider or explicit_model) else "configured"
    # 复杂多文件：优先 Qoder（仅在未显式指定 provider 时自动切换）
    if (
        multi_file
        and prefer_qoder_for_multi_file
        and not explicit_provider
        and provider != "qoder"
        and ready
    ):
        provider = "qoder"
        model = explicit_model or os.environ.get("AIOPS_QODER_MODEL") or "qoder"
        reason = "multi_file_prefers_qoder"

    tier = _tier_of(provider, model)
    entry = bar_entry(quality_bar, provider, model)
    qualified = meets_quality_bar(entry, dataset_version) if quality_bar else (tier in (_TIER_LARGE, _TIER_EXTERNAL))
    if tier == _TIER_SMALL and not meets_quality_bar(entry, dataset_version):
        # 小模型仅在质量证据明确达标时才可视为合格（8B 不作为无人值守默认）
        qualified = False

    route = ROUTE_CODE_FIX
    if not qualified:
        if require_quality_bar:
            route = ROUTE_HUMAN
            reason = "quality_bar_not_met"
        else:
            reason = f"{reason}:unverified_model" if reason else "unverified_model"

    labels = {
        "provider": provider,
        "model": model,
        "tier": tier,
        "qualified": qualified,
        "reason": reason,
        "multi_file": multi_file,
        "qoder_available": ready,
    }
    return RouteDecision(
        provider=provider,
        model=model,
        route=route,
        tier=tier,
        qualified=qualified,
        reason=reason,
        multi_file=multi_file,
        labels=labels,
    )


def describe_for_meta(provider: str, model: str, suspect_files: list[str] | None = None) -> dict:
    """为 patch meta 生成路由标签（不改变既有 provider/model 解析结果）。

    `fix_agent.run_fix` 已按显式参数/环境变量解析出实际使用的 provider/model，
    这里只补齐 tier / 达标 / 多文件 等可观测字段，供评测与运营分析聚合。
    """
    tier = _tier_of(provider, model)
    files = {f for f in (suspect_files or []) if f}
    entry = bar_entry(load_quality_bar(), provider, model)
    return {
        "provider": provider,
        "model": model,
        "tier": tier,
        "qualified": meets_quality_bar(entry) if entry else tier in (_TIER_LARGE, _TIER_EXTERNAL),
        "multi_file": len(files) >= MULTI_FILE_THRESHOLD,
    }
