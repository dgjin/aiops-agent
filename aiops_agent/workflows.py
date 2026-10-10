"""AIOpsFixWorkflow 主工作流（设计方案第 3 节状态机、第 8 节信号接口）。

状态机：
    DETECTED → TRIAGING → FIXING → TESTING → WAIT_APPROVAL
    → NOTIFYING → CANARY → ROLLING_OUT → DONE

异常路径：
    - 闸门 1：置信度 < 阈值 → ESCALATED
    - 测试回炉重试耗尽 → ESCALATED
    - 闸门 2：驳回 / 超时 → ESCALATED
    - 窗口内 cancel → CANCELLED（排队补丁保留，队首补丁自动开启新周期）
    - 金丝雀劣化 → 回滚 + ESCALATED

渐进信任档（P1-2）：
    - shadow_mode 开启时，测试通过后不创建 MR、不进入审批/发布，
      仅生成建议卡片留痕后以 SHADOWED 终态结束（只建议不执行）。
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import activities
    from .models import (
        Alert,
        CanaryResult,
        Patch,
        ReleaseResult,
        RequirementTask,
        RootCause,
        TestReport,
    )

_ACTIVITY_TIMEOUT = timedelta(seconds=60)
# 金丝雀发布为长时活动：含镜像构建、容器滚动与真实观测窗口（生产 observe 可达 5m），单独放宽
_CANARY_ACTIVITY_TIMEOUT = timedelta(seconds=600)
# 修复活动超时：Qoder 提供者为仓库级多轮自主修复（实测约 50s 起），显著长于本地 LLM 单次生成。
# 约束：必须 **大于** 提供者内部最坏耗时（子进程超时 720s + 服务端快速重试预算 180s），
# 这样超时会在活动内部触发优雅降级（QoderFixError → 兜底补丁），而不是被 Temporal 取消
# ——被取消会一路冒泡为活动失败并触发重试，最终无法降级。
# 服务端瞬时故障快速重试（仅 wall<60s 且零轮次的调用，退避 15s+45s）预算 ≈ 180s，含于主预算内。
_FIX_ACTIVITY_TIMEOUT = timedelta(seconds=960)
# RAG 检索活动：首次接入的应用在检索前会自动构建专属索引（嵌入批量请求，分钟级），
# 单独放宽；构建在独立线程内完成且不随活动取消，超时重试时命中已完成索引。
_RAG_ACTIVITY_TIMEOUT = timedelta(seconds=300)
_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
)


def _clip_event_text(text: str, limit: int = 180) -> str:
    """压平空白并截断文本（确定性纯函数；事件串/升级文案保持单行且不过长）。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _attempt_failure_summary(patch: Patch | None, report: TestReport | None) -> str:
    """单轮修复失败的确定性摘要（gate_events 与转人工原因共用，使升级提示具体到真实原因）。

    分类优先级对应失败现场：
    1. 补丁生成降级（degraded）——生成环节失败（服务端故障 / diff 校验拒绝 /
       未定位目标文件），而非沙箱测试结论；附降级原因摘要；
    2. 静态扫描（SAST / Bandit）拦截；
    3. 工作区准备失败（含前端契约模式的补丁应用失败）；
    4. 真实测试失败——取单测结论文本。
    """
    if patch is not None and patch.degraded:
        detail = patch.degrade_reason or "详见沙箱报告"
        return f"补丁生成失败（生成环节降级）：{_clip_event_text(detail)}"
    if report is not None:
        unit = (report.unit_tests or "").strip()
        details = (report.details or "").strip()
        if "SAST" in unit or "Bandit" in unit:
            return f"静态扫描拦截：{_clip_event_text(details or unit)}"
        if "工作区准备失败" in unit:
            return f"工作区准备失败：{_clip_event_text(details or unit)}"
        return f"沙箱测试未通过：{_clip_event_text(unit or details or '无详情')}"
    return "失败原因未知（沙箱报告缺失）"


