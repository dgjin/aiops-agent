"""运营度量域：/api/metrics/ops（M5 值班看板数据源）。

数据面双轨（口径同一份，见 :mod:`bff.ops_metrics`）：
    - production：``workflow_runs`` / ``audit_events`` / ``log_evidence`` SQL 聚合，
      规避 Temporal 列表截断；镜像为空时退回 Temporal 读侧并标记 ``warehouse-empty``；
    - demo：Temporal 读侧 + 证据文件扫描（``data/evidence/``）。

读侧聚合，无写路径；数据源任一分量不可读时降级（该分量置空并记入 ``degraded``）
——派生统计失败不应让面板整体 500。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from aiops_agent import log_health, mode

from .. import audit, commands, escalations, ops_metrics, ops_warehouse, runbooks
from ..deps import gw, ok

router = APIRouter()


async def _flow_items(degraded: list[str]) -> list[dict]:
    try:
        return await gw.list_flows(limit=500)
    except Exception:  # noqa: BLE001 - Temporal 不可达：空窗口展示
        degraded.append("temporal")
        return []


async def _audit_rows(degraded: list[str]) -> list[dict]:
    try:
        return await asyncio.to_thread(audit.read_audit, 2000)
    except Exception:  # noqa: BLE001 - 审计不可读：人工干预率显示「暂无数据」
        degraded.append("audit")
        return []


async def _escalation_stats(degraded: list[str]) -> dict | None:
    try:
        return await asyncio.to_thread(escalations.stats)
    except Exception:  # noqa: BLE001 - 待办存储不可读：仅隐藏转人工现状
        degraded.append("escalations")
        return None


async def _escalation_entries(degraded: list[str]) -> list[dict] | None:
    try:
        return await asyncio.to_thread(escalations.list_all)
    except Exception:  # noqa: BLE001 - 待办条目不可读：SLA/关闭原因显示「暂无数据」
        degraded.append("escalation_entries")
        return None


async def _evidence_rows(days: int, degraded: list[str]) -> list[dict] | None:
    try:
        return await asyncio.to_thread(ops_warehouse.evidence_rows, days)
    except Exception:  # noqa: BLE001 - 证据归档不可读（≠ 0 条归档）
        degraded.append("evidence")
        return None


def _managed_services(degraded: list[str]) -> list[str] | None:
    try:
        return ops_warehouse.managed_services()
    except Exception:  # noqa: BLE001 - 清单不可读：覆盖率显示「暂无数据」
        degraded.append("monitored_apps")
        return None


async def _command_stats(degraded: list[str]) -> dict | None:
    """写命令 outbox 积压（M5 上线硬门禁）；不可读时返回 None 而非 0。"""
    try:
        return await asyncio.to_thread(commands.stats)
    except Exception:  # noqa: BLE001 - 命令存储不可读：门禁按 unknown 处理
        degraded.append("commands")
        return None


async def _audit_integrity(degraded: list[str]) -> dict | None:
    """审计链完整性（M5 上线硬门禁）；不可读时返回 None 而非「链正常」。"""
    try:
        return await asyncio.to_thread(audit.verify)
    except Exception:  # noqa: BLE001 - 链校验失败/存储不可读：门禁按 unknown 处理
        degraded.append("audit_integrity")
        return None


async def _load_warehouse(days: int, degraded: list[str]) -> dict | None:
    """production SQL 数据面；引擎不可达或镜像为空时返回 None（退回 Temporal）。"""
    try:
        plane = await asyncio.to_thread(ops_warehouse.load, days)
    except Exception:  # noqa: BLE001 - DB 不可达：退回 Temporal 读侧
        degraded.append("warehouse")
        return None
    degraded.extend(plane["degraded"])
    if not plane["items"] and plane["mirror_updated_at"] is None:
        # 镜像从未同步：不能用「0 条」冒充「无流程」，退回 Temporal 并明示
        degraded.append("warehouse-empty")
        return None
    return plane


@router.get("/api/metrics/ops")
async def api_ops_metrics(days: int = Query(7, ge=1, le=30)) -> dict:
    """近 N 天运营度量：分维度完成率 / MTTA / MTTR / 回滚 / 证据 / 沙箱 / 值班告警。"""
    degraded: list[str] = []
    source = "temporal"
    mirror_updated_at: str | None = None

    plane = await _load_warehouse(days, degraded) if mode.is_production() else None
    if plane is not None:
        items = plane["items"]
        audit_rows = plane["audit_rows"]
        evidence_rows = plane["evidence_rows"]
        managed_services = plane["managed_services"]
        mirror_updated_at = plane["mirror_updated_at"]
        source = plane["source"]
    else:
        items = await _flow_items(degraded)
        audit_rows = await _audit_rows(degraded)
        evidence_rows = await _evidence_rows(days, degraded)
        managed_services = _managed_services(degraded)

    esc_stats = await _escalation_stats(degraded)
    esc_entries = await _escalation_entries(degraded)
    log_ship = await asyncio.to_thread(_log_ship_snapshot, degraded)
    command_stats = await _command_stats(degraded)
    audit_integrity = await _audit_integrity(degraded)

    data = ops_metrics.compute(
        items,
        audit_rows,
        esc_stats,
        days=days,
        esc_entries=esc_entries,
        evidence_rows=evidence_rows,
        log_ship=log_ship,
        managed_services=managed_services,
        source=source,
        command_stats=command_stats,
        audit_integrity=audit_integrity,
    )
    data["mirror_updated_at"] = mirror_updated_at
    data["degraded"] = degraded
    return ok(data)


def _log_ship_snapshot(degraded: list[str]) -> list[dict] | None:
    """采集心跳快照；心跳文件缺失返回空列表（前端显示「暂无数据」而非「正常」）。"""
    try:
        return log_health.snapshot()
    except Exception:  # noqa: BLE001 - 心跳不可读：采集新鲜度显示「暂无数据」
        degraded.append("log_health")
        return None


@router.get("/api/metrics/ops/runbook")
async def api_ops_runbook(code: str = Query(..., min_length=2, max_length=48)) -> dict:
    """值班 Runbook 小节（按告警 code 从 docs 读取；未知 code 返回 known=False）。"""
    return ok(await asyncio.to_thread(runbooks.section, code))
