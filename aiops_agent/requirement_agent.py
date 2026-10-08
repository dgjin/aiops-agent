"""需求分析器（需求智能分析闭环的 LLM 侧）：结构化分析 + 反馈迭代 + 安全侧兜底。

链路（「分析结果查看 → 管理员反馈 → 再次分析 → 批准进入修复工作流」的分析段）：

    analyze_requirement(entry, app, feedbacks, previous, references)
      1. build_analysis_prompt：需求原文（标题 / 内容 / 评估意见）+ 管理员历次反馈 +
         上一版分析（迭代时注入）+ Code RAG 代码引用（可选）→ 要求输出 JSON；
      2. call_ollama（复用 triage 的本地 LLM 通道，JSON 模式）→ AnalysisOutput pydantic 校验；
      3. 任一步失败 → degraded 分析（degraded=True 留痕、confidence=0），**绝不抛异常**——
         由 BFF 存为「失败版本」，管理员可提交反馈或重试。

安全侧原则（与 triage / fix_agent 一致）：分析结果只进入「展示 + 待批准」链路，
能否进入修复工作流由管理员批准（approve）决定，LLM 不直接触发任何执行。

环境变量：
    AIOPS_REQUIREMENT_MODEL   分析模型（缺省复用 triage 默认模型，如 qwen3:8b）。
"""

from __future__ import annotations

import os
import time

from pydantic import BaseModel, ConfigDict, Field

from .triage import DEFAULT_MODEL, call_ollama, strip_code_fence

# 分析模型：AIOPS_REQUIREMENT_MODEL 未设置时复用 triage 默认模型
DEFAULT_ANALYSIS_MODEL = os.environ.get("AIOPS_REQUIREMENT_MODEL") or DEFAULT_MODEL

# 单次分析默认超时（结构化长文输出，比 triage 的单次判定留更宽裕）
DEFAULT_ANALYSIS_TIMEOUT = 120

_KIND_LABELS = {
    "REQUIREMENT": "功能需求",
    "SUGGESTION": "改进建议",
    "BUG": "问题缺陷",
    "OTHER": "其他",
}


class AnalysisOutput(BaseModel):
    """LLM 结构化分析输出 schema（pydantic 强校验）。"""

    model_config = ConfigDict(extra="ignore")

    understanding: str = Field(min_length=1)
    plan: list[str] = Field(default_factory=list)
    suspect_files: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    risk: str = ""
    complexity: str = ""
    confidence: float = Field(ge=0.0, le=1.0)


def _clean_list(values: list[str] | None) -> list[str]:
    """清洗列表字段：去空白、滤空项（LLM 常混入空串/占位）。"""
    return [str(item).strip() for item in values or [] if str(item).strip()]


def _format_feedbacks(feedbacks: list[dict] | None) -> str:
    """历次管理员反馈（优化建议 / 具体要求）→ prompt 段落。"""
    lines: list[str] = []
    for item in feedbacks or []:
        text = str(item.get("feedback") or "").strip()
        if not text:
            continue
        actor = str(item.get("actor") or "").strip()
        ts = str(item.get("ts") or "").strip()
        lines.append(f"- [v{item.get('version', '?')} · {ts} · {actor or '管理员'}] {text}")
    return "\n".join(lines) if lines else "- （无，首轮分析）"


def _format_previous(previous: dict | None) -> str:
    """上一版分析摘要 → prompt 段落（迭代时供模型对照修订）。"""
    if not previous:
        return "- （无，首轮分析）"
    plan_lines = "\n".join(f"  {i}. {step}" for i, step in enumerate(previous.get("plan") or [], 1))
    return (
        f"- 理解：{previous.get('understanding', '')}\n"
        f"- 方案：\n{plan_lines or '  （空）'}\n"
        f"- 风险：{previous.get('risk', '')}\n"
        f"- 复杂度：{previous.get('complexity', '')}"
    )


def _format_references(references: list[dict] | None) -> str:
    """Code RAG 检索结果 → prompt 段落（供分析定位真实文件与落点）。"""
    lines: list[str] = []
    for ref in references or []:
        if not isinstance(ref, dict):
            continue
        lines.append(
            f"- [{ref.get('similarity', '?')}] {ref.get('file', '?')}::{ref.get('qualname', '')}"
            f" (L{ref.get('start_line', '?')}-{ref.get('end_line', '?')})"
        )
    return "\n".join(lines) if lines else "- （无检索结果）"


