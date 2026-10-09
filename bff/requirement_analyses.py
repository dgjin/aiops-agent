"""需求智能分析闭环存储（BFF 侧）：分析会话 → 版本迭代 → 批准。

闭环流程（管理员视角）：
    发起分析 → 查看分析结果 → 提出优化建议 / 具体要求（反馈）→ 再次分析优化
    → …循环… → 「同意并进入修复工作流」（启动 AIOpsRequirementWorkflow）。

双后端（AIOPS_MODE 决定，同 escalations 模式）：
    - **demo**：``data/requirement_analyses.json``（临时文件 + 原子替换；每次读取重新解析——热生效）；
    - **production**：MySQL ``requirement_analyses`` 表（每次查询即读库）。

数据结构（一条需求一个会话，id = ``ra-<app_id>-<entry_id>``）：
    status（顶层，查询过滤友好）：
        analyzing（最新版本执行中）→ analyzed（最新版本完成、待查看/反馈）
        analyzing → failed（LLM 兜底降级 / 进程中断，可重试）
        analyzed → analyzing（反馈 / 重试 → 追加新版本）
        analyzed → approved（管理员批准并已启动修复工作流，终态）

    versions：逐次分析版本（append-only），每次携带触发方式（initial/feedback/retry/refresh）
    与触发它的反馈原文；最新版本的分析结果即「当前分析结果」。
    refresh：被监控系统侧「继续评估」等更新后，经 :func:`apply_refresh` 刷新快照并重分析
    （approved 终态仅刷新快照留档）。

分析执行在 BFF 后台线程（:func:`execute_analysis`）：
    代码引用检索（可选，失败降级）→ requirement_agent 分析（LLM + 兜底）→ 写回版本。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

log = logging.getLogger("aiops.bff.requirement")

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（与 monitored_apps / escalations 同口径）
DATA_PATH = DATA_DIR / "requirement_analyses.json"

STATUSES = ("analyzing", "analyzed", "failed", "approved")
VERSION_STATUSES = ("running", "done", "failed")
TRIGGERS = ("initial", "feedback", "retry", "refresh")

# 分析会话卡死判定的默认阈值（秒）：超过视为执行中断（如 BFF 重启），转 failed 可重试
DEFAULT_STALE_SECONDS = 900


class RequirementAnalysisError(ValueError):
    """分析会话操作校验错误（由路由层统一转 409/400）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stale_seconds() -> int:
    try:
        value = int(os.environ.get("AIOPS_REQUIREMENT_ANALYSIS_STALE_SECONDS", ""))
        return value if value > 0 else DEFAULT_STALE_SECONDS
    except ValueError:
        return DEFAULT_STALE_SECONDS


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ----------------------------------------------------------------------
# 会话构造与版本状态机（纯函数，便于单测）
# ----------------------------------------------------------------------


def analysis_id_for(app_id: str, entry_id: str | int) -> str:
    """稳定会话 ID：同一条需求（app + 条目）只有一个分析会话。"""
    return f"ra-{app_id}-{entry_id}"


def new_entry(app_id: str, service: str, snapshot: dict, actor: str) -> dict:
    """新分析会话（v1 initial 版本，running 状态）。"""
    ts = _now()
    return {
        "id": analysis_id_for(app_id, str(snapshot.get("id", ""))),
        "app_id": app_id,
        "service": service or app_id,
        "entry_id": str(snapshot.get("id", "")),
        "entry_title": str(snapshot.get("title") or ""),
        "entry_kind": str(snapshot.get("kind") or ""),
        "entry_priority": str(snapshot.get("priority") or ""),
        "entry_snapshot": snapshot,
        "status": "analyzing",
        "current_version": 1,
        "versions": [
            {
                "version": 1,
                "ts": ts,
                "trigger": "initial",
                "feedback": "",
                "actor": actor or "system",
                "status": "running",
                "error": "",
                "analysis": None,
                "meta": None,
            }
        ],
        "approved": None,
        "created_at": ts,
        "updated_at": ts,
    }