@workflow.defn
class AIOpsFixWorkflow:
    """监控 → 分析 → 修复 → 测试 → 审批 → 延迟发布 全链路工作流。"""

    def __init__(self) -> None:
        self.stage: str = "DETECTED"
        self._alert: Alert | None = None
        self._deadline: dict | None = None
        self._needs_second: bool = False
        self._test_report: TestReport | None = None
        self._patch_id: str | None = None
        # 保留补丁对象本身：审批发生在工作流结束前，此时 result 尚不可读，
        # 控制台只能通过 status 查询拿到 diff（评估报告 B4：审批点缺判断依据）。
        self._patch: Patch | None = None
        self._root_cause: RootCause | None = None
        self._approval: str | None = None
        self._second_approval: str | None = None
        self._deploy_command: str | None = None
        self._queued_patches: list[tuple[str, Alert]] = []
        self._gate_events: list[str] = []
        self._approvals: list[dict] = []
        self._policy: dict = {}

    # ------------------------------------------------------------------
    # 信号接口（设计方案 8.1）
    # ------------------------------------------------------------------

    @workflow.signal
    def submit_approval(self, decision: str) -> None:
        """运维一级审批：approve / reject。"""
        self._approval = decision

    @workflow.signal
    def submit_second_approval(self, decision: str) -> None:
        """受保护目录二级审批：approve / reject。"""
        self._second_approval = decision

    @workflow.signal
    def submit_deploy_command(self, command: str) -> None:
        """窗口内指令：deploy_now / cancel。"""
        self._deploy_command = command

    @workflow.signal
    def submit_patch(self, workflow_id: str, alert: Alert) -> None:
        """窗口内新补丁：仅 FIFO 入队——不重置倒计时、不合并、数量不设上限（锁定策略）。"""
        self._queued_patches.append((workflow_id, alert))
        self._gate_events.append(f"gate3:patch-queued:{alert.alert_id}")

    # ------------------------------------------------------------------
    # 查询接口（设计方案 8.2）
    # ------------------------------------------------------------------

    @workflow.query
    def status(self) -> dict:
        return {
            "stage": self.stage,
            "alert": dataclasses.asdict(self._alert) if self._alert else None,
            "deadline": self._deadline,
            "needs_second": self._needs_second,
            "patch_id": self._patch_id,
            # 审批发生在工作流结束前（result 尚不可读），故在此透出补丁与测试报告，
            # 供控制台在待审批时展示 diff / 测试结论（评估报告 B4）。
            "patch": dataclasses.asdict(self._patch) if self._patch else None,
            "test_report": dataclasses.asdict(self._test_report) if self._test_report else None,
            "root_cause": dataclasses.asdict(self._root_cause) if self._root_cause else None,
            "approval": self._approval,
            "second_approval": self._second_approval,
            "deploy_command": self._deploy_command,
            "queued_patches": [
                {"workflow_id": wid, "alert_id": alert.alert_id}
                for wid, alert in self._queued_patches
            ],
        }

    # ------------------------------------------------------------------
    # 主流程（设计方案第 3 节）
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, alert: Alert, policy: dict) -> dict:
        started = workflow.now()
        self._policy = policy
        self._alert = alert
        triage_cfg = policy["triage"]
        approval_cfg = policy["approval"]
        notify_cfg = policy["notify_window"]
        canary_cfg = policy["canary"]

        # ---------- TRIAGING：日志取证 + 聚类降噪 + LLM 根因 ----------
        self.stage = "TRIAGING"
        evidence = await self._call(activities.collect_evidence, alert)
        clustered = await self._call(activities.cluster_logs, evidence)
        root_cause: RootCause = await self._call(
            activities.analyze_root_cause, alert, clustered, evidence["trace_ids"]
        )
        self._root_cause = root_cause  # 供审批期展示（status 查询）

        # 闸门 1（策略闸门）：置信度不足，转人工，不进入自动修复
        threshold = triage_cfg["confidence_threshold"]
        if root_cause.confidence < threshold:
            self.stage = "ESCALATED"
            self._gate_events.append(
                f"gate1:blocked:confidence={root_cause.confidence:.2f}<{threshold}"
            )
            await self._call(
                activities.escalate_to_human, alert,
                f"闸门1：根因置信度 {root_cause.confidence:.2f} 低于阈值 {threshold}，转人工",
            )
            return self._final(alert, root_cause, None, None, started)

        # 闸门 1 通过：修复 → 验证 → 审批 → 发布（与需求流程共用的公共链路）
        return await self._fix_to_release(alert, root_cause, started)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _fix_to_release(self, alert: Alert, root_cause: RootCause, started) -> dict:
        """FIXING → TESTING → 闸门2（含二级）→ 闸门3（公告倒计时）→ CANARY → 发布。

        告警流程（TRIAGING 产出 root_cause）与需求流程（批准版分析构造 root_cause，
        见 AIOpsRequirementWorkflow）共用该段；策略配置自 ``self._policy`` 读取。
        """
        triage_cfg = self._policy["triage"]
        approval_cfg = self._policy["approval"]
        notify_cfg = self._policy["notify_window"]
        canary_cfg = self._policy["canary"]

        # ---------- FIXING / TESTING：补丁生成 + 沙箱验证（回炉限次） ----------
        references = await self._call(
            activities.retrieve_similar_fixes, alert, root_cause, timeout=_RAG_ACTIVITY_TIMEOUT
        )
        patch: Patch | None = None
        test_report: TestReport | None = None
        max_retries = triage_cfg["max_fix_retries"]
        failures: list[str] = []  # 每轮失败的确定性摘要（升级提示与事件流共用）
        for attempt in range(max_retries + 1):
            self.stage = "FIXING"
            patch = await self._call(
                activities.generate_patch,
                alert,
                root_cause,
                references,
                attempt,
                timeout=_FIX_ACTIVITY_TIMEOUT,
            )
            self._patch_id = patch.patch_id
            self._patch = patch
            self.stage = "TESTING"
            test_report = await self._call(
                activities.run_tests_in_sandbox, alert, patch, attempt
            )
            self._test_report = test_report
            if test_report.passed:
                self._gate_events.append(f"tests:passed:attempt={attempt}")
                break
            failures.append(_attempt_failure_summary(patch, test_report))
            self._gate_events.append(f"tests:failed:attempt={attempt}:{failures[-1]}")
        else:
            # 回炉重试耗尽，防 token/资源雪崩，转人工；
            # 升级原因附最后一轮具体失败摘要（避免笼统的「测试失败」掩盖生成环节故障）
            self.stage = "ESCALATED"
            await self._call(
                activities.escalate_to_human, alert,
                f"沙箱测试回炉重试 {max_retries + 1} 次仍失败，转人工；"
                f"最后一轮失败原因：{failures[-1]}",
            )
            return self._final(alert, root_cause, patch, None, started)

        # ---------- SHADOW（渐进信任档）：只建议不执行（P1-2） ----------
        # 读取用 .get 兜底：运行中旧流程的快照无此键时行为不变（replay 安全）。
        if self._policy.get("shadow_mode", False):
            self.stage = "SHADOWED"
            self._gate_events.append("shadow:suggestion-only")
            await self._call(
                activities.notify_shadow_suggestion, alert, root_cause, patch, test_report
            )
            return self._final(alert, root_cause, patch, None, started)

        mr = await self._call(activities.create_merge_request, patch, test_report)
        self._gate_events.append(f"mr:created:{mr['mr_id']}")

        # ---------- 闸门 2（人工闸门）：运维审批 ----------
        self.stage = "WAIT_APPROVAL"
        self._approval = None  # 清空历史信号，防幽灵审批
        self._second_approval = None
        needs_second = self._is_protected(patch.files, approval_cfg["second_approval_dirs"])
        self._needs_second = needs_second
        await self._call(
            activities.notify_approvers, alert, root_cause, patch, test_report, needs_second
        )

        approval_timeout = timedelta(seconds=approval_cfg["timeout_seconds"])
        self._deadline = {
            "kind": "approval",
            "at": (workflow.now() + approval_timeout).isoformat(),
        }
        # SDK 语义：条件满足时正常返回；超时抛 asyncio.TimeoutError（不返回 False）
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=approval_timeout
            )
        except asyncio.TimeoutError:
            self._approvals.append({"gate": "approval", "decision": "timeout-reject"})
            self._gate_events.append("gate2:timeout->reject")
            self.stage = "ESCALATED"
            await self._call(
                activities.escalate_to_human, alert,
                f"闸门2：{approval_cfg['timeout_seconds']}s 内无响应，自动驳回；"
                f"以 {approval_cfg['on_timeout_escalation']} 方式升级值班负责人",
            )
            return self._final(alert, root_cause, patch, None, started)

        self._approvals.append({"gate": "approval", "decision": self._approval})
        if self._approval != "approve":
            self._gate_events.append(f"gate2:{self._approval}")
            self.stage = "ESCALATED"
            await self._call(activities.escalate_to_human, alert, f"闸门2：运维{self._approval}")
            return self._final(alert, root_cause, patch, None, started)
        self._gate_events.append("gate2:approved")

        if needs_second:
            # 受保护目录：二级人工审批（SDK 语义：条件满足正常返回，超时抛 TimeoutError）
            self._deadline = {
                "kind": "second_approval",
                "at": (workflow.now() + approval_timeout).isoformat(),
            }
            try:
                await workflow.wait_condition(
                    lambda: self._second_approval is not None, timeout=approval_timeout
                )
            except asyncio.TimeoutError:
                self._approvals.append({"gate": "second_approval", "decision": "timeout-reject"})
                self._gate_events.append("gate2b:timeout->reject")
                self.stage = "ESCALATED"
                await self._call(
                    activities.escalate_to_human, alert,
                    "闸门2（二级）：受保护目录审批超时，自动驳回并升级",
                )
                return self._final(alert, root_cause, patch, None, started)
            self._approvals.append({"gate": "second_approval", "decision": self._second_approval})
            if self._second_approval != "approve":
                self._gate_events.append(f"gate2b:{self._second_approval}")
                self.stage = "ESCALATED"
                await self._call(
                    activities.escalate_to_human, alert, f"闸门2（二级）：二级审批{self._second_approval}"
                )
                return self._final(alert, root_cause, patch, None, started)
            self._gate_events.append("gate2b:approved:protected-dir")

        # ---------- 闸门 3（用户闸门）：公告 + 倒计时 ----------
        self.stage = "NOTIFYING"
        self._deploy_command = None
        announcement = await self._call(activities.notify_users, alert, patch)
        self._gate_events.append(f"gate3:announced:{announcement['version']}")

        countdown_seconds = notify_cfg["countdown_seconds"]
        deadline = workflow.now() + timedelta(seconds=countdown_seconds)
        self._deadline = {"kind": "countdown", "at": deadline.isoformat()}
        decision = "expiry"
        while True:
            remaining = (deadline - workflow.now()).total_seconds()
            if remaining <= 0:
                decision = "expiry"
                break
            try:
                await workflow.wait_condition(
                    lambda: self._deploy_command in ("deploy_now", "cancel"),
                    timeout=timedelta(seconds=remaining),
                )
            except asyncio.TimeoutError:
                decision = "expiry"
                break
            command = self._deploy_command
            self._deploy_command = None
            self._gate_events.append(f"gate3:command:{command}")
            if command == "deploy_now":
                decision = "deploy_now"
                break
            if command == "cancel":
                decision = "cancel"
                break
            # 未知指令：忽略并继续等待剩余倒计时（不重置计时）

        if decision == "cancel":
            self.stage = "CANCELLED"
            self._gate_events.append("gate3:cancelled")
            await self._start_next_cycle()
            return self._final(alert, root_cause, patch, None, started)

        # ---------- CANARY → ROLLING_OUT：金丝雀与发布终态 ----------
        self.stage = "CANARY"
        self._deadline = None
        canary: CanaryResult = await self._call(
            activities.deploy_canary,
            alert,
            patch,
            canary_cfg["traffic_percent"],
            canary_cfg["observe_seconds"],
            timeout=_CANARY_ACTIVITY_TIMEOUT,
        )
        self.stage = "ROLLING_OUT"
        release: ReleaseResult = await self._call(
            activities.finalize_release,
            canary,
            canary_cfg["auto_rollback"],
            timeout=_CANARY_ACTIVITY_TIMEOUT,
        )

        if release.rolled_back:
            self.stage = "ESCALATED"
            self._gate_events.append("canary:degraded->rollback")
            await self._call(
                activities.escalate_to_human, alert, f"金丝雀劣化并自动回滚：{release.reason}"
            )
        else:
            self.stage = "DONE"
            self._gate_events.append("canary:healthy->full-rollout")

        await self._start_next_cycle()
        return self._final(alert, root_cause, patch, release, started)

    async def _call(self, activity_fn, *args, timeout: timedelta = _ACTIVITY_TIMEOUT):
        return await workflow.execute_activity(
            activity_fn,
            args=list(args),
            start_to_close_timeout=timeout,
            retry_policy=_RETRY_POLICY,
        )

    @staticmethod
    def _is_protected(files: list[str], patterns: list[str]) -> bool:
        """受保护目录匹配（auth/** 前缀语义）。纯计算、确定性。"""
        for path in files:
            for pattern in patterns:
                prefix = pattern[:-3] if pattern.endswith("/**") else pattern
                prefix = prefix.rstrip("/")
                if path == prefix or path.startswith(prefix + "/"):
                    return True
        return False

    async def _start_next_cycle(self) -> None:
        """队列保留：当前发布结束/取消后，队首补丁自动开启新周期（锁定策略）。"""
        if not self._queued_patches:
            return
        next_wf_id, next_alert = self._queued_patches.pop(0)
        await workflow.start_child_workflow(
            AIOpsFixWorkflow.run,
            args=[next_alert, self._policy],
            id=next_wf_id,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        self._gate_events.append(f"queue:next-cycle-started:{next_alert.alert_id}")

    def _final(
        self,
        alert: Alert,
        root_cause: RootCause | None,
        patch: Patch | None,
        release: ReleaseResult | None,
        started,
    ) -> dict:
        """生成审计记录（设计方案附录 B）。"""
        return {
            "workflow_id": workflow.info().workflow_id,
            "alert_id": alert.alert_id,
            "service": alert.service,
            "stage": self.stage,
            "root_cause": dataclasses.asdict(root_cause) if root_cause else None,
            "confidence": root_cause.confidence if root_cause else None,
            "patch_id": patch.patch_id if patch else None,
            "model_version": patch.model_version if patch else None,
            "patch": dataclasses.asdict(patch) if patch else None,
            "test_report": dataclasses.asdict(self._test_report) if self._test_report else None,
            "approvals": self._approvals,
            "gate_events": self._gate_events,
            "deploy_result": dataclasses.asdict(release) if release else None,
            "duration_seconds": (workflow.now() - started).total_seconds(),
            "queued_patches": [queued_alert.alert_id for _, queued_alert in self._queued_patches],
        }


def build_requirement_context(task: RequirementTask) -> tuple[Alert, RootCause]:
    """把获批需求任务确定性构造为告警/根因上下文（纯函数，供工作流与单测使用）。

    - alert：severity=requirement，description 携带需求原文与批准版方案
      （修复 Agent 据此走需求实现 prompt 分支生成补丁）；
    - root_cause：error_type=REQUIREMENT，confidence=1.0 表示「方向已经管理员批准」，
      不经闸门 1 的置信度拦截；suspect_files 来自分析结果。
    """
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- （未列出）"
    alert = Alert(
        alert_id=f"req-{task.entry_id}",
        service=task.service,
        severity="requirement",
        description=(
            f"[需求实现] {task.title}\n"
            f"需求原文：{task.requirement}\n"
            f"批准版实现方案（v{task.version}）：\n{task.plan}\n"
            f"验收要点：\n{acceptance}"
        ),
    )
    root_cause = RootCause(
        error_type="REQUIREMENT",
        suspect_files=list(task.suspect_files),
        confidence=1.0,
        summary=f"需求「{task.title}」实现方案（批准版 v{task.version}）：{task.plan}",
    )
    return alert, root_cause


@workflow.defn
class AIOpsRequirementWorkflow(AIOpsFixWorkflow):
    """需求驱动修复工作流：已批准的需求分析 → 修复 → 验证 → 审批 → 发布。

    与 AIOpsFixWorkflow 的差异：
    - 跳过 TRIAGING（日志取证 / 根因分析）——需求场景无故障日志，方向已由管理员批准；
    - alert / root_cause 由 RequirementTask 确定性构造（见 build_requirement_context），
      不经闸门 1（confidence=1.0 语义为「人工已确认」）；
    - FIXING 之后与告警流程完全一致（复用 ``_fix_to_release``）：沙箱验证、闸门 2
      （受保护目录二级审批）、闸门 3（公告倒计时）、金丝雀发布与自动回滚全部保留；
    - signal / query 与父类同名（status / submit_approval / submit_deploy_command 等），
      控制台既有写操作与审批中心对两类流程无需区分。

    启动幂等键：``aiops-req-{analysis_id}-v{version}``（BFF 侧构造）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._task: RequirementTask | None = None

    @workflow.run
    async def run(self, task: RequirementTask, policy: dict) -> dict:
        started = workflow.now()
        self._policy = policy
        self._task = task
        alert, root_cause = build_requirement_context(task)
        self._alert = alert
        self._root_cause = root_cause
        self._gate_events.append(
            f"requirement:approved:{task.analysis_id}:v{task.version}:by={task.approved_by}"
        )
        return await self._fix_to_release(alert, root_cause, started)

    def _final(
        self,
        alert: Alert,
        root_cause: RootCause,
        patch: Patch | None,
        release: ReleaseResult | None,
        started,
    ) -> dict:
        """审计记录：父类字段 + 需求溯源（分析会话 / 条目 / 批准版本与操作者）。"""
        result = super()._final(alert, root_cause, patch, release, started)
        task = self._task
        if task is not None:
            result["requirement"] = {
                "analysis_id": task.analysis_id,
                "app_id": task.app_id,
                "entry_id": task.entry_id,
                "title": task.title,
                "version": task.version,
                "approved_by": task.approved_by,
            }
        return result
