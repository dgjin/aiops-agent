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
      仅生成建议卡片留痕后以 SHADOWED 终态结束（只建议不执行）；
    - 影子建议续接（resume_patch 快照键）：对影子档已生成补丁执行「续接」——
      首轮跳过补丁生成直入完整沙箱验证，失败回炉后回落到正常生成链路
      （BFF「影子建议续接」端点启动，见 bff/routes/requirements.py）。

IM 通知（接收/进入修复，patched 守护）：
    - 接收即推送处理流程说明（告警：日志取证→聚类→根因→闸门1；需求：已批准→直接修复）；
    - 进入修复流程前推送分析结论 + 修复链路与模式（影子档/全链路）；
    - 新增活动调用以 workflow.patched 标记守护，旧运行中流程的历史重放不受影响。

审批证据（approval-evidence-v1，patched 守护）：
    - 进入闸门 2 时把 case / patch / diff / 测试报告 / 策略快照 / run 绑定进
      ``status.evidence_binding``，审批必须基于该次变更而不是仅凭流程 ID；
    - 新流程只接受 BFF/CLI 构造的审批信封（actor / channel / comment / command_id），
      工作流收到信号后二次校验证据、闸门前提与职责分离，非法信号只登记拒绝原因、不推进；
    - 旧在途流程（无 patch 标记）继续接受字符串 decision，保证历史重放与滚动升级兼容。
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from . import activities
    from . import change_evidence
    from . import gate_decision
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
# 沙箱验证活动：Node 全量构建 + vitest 实测 24m42s（2424 用例），必须在工作区准备与
# 计划超时（Node 默认 2400s）之上；否则 Temporal 会在 60s 直接取消活动，测试永远跑不完。
# 固定常量（不在 workflow 模块读环境变量，避免 Temporal 沙箱的非确定性限制）。
_SANDBOX_ACTIVITY_TIMEOUT = timedelta(seconds=3000)
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