def latest_version(entry: dict) -> dict | None:
    versions = entry.get("versions") or []
    return versions[-1] if versions else None


def latest_analysis(entry: dict) -> dict | None:
    """最新版本的分析结果（running 版本上为 None——继承自上一版分析）。"""
    for version in reversed(entry.get("versions") or []):
        if version.get("analysis"):
            return version["analysis"]
    return None


def feedbacks_upto(entry: dict, version: int) -> list[dict]:
    """指定版本之前（含）的全部管理员反馈（供迭代分析注入 prompt）。"""
    return [
        {
            "version": item.get("version"),
            "feedback": item.get("feedback") or "",
            "actor": item.get("actor") or "",
            "ts": item.get("ts") or "",
        }
        for item in entry.get("versions") or []
        if item.get("version", 0) <= version and item.get("trigger") == "feedback"
    ]


def begin_version(entry: dict, trigger: str, feedback: str = "", actor: str = "") -> dict:
    """追加一个 running 新版本并将会话置为 analyzing。

    允许进入的顶层状态：analyzed（正常迭代）或 failed（重试）；
    trigger="feedback" 时反馈原文必填。
    """
    if trigger not in TRIGGERS:
        raise RequirementAnalysisError(f"未知触发方式：{trigger}")
    status = entry.get("status")
    if status == "approved":
        raise RequirementAnalysisError("该需求已批准并进入修复流程，不能重新分析")
    if status == "analyzing":
        raise RequirementAnalysisError("分析进行中，请等待当前分析完成后操作")
    if status not in ("analyzed", "failed"):
        raise RequirementAnalysisError(f"当前状态 {status or '未知'} 不允许发起分析")
    text = (feedback or "").strip()
    if trigger == "feedback" and not text:
        raise RequirementAnalysisError("反馈内容不能为空")
    version = int(entry.get("current_version") or 0) + 1
    ts = _now()
    entry.setdefault("versions", []).append(
        {
            "version": version,
            "ts": ts,
            "trigger": trigger,
            "feedback": text,
            "actor": actor or "system",
            "status": "running",
            "error": "",
            "analysis": None,
            "meta": None,
        }
    )
    entry["current_version"] = version
    entry["status"] = "analyzing"
    entry["updated_at"] = ts
    return entry


def complete_version(entry: dict, version: int, analysis: dict, meta: dict) -> dict:
    """写回分析结果：版本 done（degraded 则 failed），顶层状态同步。"""
    target = next(
        (item for item in entry.get("versions") or [] if item.get("version") == version), None
    )
    if target is None:
        raise RequirementAnalysisError(f"版本不存在：v{version}")
    degraded = bool((meta or {}).get("degraded"))
    ts = _now()
    target["analysis"] = analysis
    target["meta"] = meta
    target["status"] = "failed" if degraded else "done"
    target["error"] = str((meta or {}).get("reason") or "") if degraded else ""
    # 顶层状态以「最新版本」为准（旧版本迟到的写回不翻转已推进的状态）
    if int(entry.get("current_version") or 0) == version and entry.get("status") != "approved":
        entry["status"] = "failed" if degraded else "analyzed"
    entry["updated_at"] = ts
    return entry


def apply_feedback(entry: dict, feedback: str, actor: str) -> dict:
    """管理员反馈（优化建议 / 具体要求）→ 追加 feedback 版本并进入再分析。"""
    return begin_version(entry, "feedback", feedback=feedback, actor=actor)


def apply_retry(entry: dict, actor: str) -> dict:
    """失败重试：以相同输入追加 retry 版本再分析。"""
    return begin_version(entry, "retry", actor=actor)


