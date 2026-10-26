"""日志证据归档（转人工核对）：TRIAGING 阶段采集的日志与模板聚类按 workflow 落库。

背景：``collect_evidence`` 拉取的日志此前只在流程内存中用于 LLM 推理，流程结束即丢弃；
转人工后运维人员只能看到一句升级原因，无法核对「当时到底报了什么错」。本模块在
日志聚类后立即归档证据，与 workflow_id 绑定，供控制台流程详情 / 转人工待办查看。

双后端（与 escalations / monitored_apps 同口径，由 AIOPS_MODE 决定）：
    - **demo**：``<data_root>/evidence/<wf_id>.json``（临时文件 + 原子替换）；
    - **production**：MySQL ``log_evidence`` 表（wf_id 主键，重复归档幂等覆盖）。

体积控制（证据只用于人工核对，不参与推理）：
    - 最多存 ``MAX_STORED_LINES`` 行，超出时保留**末尾**（离故障时刻最近）；
    - 单行截断 ``MAX_LINE_CHARS`` 字符，模板聚类最多 ``MAX_TEMPLATES`` 条；
    - 原始行数 / 是否截断随记录保存，前端据此提示「证据已截断」，
      不让截断后的视图被误当成全量。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

MAX_STORED_LINES = 500
MAX_LINE_CHARS = 4000
MAX_TEMPLATES = 50
MAX_TRACE_IDS = 20

_SAFE_NAME = re.compile(r"[^\w.:-]")


class EvidenceStoreError(ValueError):
    """证据归档写入失败（wf_id 非法等）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_dir() -> Path:
    return mode.data_root() / "evidence"


def evidence_path(wf_id: str) -> Path:
    """工作流证据文件路径（wf_id 做文件名字符清洗，防路径穿越）。"""
    clean = _SAFE_NAME.sub("_", wf_id or "").strip("._")[:180]
    if not clean:
        raise EvidenceStoreError("wf_id 不能为空")
    return evidence_dir() / f"{clean}.json"


def _clip_line(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + f"…（已截断，原长 {len(line)} 字符）"


def build_record(
    wf_id: str,
    evidence: dict,
    clustered: dict | None,
    now: datetime | None = None,
) -> dict:
    """证据（原始日志 + 模板聚类）→ 归档记录（纯函数，便于单测）。"""
    if not (wf_id or "").strip():
        raise EvidenceStoreError("wf_id 不能为空")
    evidence = evidence or {}
    clustered = clustered or {}
    lines = [str(line) for line in (evidence.get("lines") or [])]
    total = len(lines)
    kept = lines[-MAX_STORED_LINES:] if total > MAX_STORED_LINES else lines
    templates = [
        {
            "id": str(item.get("id") or ""),
            "pattern": _clip_line(str(item.get("pattern") or "")),
            "count": int(item.get("count") or 0),
        }
        for item in (clustered.get("templates") or [])[:MAX_TEMPLATES]
    ]
    archived_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "wf_id": wf_id,
        "alert_id": str(evidence.get("alert_id") or ""),
        "service": str(evidence.get("service") or ""),
        "collected_at": str(evidence.get("collected_at") or archived_at),
        "archived_at": archived_at,
        "loki_available": bool(evidence.get("loki_available")),
        "total_lines": total,
        "stored_lines": len(kept),
        "truncated": total > len(kept),
        "trace_ids": [str(item) for item in (evidence.get("trace_ids") or [])][:MAX_TRACE_IDS],
        "templates": templates,
        "compression_ratio": float(clustered.get("compression_ratio") or 0.0),
        "lines": [_clip_line(line) for line in kept],
    }


