"""workflow_runs 落库（production 专用）：BFF 汇总时增量 upsert，供报表与历史检索。

设计要点（与方案的差异说明见《实施计划 v2》）：
    - **不触碰 workflow 编排层**：落库由 BFF 在 ``/api/flows`` 汇总后执行，避免在
      Temporal workflow 内新增 activity 调用带来的 replay 风险；
    - **内存 delta 检测**：只有 (stage, exec_status, close_time, confidence, patch_id)
      发生变化的行才写库（BFF 单副本；重启后首轮全量补写一次）；
    - demo：no-op（Temporal 本身即数据源，控制台实时聚合，无需镜像）。

调用方式::

    from bff import runs_store
    runs_store.sync_runs(items)   # 建议经 asyncio.to_thread 调用（同步 DB 操作）
"""

from __future__ import annotations

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import metrics, mode

# 变化签名键（任一变化即触发 upsert）
_SIGNATURE_FIELDS = ("stage", "exec_status", "close_time", "confidence", "patch_id")

# 终态集合：首次出现时计 workflow_completed 指标（SHADOWED 为 P1-2 影子档终态）
_TERMINAL_STAGES = {"DONE", "ESCALATED", "CANCELLED", "FAILED", "SHADOWED"}

_last_seen: dict[str, tuple] = {}


def _signature(item: dict) -> tuple:
    return tuple(item.get(field) for field in _SIGNATURE_FIELDS)


def _clear_cache() -> None:
    """清空 delta 缓存（测试用；生产进程内无需调用）。"""
    _last_seen.clear()


def _upsert(conn, item: dict) -> None:
    wf_id = item["wf_id"]
    values = {
        "run_id": item.get("run_id") or "",
        "stage": item.get("stage") or "",
        "exec_status": item.get("exec_status") or "",
        "alert": item.get("alert"),
        "confidence": item.get("confidence"),
        "patch_id": item.get("patch_id"),
        "start_time": db_layer.to_dt(item.get("start_time")),
        "close_time": db_layer.to_dt(item.get("close_time")),
        "duration_seconds": item.get("duration_seconds"),
        "result": item.get("result"),
        "mode": "production",
        "updated_at": db_layer.now_dt(),
    }
    exists = conn.execute(
        select(db_layer.workflow_runs.c.workflow_id).where(
            db_layer.workflow_runs.c.workflow_id == wf_id
        )
    ).scalar()
    if exists is None:
        conn.execute(db_layer.workflow_runs.insert().values(workflow_id=wf_id, **values))
    else:
        conn.execute(
            db_layer.workflow_runs.update()
            .where(db_layer.workflow_runs.c.workflow_id == wf_id)
            .values(**values)
        )


def _observe_terminal(item: dict, previous: tuple | None) -> None:
    """终态首次出现 → workflow_completed 指标。

    指标以 BFF 查询为采样窗口（重启后首轮会补写并重计一次），
    仅作容量规划/趋势观测，不参与任何业务判定。
    """
    stage = item.get("stage")
    if stage not in _TERMINAL_STAGES:
        return
    previous_stage = previous[0] if previous else None
    if previous_stage == stage:
        return
    service = (item.get("alert") or {}).get("service") or "unknown"
    metrics.observe_workflow_completed(service, str(stage), item.get("duration_seconds"))


def sync_runs(items: list[dict]) -> int:
    """把流程列表结果 diff 后 upsert 到 ``workflow_runs``，返回实际写入行数。

    demo 模式 no-op 返回 0；写库失败向上抛（由调用方决定是否降级——BFF 中
    以 warning 兜底，读接口不因镜像失败而不可用）。
    """
    if not mode.is_production():
        return 0

    changed = [
        item
        for item in items
        if item.get("wf_id") and _last_seen.get(item["wf_id"]) != _signature(item)
    ]
    if not changed:
        return 0

    engine = db_layer.get_engine()
    with engine.begin() as conn:
        for item in changed:
            _upsert(conn, item)
    for item in changed:
        previous = _last_seen.get(item["wf_id"])
        _last_seen[item["wf_id"]] = _signature(item)
        _observe_terminal(item, previous)
    return len(changed)