def apply_refresh(entry: dict, snapshot: dict, actor: str) -> bool:
    """同步被监控系统最新条目内容（「继续评估」后的新结论 / 优先级变化等）。

    - 以被监控系统为权威源刷新 ``entry_snapshot``（标题 / 类型 / 优先级同步）；
    - 快照 ``updatedAt`` 与会话内快照一致 → 无更新，抛校验错误（路由转 409）；
    - analyzed / failed → 追加 refresh 版本并进入再分析（新评估结论进入分析输入）；
    - approved（终态）→ 仅刷新快照留档，不重开分析。

    返回是否需启动分析线程（approved 为 False）。
    """
    status = entry.get("status")
    if status == "analyzing":
        raise RequirementAnalysisError("分析进行中，请等待当前分析完成后同步更新")
    old = entry.get("entry_snapshot") or {}
    old_updated = str(old.get("updatedAt") or "")
    new_updated = str(snapshot.get("updatedAt") or "")
    if old_updated and new_updated and old_updated == new_updated:
        raise RequirementAnalysisError("条目内容无更新，无需同步")
    entry["entry_snapshot"] = snapshot
    entry["entry_title"] = str(snapshot.get("title") or "")
    entry["entry_kind"] = str(snapshot.get("kind") or "")
    entry["entry_priority"] = str(snapshot.get("priority") or "")
    if status == "approved":
        entry["updated_at"] = _now()
        return False
    begin_version(entry, "refresh", actor=actor)
    return True


def apply_approved(entry: dict, version: int, wf_id: str, actor: str) -> dict:
    """批准（终态）：记录批准版本与启动的修复工作流。"""
    if entry.get("status") == "approved":
        raise RequirementAnalysisError("该需求已批准，请勿重复操作")
    if entry.get("status") != "analyzed":
        raise RequirementAnalysisError(
            f"当前状态 {entry.get('status') or '未知'} 不允许批准（要求分析完成）"
        )
    analysis = latest_analysis(entry)
    if analysis is None or analysis.get("degraded"):
        raise RequirementAnalysisError("最新分析未成功完成（无有效结果），请反馈或重试后再批准")
    ts = _now()
    entry["status"] = "approved"
    entry["approved"] = {"version": version, "wf_id": wf_id, "at": ts, "actor": actor or "system"}
    entry["updated_at"] = ts
    return entry


def maybe_mark_stale(entry: dict, now: datetime | None = None) -> bool:
    """卡死自愈：analyzing 且最新版本超时未回写 → 版本与顶层转 failed（可重试）。

    典型场景：BFF 重启导致分析线程中断。返回是否发生变更（真则调用方应持久化）。
    """
    if entry.get("status") != "analyzing":
        return False
    current = now or datetime.now(timezone.utc)
    version = latest_version(entry)
    started = _parse_ts((version or {}).get("ts"))
    if started is None:
        return False
    if (current - started).total_seconds() <= _stale_seconds():
        return False
    ts = _now()
    if version is not None:
        version["status"] = "failed"
        version["error"] = f"分析执行中断（超过 {_stale_seconds()} 秒无结果，可能 BFF 重启）"
    entry["status"] = "failed"
    entry["updated_at"] = ts
    return True


# ----------------------------------------------------------------------
# DB 后端（production）
# ----------------------------------------------------------------------


def _row_to_entry(row) -> dict:
    return {
        "id": row["id"],
        "app_id": row["app_id"],
        "service": row["service"],
        "entry_id": row["entry_id"],
        "entry_title": row["entry_title"],
        "entry_kind": row["entry_kind"],
        "entry_priority": row["entry_priority"],
        "entry_snapshot": row["entry_snapshot"] or {},
        "status": row["status"],
        "current_version": int(row["current_version"] or 0),
        "versions": row["versions"] or [],
        "approved": row["approved"],
        "created_at": db_layer.to_iso(row["created_at"]) or "",
        "updated_at": db_layer.to_iso(row["updated_at"]) or "",
    }


def _entry_values(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "app_id": entry["app_id"],
        "service": entry.get("service") or "",
        "entry_id": entry.get("entry_id") or "",
        "entry_title": entry.get("entry_title") or "",
        "entry_kind": entry.get("entry_kind") or "",
        "entry_priority": entry.get("entry_priority") or "",
        "entry_snapshot": entry.get("entry_snapshot") or {},
        "status": entry.get("status") or "analyzing",
        "current_version": int(entry.get("current_version") or 0),
        "versions": entry.get("versions") or [],
        "approved": entry.get("approved"),
        "created_at": db_layer.to_dt(entry.get("created_at")) or db_layer.now_dt(),
        "updated_at": db_layer.to_dt(entry.get("updated_at")) or db_layer.now_dt(),
    }


