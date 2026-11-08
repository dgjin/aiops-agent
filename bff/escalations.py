"""转人工处置闭环（P1-3 / M3）：ESCALATED 流程 → 待办 → 指派 → 处置/重试 → 超时再升级。

背景（评估报告 P1-3）：升级人工后此前仅剩 KPI 计数——没有待办列表、责任人指派、
超时再升级等处置通道，转人工场景容易「烂尾」。M3 补齐重试跟踪与送达闭环：

双后端（AIOPS_MODE 决定，同 monitored_apps 模式）：
    - **demo**：``data/escalations.json``（fcntl 文件锁 + 临时文件 + 原子替换）；
    - **production**：MySQL ``escalations`` 表（每次查询即读库）。

状态机：
    open（新登记）→ assigned（已指派责任人）→ retrying（人工重试已启动）→ closed
    - assign 可反复改派（除 closed 外均可）；
    - close 必须提交结构化 ``resolution``（仅备注的弱关闭已停用）；
    - retry（路由层以新幂等键重启修复流程——需求来源重开需求流——后调 ``apply_retried``）
      进入 ``retrying``；后台跟踪新流程终态：DONE 才自动结构化关闭，
      ESCALATED/FAILED/CANCELLED/SHADOWED 重新打开并记录原因；
    - 写入带 ``version``（CAS）：客户端携带旧版本时返回冲突，避免并发覆盖。

超时再升级（以真实送达为准）：
    ``due_for_re_escalation`` 找出「open/assigned 且登记超过 SLA（默认 30 分钟，
    ``AIOPS_ESCALATION_SLA_MINUTES`` 可配）且未再升级、且不在通知退避窗口」的条目；
    ``sweep_due`` 经通知渠道投递：仅 ``delivery.mode == live`` 且无错误才置
    ``re_escalated``；``recorded``（仅留档）/ ``failed``（投递失败）按有界退避持续重试，
    保持 overdue，绝不显示为已通知。
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import gate_decision
from aiops_agent import mode

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（与 monitored_apps 同口径）
DATA_PATH = DATA_DIR / "escalations.json"

# retrying 属于未闭环（开放统计包含），closed 才是终态
STATUSES = ("open", "assigned", "retrying", "closed")
OPEN_STATUSES = ("open", "assigned", "retrying")
DEFAULT_SLA_MINUTES = 30

# 通知投递退避（秒）：第 1 次失败后 1 分钟，逐步放大到 1 小时封顶
NOTIFY_BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)

# 重试流程终态：只有 DONE 自动关闭，以下状态重新打开并记录原因
RETRY_REOPEN_OUTCOMES = ("ESCALATED", "FAILED", "CANCELLED", "SHADOWED")

# M1 转人工分类：观测异常/证据不足与业务闸门分开，避免污染模型低置信指标。
CATEGORY_LABELS = {
    "model": "模型低置信",
    "approval": "审批未通过",
    "sandbox": "测试未通过",
    "release": "发布劣化",
    "observability": "观测异常",
    "evidence": "证据不足",
    "requirement": "需求来源",
    "other": "其他",
}
CATEGORY_GROUPS = {
    "observability": "观测异常",
    "evidence": "证据不足",
}

# M2 结构化关闭记录：关闭原因 / 智能体判断评价 / 知识审核状态。
CLOSE_REASONS = {
    "RESOLVED": "已修复",
    "FALSE_POSITIVE": "误报",
    "OBSERVABILITY_FIXED": "观测已修复",
    "DUPLICATE": "重复告警",
    "NO_CODE_CHANGE": "无需改代码",
    "OTHER": "其他",
}
VERDICTS = {
    "CORRECT": "判断正确",
    "PARTLY_CORRECT": "部分正确",
    "INCORRECT": "判断错误",
    "UNKNOWN": "未评价",
}
KNOWLEDGE_STATUSES = ("", "DRAFT", "APPROVED", "REJECTED")
KNOWLEDGE_LABELS = {
    "": "未提交",
    "DRAFT": "待审核",
    "APPROVED": "已入库",
    "REJECTED": "已拒绝",
}


class EscalationStoreError(ValueError):
    """处置操作校验错误（由 BFF 统一转 400/404）。"""


class EscalationConflictError(EscalationStoreError):
    """记录版本冲突（并发写覆盖），路由统一转 409。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def sla_minutes() -> int:
    try:
        value = int(os.environ.get("AIOPS_ESCALATION_SLA_MINUTES", ""))
        return value if value > 0 else DEFAULT_SLA_MINUTES
    except ValueError:
        return DEFAULT_SLA_MINUTES


# ----------------------------------------------------------------------
# 自动升级原因：gate_events → 人类可读摘要
# ----------------------------------------------------------------------