def build_analysis_prompt(
    entry: dict,
    app: dict | None,
    feedbacks: list[dict] | None = None,
    previous: dict | None = None,
    references: list[dict] | None = None,
) -> str:
    """构造需求分析 prompt：需求原文 + 管理员反馈 + 上一版分析 + 代码引用。"""
    kind = str(entry.get("kind") or "")
    app_name = str((app or {}).get("name") or (app or {}).get("service") or "未知应用")
    return f"""你是资深需求分析师。请分析以下已纳入需求基线的条目，输出结构化分析结果（只输出一个 JSON 对象）。

## 需求条目
- 编号: #{entry.get('id', '?')}（{_KIND_LABELS.get(kind, kind or '未分类')}，优先级 {entry.get('priority') or '未定级'}）
- 目标应用: {app_name}
- 标题: {entry.get('title', '')}
- 内容: {entry.get('content') or '（未填写）'}
- 需求方评估意见: {entry.get('assessment') or '（无）'}
- 提交人 / 部门: {entry.get('submitter') or '—'} / {entry.get('department') or '—'}
- 基线版本: {entry.get('baselineVersion') or '—'}

## 相关代码引用（自动检索，供定位真实文件与落点）
{_format_references(references)}

## 管理员历次反馈（必须逐条落实）
{_format_feedbacks(feedbacks)}

## 上一版分析（供对照修订）
{_format_previous(previous)}

## 输出 JSON（不要输出其他内容）
{{
  "understanding": "需求理解：复述需求意图、明确范围与边界（一两句话）",
  "plan": ["实现步骤1", "实现步骤2", "…"],
  "suspect_files": ["仓库相对路径1", "…"],
  "acceptance": ["验收要点1", "…"],
  "risk": "风险与注意事项（兼容性 / 数据 / 回归面）",
  "complexity": "复杂度：低 / 中 / 高（一句话理由）",
  "confidence": 0.0
}}

## 分析要求
- 若存在管理员反馈，必须逐条落实其中的优化建议或具体要求，并在 plan 中体现调整；
- plan 步骤须可执行、可验证（尽量结合代码引用给出落地位置与做法）；
- suspect_files 填预计改动的仓库相对路径（优先引用中出现的真实文件；不确定可少填）；
- 全部使用中文。"""


def analysis_payload(output: AnalysisOutput) -> dict:
    """pydantic 输出 → 存储/展示用 dict。"""
    return {
        "understanding": output.understanding.strip(),
        "plan": _clean_list(output.plan),
        "suspect_files": _clean_list(output.suspect_files),
        "acceptance": _clean_list(output.acceptance),
        "risk": output.risk.strip(),
        "complexity": output.complexity.strip(),
        "confidence": output.confidence,
        "degraded": False,
    }


def degraded_analysis(entry: dict, reason: str) -> dict:
    """LLM 不可用/输出非法时的安全侧降级：保留需求骨架，confidence=0（不可批准）。"""
    return {
        "understanding": f"自动分析未完成：{reason}。需求原文：{entry.get('title', '')}",
        "plan": [],
        "suspect_files": [],
        "acceptance": [],
        "risk": "分析未成功，请提交反馈重试，或人工补充方案后再考虑批准。",
        "complexity": "",
        "confidence": 0.0,
        "degraded": True,
    }


def analyze_requirement(
    entry: dict,
    app: dict | None = None,
    feedbacks: list[dict] | None = None,
    previous: dict | None = None,
    references: list[dict] | None = None,
    model: str | None = None,
    timeout: int = DEFAULT_ANALYSIS_TIMEOUT,
) -> tuple[dict, dict]:
    """执行一次需求分析：返回 (analysis dict, meta)。

    meta 含 model / degraded / reason / elapsed_seconds 留痕。
    与 fix_agent.run_fix 同约定：一切异常走安全侧兜底，绝不抛出。
    """
    used_model = model or DEFAULT_ANALYSIS_MODEL
    meta: dict = {
        "model": used_model,
        "degraded": False,
        "reason": "",
        "elapsed_seconds": 0.0,
    }
    start = time.monotonic()
    try:
        prompt = build_analysis_prompt(entry, app, feedbacks, previous, references)
        raw = call_ollama(prompt, model=used_model, timeout=timeout)
        output = AnalysisOutput.model_validate_json(strip_code_fence(raw))
        analysis = analysis_payload(output)
        meta["model"] = used_model
    except Exception as exc:  # noqa: BLE001 - 一切异常走安全侧兜底，不阻断闭环节奏
        reason = f"{type(exc).__name__}: {exc}"
        analysis = degraded_analysis(entry, reason)
        meta.update({"degraded": True, "reason": reason})
    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return analysis, meta