def _patch_from_resume(payload) -> Patch | None:
    """从续接快照恢复补丁（影子建议续接；字段防御性兜底，非法输入返回 None）。

    影子档下流程以 SHADOWED 终态结束（只建议不执行）。续接执行时，原补丁经策略
    快照 ``resume_patch`` 注入：首轮跳过补丁生成、直接进入完整沙箱验证；验证
    失败回炉后仍走正常生成链路。纯函数、只读快照（replay 安全）。
    """
    if not isinstance(payload, dict):
        return None
    patch_id = str(payload.get("patch_id") or "").strip()
    if not patch_id:
        return None
    raw_files = payload.get("files")
    files = [str(item) for item in raw_files] if isinstance(raw_files, (list, tuple)) else []
    return Patch(
        patch_id=patch_id,
        alert_id=str(payload.get("alert_id") or ""),
        files=files,
        diff=str(payload.get("diff") or ""),
        description=str(payload.get("description") or ""),
        risk=str(payload.get("risk") or ""),
        model_version=str(payload.get("model_version") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        degraded=bool(payload.get("degraded", False)),
        degrade_reason=str(payload.get("degrade_reason") or ""),
    )


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
        # 日志证据归档摘要（转人工核对）：TRIAGING 后即产出，供 status/result 展示
        self._evidence_summary: dict | None = None
        self._approval: str | None = None
        self._second_approval: str | None = None
        self._deploy_command: str | None = None
        self._queued_patches: list[tuple[str, Alert]] = []
        self._gate_events: list[str] = []
        self._approvals: list[dict] = []
        self._policy: dict = {}
        # ---------- 审批证据（approval-evidence-v1） ----------
        # ``_approval_contract`` 为空表示旧协议（字符串 decision）；进入闸门 2 时
        # 由 patched 标记决定是否升级为证据信封协议。旧在途流程保持字符串协议。
        self._approval_contract: str = ""
        self._evidence_binding: dict | None = None
        self._approval_error: dict | None = None
        self._approval_rejections: list[dict] = []
        self._command_ids: dict[str, str] = {}
        self._run_id: str = ""
        self._case_id: str = ""
        self._requested_by: str = ""
        self._parent_run_id: str = ""
        self._parent_wf_id: str = ""
        self._retry_wf_id: str = ""

    # ------------------------------------------------------------------
    # 信号接口（设计方案 8.1）
    # ------------------------------------------------------------------

    @workflow.signal
    def submit_approval(self, payload) -> None:
        """运维一级审批：approval-evidence-v1 信封；旧流程兼容字符串 decision。

        参数故意不加类型注解：Temporal SDK 对无注解参数使用 ``arg_types=[None]``
        解码，可同时接收字符串（旧协议）与 dict（证据信封），从而支持滚动升级。
        """
        self._handle_approval_signal("approval", payload)

    @workflow.signal
    def submit_second_approval(self, payload) -> None:
        """受保护目录二级审批：approval-evidence-v1 信封；旧流程兼容字符串 decision。"""
        self._handle_approval_signal("second_approval", payload)

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
    # 审批信号处理（approval-evidence-v1）
    # ------------------------------------------------------------------

    def _handle_approval_signal(self, gate: str, payload) -> None:
        """审批信号统一入口（确定性：不读时钟以外的外部状态、无 IO）。

        - 旧流程（``_approval_contract`` 为空）：保持字符串协议，历史重放不受影响；
        - 新流程：结构 → 闸门前提 → 证据绑定 → 职责分离逐项校验，非法信号只登记
          拒绝原因并继续等待，绝不推进流程（fail-closed）。

        command_id 幂等：相同 ID + 相同内容视为重复投递（忽略并留痕）；
        相同 ID + 不同内容视为篡改/冲突，直接拒绝。
        """
        if self._approval_contract != change_evidence.CONTRACT:
            if not isinstance(payload, str):
                self._record_approval_rejection(
                    gate,
                    "legacy-payload",
                    "旧流程仅接受字符串审批决策；请升级 BFF/控制台后重试",
                )
                return
            if gate == "approval":
                self._approval = payload
            else:
                self._second_approval = payload
            return

        # 回执关联：即使信封结构非法，只要携带 command_id 也写入拒收记录，
        # 使 BFF 能把「业务拒绝」准确回给发起命令的客户端。
        raw_command_id = ""
        if isinstance(payload, dict):
            raw_command_id = str(payload.get("command_id") or "")[:128]
        structure = change_evidence.envelope_problem(payload)
        if structure is not None:
            self._record_approval_rejection(
                gate, structure[0], structure[1], command_id=raw_command_id
            )
            return
        command_id = str(payload["command_id"])
        digest = change_evidence.envelope_digest(payload)
        previous = self._command_ids.get(command_id)
        if previous is not None:
            if previous == digest:
                # 重投（网络重试/双点）：幂等忽略，不产生第二条审批记录
                self._gate_events.append(f"approval:duplicate-command:{command_id}")
            else:
                self._record_approval_rejection(
                    gate,
                    "command-id-conflict",
                    f"审批命令 {command_id} 的内容不一致，已拒绝",
                    command_id=command_id,
                )
            return

        current_decision = self._approval if gate == "approval" else self._second_approval
        problem = change_evidence.validate_envelope(
            payload,
            gate=gate,
            binding=self._evidence_binding,
            requested_by=self._requested_by,
            first_approval=self._approved_first_record(),
            needs_second=self._needs_second,
            already_decided=current_decision is not None,
            second_decided=self._second_approval is not None,
        )
        if problem is not None:
            self._record_approval_rejection(
                gate, problem[0], problem[1], command_id=command_id
            )
            return

        self._command_ids[command_id] = digest
        self._approvals.append(
            change_evidence.approval_record(payload, received_at=workflow.now().isoformat())
        )
        if gate == "approval":
            self._approval = str(payload["decision"])
        else:
            self._second_approval = str(payload["decision"])
        self._approval_error = None
        self._gate_events.append(
            f"approval:received:{gate}:{payload['decision']}:by={payload['actor']}:"
            f"channel={payload['channel']}:command={command_id}"
        )

    def _approved_first_record(self) -> dict | None:
        """已生效的一级审批记录（二级审批职责分离依据）。"""
        for record in self._approvals:
            if record.get("gate") == "approval" and record.get("decision") == "approve":
                return record
        return None

    def _record_approval_rejection(
        self, gate: str, code: str, reason: str, *, command_id: str = ""
    ) -> None:
        """登记非法审批信号（可见、可审计、不推进流程）；列表上限防历史膨胀。"""
        entry = {
            "gate": gate,
            "code": code,
            "reason": reason,
            "command_id": str(command_id or ""),
            "at": workflow.now().isoformat(),
        }
        self._approval_error = entry
        self._approval_rejections.append(entry)
        if len(self._approval_rejections) > 20:
            del self._approval_rejections[0]
        self._gate_events.append(f"approval_rejected:{gate}:{code}")

    def _record_timeout(self, gate: str) -> None:
        """超时自动驳回：v2 记录同样携带完整证据绑定，保持审计同构。"""
        self._approvals.append(
            {
                "gate": gate,
                "decision": "timeout-reject",
                "actor": "system",
                "channel": "system",
                "comment": "审批超时，系统自动驳回",
                "command_id": "",
                "submitted_at": "",
                "received_at": workflow.now().isoformat(),
                **(self._evidence_binding or {}),
            }
        )

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
            "evidence": self._evidence_summary,
            "approval": self._approval,
            "second_approval": self._second_approval,
            "deploy_command": self._deploy_command,
            "queued_patches": [
                {"workflow_id": wid, "alert_id": alert.alert_id}
                for wid, alert in self._queued_patches
            ],
            # ---------- 审批证据与运行溯源（approval-evidence-v1） ----------
            # BFF 依据 approval_contract 判断能否构造信封（空串=旧流程，只发字符串）。
            "run_id": self._run_id,
            "case_id": self._case_id,
            "requested_by": self._requested_by,
            "parent_run_id": self._parent_run_id,
            "parent_wf_id": self._parent_wf_id,
            "retry_wf_id": self._retry_wf_id,
            "approval_contract": self._approval_contract,
            "evidence_binding": self._evidence_binding,
            "approvals": self._approvals,
            "gate_events": self._gate_events,
            "approval_error": self._approval_error,
            "approval_rejections": self._approval_rejections,
        }

    # ------------------------------------------------------------------
    # 主流程（设计方案第 3 节）
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, alert: Alert, policy: dict) -> dict:
        started = workflow.now()
        self._policy = policy
        self._alert = alert
        self._run_id = workflow.info().run_id
        self._case_id = alert.alert_id
        self._apply_evidence_context()
        triage_cfg = policy["triage"]
        approval_cfg = policy["approval"]
        notify_cfg = policy["notify_window"]
        canary_cfg = policy["canary"]

        # ---------- 接收通知：监控到告警/日志 → IM 推送处理流程说明（IM 交互补齐） ----------
        if workflow.patched("flow-intake-notify-g1"):
            await self._call(activities.notify_flow_intake, alert)

        # ---------- TRIAGING：日志取证 + 证据质量预检 + 聚类降噪 + LLM 根因 ----------
        self.stage = "TRIAGING"
        evidence_quality_gate = workflow.patched("evidence-quality-g1")
        if evidence_quality_gate:
            evidence = await self._call(
                activities.collect_evidence,
                alert,
                {
                    "lookback_minutes": triage_cfg.get("evidence_lookback_minutes", 10),
                    "stale_after_minutes": triage_cfg.get("evidence_stale_minutes", 15),
                    "min_signal_lines": triage_cfg.get("evidence_min_signal_lines", 1),
                },
            )
        else:
            # 在途旧流程：保持单参数调用，确保历史重放不产生额外命令。
            evidence = await self._call(activities.collect_evidence, alert)
        clustered = await self._call(activities.cluster_logs, evidence)
        # 证据归档（转人工核对）：必须在 LLM 之前落库——即使 triage 降级/超时转人工，
        # 原始日志也已可查；patched 门确保在途旧流程的历史重放不受影响。
        if workflow.patched("evidence-archive-g1"):
            self._evidence_summary = await self._call(
                activities.archive_evidence, evidence, clustered
            )
        if evidence_quality_gate:
            quality = evidence.get("quality") or {}
            status = str(quality.get("status") or "")
            blocked = status not in ("", "OK")
            if status == "INSUFFICIENT_SIGNAL" and not triage_cfg.get(
                "evidence_insufficient_escalate", True
            ):
                blocked = False
            if blocked:
                # 观测异常/证据不足独立分类：事件前缀不得伪装成 gate1，避免污染模型低置信指标。
                self.stage = "ESCALATED"
                self._gate_events.append(f"evidence:blocked:{status}")
                reason = quality.get("escalation_reason") or f"证据预检未通过（{status}），转人工"
                await self._call(activities.escalate_to_human, alert, reason)
                return self._final(alert, None, None, None, started)
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

        # 闸门 1（M3 组合判据，patched 守护）：
        # 置信度通过后，再校验证据/根因类型/嫌疑文件/模型路由（只收紧不放松）。
        # 非代码根因（基础设施/配置/证书/DNS 等）转 Runbook，不生成代码补丁。
        composite_cfg = triage_cfg.get("composite_gate") or {}
        if workflow.patched("composite-gate-v1") and composite_cfg.get("enabled", False):
            routing_cfg = triage_cfg.get("fix_routing") or {}
            scope = await self._call(
                activities.assess_fix_scope,
                alert,
                root_cause,
                {
                    "require_quality_bar": bool(routing_cfg.get("require_quality_bar", False)),
                    "quality_bar_path": str(routing_cfg.get("quality_bar_path", "")),
                    "prefer_qoder_for_multi_file": bool(
                        routing_cfg.get("prefer_qoder_for_multi_file", True)
                    ),
                },
            )
            quality = (evidence or {}).get("quality") or {}
            decision = gate_decision.decide(
                confidence=root_cause.confidence,
                threshold=threshold,
                error_type=root_cause.error_type,
                summary=root_cause.summary,
                suspect_files=root_cause.suspect_files,
                evidence_status=str(quality.get("status") or ""),
                files_found=scope.get("files_found") or [],
                files_missing=scope.get("files_missing") or [],
                scope_known=bool(scope.get("scope_known", False)),
                require_suspect_files=bool(composite_cfg.get("require_suspect_files", True)),
                routing=scope.get("routing"),
            )
            self._gate_events.extend(decision.events())
            if not decision.allowed:
                self.stage = "ESCALATED"
                detail = "；".join(
                    gate_decision.reason_label(reason["code"])
                    for reason in decision.reasons
                    if reason["severity"] == "block"
                )
                target = "Runbook" if decision.route == gate_decision.ROUTE_RUNBOOK else "人工"
                await self._call(
                    activities.escalate_to_human,
                    alert,
                    _clip_event_text(f"闸门1（组合判据）：{detail}，转{target}", 400),
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

        # ---------- 进入修复通知：分析结论 + 修复链路与模式（IM 交互补齐） ----------
        if workflow.patched("fix-start-notify-g1"):
            await self._call(
                activities.notify_fix_started,
                alert,
                root_cause,
                self._policy.get("shadow_mode", False),
            )

        # ---------- FIXING / TESTING：补丁生成 + 沙箱验证（回炉限次） ----------
        references = await self._call(
            activities.retrieve_similar_fixes, alert, root_cause, timeout=_RAG_ACTIVITY_TIMEOUT
        )
        patch: Patch | None = None
        test_report: TestReport | None = None
        max_retries = triage_cfg["max_fix_retries"]
        failures: list[str] = []  # 每轮失败的确定性摘要（升级提示与事件流共用）
        # 影子建议续接：快照携带 resume_patch 时首轮复用其补丁（跳过生成直入完整
        # 沙箱验证）；验证失败回炉后仍走正常生成链路。读取用 .get 兜底（replay 安全）。
        resume_patch = _patch_from_resume(self._policy.get("resume_patch"))
        for attempt in range(max_retries + 1):
            if attempt == 0 and resume_patch is not None:
                patch = resume_patch
                self._gate_events.append(f"resume:patch:{patch.patch_id}")
            else:
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
                activities.run_tests_in_sandbox,
                alert,
                patch,
                attempt,
                timeout=_SANDBOX_ACTIVITY_TIMEOUT,
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

        # ---------- 发布准入：静态验收不得单独支撑高风险发布（M4） ----------
        protected = self._is_protected(patch.files, approval_cfg["second_approval_dirs"])
        block_reason = ""
        if workflow.patched("sandbox-verification-gate-v1"):
            block_reason = self._static_verification_block(
                patch,
                test_report,
                protected,
                bool(self._policy.get("sandbox", {}).get("require_real_tests_for_high_risk", True)),
            )
        if block_reason:
            self.stage = "ESCALATED"
            self._gate_events.append("release:blocked:static-only:high-risk")
            await self._call(activities.escalate_to_human, alert, block_reason)
            return self._final(alert, root_cause, patch, None, started)

        mr = await self._call(activities.create_merge_request, patch, test_report)
        self._gate_events.append(f"mr:created:{mr['mr_id']}")

        # ---------- 闸门 2（人工闸门）：运维审批 ----------
        self.stage = "WAIT_APPROVAL"
        self._approval = None  # 清空历史信号，防幽灵审批
        self._second_approval = None
        needs_second = protected
        self._needs_second = needs_second
        # 审批证据 v1：新流程把本次变更绑定进 status，并要求审批携带信封；
        # 旧在途流程 patched=False（无 marker），继续字符串协议，历史重放无额外命令。
        approval_evidence = workflow.patched("approval-evidence-v1")
        if approval_evidence:
            self._approval_contract = change_evidence.CONTRACT
            self._evidence_binding = change_evidence.build_binding(
                case_id=self._case_id,
                patch=patch,
                test_report=test_report,
                policy_sha256=str(self._policy.get("policy_sha256") or ""),
                run_id=self._run_id,
            )
            binding_error = change_evidence.binding_problem(self._evidence_binding)
            if binding_error is not None:
                # 证据不完整不能进入审批（不伪造哈希）：fail-closed 转人工
                self.stage = "ESCALATED"
                self._gate_events.append("approval:blocked:evidence-unavailable")
                await self._call(
                    activities.escalate_to_human,
                    alert,
                    _clip_event_text(f"审批证据不可用：{binding_error}；已停止推进并转人工", 400),
                )
                return self._final(alert, root_cause, patch, None, started)
            self._gate_events.append(
                "approval:evidence-bound:"
                f"case={self._case_id}:patch={self._evidence_binding['patch_id']}:"
                f"diff={self._evidence_binding['diff_hash'][:12]}:"
                f"run={self._run_id}"
            )
            await self._call(
                activities.notify_approvers,
                alert,
                root_cause,
                patch,
                test_report,
                needs_second,
                self._evidence_binding,
            )
        else:
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
            if approval_evidence:
                self._record_timeout("approval")
            else:
                self._approvals.append({"gate": "approval", "decision": "timeout-reject"})
            self._gate_events.append("gate2:timeout->reject")
            self.stage = "ESCALATED"
            await self._call(
                activities.escalate_to_human, alert,
                f"闸门2：{approval_cfg['timeout_seconds']}s 内无响应，自动驳回；"
                f"以 {approval_cfg['on_timeout_escalation']} 方式升级值班负责人",
            )
            return self._final(alert, root_cause, patch, None, started)

        if not approval_evidence:
            # v2 的完整审批记录（谁/何时/哪个通道/基于哪份证据/意见）已由信号处理器写入
            self._approvals.append({"gate": "approval", "decision": self._approval})
        if self._approval != "approve":
            self._gate_events.append(f"gate2:{self._approval}")
            self.stage = "ESCALATED"
            await self._call(
                activities.escalate_to_human,
                alert,
                self._rejection_reason("闸门2：运维", "approval"),
            )
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
                if approval_evidence:
                    self._record_timeout("second_approval")
                else:
                    self._approvals.append(
                        {"gate": "second_approval", "decision": "timeout-reject"}
                    )
                self._gate_events.append("gate2b:timeout->reject")
                self.stage = "ESCALATED"
                await self._call(
                    activities.escalate_to_human, alert,
                    "闸门2（二级）：受保护目录审批超时，自动驳回并升级",
                )
                return self._final(alert, root_cause, patch, None, started)
            if not approval_evidence:
                self._approvals.append(
                    {"gate": "second_approval", "decision": self._second_approval}
                )
            if self._second_approval != "approve":
                self._gate_events.append(f"gate2b:{self._second_approval}")
                self.stage = "ESCALATED"
                await self._call(
                    activities.escalate_to_human,
                    alert,
                    self._rejection_reason("闸门2（二级）：二级审批", "second_approval"),
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
        release_safety = workflow.patched("release-safety-g1")
        options = {"release_safety": True} if release_safety else {}
        try:
            canary: CanaryResult = await self._call(
                activities.deploy_canary,
                alert,
                patch,
                canary_cfg["traffic_percent"],
                canary_cfg["observe_seconds"],
                timeout=_CANARY_ACTIVITY_TIMEOUT,
                **options,
            )
            self.stage = "ROLLING_OUT"
            release: ReleaseResult = await self._call(
                activities.finalize_release,
                canary,
                canary_cfg["auto_rollback"],
                timeout=_CANARY_ACTIVITY_TIMEOUT,
                **options,
            )
        except ActivityError:
            if not release_safety and not workflow.patched("release-safety-failure-g1"):
                raise
            self.stage = "ESCALATED"
            self._gate_events.append("release:safety-stop->manual")
            await self._call(activities.escalate_to_human, alert,
                             "发布安全拒绝/执行状态未知；保留目标占用，须核对发布日志及恢复材料，禁止自动重试")
            return self._final(alert, root_cause, patch, None, started)

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

    def _apply_evidence_context(self) -> None:
        """应用启动快照中的证据上下文（重试溯源；旧快照缺键时保持默认值）。

        只读工作流输入，不访问外部状态；因此重放结果确定。
        """
        context = self._policy.get("evidence_context")
        if not isinstance(context, dict):
            return
        self._case_id = str(context.get("case_id") or self._case_id)
        self._requested_by = str(context.get("requested_by") or self._requested_by)
        self._parent_run_id = str(context.get("parent_run_id") or "")
        self._parent_wf_id = str(context.get("parent_wf_id") or "")
        self._retry_wf_id = str(context.get("retry_wf_id") or "")

    def _rejection_reason(self, prefix: str, gate: str) -> str:
        """驳回升级文案：v2 附审批人与意见（审计可读）；旧流程保持原文案。"""
        decision = self._approval if gate == "approval" else self._second_approval
        base = f"{prefix}{decision}"
        if self._approval_contract != change_evidence.CONTRACT:
            return base
        record = next(
            (item for item in reversed(self._approvals) if item.get("gate") == gate), None
        )
        if not record:
            return base
        actor = str(record.get("actor") or "")
        comment = str(record.get("comment") or "").strip()
        detail = f"{actor}：{comment}" if comment else f"{actor}（未填写意见）"
        return _clip_event_text(f"{base}（{detail}）", 400)

    async def _call(self, activity_fn, *args, timeout: timedelta = _ACTIVITY_TIMEOUT,
                    release_safety: bool = False):
        options = {}
        if release_safety:
            options = {"heartbeat_timeout": timedelta(seconds=15),
                       "cancellation_type": workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED}
        return await workflow.execute_activity(
            activity_fn,
            args=list(args),
            start_to_close_timeout=timeout,
            retry_policy=_RETRY_POLICY,
            **options,
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

    @staticmethod
    def _static_verification_block(
        patch: Patch, report: TestReport | None, protected: bool, enabled: bool
    ) -> str:
        """静态验收（CONTRACT_TEST / STATIC_ONLY）不得单独支撑高风险发布（M4）。

        高风险 = 受保护目录，或补丁风险描述含「高 / high」。返回空串表示放行；
        非空为转人工原因。仅 UNIT_TEST（真实执行测试）可通过，历史空模式按静态处理。
        """
        if not enabled or report is None:
            return ""
        if (report.verification_mode or "") == "UNIT_TEST":
            return ""
        risk_text = str(patch.risk or "")
        high_risk = protected or "高" in risk_text or "high" in risk_text.lower()
        if not high_risk:
            return ""
        mode = report.verification_mode or "UNKNOWN"
        return (
            f"发布准入：本次验证为 {mode}（{report.unit_tests or '未执行测试'}），"
            f"不构成高风险变更的真实测试证据（补丁风险：{risk_text or '未标注'}，"
            f"受保护目录：{'是' if protected else '否'}）；已停止推进并转人工复核"
        )

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
            "evidence": self._evidence_summary,
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
            # ---------- 审批证据与运行溯源（approval-evidence-v1） ----------
            "run_id": self._run_id,
            "case_id": self._case_id,
            "requested_by": self._requested_by,
            "parent_run_id": self._parent_run_id,
            "parent_wf_id": self._parent_wf_id,
            "retry_wf_id": self._retry_wf_id,
            "approval_contract": self._approval_contract,
            "evidence_binding": self._evidence_binding,
            "approval_error": self._approval_error,
            "approval_rejections": self._approval_rejections,
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
        # 证据绑定：case 为需求分析会话（重试不变），发起人为批准需求的管理员；
        # 重试时启动快照可携带 evidence_context 覆盖发起人并关联父 run。
        self._run_id = workflow.info().run_id
        self._case_id = task.analysis_id
        self._requested_by = task.approved_by
        self._apply_evidence_context()
        alert, root_cause = build_requirement_context(task)
        self._alert = alert
        self._root_cause = root_cause
        self._gate_events.append(
            f"requirement:approved:{task.analysis_id}:v{task.version}:by={task.approved_by}"
        )
        # ---------- 接收通知：已批准需求任务 → IM 推送任务与流程说明（IM 交互补齐） ----------
        if workflow.patched("requirement-intake-notify-g1"):
            await self._call(activities.notify_requirement_intake, task)
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