def auto_reason_from_events(gate_events: list[str] | None) -> str:
    """从闸门事件序列推导升级原因摘要（取最后一条有意义的阻断/劣化事件）。"""
    evidence_labels = {
        "LOKI_UNAVAILABLE": "Loki 不可用",
        "NO_LOGS": "窗口内无服务日志",
        "STALE_LOGS": "日志已过期",
        "UNMAPPED_SERVICE": "服务标签无法映射",
        "INSUFFICIENT_SIGNAL": "日志无可分析信号",
    }
    for event in reversed([str(e) for e in gate_events or []]):
        if event.startswith("evidence:blocked:"):
            status = event.split(":", 2)[2]
            return f"证据预检：{evidence_labels.get(status, status)}（{status}）"
        if event.startswith("gate1:composite:blocked:"):
            code = event.split("gate1:composite:blocked:", 1)[1]
            return f"闸门1（组合判据）：{gate_decision.reason_label(code)}"
        if event.startswith("gate1:blocked"):
            detail = event.split(":", 2)[-1]
            return f"闸门1：根因置信度不足（{detail}）"
        if event.startswith("gate2b:timeout"):
            return "闸门2（二级）：受保护目录审批超时自动驳回"
        if event.startswith("gate2b:"):
            return f"闸门2（二级）：二级审批未通过（{event.split(':', 1)[1]}）"
        if event.startswith("gate2:timeout"):
            return "闸门2：审批超时自动驳回"
        if event.startswith("gate2:"):
            return f"闸门2：人工审批未通过（{event.split(':', 1)[1]}）"
        if event.startswith("tests:failed"):
            # 新格式 tests:failed:attempt=N:<具体失败摘要>（生成环节降级 / 静态扫描拦截 /
            # 工作区准备失败等）→ 升级原因直达真实原因；旧格式无第 4 段时回退概述
            base = "沙箱测试回炉重试耗尽（多轮补丁均未通过）"
            parts = event.split(":", 3)
            detail = parts[3].strip() if len(parts) > 3 else ""
            return f"{base}；最后一轮失败原因：{detail}" if detail else base
        if event.startswith("canary:degraded"):
            return "金丝雀劣化自动回滚"
    return "转人工（原因见流程详情与审计记录）"


def category_from_events(gate_events: list[str] | None) -> str:
    """由闸门事件推导分类（取最后一条有意义的阻断事件）。"""
    for event in reversed([str(e) for e in gate_events or []]):
        if event.startswith("evidence:blocked:"):
            status = event.split(":", 2)[2]
            return "evidence" if status == "INSUFFICIENT_SIGNAL" else "observability"
        if event.startswith("gate1:composite:blocked:"):
            return "model"
        if event.startswith("gate1:blocked"):
            return "model"
        if event.startswith("gate2"):
            return "approval"
        if event.startswith("tests:failed"):
            return "sandbox"
        if event.startswith("canary:degraded"):
            return "release"
    return ""


def category_from_reason(reason: str, requirement: bool = False) -> str:
    """历史待办无分类字段时，按升级原因确定性回填（只分类，不关闭）。"""
    if requirement:
        return "requirement"
    text = str(reason or "")
    if text.startswith("证据预检："):
        return "evidence" if "证据不足" in text or "INSUFFICIENT_SIGNAL" in text else "observability"
    if text.startswith("闸门1"):
        return "model"
    if text.startswith("闸门2"):
        return "approval"
    if text.startswith("沙箱") or "补丁生成失败" in text or "测试未通过" in text:
        return "sandbox"
    if text.startswith("金丝雀") or "回滚" in text:
        return "release"
    return "other" if text else ""


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(str(category or ""), "未分类")


# ----------------------------------------------------------------------
# 记录构造与状态迁移（纯函数，便于单测）
# ----------------------------------------------------------------------


def new_entry(flow: dict, now: datetime | None = None) -> dict:
    """由流程列表项构造待办条目（open 状态）。"""
    wf_id = str(flow.get("wf_id") or "")
    alert = flow.get("alert") or {}
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    reason = auto_reason_from_events(flow.get("gate_events"))
    wf_is_requirement = wf_id.startswith("aiops-req-")
    category = category_from_events(flow.get("gate_events")) or category_from_reason(
        reason, requirement=wf_is_requirement
    )
    return {
        "id": f"esc-{wf_id}",
        "wf_id": wf_id,
        # 原运行实例：重试时作为新流程的 parent_run_id（同一 wf_id 重跑不混淆）
        "run_id": str(flow.get("run_id") or ""),
        "alert_id": str(alert.get("alert_id") or ""),
        "service": str(alert.get("service") or ""),
        "auto_reason": reason,
        "category": category,
        "plan": "",
        "resolution": None,
        "alert": alert,
        "status": "open",
        "assignee": "",
        "note": "",
        "escalated_at": ts,
        "updated_at": ts,
        "closed_at": None,
        "re_escalated": False,
        # ---- M3：重试跟踪与通知送达（初始为空，重试/巡检时写入）----
        "retry_wf_id": "",
        "parent_run_id": "",
        "retry_started_at": None,
        "retry_outcome": "",
        "retry_outcome_at": None,
        "notify_mode": "",
        "notify_attempts": 0,
        "notify_next_retry_at": None,
        "notify_error": "",
        "notify_updated_at": None,
        # 记录版本：所有写操作按 version 做 CAS
        "version": 1,
        "history": [{"ts": ts, "action": "open", "actor": "system", "detail": "ESCALATED 自动登记"}],
    }


