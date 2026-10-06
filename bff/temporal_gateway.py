"""Temporal 网关：列表聚合 / 状态查询 / 详情 / 信号下发（设计方案 5.3、6.1）。

设计要点：
- 运行中流程：实时 ``status`` query（含 deadline / needs_second 扩展字段）；
- 终态流程：读取工作流 result（审计记录），避免对已关闭流程重复 replay query；
- 短时缓存（3s）：同一 tick 内多页面复用列表结果，降低 Temporal 压力；
- 写操作前置校验由 :func:`check_stage_allowed` 提供（配合 app 层 409 语义）。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus, WorkflowHandle

from aiops_agent.workflows import AIOpsFixWorkflow

WF_TYPE = "AIOpsFixWorkflow"
TASK_QUEUE = "aiops-tasks"  # 与 worker.py / alert_webhook.py 一致
_QUERY_TIMEOUT = timedelta(seconds=5)
_LIST_CACHE_TTL = 3.0
_LIST_MAX_FETCH = 200


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=timezone.utc)


def check_stage_allowed(current: str | None, allowed: set[str]) -> str | None:
    """写操作前置校验（纯函数）：不满足返回错误说明，满足返回 None。"""
    if current not in allowed:
        return f"当前阶段 {current or '未知'} 不允许该操作（要求 {'/'.join(sorted(allowed))}）"
    return None


# 执行状态 → 流程终态分类（仅在 result 不可读、stage 为空时用于补全）
_FAILED_EXEC_STATUSES = {"FAILED", "TIMED_OUT"}
_CANCELLED_EXEC_STATUSES = {"CANCELED", "TERMINATED"}


def stage_from_exec_status(exec_status: str | None) -> str | None:
    """把 Temporal 执行状态映射为流程终态（纯函数）。

    「硬失败」流程读不到 result，stage 会为空，导致看板与审计把它显示成「未知」而
    在统计中消失（缺陷 B3）。这里按执行状态派生可辨识的终态：失败 → ``FAILED``，
    取消/终止 → ``CANCELLED``；其余返回 None（保持未知，不臆造）。
    """
    status = (exec_status or "").upper()
    if status in _FAILED_EXEC_STATUSES:
        return "FAILED"
    if status in _CANCELLED_EXEC_STATUSES:
        return "CANCELLED"
    return None


def _item_from_status(base: dict, status: dict) -> None:
    base.update(
        stage=status.get("stage"),
        alert=status.get("alert"),
        deadline=status.get("deadline"),
        needs_second=bool(status.get("needs_second")),
        patch_id=status.get("patch_id"),
        approval=status.get("approval"),
        second_approval=status.get("second_approval"),
        deploy_command=status.get("deploy_command"),
        queued_patches=status.get("queued_patches") or [],
        # 待审批期的判断依据（工作流未结束，result 尚不可读）
        patch=status.get("patch"),
        test_report=status.get("test_report"),
        root_cause=status.get("root_cause"),
        confidence=(status.get("root_cause") or {}).get("confidence"),
    )


def _item_from_result(base: dict, result: dict) -> None:
    base.update(
        stage=result.get("stage"),
        alert={
            "alert_id": result.get("alert_id"),
            "service": result.get("service"),
            # description/severity 供重试构造原告警（P1-3）
            "description": (result.get("alert") or {}).get("description") or "",
            "severity": (result.get("alert") or {}).get("severity") or "critical",
        },
        duration_seconds=result.get("duration_seconds"),
        confidence=result.get("confidence"),
        model_version=result.get("model_version"),
        patch_id=result.get("patch_id"),
        patch=result.get("patch"),
        test_report=result.get("test_report"),
        # 闸门事件链（P1-3 升级原因摘要 / P1-4 拦截率统计的数据源）
        gate_events=result.get("gate_events") or [],
        root_cause=result.get("root_cause"),
        queued_patches=[
            {"workflow_id": None, "alert_id": alert_id}
            for alert_id in result.get("queued_patches") or []
        ],
    )


class TemporalGateway:
    """Temporal Client 的懒连接封装（BFF 单进程复用）。"""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._list_cache: tuple[float, list[dict]] | None = None

    async def client(self) -> Client:
        async with self._lock:
            if self._client is None:
                address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
                self._client = await Client.connect(address)
            return self._client

    async def ping(self) -> tuple[bool, float, str | None]:
        """连通性探测：返回 (ok, 延迟ms, 错误)。"""
        started = time.monotonic()
        try:
            await self.client()
            return True, (time.monotonic() - started) * 1000, None
        except Exception as exc:  # noqa: BLE001 - 探测端点需要吞掉所有连接异常
            return False, (time.monotonic() - started) * 1000, str(exc)

    def handle(self, wf_id: str, run_id: str | None = None) -> WorkflowHandle:
        assert self._client is not None, "client 未初始化"
        return self._client.get_workflow_handle(wf_id, run_id=run_id)

    # ------------------------------------------------------------------
    # 列表聚合
    # ------------------------------------------------------------------

    async def list_flows(self, limit: int = 100, *, use_cache: bool = True) -> list[dict]:
        if (
            use_cache
            and self._list_cache
            and time.monotonic() - self._list_cache[0] < _LIST_CACHE_TTL
        ):
            return self._list_cache[1][:limit]

        client = await self.client()
        records: list[WorkflowExecution] = []
        async for wf in client.list_workflows(f'WorkflowType="{WF_TYPE}"'):
            records.append(wf)
            if len(records) >= _LIST_MAX_FETCH:
                break
        records.sort(key=lambda wf: _aware(wf.start_time), reverse=True)

        items = await asyncio.gather(*(self._enrich(client, wf) for wf in records))
        if use_cache:
            self._list_cache = (time.monotonic(), list(items))
        return list(items)[:limit]

    async def _enrich(self, client: Client, wf: WorkflowExecution) -> dict:
        base: dict = {
            "wf_id": wf.id,
            "run_id": wf.run_id,
            "exec_status": wf.status.name if wf.status else "UNKNOWN",
            "start_time": _iso(wf.start_time),
            "close_time": _iso(wf.close_time),
            "stage": None,
            "alert": None,
            "deadline": None,
            "needs_second": False,
            "approval": None,
            "second_approval": None,
            "deploy_command": None,
            "queued_patches": [],
            "duration_seconds": None,
            "confidence": None,
            "model_version": None,
            "patch_id": None,
            # 审批期证据（status 查询或终态 result 中取得）
            "patch": None,
            "test_report": None,
            "root_cause": None,
            "stage_error": None,
        }
        handle = client.get_workflow_handle(wf.id, run_id=wf.run_id)
        if wf.status == WorkflowExecutionStatus.RUNNING:
            try:
                status = await handle.query(AIOpsFixWorkflow.status, rpc_timeout=_QUERY_TIMEOUT)
                _item_from_status(base, status)
            except Exception as exc:  # noqa: BLE001 - 聚合失败降级为占位项
                base["stage_error"] = f"status query 失败: {exc}"
        else:
            try:
                result = await handle.result(rpc_timeout=_QUERY_TIMEOUT)
                _item_from_result(base, result)
            except Exception as exc:  # noqa: BLE001 - 结果缺失时降级
                base["stage_error"] = f"result 读取失败: {exc}"
        # 终态补全：硬失败/取消的流程读不到 result，按执行状态派生终态，避免显示为「未知」
        if not base["stage"]:
            base["stage"] = stage_from_exec_status(base["exec_status"])
        return base

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------

    async def flow_detail(self, wf_id: str) -> dict:
        client = await self.client()
        handle = client.get_workflow_handle(wf_id)
        desc = await handle.describe(rpc_timeout=_QUERY_TIMEOUT)

        status: dict | None = None
        result: dict | None = None
        errors: list[str] = []
        try:
            status = await handle.query(AIOpsFixWorkflow.status, rpc_timeout=_QUERY_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"status query 失败: {exc}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            try:
                result = await handle.result(rpc_timeout=_QUERY_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"result 读取失败: {exc}")

        return {
            "wf_id": desc.id,
            "run_id": desc.run_id,
            "exec_status": desc.status.name if desc.status else "UNKNOWN",
            "start_time": _iso(desc.start_time),
            "close_time": _iso(desc.close_time),
            "workflow_type": str(desc.workflow_type),
            "task_queue": desc.task_queue,
            "status": status,
            "result": result,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # 信号（写操作；前置校验与审计由 app 层负责）
    # ------------------------------------------------------------------

    async def signal_approval(self, wf_id: str, decision: str) -> None:
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_approval, decision)

    async def signal_second_approval(self, wf_id: str, decision: str) -> None:
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_second_approval, decision)

    async def signal_deploy_command(self, wf_id: str, command: str) -> None:
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_deploy_command, command)

    async def signal_queue_patch(self, wf_id: str, new_wf_id: str, alert) -> None:
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_patch, args=[new_wf_id, alert])

    async def start_flow(self, alert, wf_id: str) -> str:
        """以指定幂等键启动修复流程（P1-3 重试修复）；已存在同 ID 流程时抛 ValueError。

        策略快照在启动时读取（与 webhook/demo_cli 同约定：启动方决定快照）。
        """
        from temporalio.client import WorkflowAlreadyStartedError
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        from aiops_agent.config import load_policy, snapshot_for_workflow

        snapshot = snapshot_for_workflow(load_policy())
        client = await self.client()
        try:
            handle = await client.start_workflow(
                AIOpsFixWorkflow.run,
                args=[alert, snapshot],
                id=wf_id,
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError as exc:
            raise ValueError(f"流程已存在（幂等拒绝）：{wf_id}") from exc
        self.invalidate_cache()
        return handle.id

    def invalidate_cache(self) -> None:
        self._list_cache = None
