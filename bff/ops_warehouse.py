"""运营指标数据面（M5）：production 走 SQL 取数，demo 回退文件/读侧数据。

本模块只负责「窗口内取数 + 行结构归一」，**不做任何比率计算**——
算术集中在 :func:`bff.ops_metrics.compute`，保证接口与 SQL 手算逐位一致。

取数策略：
    - production：``workflow_runs``（``result`` 内含闸门事件链 / 测试报告 / 补丁）、
      ``audit_events``、``log_evidence``、``monitored_apps`` 各一次窗口查询，
      规避 Temporal ``list_flows`` 的 200 条截断与逐流程 N+1；
    - demo：证据质量回退到 ``data/evidence/*.json`` 文件扫描（production 无文件后端）。

降级契约：
    每个分量独立 try/except，失败时返回空列表并记录 ``degraded``（与路由既有
    口径一致：派生统计失败不应让面板整体 500）；证据分量额外区分「不可读」
    与「0 条」——前者由调用方传 ``None``，前端显示「暂无数据」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import evidence_store, mode

from . import monitored_apps

# 单次查询上限：7 天窗口的常态量级远低于该值；上限只防异常膨胀拖垮 BFF。
MAX_ROWS = 5000
MAX_EVIDENCE_FILES = 2000


def _cutoff(days: int, now: datetime | None) -> tuple[datetime, datetime]:
    """返回 (aware now, naive UTC cutoff)——入库列为 naive UTC，比较前必须去时区。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current, (current - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)


def _alert_of(row) -> dict | None:
    value = row["alert"]
    return value if isinstance(value, dict) else None


def _row_to_item(row) -> dict:
    """workflow_runs 行 → ``ops_metrics`` 的流程项（字段名与 Temporal 读侧对齐）。"""
    result = row["result"] if isinstance(row["result"], dict) else {}
    return {
        "wf_id": row["workflow_id"],
        "run_id": row["run_id"],
        "exec_status": row["exec_status"],
        "stage": row["stage"],
        "kind": row["kind"] or "alert",
        # 风险列是 patch.risk 的镜像（历史行可能只有列没有 result.patch）
        "risk": row["risk"] or "",
        "start_time": db_layer.to_iso(row["start_time"]),
        "close_time": db_layer.to_iso(row["close_time"]),
        "duration_seconds": row["duration_seconds"],
        "confidence": row["confidence"],
        "patch_id": row["patch_id"],
        "alert": _alert_of(row),
        "gate_events": [str(event) for event in result.get("gate_events") or []],
        "test_report": result.get("test_report"),
        "patch": result.get("patch") if isinstance(result.get("patch"), dict) else None,
    }


def items_window(days: int = 7, now: datetime | None = None) -> list[dict]:
    """窗口内已终态流程（production SQL）：``close_time`` 落在窗口且非 RUNNING。"""
    _, cutoff = _cutoff(days, now)
    table = db_layer.workflow_runs
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(table)
            .where(table.c.close_time.is_not(None))
            .where(table.c.close_time >= cutoff)
            .where(table.c.exec_status != "RUNNING")
            .order_by(table.c.close_time.desc())
            .limit(MAX_ROWS)
        ).mappings()
        return [_row_to_item(row) for row in rows]


def audit_rows(days: int = 7, now: datetime | None = None) -> list[dict]:
    """窗口内操作审计行（production SQL）：``{wf_id, action, ts}``，最新在前。"""
    _, cutoff = _cutoff(days, now)
    table = db_layer.audit_events
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(table.c.workflow_id, table.c.action, table.c.ts)
            .where(table.c.ts >= cutoff)
            .order_by(table.c.id.desc())
            .limit(MAX_ROWS)
        ).mappings()
        return [
            {
                "wf_id": row["workflow_id"],
                "action": row["action"],
                "ts": db_layer.to_iso(row["ts"]),
            }
            for row in rows
        ]


def mirror_updated_at() -> str | None:
    """workflow_runs 最近一次镜像时间（面板展示数据面新鲜度；空表返回 None）。"""
    table = db_layer.workflow_runs
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        latest = conn.execute(select(table.c.updated_at).order_by(table.c.updated_at.desc()).limit(1)).scalar()
    return db_layer.to_iso(latest)


def _row_to_evidence(row) -> dict:
    quality = row["quality"] if isinstance(row["quality"], dict) else None
    return {
        "wf_id": row["wf_id"],
        "service": row["service"],
        "quality": quality,
        "quality_status": (quality or {}).get("status"),
    }


def _evidence_from_files(days: int, now: datetime | None) -> list[dict]:
    """demo：扫描 ``data/evidence/*.json``，按 ``archived_at`` 过滤窗口。"""
    _, cutoff = _cutoff(days, now)
    directory = evidence_store.evidence_dir()
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.json"))[:MAX_EVIDENCE_FILES]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        archived = db_layer.to_dt(record.get("archived_at") or record.get("collected_at"))
        if archived is not None and archived < cutoff:
            continue
        quality = record.get("quality") if isinstance(record.get("quality"), dict) else None
        rows.append(
            {
                "wf_id": record.get("wf_id") or path.stem,
                "service": record.get("service") or "",
                "quality": quality,
                "quality_status": (quality or {}).get("status"),
            }
        )
    return rows


def evidence_rows(days: int = 7, now: datetime | None = None) -> list[dict]:
    """窗口内证据归档质量行；production 读 ``log_evidence``，demo 扫文件。"""
    if not mode.is_production():
        return _evidence_from_files(days, now)
    _, cutoff = _cutoff(days, now)
    table = db_layer.log_evidence
    engine = db_layer.get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(table.c.wf_id, table.c.service, table.c.quality)
            .where(table.c.archived_at >= cutoff)
            .limit(MAX_ROWS)
        ).mappings()
        return [_row_to_evidence(row) for row in rows]


def managed_services() -> list[str]:
    """受管服务清单（覆盖率分母）：只取启用且配置了服务标签的条目。"""
    services = {
        str(app.get("service") or "").strip()
        for app in monitored_apps.list_all()
        if app.get("enabled", True)
    }
    return sorted(service for service in services if service)


def load(days: int = 7, now: datetime | None = None) -> dict:
    """production 数据面：一次取齐 ``compute`` 所需的 SQL 分量。

    返回 ``{items, audit_rows, evidence_rows, managed_services, mirror_updated_at,
    degraded, source}``；任一分量失败只降级该分量（空列表 + degraded 标记）。
    证据不可读时 ``evidence_rows=None``（区别于「0 条归档」）。
    """
    if not mode.is_production():
        raise mode.ModeConfigError("ops_warehouse 仅用于 production 数据面；demo 请读 Temporal")

    degraded: list[str] = []

    def _safe(name: str, loader, fallback):
        try:
            return loader()
        except Exception:  # noqa: BLE001 - 单分量失败不得拖垮整个面板
            degraded.append(name)
            return fallback

    items = _safe("warehouse_items", lambda: items_window(days, now), [])
    audits = _safe("warehouse_audit", lambda: audit_rows(days, now), [])
    evidence = _safe("warehouse_evidence", lambda: evidence_rows(days, now), None)
    services = _safe("warehouse_services", managed_services, [])
    updated = _safe("warehouse_meta", mirror_updated_at, None)

    return {
        "items": items,
        "audit_rows": audits,
        "evidence_rows": evidence,
        "managed_services": services,
        "mirror_updated_at": updated,
        "degraded": degraded,
        "source": "warehouse",
    }