def summary_of(record: dict | None) -> dict | None:
    """归档记录 → 列表/徽标用摘要（不含原始日志正文）。"""
    if not record:
        return None
    return {
        "available": True,
        "wf_id": record.get("wf_id") or "",
        "alert_id": record.get("alert_id") or "",
        "service": record.get("service") or "",
        "collected_at": record.get("collected_at") or "",
        "loki_available": bool(record.get("loki_available")),
        "total_lines": int(record.get("total_lines") or 0),
        "stored_lines": int(record.get("stored_lines") or 0),
        "truncated": bool(record.get("truncated")),
        "template_count": len(record.get("templates") or []),
        "trace_count": len(record.get("trace_ids") or []),
        "compression_ratio": float(record.get("compression_ratio") or 0.0),
    }


# ----------------------------------------------------------------------
# DB 后端（production）
# ----------------------------------------------------------------------


def _row_to_record(row) -> dict:
    return {
        "wf_id": row["wf_id"],
        "alert_id": row["alert_id"],
        "service": row["service"],
        "collected_at": db_layer.to_iso(row["collected_at"]) or "",
        "archived_at": db_layer.to_iso(row["archived_at"]) or "",
        "loki_available": bool(row["loki_available"]),
        "total_lines": int(row["total_lines"] or 0),
        "stored_lines": int(row["stored_lines"] or 0),
        "truncated": bool(row["truncated"]),
        "trace_ids": list(row["trace_ids"] or []),
        "templates": list(row["templates"] or []),
        "compression_ratio": float(row["compression_ratio"] or 0.0),
        "lines": list(row["lines"] or []),
    }


def _read_db(wf_id: str) -> dict | None:
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(db_layer.log_evidence).where(db_layer.log_evidence.c.wf_id == wf_id)
            )
            .mappings()
            .first()
        )
    return _row_to_record(row) if row else None


def _upsert_db(record: dict) -> None:
    engine = db_layer.get_engine()
    values = {
        "wf_id": record["wf_id"],
        "alert_id": record["alert_id"],
        "service": record["service"],
        "collected_at": db_layer.to_dt(record["collected_at"]) or db_layer.now_dt(),
        "archived_at": db_layer.to_dt(record["archived_at"]) or db_layer.now_dt(),
        "loki_available": record["loki_available"],
        "total_lines": record["total_lines"],
        "stored_lines": record["stored_lines"],
        "truncated": record["truncated"],
        "trace_ids": record["trace_ids"],
        "templates": record["templates"],
        "compression_ratio": record["compression_ratio"],
        "lines": record["lines"],
    }
    table = db_layer.log_evidence
    with engine.begin() as conn:
        exists = conn.execute(select(table.c.wf_id).where(table.c.wf_id == record["wf_id"])).scalar()
        if exists is None:
            conn.execute(table.insert().values(**values))
        else:
            conn.execute(table.update().where(table.c.wf_id == record["wf_id"]).values(**values))


# ----------------------------------------------------------------------
# 文件后端（demo）
# ----------------------------------------------------------------------


def _read_file(wf_id: str) -> dict | None:
    path = evidence_path(wf_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_file(record: dict) -> None:
    path = evidence_path(record["wf_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ----------------------------------------------------------------------
# 读写入口（按模式分发）
# ----------------------------------------------------------------------


def save(wf_id: str, evidence: dict, clustered: dict | None = None) -> dict:
    """归档一次证据（幂等覆盖），返回摘要。"""
    record = build_record(wf_id, evidence, clustered)
    if mode.is_production():
        _upsert_db(record)
    else:
        _write_file(record)
    return summary_of(record)  # type: ignore[return-value]


def get(wf_id: str) -> dict | None:
    """读取完整证据（含原始日志行）；无归档返回 None。"""
    if not (wf_id or "").strip():
        return None
    return _read_db(wf_id) if mode.is_production() else _read_file(wf_id)


def summary(wf_id: str) -> dict | None:
    return summary_of(get(wf_id))


def summaries_for(wf_ids: list[str]) -> dict[str, dict]:
    """批量摘要（转人工列表一次取齐，避免逐条读文件/查库）。"""
    out: dict[str, dict] = {}
    for wf_id in dict.fromkeys(w for w in wf_ids if w):
        item = summary(wf_id)
        if item:
            out[wf_id] = item
    return out