def entry_category(entry: dict) -> str:
    """待办分类：新记录读字段；历史记录按原因确定性回填（不写库、不关闭）。"""
    current = str(entry.get("category") or "")
    if current in CATEGORY_LABELS:
        return current
    wf_id = str(entry.get("wf_id") or "")
    alert_id = str(entry.get("alert_id") or "")
    requirement = wf_id.startswith("aiops-req-") or bool(alert_id.startswith("req-"))
    return category_from_reason(str(entry.get("auto_reason") or ""), requirement=requirement) or "other"


def _append_history(entry: dict, action: str, actor: str, detail: str = "") -> None:
    entry.setdefault("history", []).append(
        {"ts": _now(), "action": action, "actor": actor or "system", "detail": detail}
    )
    entry["updated_at"] = _now()


def apply_assign(entry: dict, assignee: str, actor: str) -> dict:
    clean = (assignee or "").strip()
    if not clean:
        raise EscalationStoreError("责任人不能为空")
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已处置关闭，不能改派")
    before = entry.get("assignee") or ""
    entry["assignee"] = clean
    entry["status"] = "assigned"
    _append_history(
        entry, "assign", actor, f"指派给 {clean}" + (f"（由 {before} 改派）" if before else "")
    )
    return entry


def build_resolution(payload: dict, actor: str, now: datetime | None = None) -> dict:
    """校验并构造结构化关闭记录（M3 起为唯一关闭入口）。

    仅备注的弱关闭已停用（旧客户端会收到明确迁移错误），按关闭原因动态校验：
        - ``RESOLVED``：实际根因、实际处置、验证结果三项必填；
        - 其他原因：备注 / 实际处置 / 实际根因至少一项说明；
        - 建议入知识库（``knowledge_candidate``）时必须确认已检查敏感信息。
    """
    raw = payload or {}
    reason = str(raw.get("close_reason") or "OTHER").strip().upper()
    if reason not in CLOSE_REASONS:
        raise EscalationStoreError(f"关闭原因非法：{reason}")
    verdict = str(raw.get("verdict") or "UNKNOWN").strip().upper()
    if verdict not in VERDICTS:
        raise EscalationStoreError(f"智能体判断评价非法：{verdict}")
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    resolution = {
        "close_reason": reason,
        "actual_root_cause": str(raw.get("actual_root_cause") or "").strip(),
        "actual_action": str(raw.get("actual_action") or "").strip(),
        "verification": str(raw.get("verification") or "").strip(),
        "verdict": verdict,
        "knowledge_candidate": bool(raw.get("knowledge_candidate")),
        "sensitive_confirmed": bool(raw.get("sensitive_confirmed")),
        "knowledge_status": "",
        "knowledge_reviewed_by": "",
        "knowledge_reviewed_at": "",
        "knowledge_reject_reason": "",
        "note": str(raw.get("note") or "").strip(),
        "closed_by": actor,
        "closed_at": stamp,
    }
    if reason == "RESOLVED":
        required = (
            ("actual_root_cause", "实际根因"),
            ("actual_action", "实际处置"),
            ("verification", "验证结果"),
        )
        missing = [label for key, label in required if not resolution[key]]
        if missing:
            raise EscalationStoreError("关闭原因为「已修复」时必须填写：" + "、".join(missing))
    elif not (resolution["note"] or resolution["actual_action"] or resolution["actual_root_cause"]):
        raise EscalationStoreError("请填写处置说明（备注或实际处置）")
    if resolution["knowledge_candidate"] and not resolution["sensitive_confirmed"]:
        raise EscalationStoreError("建议入知识库前必须确认已检查敏感信息")
    if resolution["knowledge_candidate"]:
        resolution["knowledge_status"] = "DRAFT"
    return resolution


def apply_close(
    entry: dict, actor: str, note: str = "", resolution: dict | None = None
) -> dict:
    """关闭待办；M3 起必须提供结构化 ``resolution``。

    “仅备注”弱关闭不再兼容：历史已关闭记录保持原样（不批量改写），
    但任何新关闭都必须经过 ``build_resolution`` 的完整校验。
    """
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已关闭，无需重复处置")
    if resolution is None:
        raise EscalationStoreError(
            "关闭必须提交结构化处置记录（关闭原因 / 实际根因 / 实际处置 / 验证结果）；"
            "仅备注关闭已停用，请升级客户端"
        )
    entry["status"] = "closed"
    entry["closed_at"] = _now()
    entry["resolution"] = resolution
    entry["note"] = str(resolution.get("note") or (note or "")).strip()
    label = CLOSE_REASONS.get(str(resolution.get("close_reason") or ""), "已关闭")
    detail = label + (f"：{entry['note']}" if entry["note"] else "")
    _append_history(entry, "close", actor, detail)
    return entry