def _read_db() -> list[dict]:
    engine = db_layer.get_engine()
    with engine.begin() as conn:
        return [
            _row_to_entry(row)
            for row in conn.execute(
                select(db_layer.requirement_analyses).order_by(
                    db_layer.requirement_analyses.c.updated_at
                )
            ).mappings()
        ]


def _upsert_db(entry: dict) -> None:
    engine = db_layer.get_engine()
    values = _entry_values(entry)
    with engine.begin() as conn:
        exists = conn.execute(
            select(db_layer.requirement_analyses.c.id).where(
                db_layer.requirement_analyses.c.id == entry["id"]
            )
        ).scalar()
        if exists is None:
            conn.execute(db_layer.requirement_analyses.insert().values(**values))
        else:
            conn.execute(
                db_layer.requirement_analyses.update()
                .where(db_layer.requirement_analyses.c.id == entry["id"])
                .values(**values)
            )


# ----------------------------------------------------------------------
# 读取 / 写入入口（按模式分发）
# ----------------------------------------------------------------------


def _read_raw() -> list[dict]:
    if mode.is_production():
        return _read_db()
    if not DATA_PATH.is_file():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []  # 文件损坏：返回空，不覆盖现场（下次写入前可由人工排查）
    return data if isinstance(data, list) else []


def _write_raw(entries: list[dict]) -> None:
    if mode.is_production():
        return  # 生产：单条 upsert（见 persist_entry）
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def persist_entry(entry: dict) -> None:
    """写入单条（demo：全量原子替换；production：单条 upsert）。"""
    if mode.is_production():
        _upsert_db(entry)
        return
    entries = [item for item in _read_raw() if item.get("id") != entry["id"]]
    entries.append(entry)
    _write_raw(entries)


def _sweep_stale(entries: list[dict]) -> list[dict]:
    """读取路径上的卡死自愈（幂等）：发生转失败的条目即时持久化。"""
    for entry in entries:
        if maybe_mark_stale(entry):
            persist_entry(entry)
    return entries


def list_all(app_id: str | None = None) -> list[dict]:
    """全部分析会话（可按应用过滤；最近更新在前）。读取即取最新——热生效。"""
    entries = _sweep_stale(_read_raw())
    if app_id:
        entries = [e for e in entries if e.get("app_id") == app_id]
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return entries


def get(analysis_id: str) -> dict | None:
    entry = next((e for e in _read_raw() if e.get("id") == analysis_id), None)
    if entry is not None and maybe_mark_stale(entry):
        persist_entry(entry)
    return entry


def find(app_id: str, entry_id: str | int) -> dict | None:
    return get(analysis_id_for(app_id, str(entry_id)))


def create_or_touch(app_id: str, service: str, snapshot: dict, actor: str) -> tuple[dict, bool]:
    """发起分析入口：返回 (entry, need_run)。

    - 无会话 → 新建（v1 initial，need_run=True）；
    - 已 analyzed → 直接返回既有结果（need_run=False，前端进入查看）；
    - 已 failed → 追加 retry 版本（need_run=True）；
    - analyzing / approved → 抛 :class:`RequirementAnalysisError`（路由转 409）。
    """
    existing = find(app_id, snapshot.get("id", ""))
    if existing is not None:
        if existing.get("status") == "analyzing":
            raise RequirementAnalysisError("分析进行中，请稍候再试")
        if existing.get("status") == "approved":
            raise RequirementAnalysisError("该需求已批准并进入修复流程，不能重新分析")
        if existing.get("status") == "analyzed":
            return existing, False
        apply_retry(existing, actor)
        persist_entry(existing)
        return existing, True
    entry = new_entry(app_id, service, snapshot, actor)
    persist_entry(entry)
    return entry, True


