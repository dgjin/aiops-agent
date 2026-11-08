"""审计域（M4）：流程审计 SQL 数据面 / 操作审计游标分页 / 链校验与归档。

端点：

- ``GET /api/audit``：终态流程审计（production 查 ``workflow_runs``；demo 回退
  Temporal 列表），支持 ``q`` / ``stage`` / ``kind`` / ``exec_status`` / ``days``
  与游标分页；``ops`` 字段仅保留最近 20 条操作审计预览（完整列表走下一端点）；
- ``GET /api/audit/ops``：操作审计游标分页，支持操作者 / 动作 / 结果 / 流程 /
  时间范围过滤（不再固定最近 100 条）；
- ``GET /api/audit/integrity``：审计哈希链校验 + 归档清单（admin）；
- ``POST /api/audit/archive``：归档过期审计，可选 purge（admin，且只删除已归档
  并通过 SHA-256 复核的记录）；
- ``GET /api/audit/summary``：报表聚合（保持既有口径）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from aiops_agent import db as db_layer
from aiops_agent import mode

from .. import audit
from ..deps import ApiError, close_dt, gw, ok

router = APIRouter()


def _flow_item(item: dict) -> dict:
    """Temporal 列表项 → 审计行（与 SQL 数据面同字段）。"""
    return {
        "wf_id": item.get("wf_id"),
        "run_id": item.get("run_id") or "",
        "exec_status": item.get("exec_status") or "",
        "start_time": item.get("start_time"),
        "close_time": item.get("close_time"),
        "stage": item.get("stage") or "",
        "alert": item.get("alert"),
        "confidence": item.get("confidence"),
        "patch_id": item.get("patch_id"),
        "duration_seconds": item.get("duration_seconds"),
        "kind": item.get("kind") or "alert",
        "risk": (item.get("patch") or {}).get("risk") or "",
        "gate_events": item.get("gate_events") or [],
        "test_report": item.get("test_report"),
        "patch": item.get("patch"),
        "root_cause": item.get("root_cause"),
        "source": "temporal",
    }


def _db_item(row) -> dict:
    """``workflow_runs`` 行 → 审计行（result 仅存摘要，不含日志正文）。"""
    result = row["result"] or {}
    return {
        "wf_id": row["workflow_id"],
        "run_id": row["run_id"] or "",
        "exec_status": row["exec_status"] or "",
        "start_time": db_layer.to_iso(row["start_time"]),
        "close_time": db_layer.to_iso(row["close_time"]),
        "stage": row["stage"] or "",
        "alert": row["alert"],
        "confidence": row["confidence"],
        "patch_id": row["patch_id"],
        "duration_seconds": row["duration_seconds"],
        "kind": row["kind"] or "alert",
        "risk": row["risk"] or "",
        "gate_events": result.get("gate_events") or [],
        "test_report": result.get("test_report"),
        "patch": result.get("patch"),
        "root_cause": result.get("root_cause"),
        "source": "db",
    }


def _temporal_rows(
    items: list[dict],
    *,
    days: int,
    q: str,
    stage: str,
    kind: str,
    exec_status: str,
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    needle = q.strip().lower()
    rows: list[dict] = []
    for item in items:
        if item.get("exec_status") == "RUNNING":
            continue
        closed_at = close_dt(item)
        if closed_at is None or closed_at < cutoff:
            continue
        if stage and item.get("stage") != stage:
            continue
        if kind and (item.get("kind") or "alert") != kind:
            continue
        if exec_status and item.get("exec_status") != exec_status:
            continue
        if needle:
            alert = item.get("alert") or {}
            haystack = " ".join(
                str(value)
                for value in (
                    item.get("wf_id"),
                    alert.get("alert_id"),
                    alert.get("service"),
                    item.get("patch_id"),
                )
            ).lower()
            if needle not in haystack:
                continue
        rows.append(_flow_item(item))
    rows.sort(key=lambda row: (row.get("close_time") or "", row.get("wf_id") or ""), reverse=True)
    return rows


def _slice_cursor(rows: list[dict], *, limit: int, cursor: str) -> dict:
    """Temporal 回退路径的游标分页（键：close_time + wf_id，倒序）。"""
    payload = audit.decode_cursor(cursor) if cursor else {}
    cursor_key = (
        str(payload.get("close_time") or ""),
        str(payload.get("wf_id") or ""),
    )
    if cursor:
        rows = [
            row
            for row in rows
            if (str(row.get("close_time") or ""), str(row.get("wf_id") or "")) < cursor_key
        ]
    page = rows[: limit + 1]
    has_more = len(page) > limit
    items = page[:limit]
    next_cursor = ""
    if has_more and items:
        last = items[-1]
        next_cursor = audit.encode_cursor(
            {"close_time": last.get("close_time") or "", "wf_id": last.get("wf_id") or ""}
        )
    return {
        "items": items,
        "next_cursor": next_cursor or None,
        "has_more": has_more,
        "source": "temporal",
    }


def _flow_audit_db(
    *,
    days: int,
    q: str,
    stage: str,
    kind: str,
    exec_status: str,
    limit: int,
    cursor: str,
) -> dict | None:
    """production 流程审计（SQL 数据面）；表为空返回 None 由路由回退 Temporal。"""
    table = db_layer.workflow_runs
    engine = db_layer.get_engine()
    payload = audit.decode_cursor(cursor) if cursor else {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    query = select(table).where(
        and_(table.c.close_time.is_not(None), table.c.close_time >= cutoff)
    )
    if stage:
        query = query.where(table.c.stage == stage)
    if kind:
        query = query.where(table.c.kind == kind)
    if exec_status:
        query = query.where(table.c.exec_status == exec_status)
    if q:
        needle = f"%{q.strip()}%"
        query = query.where(
            or_(table.c.workflow_id.like(needle), table.c.patch_id.like(needle))
        )
    if payload.get("close_time"):
        cursor_time = db_layer.to_dt(str(payload.get("close_time")))
        cursor_id = str(payload.get("wf_id") or "")
        if cursor_time is not None:
            query = query.where(
                or_(
                    table.c.close_time < cursor_time,
                    and_(table.c.close_time == cursor_time, table.c.workflow_id < cursor_id),
                )
            )
    query = query.order_by(table.c.close_time.desc(), table.c.workflow_id.desc()).limit(limit + 1)
    with engine.connect() as conn:
        total = int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)
        if total == 0:
            return None
        rows = conn.execute(query).mappings().all()
    has_more = len(rows) > limit
    items = [_db_item(row) for row in rows[:limit]]
    next_cursor = ""
    if has_more and items:
        last = items[-1]
        next_cursor = audit.encode_cursor(
            {"close_time": last.get("close_time") or "", "wf_id": last.get("wf_id") or ""}
        )
    return {
        "items": items,
        "next_cursor": next_cursor or None,
        "has_more": has_more,
        "source": "db",
    }


@router.get("/api/audit")
async def api_audit(
    q: str = "",
    stage: str = "",
    kind: str = Query("", pattern="^(|alert|requirement)$"),
    exec_status: str = "",
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
    cursor: str = "",
) -> dict:
    """终态流程审计：production 优先 SQL 数据面，demo/空库回退 Temporal。"""
    try:
        page = None
        if mode.is_production():
            page = await asyncio.to_thread(
                _flow_audit_db,
                days=days,
                q=q,
                stage=stage,
                kind=kind,
                exec_status=exec_status,
                limit=limit,
                cursor=cursor,
            )
        if page is None:
            try:
                items = await gw.list_flows(limit=200)
            except Exception as exc:  # noqa: BLE001 - 读侧降级为明确错误
                raise ApiError(503, f"流程审计数据源不可用：{exc}") from exc
            rows = _temporal_rows(
                items,
                days=days,
                q=q,
                stage=stage,
                kind=kind,
                exec_status=exec_status,
            )
            page = _slice_cursor(rows, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    ops = await asyncio.to_thread(audit.read_audit, 20)
    return ok({**page, "ops": ops})


@router.get("/api/audit/ops")
async def api_audit_ops(
    limit: int = Query(50, ge=1, le=500),
    cursor: str = "",
    actor: str = "",
    action: str = "",
    result: str = Query("", pattern="^(|ok|pending|signaled|denied|rejected|failed)$"),
    wf_id: str = "",
    start: str = "",
    end: str = "",
) -> dict:
    """操作审计游标分页（操作者 / 动作 / 结果 / 流程 / 时间过滤）。"""
    try:
        page = await asyncio.to_thread(
            audit.list_audit,
            limit=limit,
            cursor=cursor,
            actor=actor,
            action=action,
            result=result,
            wf_id=wf_id,
            start=start,
            end=end,
        )
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return ok(page)


@router.get("/api/audit/integrity")
async def api_audit_integrity() -> dict:
    """审计哈希链校验（admin）：任何修改 / 插入 / 删除都会在结果中暴露。"""
    return ok(await asyncio.to_thread(audit.verify))


class ArchiveBody(BaseModel):
    days: int = Field(default=365, ge=1, le=3650)
    purge: bool = False


@router.post("/api/audit/archive")
async def api_audit_archive(request: Request, body: ArchiveBody) -> dict:
    """归档过期审计；``purge=true`` 仅删除已归档且 SHA-256 复核通过的文件/行。"""
    actor = request.state.identity.user
    result = await asyncio.to_thread(audit.archive, days=body.days, purge=body.purge)
    await asyncio.to_thread(
        audit.write_audit,
        actor=actor,
        action="audit:archive",
        params={
            "days": body.days,
            "purge": body.purge,
            "archived": result.get("archived"),
            "purged": result.get("purged"),
            "error": result.get("error") or "",
        },
        result="failed" if result.get("error") else "ok",
    )
    return ok(result)


@router.get("/api/audit/summary")
async def api_audit_summary(
    days: int = Query(30, ge=1, le=365),
    top: int = Query(10, ge=1, le=50),
) -> dict:
    """操作审计报表聚合（P3-07）：按操作者 / 动作 / 日期 / 结果聚合计数。

    控制台「操作审计」页据此渲染汇总条与日趋势；production 全量 SQL 聚合、
    demo 采样最近 5000 条（truncated 标记）。
    """
    summary = await asyncio.to_thread(audit.aggregate_audit, days=days, top=top)
    return ok(summary)