def apply_plan(entry: dict, plan: str, actor: str) -> dict:
    """设置/清除未关闭待办的处理计划（M2；允许保持 open，不要求清零）。"""
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已关闭，不能修改处置计划")
    clean = (plan or "").strip()
    entry["plan"] = clean
    _append_history(entry, "plan", actor, f"处置计划：{clean}" if clean else "清空处置计划")
    return entry


def knowledge_status(entry: dict) -> str:
    resolution = entry.get("resolution") or {}
    if not isinstance(resolution, dict):
        return ""
    return str(resolution.get("knowledge_status") or "")


def apply_knowledge_review(
    entry: dict, decision: str, actor: str, reason: str = ""
) -> dict:
    """知识审核状态机（M2）：approve / reject / revoke；仅管理员可调用（路由层强制）。

    - 仅关闭且提交过候选（knowledge_candidate）的待办可审核；
    - 仅 ``RESOLVED`` 的记录可批准入知识库（未解决不得进索引）；
    - 撤销只允许针对已入库记录，撤销后状态为 REJECTED，可重新审核。
    """
    if entry.get("status") != "closed":
        raise EscalationStoreError("待办尚未关闭，无法审核知识候选")
    resolution = entry.get("resolution") or {}
    if not isinstance(resolution, dict) or not resolution.get("knowledge_candidate"):
        raise EscalationStoreError("该待办未提交知识库候选，无法审核")
    action = str(decision or "").strip().lower()
    current = str(resolution.get("knowledge_status") or "")
    note = str(reason or "").strip()
    if action == "approve":
        if current == "APPROVED":
            raise EscalationStoreError("该记录已入库，无需重复批准")
        if str(resolution.get("close_reason") or "") != "RESOLVED":
            raise EscalationStoreError("仅「已修复（RESOLVED）」的关闭记录可进入知识库")
        resolution.update(
            knowledge_status="APPROVED",
            knowledge_reviewed_by=actor,
            knowledge_reviewed_at=_now(),
            knowledge_reject_reason="",
        )
        detail = "审核通过，已加入知识库候选（待重建索引生效）"
    elif action == "reject":
        resolution.update(
            knowledge_status="REJECTED",
            knowledge_reviewed_by=actor,
            knowledge_reviewed_at=_now(),
            knowledge_reject_reason=note,
        )
        detail = "审核拒绝" + (f"：{note}" if note else "")
    elif action == "revoke":
        if current != "APPROVED":
            raise EscalationStoreError("仅已入库记录可撤销")
        resolution.update(
            knowledge_status="REJECTED",
            knowledge_reviewed_by=actor,
            knowledge_reviewed_at=_now(),
            knowledge_reject_reason=note or "已撤销",
        )
        detail = "撤销知识入库" + (f"：{note}" if note else "")
    else:
        raise EscalationStoreError(f"审核动作非法：{decision}")
    entry["resolution"] = resolution
    _append_history(entry, f"knowledge:{action}", actor, detail)
    return entry


def apply_retried(
    entry: dict, new_wf_id: str, actor: str, now: datetime | None = None
) -> dict:
    """记录「重试修复」处置：原待办进入 ``retrying``，等新流程终态后再决定关闭/重开。

    不再“启动即关闭”：新流程可能再次升级/失败，提前关闭会让原待办漏跟。
    保留 ``retry_wf_id`` 与 ``parent_run_id``，形成完整父子链。
    """
    if entry.get("status") == "closed":
        raise EscalationStoreError("该待办已关闭，无需重试")
    if entry.get("status") == "retrying":
        raise EscalationStoreError("该待办已在重试跟踪中，请等待新流程终态或手动关闭")
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    entry["status"] = "retrying"
    entry["retry_wf_id"] = str(new_wf_id or "")
    entry["parent_run_id"] = str(entry.get("run_id") or entry.get("parent_run_id") or "")
    entry["retry_started_at"] = stamp
    entry["retry_outcome"] = ""
    entry["retry_outcome_at"] = None
    entry["note"] = f"已启动重试修复（新流程 {new_wf_id}），等待新流程终态"
    _append_history(
        entry,
        "retry",
        actor,
        f"重启修复流程 {new_wf_id}（进入重试跟踪，DONE 后自动关闭）",
    )
    return entry


def build_auto_resolution(
    entry: dict,
    detail: dict | None,
    actor: str,
    now: datetime | None = None,
) -> dict:
    """重试流程 DONE 时的结构化自动关闭记录（根因/处置尽量取新流程结果）。"""
    result = (detail or {}).get("result") or {}
    root = result.get("root_cause") or {}
    patch = result.get("patch") or {}
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    retry_wf_id = str(entry.get("retry_wf_id") or "")
    root_summary = str(root.get("summary") or "").strip()
    action = f"重试流程 {retry_wf_id} 执行完成"
    if str(patch.get("description") or "").strip():
        action += f"：{str(patch['description']).strip()}"
    return {
        "close_reason": "RESOLVED",
        "actual_root_cause": root_summary or "重试流程自动修复成功（根因以新流程结果为准）",
        "actual_action": action,
        "verification": f"新流程 {retry_wf_id} 终态 DONE",
        "verdict": "UNKNOWN",
        "knowledge_candidate": False,
        "sensitive_confirmed": False,
        "knowledge_status": "",
        "knowledge_reviewed_by": "",
        "knowledge_reviewed_at": "",
        "knowledge_reject_reason": "",
        "note": f"重试流程 {retry_wf_id} 成功，系统自动关闭",
        "closed_by": actor,
        "closed_at": stamp,
        "source": "retry-tracking",
    }