# ----------------------------------------------------------------------
# 分析执行（BFF 后台线程体）
# ----------------------------------------------------------------------


def _code_references(entry: dict) -> list[dict]:
    """Code RAG 检索需求相关代码引用（可选增强；任何失败降级为空列表）。"""
    try:
        from aiops_agent import code_rag

        from . import monitored_apps as app_store

        app = next(
            (item for item in app_store.list_all() if item.get("id") == entry.get("app_id")), None
        )
        repo_raw = str((app or {}).get("repo") or "").strip()
        if not repo_raw or not Path(repo_raw).is_dir():
            return []
        index_path = code_rag.app_index_path(entry.get("service") or entry.get("app_id") or "")
        if not index_path.is_file():
            code_rag.build_index(Path(repo_raw), index_path=index_path)
        snapshot = entry.get("entry_snapshot") or {}
        query = " ".join(
            str(snapshot.get(key) or "") for key in ("title", "content", "assessment")
        ).strip()
        if not query:
            return []
        return code_rag.search_index(query, index_path=index_path, top_k=5)
    except Exception as exc:  # noqa: BLE001 - 检索增强失败不影响分析主流程
        log.warning("[requirement] 代码引用检索失败（降级为空引用）：%s", exc)
        return []


def execute_analysis(analysis_id: str, version: int) -> None:
    """后台线程体：检索引用 → LLM 分析 → 写回指定版本（绝不抛异常）。"""
    from aiops_agent import requirement_agent

    try:
        entry = get(analysis_id)
        if entry is None:
            return
        target = next(
            (v for v in entry.get("versions") or [] if v.get("version") == version), None
        )
        if target is None or target.get("status") != "running":
            return  # 已被自愈/重复触发覆盖：放弃本次执行
        snapshot = entry.get("entry_snapshot") or {}
        feedbacks = feedbacks_upto(entry, version)
        previous = latest_analysis(entry)
        references = _code_references(entry)
        app = {"id": entry.get("app_id"), "name": entry.get("service") or entry.get("app_id")}
        analysis, meta = requirement_agent.analyze_requirement(
            snapshot, app=app, feedbacks=feedbacks, previous=previous, references=references
        )
        # 重新读取最新状态再写回（分析期间可能有其他更新；版本级写入相互正交）
        entry = get(analysis_id)
        if entry is None:
            return
        complete_version(entry, version, analysis, meta)
        persist_entry(entry)
        log.info(
            "[requirement] 分析完成：%s v%d（degraded=%s，耗时=%.1fs，引用=%d 条）",
            analysis_id,
            version,
            meta.get("degraded"),
            meta.get("elapsed_seconds", 0.0),
            len(references),
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：线程体绝不向上抛
        log.exception("[requirement] 分析执行异常（%s v%s）：%s", analysis_id, version, exc)


def start_analysis_thread(analysis_id: str, version: int) -> None:
    """启动后台分析线程（daemon；BFF 重启后由读取路径的卡死自愈兜底）。"""
    thread = threading.Thread(
        target=execute_analysis,
        args=(analysis_id, version),
        name=f"req-analysis-{analysis_id}-v{version}",
        daemon=True,
    )
    thread.start()


def summary(entry: dict) -> dict:
    """列表用摘要（不含 versions / 快照全文）。"""
    latest = latest_analysis(entry)
    approved = entry.get("approved") or None
    return {
        "id": entry.get("id"),
        "app_id": entry.get("app_id"),
        "service": entry.get("service"),
        "entry_id": entry.get("entry_id"),
        "entry_title": entry.get("entry_title"),
        "entry_kind": entry.get("entry_kind"),
        "entry_priority": entry.get("entry_priority"),
        "status": entry.get("status"),
        "current_version": entry.get("current_version"),
        "latest_degraded": bool((latest or {}).get("degraded")) if latest else None,
        "latest_confidence": (latest or {}).get("confidence") if latest else None,
        "approved": approved,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }
