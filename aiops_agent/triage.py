"""LLM 根因分析服务（WP3 交付物）：Ollama 调用 + pydantic 结构化输出校验。

流程（对齐实施计划 WP3 任务 2/3）：
    build_triage_prompt(alert, clustered, trace_ids)
      → call_ollama(prompt)（JSON 模式、关闭思维链，低温采样）
      → TriageOutput.model_validate_json（解析失败先剥 markdown 代码围栏）
      → RootCause 数据模型（供工作流消费）

安全侧设计（对齐闸门 1「不确定路径一律走向驳回或转人工」）：
    - LLM 输出不合 schema / 调用超时或不可用 → fallback_root_cause（confidence=0.0）
      → 工作流闸门 1 自动拦截转人工，绝不进入自动修复；
    - confidence 必须为 0~1，越界即 schema 校验失败。

环境变量：
    AIOPS_OLLAMA_URL     Ollama 地址（默认 http://localhost:11434）
    AIOPS_TRIAGE_MODEL   triage 模型（默认 qwen3:8b；本地模型，无需外网）
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import Alert, RootCause

DEFAULT_OLLAMA_URL = os.environ.get("AIOPS_OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("AIOPS_TRIAGE_MODEL", "qwen3:8b")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class TriageOutput(BaseModel):
    """LLM 结构化根因输出 schema（pydantic 强校验，WP3 任务 2）。"""

    model_config = ConfigDict(extra="ignore")

    error_type: str = Field(min_length=1)
    suspect_files: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)


def strip_code_fence(text: str) -> str:
    """剥离可能的 markdown 代码围栏（```json ... ```），返回裸 JSON 文本。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def build_triage_prompt(alert: Alert, clustered: dict, trace_ids: list[str] | None = None) -> str:
    """构造 triage prompt：告警 + 聚类模板（降噪后）+ 调用链 ID。"""
    templates = clustered.get("templates", [])[:10]
    template_lines = (
        "\n".join(f"- [{t['count']}x] {t['pattern']}" for t in templates)
        if templates
        else "- （无日志证据）"
    )
    trace_line = ", ".join(trace_ids or []) or "（无）"
    return f"""你是资深 SRE 故障根因分析专家。请基于以下证据判定故障根因，只输出一个 JSON 对象。

## 告警
- alert_id: {alert.alert_id}
- 服务: {alert.service}
- 级别: {alert.severity}
- 描述: {alert.description}

## 日志模板聚类（Drain3 降噪，格式 [出现次数x] 模板）
{template_lines}

## 相关调用链 trace_id
{trace_line}

## 输出要求（JSON，不要输出其他内容）
{{
  "error_type": "异常/故障类型，如 NullPointerException / TimeoutError / OutOfMemoryError",
  "suspect_files": ["最可疑的源文件相对路径，按可疑度降序，最多 3 个"],
  "confidence": 0.0,
  "summary": "一两句中文根因说明"
}}

## 置信度校准规则（必须遵守）
- 证据明确指向特定异常类型（模板中直接出现异常名/堆栈/错误码）时，confidence 可高于 0.8；
- 证据不足、模板全是正常日志或日志极少、无法定位具体异常时，confidence 必须低于 0.8；
- confidence 必须是 0 到 1 之间的小数。"""


def call_ollama(prompt: str, model: str | None = None, timeout: int = 45) -> str:
    """调用 Ollama /api/chat（JSON 模式、关闭思维链），返回助手回复文本。"""
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{DEFAULT_OLLAMA_URL.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    content = (data.get("message", {}).get("content") or "").strip()
    if not content:
        raise ValueError("Ollama 返回空内容")
    return content


def fallback_root_cause(alert: Alert, reason: str) -> RootCause:
    """LLM 不可用/输出非法时的安全侧降级：confidence=0.0 → 闸门 1 转人工。"""
    return RootCause(
        error_type="Unknown",
        suspect_files=[],
        confidence=0.0,
        summary=f"LLM triage 不可用（{reason}），按安全侧转人工。",
    )


def run_triage(
    alert: Alert,
    clustered: dict,
    trace_ids: list[str] | None = None,
    model: str | None = None,
    timeout: int = 45,
) -> tuple[RootCause, dict]:
    """执行一次 triage：返回 (RootCause, meta)。meta 含 degraded/elapsed 等评测所需信息。"""
    used_model = model or DEFAULT_MODEL
    meta: dict = {"model": used_model, "degraded": False, "reason": ""}
    start = time.monotonic()
    try:
        prompt = build_triage_prompt(alert, clustered, trace_ids)
        raw = call_ollama(prompt, model=used_model, timeout=timeout)
        parsed = TriageOutput.model_validate_json(strip_code_fence(raw))
        root_cause = RootCause(
            error_type=parsed.error_type,
            suspect_files=parsed.suspect_files[:3],
            confidence=parsed.confidence,
            summary=parsed.summary,
        )
    except Exception as exc:  # noqa: BLE001 - 一切异常均走安全侧降级
        reason = f"{type(exc).__name__}: {exc}"
        meta.update({"degraded": True, "reason": reason})
        root_cause = fallback_root_cause(alert, reason)
    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
    meta["confidence"] = root_cause.confidence
    meta["error_type"] = root_cause.error_type
    return root_cause, meta


def load_historical_tickets(path: Path | None = None) -> list[dict]:
    """加载历史工单库（WP3 任务 4：为 WP4 Code RAG 准备语料）。"""
    ticket_path = path or (DATA_DIR / "historical_tickets.json")
    return json.loads(ticket_path.read_text(encoding="utf-8"))