def apply_retry_outcome(
    entry: dict,
    outcome: str,
    actor: str,
    detail: dict | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> dict:
    """按重试流程终态推进原待办：DONE → 自动关闭；失败类 → 重新打开。

    返回原 entry；未到终态（RUNNING / 空）时不做任何修改。
    """
    if entry.get("status") != "retrying":
        return entry
    normalized = str(outcome or "").strip().upper()
    if normalized in ("", "RUNNING"):
        return entry
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    entry["retry_outcome"] = normalized
    entry["retry_outcome_at"] = stamp
    if normalized == "DONE":
        resolution = build_auto_resolution(entry, detail, actor, now)
        entry["status"] = "closed"
        entry["closed_at"] = stamp
        entry["resolution"] = resolution
        entry["note"] = resolution["note"]
        _append_history(
            entry,
            "retry:done",
            actor,
            f"重试流程 {entry.get('retry_wf_id')} 成功，已自动结构化关闭",
        )
        return entry
    if normalized in RETRY_REOPEN_OUTCOMES:
        entry["status"] = "assigned" if entry.get("assignee") else "open"
        entry["re_escalated"] = False
        entry["notify_mode"] = ""
        entry["notify_attempts"] = 0
        entry["notify_next_retry_at"] = None
        entry["notify_error"] = ""
        entry["notify_updated_at"] = None
        entry["note"] = (
            f"重试流程 {entry.get('retry_wf_id')} 以 {normalized} 结束，已重新打开"
            + (f"：{reason}" if reason else "")
        )
        _append_history(
            entry,
            "retry:reopen",
            actor,
            f"重试流程终态 {normalized}，重新打开" + (f"：{reason}" if reason else ""),
        )
        return entry
    # 非预期终态：保持 retrying，等下一轮跟踪（不伪造成功/失败）
    return entry


def retry_outcome_from_detail(detail: dict | None) -> tuple[str, str]:
    """从 ``gw.flow_detail`` 结果推导重试终态与原待办处置原因。

    返回 ``(outcome, reason)``；RUNNING 表示继续跟踪。
    """
    data = detail or {}
    exec_status = str(data.get("exec_status") or "").upper()
    result = data.get("result") or {}
    stage = str(result.get("stage") or "").upper()
    if exec_status == "RUNNING":
        return "RUNNING", ""
    if stage == "DONE":
        return "DONE", "重试流程已完成发布"
    if stage in RETRY_REOPEN_OUTCOMES:
        return stage, auto_reason_from_events(result.get("gate_events"))
    if exec_status in ("FAILED", "TIMED_OUT", "TERMINATED", "CONTINUED_AS_NEW"):
        return "FAILED", f"重试流程执行状态 {exec_status}"
    if exec_status in ("CANCELED", "CANCELLED"):
        return "CANCELLED", "重试流程被取消"
    if stage == "FAILED":
        return "FAILED", auto_reason_from_events(result.get("gate_events"))
    errors = [str(item) for item in data.get("errors") or [] if str(item).strip()]
    if errors:
        return "FAILED", f"重试流程结果不可读：{'；'.join(errors)}"
    # COMPLETED 但读不到 stage：按失败处理（不冒充成功）
    return "FAILED", "重试流程终态无法确认 DONE（缺少 stage）"


# ----------------------------------------------------------------------
# 再升级通知结果（真实送达语义 + 有界退避）
# ----------------------------------------------------------------------


def notify_backoff_seconds(attempts: int) -> int:
    """第 ``attempts`` 次失败后的等待秒数（1 分钟起，1 小时封顶）。"""
    index = max(0, min(int(attempts or 1) - 1, len(NOTIFY_BACKOFF_SECONDS) - 1))
    return NOTIFY_BACKOFF_SECONDS[index]


def apply_notify_result(
    entry: dict, delivery: dict, minutes: int, now: datetime | None = None
) -> dict:
    """记录一次再升级投递结果：live 才算已通知，其余按退避保持 overdue。"""
    stamp_dt = now or datetime.now(timezone.utc)
    if stamp_dt.tzinfo is None:
        stamp_dt = stamp_dt.replace(tzinfo=timezone.utc)
    stamp = stamp_dt.isoformat(timespec="seconds")
    mode = str((delivery or {}).get("mode") or "failed")
    error = str((delivery or {}).get("error") or "")
    attempts = int(entry.get("notify_attempts") or 0) + 1
    entry["notify_mode"] = mode
    entry["notify_attempts"] = attempts
    entry["notify_updated_at"] = stamp
    if mode == "live" and not error:
        entry["notify_error"] = ""
        entry["notify_next_retry_at"] = None
        entry["re_escalated"] = True
        _append_history(
            entry,
            "re-escalate",
            "system",
            f"处置超时（>{minutes} 分钟），再升级已送达（第 {attempts} 次投递）",
        )
        return entry
    entry["re_escalated"] = False
    entry["notify_error"] = error or str((delivery or {}).get("path") or "")
    backoff = notify_backoff_seconds(attempts)
    entry["notify_next_retry_at"] = (
        stamp_dt + timedelta(seconds=backoff)
    ).isoformat(timespec="seconds")
    label = "仅留档（未配置通知通道）" if mode == "recorded" else "投递失败"
    _append_history(
        entry,
        "re-escalate:deferred",
        "system",
        f"处置超时（>{minutes} 分钟），再升级{label}：{entry['notify_error'] or '原因未记录'}；"
        f"{backoff} 秒后重试（第 {attempts} 次）",
    )
    return entry


def apply_re_escalate(entry: dict, actor: str, detail: str = "") -> dict:
    entry["re_escalated"] = True
    _append_history(entry, "re-escalate", actor or "system", detail or "处置超时，再升级一次")
    return entry


def list_retrying() -> list[dict]:
    """待跟踪的重试条目（后台巡检用）。"""
    return [e for e in _read_raw() if e.get("status") == "retrying" and e.get("retry_wf_id")]



# ----------------------------------------------------------------------
# DB 后端（production）
# ----------------------------------------------------------------------


def _row_to_entry(row) -> dict:
    return {
        "id": row["id"],
        "wf_id": row["wf_id"],
        "run_id": row["run_id"] if "run_id" in row else "",
        "alert_id": row["alert_id"],
        "service": row["service"],
        "auto_reason": row["auto_reason"],
        "category": row["category"] if "category" in row else "",
        "plan": row["plan"] if "plan" in row else "",
        "resolution": row["resolution"] if "resolution" in row else None,
        "alert": row["alert"],
        "status": row["status"],
        "assignee": row["assignee"],
        "note": row["note"],
        "escalated_at": db_layer.to_iso(row["escalated_at"]) or "",
        "updated_at": db_layer.to_iso(row["updated_at"]) or "",
        "closed_at": db_layer.to_iso(row["closed_at"]),
        "re_escalated": bool(row["re_escalated"]),
        "history": row["history"] or [],
        "retry_wf_id": row["retry_wf_id"] if "retry_wf_id" in row else "",
        "parent_run_id": row["parent_run_id"] if "parent_run_id" in row else "",
        "retry_started_at": db_layer.to_iso(row["retry_started_at"])
        if "retry_started_at" in row
        else None,
        "retry_outcome": row["retry_outcome"] if "retry_outcome" in row else "",
        "retry_outcome_at": db_layer.to_iso(row["retry_outcome_at"])
        if "retry_outcome_at" in row
        else None,
        "notify_mode": row["notify_mode"] if "notify_mode" in row else "",
        "notify_attempts": int(row["notify_attempts"] or 0) if "notify_attempts" in row else 0,
        "notify_next_retry_at": db_layer.to_iso(row["notify_next_retry_at"])
        if "notify_next_retry_at" in row
        else None,
        "notify_error": row["notify_error"] if "notify_error" in row else "",
        "notify_updated_at": db_layer.to_iso(row["notify_updated_at"])
        if "notify_updated_at" in row
        else None,
        "version": int(row["version"] or 1) if "version" in row else 1,
    }


def _entry_values(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "wf_id": entry["wf_id"],
        "run_id": entry.get("run_id") or "",
        "alert_id": entry.get("alert_id") or "",
        "service": entry.get("service") or "",
        "auto_reason": entry.get("auto_reason") or "",
        "category": entry.get("category") or "",
        "plan": entry.get("plan") or "",
        "resolution": entry.get("resolution"),
        "alert": entry.get("alert"),
        "status": entry.get("status") or "open",
        "assignee": entry.get("assignee") or "",
        "note": entry.get("note") or "",
        "escalated_at": db_layer.to_dt(entry.get("escalated_at")) or db_layer.now_dt(),
        "updated_at": db_layer.to_dt(entry.get("updated_at")) or db_layer.now_dt(),
        "closed_at": db_layer.to_dt(entry.get("closed_at")),
        "re_escalated": bool(entry.get("re_escalated")),
        "history": entry.get("history") or [],
        "retry_wf_id": entry.get("retry_wf_id") or "",
        "parent_run_id": entry.get("parent_run_id") or "",
        "retry_started_at": db_layer.to_dt(entry.get("retry_started_at")),
        "retry_outcome": entry.get("retry_outcome") or "",
        "retry_outcome_at": db_layer.to_dt(entry.get("retry_outcome_at")),
        "notify_mode": entry.get("notify_mode") or "",
        "notify_attempts": int(entry.get("notify_attempts") or 0),
        "notify_next_retry_at": db_layer.to_dt(entry.get("notify_next_retry_at")),
        "notify_error": entry.get("notify_error") or "",
        "notify_updated_at": db_layer.to_dt(entry.get("notify_updated_at")),
        "version": int(entry.get("version") or 1),
    }


def _read_db() -> list[dict]:
    engine = db_layer.get_engine()
    with engine.begin() as conn:
        return [
            _row_to_entry(row)
            for row in conn.execute(
                select(db_layer.escalations).order_by(db_layer.escalations.c.escalated_at)
            ).mappings()
        ]


def _upsert_db(entry: dict, expected_version: int | None = None) -> None:
    """单条 upsert；更新时按 ``version`` 做 CAS（rowcount=0 → 冲突）。

    ``expected_version`` 缺省取 entry 当前版本：即使旧客户端不传版本，
    也不会用陈旧快照覆盖并发更新。
    """
    engine = db_layer.get_engine()
    values = _entry_values(entry)
    expected = int(
        expected_version if expected_version is not None else entry.get("version") or 1
    )
    table = db_layer.escalations
    with engine.begin() as conn:
        exists = conn.execute(select(table.c.id).where(table.c.id == entry["id"])).scalar()
        if exists is None:
            values["version"] = expected
            conn.execute(table.insert().values(**values))
            entry["version"] = expected
            return
        result = conn.execute(
            table.update()
            .where(table.c.id == entry["id"])
            .where(table.c.version == expected)
            .values(**{**values, "version": expected + 1})
        )
        if result.rowcount != 1:
            raise EscalationConflictError(
                f"待办 {entry['id']} 已被其他操作更新（期望版本 {expected}），请刷新后重试"
            )
        entry["version"] = expected + 1


# ----------------------------------------------------------------------
# 读取 / 写入入口（按模式分发）
# ----------------------------------------------------------------------


def _read_raw() -> list[dict]:
    if mode.is_production():
        return _read_db()
    return _read_demo()


def _read_demo() -> list[dict]:
    """读取 demo JSON（不在锁内调用；原子替换保证不会读到半文件）。"""
    if not DATA_PATH.is_file():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []  # 文件损坏：返回空，不覆盖现场（下次写入前可由人工排查）
    return data if isinstance(data, list) else []


@contextmanager
def _file_lock():
    """demo 写锁（sidecar lock 文件；跨进程串行化读-改-写）。"""
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_PATH.with_name(DATA_PATH.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_raw(entries: list[dict]) -> None:
    if mode.is_production():
        # 生产：单条 upsert（同步方法只写单条，见 persist_entry）
        return
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def persist_entry(entry: dict, expected_version: int | None = None) -> None:
    """写入单条（demo：文件锁 + 全量原子替换；production：CAS 单条 upsert）。

    ``expected_version`` 为客户端读取时的 ``version``；不传则用 entry 当前版本。
    版本不一致抛 ``EscalationConflictError``，由路由转 409——不得用旧快照覆盖新状态。
    """
    if mode.is_production():
        _upsert_db(entry, expected_version)
        return
    expected = int(
        expected_version if expected_version is not None else entry.get("version") or 1
    )
    with _file_lock():
        entries = _read_demo()
        index = next(
            (i for i, item in enumerate(entries) if item.get("id") == entry["id"]), None
        )
        if index is None:
            entry["version"] = expected
            entries.append(entry)
        else:
            current = int(entries[index].get("version") or 1)
            if current != expected:
                raise EscalationConflictError(
                    f"待办 {entry['id']} 已被其他操作更新（当前版本 {current}，"
                    f"期望 {expected}），请刷新后重试"
                )
            entry["version"] = expected + 1
            entries[index] = entry
        _write_raw(entries)


def list_all(status: str | None = None) -> list[dict]:
    """全部待办（可按状态过滤）；每次读取即取最新——热生效。"""
    entries = _read_raw()
    if status and status != "all":
        entries = [e for e in entries if e.get("status") == status]
    entries.sort(key=lambda e: e.get("escalated_at") or "", reverse=True)
    return entries


def get(esc_id: str) -> dict | None:
    return next((e for e in _read_raw() if e.get("id") == esc_id), None)


def stats() -> dict:
    entries = _read_raw()
    categories: dict[str, int] = {}
    for entry in entries:
        key = entry_category(entry)
        categories[key] = categories.get(key, 0) + 1
    knowledge = {"draft": 0, "approved": 0, "rejected": 0}
    for entry in entries:
        status = knowledge_status(entry)
        if status == "DRAFT":
            knowledge["draft"] += 1
        elif status == "APPROVED":
            knowledge["approved"] += 1
        elif status == "REJECTED":
            knowledge["rejected"] += 1
    return {
        "total": len(entries),
        "open": sum(1 for e in entries if e.get("status") == "open"),
        "assigned": sum(1 for e in entries if e.get("status") == "assigned"),
        "retrying": sum(1 for e in entries if e.get("status") == "retrying"),
        "closed": sum(1 for e in entries if e.get("status") == "closed"),
        "re_escalated": sum(1 for e in entries if e.get("re_escalated")),
        # 通知未送达（recorded/failed 且未完成 live 升级）：值班可见的缺口
        "notify_pending": sum(
            1
            for e in entries
            if e.get("status") in ("open", "assigned")
            and not e.get("re_escalated")
            and e.get("notify_mode") in ("recorded", "failed")
        ),
        "categories": categories,
        "knowledge": knowledge,
        "sla_minutes": sla_minutes(),
    }


def backfill_categories(dry_run: bool = True) -> dict:
    """历史待办批量分类（M1）：只写 category，不改变状态、不关闭任何待办。

    默认 dry-run 返回将变更清单；``dry_run=False`` 才落库。历史 41 条待办必须在
    日志恢复后由值班逐条重试/关闭，本函数只解决「无法筛选」的问题。
    """
    changed: list[dict] = []
    for entry in _read_raw():
        current = str(entry.get("category") or "")
        target = entry_category(entry)
        if current == target:
            continue
        changed.append(
            {
                "id": entry.get("id"),
                "wf_id": entry.get("wf_id"),
                "service": entry.get("service"),
                "from": current,
                "to": target,
                "label": category_label(target),
            }
        )
        if not dry_run:
            entry["category"] = target
            persist_entry(entry)
    return {"dry_run": dry_run, "changed": len(changed), "items": changed}


def sync_from_flows(flows: list[dict]) -> int:
    """把流程列表中的 ESCALATED 流程幂等登记为待办；返回本次新增数。

    规则：
    - wf_id 已登记（不论状态）→ 跳过——已关闭的待办不因流程仍在列表中而重开；
    - wf_id 是某个未关闭待办的 ``retry_wf_id`` → 跳过：重试流程的终态由
      ``apply_retry_outcome`` 回写到原待办，不能另开一条重复待办。
    """
    entries = _read_raw()
    existing = {e.get("wf_id") for e in entries}
    # 重试链拥有权：原待办未关闭时，新流程不再单独登记（避免重复待办）
    tracked = {
        str(e.get("retry_wf_id") or "")
        for e in entries
        if str(e.get("retry_wf_id") or "") and e.get("status") != "closed"
    }
    added = 0
    for flow in flows:
        if flow.get("stage") != "ESCALATED":
            continue
        wf_id = str(flow.get("wf_id") or "")
        if not wf_id or wf_id in existing or wf_id in tracked:
            continue
        persist_entry(new_entry(flow))
        existing.add(wf_id)
        added += 1
    return added


# ----------------------------------------------------------------------
# 超时再升级（SLA 巡检）
# ----------------------------------------------------------------------


def due_for_re_escalation(
    now: datetime | None = None, sla: int | None = None
) -> list[dict]:
    """超过 SLA 未处置、未再升级且不在通知退避窗口的待办（open/assigned）。

    - ``retrying`` 暂停 SLA（新流程运行中，不再重复升级原待办）；
    - ``recorded/failed`` 保持 overdue，但需等 ``notify_next_retry_at`` 到达后再投递。
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    minutes = sla if sla is not None else sla_minutes()
    threshold = current - timedelta(minutes=minutes)
    due: list[dict] = []
    for entry in _read_raw():
        if entry.get("status") not in ("open", "assigned") or entry.get("re_escalated"):
            continue
        next_retry = _parse_ts(entry.get("notify_next_retry_at"))
        if next_retry and next_retry > current:
            continue
        escalated_at = _parse_ts(entry.get("escalated_at"))
        if escalated_at and escalated_at <= threshold:
            due.append(entry)
    return due


def _send_re_escalation(entry: dict, minutes: int, attempt: int = 1) -> dict:
    """经通知渠道再升级一次（失败降级不抛错；留痕 data/notify/escalation-re-*.json）。"""
    from aiops_agent import notify
    from aiops_agent.models import Alert

    alert = Alert(
        alert_id=entry.get("alert_id") or entry.get("id") or "unknown",
        service=entry.get("service") or "unknown",
        severity="critical",
    )
    reason = (
        f"转人工待办处置超时：已超过 {minutes} 分钟未关闭（{entry.get('auto_reason') or '原因见详情'}）"
    )
    sender = notify.NotificationSender()
    card = notify.render_escalation_card(alert, reason, provider=sender.provider)
    return sender.send(card, msg_id=f"escalation-re-{entry['id']}-{attempt}")


def sweep_due(now: datetime | None = None) -> list[dict]:
    """SLA 巡检：对到期未处置的待办投递再升级，返回本轮处理过的条目。

    - ``live`` 且无错误：置 ``re_escalated=True``，不再重复通知；
    - ``recorded/failed``：记录投递方式与失败原因，按有界退避重试，保持 overdue；
    - 并发修改（版本冲突）跳过本轮，不覆盖人工刚写入的新状态。
    """
    minutes = sla_minutes()
    processed: list[dict] = []
    for entry in due_for_re_escalation(now, minutes):
        attempt = int(entry.get("notify_attempts") or 0) + 1
        delivery = _send_re_escalation(entry, minutes, attempt)
        updated = apply_notify_result(entry, delivery, minutes, now)
        try:
            persist_entry(updated)
        except EscalationConflictError:
            continue
        processed.append(updated)
    return processed
