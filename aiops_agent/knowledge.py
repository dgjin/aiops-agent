"""审核通过的人工处置 → 知识语料导出（M2）。

数据流：``escalations.resolution``（已关闭 + ``knowledge_status=APPROVED`` + ``RESOLVED``）
→ 清洗 → ``data/knowledge_records.json`` → ``code_rag.build_index`` 显式重建索引。

约束：
    - 只有管理员审核通过的记录才导出；DRAFT / REJECTED / 未关闭 / 非 RESOLVED 一律跳过；
    - 导出前清洗凭据、令牌、内部地址与个人信息；清洗后仍命中敏感模式的记录跳过
      （宁可缺一条知识，也不把密钥带进索引）；
    - 导出文件保留来源待办与流程 ID（``source_esc_id`` / ``source_wf_id``），撤销审核后
      重新导出即可让记录从下一次索引中消失；
    - 本模块只写导出文件，不触发嵌入或索引重建。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from aiops_agent import mode

ENV_PATH = "AIOPS_KNOWLEDGE_PATH"
DEFAULT_NAME = "knowledge_records.json"

# 清洗规则：命中即替换为占位符（保留字段语义，不保留敏感值）。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
            r"\s*[=:]\s*[^\s,;，；]+"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"), "Authorization: Bearer [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{16,}"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"\b(?:10|127|172\.(?:1[6-9]|2\d|3[01])|192\.168)"
            r"(?:\.\d{1,3}){2,3}(?::\d+)?\b"
        ),
        "[内部地址]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[邮箱]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
)


def export_path() -> Path:
    """导出文件路径（``AIOPS_KNOWLEDGE_PATH`` 可覆盖，测试指向临时文件）。"""
    override = (os.environ.get(ENV_PATH) or "").strip()
    return Path(override).expanduser() if override else mode.data_root() / DEFAULT_NAME


# 清洗占位符：检测残留敏感时按「命中片段是否含占位符」判定，避免占位符本身或
# 其后的普通文字被二次误判（如 password=[REDACTED] 重启 → 不应算敏感）。
_PLACEHOLDER_RE = re.compile(r"\[(?:REDACTED|内部地址|邮箱|手机号)\]")


def sanitize(text: object) -> str:
    """清洗单段文本（幂等：已替换的占位符不会被二次替换）。"""
    value = str(text or "")
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def contains_sensitive(text: object) -> bool:
    """是否仍含敏感模式（清洗后调用；命中则禁止导出）。

    逐条命中判定：命中片段里含占位符即视为「已清洗」，否则仍有明文敏感信息。
    """
    value = str(text or "")
    for pattern, _ in _PATTERNS:
        for match in pattern.finditer(value):
            if not _PLACEHOLDER_RE.search(match.group(0)):
                return True
    return False


def _clean(value: object) -> str:
    return sanitize(value).strip()


def record_from_entry(entry: dict) -> dict | None:
    """待办 → 知识记录；不满足入索引条件时返回 None（调用方统计跳过数）。"""
    if str(entry.get("status") or "") != "closed":
        return None
    resolution = entry.get("resolution") or {}
    if not isinstance(resolution, dict):
        return None
    if str(resolution.get("knowledge_status") or "") != "APPROVED":
        return None
    # 未解决（误报 / 重复 / 无需改代码等）不进入代码知识库。
    if str(resolution.get("close_reason") or "") != "RESOLVED":
        return None
    fields = {
        "service": _clean(entry.get("service")),
        "symptoms": _clean(entry.get("auto_reason")),
        "root_cause": _clean(resolution.get("actual_root_cause")),
        "resolution": _clean(resolution.get("actual_action")),
        "verification": _clean(resolution.get("verification")),
        "verdict": _clean(resolution.get("verdict")),
    }
    if not fields["root_cause"] or not fields["resolution"] or not fields["verification"]:
        return None
    if any(contains_sensitive(value) for value in fields.values()):
        return None
    esc_id = str(entry.get("id") or "")
    if not esc_id:
        return None
    return {
        "ticket_id": esc_id,
        "source_esc_id": esc_id,
        "source_wf_id": str(entry.get("wf_id") or ""),
        "service": fields["service"],
        # 知识来源可区分：告警 / 需求（需求待办不采集日志，但仍可沉淀处置知识）。
        "source_kind": "requirement" if str(entry.get("wf_id") or "").startswith("aiops-req-") else "alert",
        "error_type": "人工处置",
        "symptoms": fields["symptoms"],
        "root_cause": fields["root_cause"],
        "resolution": fields["resolution"],
        "verification": fields["verification"],
        "verdict": fields["verdict"],
        "closed_at": str(resolution.get("closed_at") or entry.get("closed_at") or ""),
    }


def collect(entries: list[dict]) -> tuple[list[dict], int]:
    """从待办列表提取知识记录；返回 (记录, 跳过数)。"""
    records: list[dict] = []
    skipped = 0
    for entry in entries:
        record = record_from_entry(entry)
        if record is None:
            # 只统计「审核通过但被清洗/字段拦截」的记录，避免把未审核条目标成跳过。
            resolution = entry.get("resolution") or {}
            if str(resolution.get("knowledge_status") or "") == "APPROVED":
                skipped += 1
            continue
        records.append(record)
    records.sort(key=lambda item: item.get("closed_at") or "", reverse=True)
    return records, skipped


def write_export(entries: list[dict], path: Path | str | None = None) -> dict:
    """原子写入导出文件；返回导出数量与路径（不触发索引重建）。"""
    target = Path(path) if path else export_path()
    records, skipped = collect(entries)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".knowledge-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {"exported": len(records), "skipped": skipped, "path": str(target)}


def load(path: Path | str | None = None) -> list[dict]:
    """读取导出文件；缺失/损坏返回空列表（索引重建不应因知识文件失败而中断）。"""
    target = Path(path) if path else export_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []
