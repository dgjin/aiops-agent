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

from aiops_agent.workflows import AIOpsFixWorkflow, AIOpsRequirementWorkflow
from aiops_agent.mode import ModeConfigError, temporal_namespace, temporal_task_queue
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

WF_TYPE = "AIOpsFixWorkflow"
# 需求驱动修复流程（管理员批准分析结果后启动）；status query / 信号与告警流程按名称绑定，通用
WF_TYPE_REQUIREMENT = "AIOpsRequirementWorkflow"
TASK_QUEUE = temporal_task_queue()  # 与 worker.py / alert_webhook.py 一致
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


def classify_release_status(stage: str | None) -> str:
    """流程终态 stage → 发布状态分类（纯函数；需求基线的状态控制用）。

    ``DONE``（金丝雀健康 → 全量发布成功）→ ``released``；``ESCALATED``（转人工，
    含自动回滚）→ ``escalated``；``CANCELLED`` → ``cancelled``；``FAILED`` →
    ``failed``；其余（缺失 / 无法确认）→ ``unknown``——不臆造「已发布成功」。
    """
    return {
        "DONE": "released",
        "ESCALATED": "escalated",
        "CANCELLED": "cancelled",
        "FAILED": "failed",
    }.get(stage or "", "unknown")


def approval_receipt_from(payload: dict, command_id: str) -> tuple[str, dict] | None:
    """从 status/result 中定位审批命令回执（纯函数）。

    - 命中 ``approvals[].command_id`` → ``accepted``（工作流已记录并生效）；
    - 命中 ``approval_error.command_id`` → ``rejected``（工作流业务拒绝）；
    - 未命中 → None（尚未可见，仍为已提交）。
    """
    command_id = str(command_id or "")
    if not command_id or not isinstance(payload, dict):
        return None
    for record in reversed(payload.get("approvals") or []):
        if isinstance(record, dict) and record.get("command_id") == command_id:
            return "accepted", record
    error = payload.get("approval_error")
    if isinstance(error, dict) and error.get("command_id") == command_id:
        return "rejected", error
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
        **_audit_fields(status),
    )


def _audit_fields(payload: dict) -> dict:
    """审批证据与运行溯源字段（status 与 result 同名同义，缺失时保持网关默认值）。"""
    return {
        "case_id": payload.get("case_id") or "",
        "approval_contract": payload.get("approval_contract") or "",
        "evidence_binding": payload.get("evidence_binding"),
        "approvals": payload.get("approvals") or [],
        "gate_events": payload.get("gate_events") or [],
        "approval_error": payload.get("approval_error"),
        "approval_rejections": payload.get("approval_rejections") or [],
        "command_rejections": payload.get("command_rejections") or [],
        "command_error": payload.get("command_error"),
        "requested_by": payload.get("requested_by") or "",
        "parent_run_id": payload.get("parent_run_id") or "",
        "parent_wf_id": payload.get("parent_wf_id") or "",
        "retry_wf_id": payload.get("retry_wf_id") or "",
    }


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
        root_cause=result.get("root_cause"),
        queued_patches=[
            {"workflow_id": None, "alert_id": alert_id}
            for alert_id in result.get("queued_patches") or []
        ],
        **_audit_fields(result),
    )


class TemporalGateway:
    """Temporal Client 的懒连接封装（BFF 单进程复用）。"""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._list_cache: tuple[float, list[dict]] | None = None

    async def client(self) -> Client:
        namespace = temporal_namespace()
        temporal_task_queue()
        async with self._lock:
            if self._client is None:
                address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
                self._client = await asyncio.wait_for(Client.connect(address, namespace=namespace), timeout=5)
            if self._client.namespace != namespace:
                raise ModeConfigError("缓存 Temporal Client namespace 不符；要求受控 restart")
            return self._client

    async def ping(self) -> tuple[bool, float, str | None]:
        """连通性探测：返回 (ok, 延迟ms, 错误)。"""
        started = time.monotonic()
        try:
            client = await self.client()
            reply = await asyncio.wait_for(client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=temporal_namespace()), timeout=_QUERY_TIMEOUT,
            ), timeout=5)
            if reply.namespace_info.name != temporal_namespace():
                raise ModeConfigError("Temporal RPC 返回错误 namespace")
            return True, (time.monotonic() - started) * 1000, None
        except Exception as exc:  # noqa: BLE001 - 探测端点需要吞掉所有连接异常
            return False, (time.monotonic() - started) * 1000, f"Temporal RPC 失败 ({type(exc).__name__})"

    def handle(self, wf_id: str, run_id: str | None = None) -> WorkflowHandle:
        assert self._client is not None, "client 未初始化"
        if self._client.namespace != temporal_namespace():
            raise ModeConfigError("缓存 Temporal Client namespace 不符")
        return self._client.get_workflow_handle(wf_id, run_id=run_id)

    # ------------------------------------------------------------------
    # 列表聚合
    # ------------------------------------------------------------------

    async def list_flows(self, limit: int = 100, *, use_cache: bool = True) -> list[dict]:
        client = await self.client()  # 缓存命中也必须验证 namespace，不能跨档返回旧列表
        if (
            use_cache
            and self._list_cache
            and time.monotonic() - self._list_cache[0] < _LIST_CACHE_TTL
        ):
            return self._list_cache[1][:limit]

        records: list[WorkflowExecution] = []
        # 两类工作流同列聚合：告警驱动 + 需求驱动（控制台列表统一展示，审批/发布指令复用同一交互）
        query = f'WorkflowType="{WF_TYPE}" OR WorkflowType="{WF_TYPE_REQUIREMENT}"'
        async for wf in client.list_workflows(query):
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
            # M5：运营指标按来源拆分（告警修复 vs 需求修复），不得混入同一自动完成率
            "kind": "requirement" if wf.workflow_type == WF_TYPE_REQUIREMENT else "alert",
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
            # 审批证据与运行溯源（approval-evidence-v1；旧流程 contract 为空）
            "case_id": "",
            "approval_contract": "",
            "evidence_binding": None,
            "approvals": [],
            "gate_events": [],
            "approval_error": None,
            "approval_rejections": [],
            "requested_by": "",
            "parent_run_id": "",
            "parent_wf_id": "",
            "retry_wf_id": "",
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
    # 轻量执行列表（History 投影器专用）
    # ------------------------------------------------------------------

    async def list_executions(self, limit: int = 200) -> list[dict]:
        """只返回执行元数据（不 query status / result），供后台 History 同步使用。

        与 :meth:`list_flows` 共用同一查询口径，但避免为每个流程额外发 query RPC；
        M4 投影器按 ``exec_status`` 决定增量同步或终态最终同步。
        """
        client = await self.client()
        records: list[WorkflowExecution] = []
        query = f'WorkflowType="{WF_TYPE}" OR WorkflowType="{WF_TYPE_REQUIREMENT}"'
        async for wf in client.list_workflows(query):
            records.append(wf)
            if len(records) >= _LIST_MAX_FETCH:
                break
        records.sort(key=lambda wf: _aware(wf.start_time), reverse=True)
        return [
            {
                "wf_id": wf.id,
                "run_id": wf.run_id,
                "exec_status": wf.status.name if wf.status else "UNKNOWN",
                "start_time": _iso(wf.start_time),
                "close_time": _iso(wf.close_time),
                "workflow_type": str(wf.workflow_type),
            }
            for wf in records[:limit]
        ]

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

    async def release_status(self, wf_id: str) -> str:
        """单流程发布状态（需求状态控制）：released / running / escalated / cancelled / failed / unknown。

        需求分析批准后启动的修复工作流走完整沙箱 / 审批 / 公告 / 金丝雀链路：
        - 运行中不读 result（避免阻塞等待完成），直接 ``running``；
        - 终态读取 result.stage 分类（DONE=已发布成功 / ESCALATED / CANCELLED）；
        - 硬失败读不到 result 时按执行状态派生（见 :func:`stage_from_exec_status`）；
        - 任何查询异常返回 ``unknown``（安全侧：不臆造「已发布成功」）。
        """
        client = await self.client()
        handle = client.get_workflow_handle(wf_id)
        try:
            desc = await handle.describe(rpc_timeout=_QUERY_TIMEOUT)
        except Exception:  # noqa: BLE001 - 查询失败降级 unknown
            return "unknown"
        if desc.status == WorkflowExecutionStatus.RUNNING:
            return "running"
        stage: str | None = None
        try:
            result = await handle.result(rpc_timeout=_QUERY_TIMEOUT)
            if isinstance(result, dict):
                stage = result.get("stage")
        except Exception:  # noqa: BLE001 - 硬失败/终止读不到 result：按执行状态派生
            pass
        if not stage:
            stage = stage_from_exec_status(desc.status.name if desc.status else None)
        return classify_release_status(stage)

    # ------------------------------------------------------------------
    # 信号（写操作；前置校验与审计由 app 层负责）
    # ------------------------------------------------------------------

    async def signal_approval(self, wf_id: str, decision: str | dict) -> None:
        """下发一级审批：字符串＝旧协议；dict＝approval-evidence-v1 证据信封。"""
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_approval, decision)

    async def signal_second_approval(self, wf_id: str, decision: str | dict) -> None:
        """下发二级审批：字符串＝旧协议；dict＝approval-evidence-v1 证据信封。"""
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_second_approval, decision)

    async def signal_deploy_command(self, wf_id: str, command: str | dict) -> None:
        """下发窗口指令：字符串＝旧协议；dict＝workflow-command-v1 信封。"""
        await self.handle(wf_id).signal(AIOpsFixWorkflow.submit_deploy_command, command)

    async def signal_queue_patch(
        self, wf_id: str, new_wf_id: str | dict, alert=None
    ) -> None:
        """下发排队补丁：dict＝workflow-command-v1 信封（单参数）；否则旧两参数协议。"""
        handle = self.handle(wf_id)
        if isinstance(new_wf_id, dict):
            await handle.signal(AIOpsFixWorkflow.submit_patch, new_wf_id)
            return
        await handle.signal(AIOpsFixWorkflow.submit_patch, args=[new_wf_id, alert])

    async def approval_receipt(
        self, wf_id: str, command_id: str, attempts: int = 4, delay: float = 0.1
    ) -> tuple[str, dict | None]:
        """审批命令业务回执：``accepted`` / ``rejected`` / ``submitted``（尚未可见）。

        signal RPC 成功只表示命令已送达 Temporal，不代表工作流接受了审批（证据过期、
        职责分离等会在工作流侧拒绝）。控制台必须展示真实回执，避免把「已提交」误报为
        「已批准」。短轮询仅用于消除信号与 query 之间的可见性延迟。
        """
        handle = self.handle(wf_id)
        last_error = ""
        for attempt in range(max(1, attempts)):
            receipt = None
            try:
                status = await handle.query(
                    AIOpsFixWorkflow.status, rpc_timeout=_QUERY_TIMEOUT
                )
                if isinstance(status, dict):
                    receipt = approval_receipt_from(status, command_id)
            except Exception as exc:  # noqa: BLE001 - 流程可能已结束，回退读 result
                last_error = str(exc)
                try:
                    desc = await handle.describe(rpc_timeout=_QUERY_TIMEOUT)
                    if desc.status != WorkflowExecutionStatus.RUNNING:
                        result = await handle.result(rpc_timeout=_QUERY_TIMEOUT)
                        if isinstance(result, dict):
                            receipt = approval_receipt_from(result, command_id)
                except Exception:  # noqa: BLE001 - 保持 submitted
                    pass
            if receipt is not None:
                return receipt
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        return "submitted", ({"error": last_error} if last_error else None)

    async def start_flow(self, alert, wf_id: str, evidence_context: dict | None = None) -> str:
        """以指定幂等键启动修复流程（P1-3 重试修复）；已存在同 ID 流程时抛 ValueError。

        策略快照在启动时读取（与 webhook/demo_cli 同约定：启动方决定快照）。
        ``evidence_context``：人工重试溯源（case_id / requested_by / parent_run_id /
        parent_wf_id / retry_wf_id），工作流据此把重试关联回原件并拒绝自审。
        """
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        from aiops_agent.config import load_policy, snapshot_for_workflow

        snapshot = snapshot_for_workflow(load_policy())
        if evidence_context:
            snapshot["evidence_context"] = dict(evidence_context)
        client = await self.client()
        try:
            handle = await client.start_workflow(
                AIOpsFixWorkflow.run,
                args=[alert, snapshot],
                id=wf_id,
                task_queue=temporal_task_queue(),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError as exc:
            raise ValueError(f"流程已存在（幂等拒绝）：{wf_id}") from exc
        self.invalidate_cache()
        return handle.id

    async def start_requirement_flow(
        self,
        task,
        wf_id: str,
        resume_patch: dict | None = None,
        evidence_context: dict | None = None,
    ) -> str:
        """启动需求驱动修复流程（管理员批准分析结果后）；幂等键 ``aiops-req-{analysis_id}-v{version}``。

        与 :meth:`start_flow` 同约定：策略快照在启动时读取；已存在同 ID 流程时抛 ValueError。
        工作流内部保留沙箱验证 / 人工审批 / 公告倒计时 / 金丝雀发布全链路。

        ``resume_patch``：影子建议续接——影子档已生成补丁的 dict（取自原 SHADOWED
        流程 result）注入策略快照，工作流首轮跳过补丁生成、直接以该补丁进入完整
        沙箱验证（见 AIOpsRequirementWorkflow / workflows._patch_from_resume）。
        """
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        from aiops_agent.config import load_policy, snapshot_for_workflow

        snapshot = snapshot_for_workflow(load_policy())
        if resume_patch is not None:
            snapshot["resume_patch"] = resume_patch
        if evidence_context:
            snapshot["evidence_context"] = dict(evidence_context)
        client = await self.client()
        try:
            handle = await client.start_workflow(
                AIOpsRequirementWorkflow.run,
                args=[task, snapshot],
                id=wf_id,
                task_queue=temporal_task_queue(),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError as exc:
            raise ValueError(f"流程已存在（幂等拒绝）：{wf_id}") from exc
        self.invalidate_cache()
        return handle.id

    def invalidate_cache(self) -> None:
        self._list_cache = None
