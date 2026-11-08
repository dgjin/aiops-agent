"""审批证据绑定与职责分离校验（纯函数；BFF 与 Temporal 工作流共用）。

定位：把「审批的是哪一次变更」变成可校验的数据，而不是只看流程 ID 与阶段。
一次审批必须同时绑定：

    case_id / patch_id / diff_hash / test_report_hash / policy_sha256 / run_id

- ``diff_hash`` / ``test_report_hash``：补丁与测试证据的 SHA-256（带领域前缀，
  避免不同语义的相同文本互相碰撞）；
- ``policy_sha256``：工作流启动时冻结的策略快照哈希（运行期改 YAML 不影响在途流程）；
- ``run_id``：Temporal 运行实例 ID（同一 workflow ID 重跑不会复用）；
- ``case_id``：业务案件 ID（告警原始 ID / 需求分析 ID），重试不改变。

审批信封（``approval-evidence-v1``）在绑定之外携带
``command_id/gate/decision/actor/channel/comment/submitted_at``，由 BFF 在通过
身份认证后构造；工作流收到信号后二次校验，防止直接向 Temporal 发信号绕过。

本模块零 IO、无时钟依赖：``new_command_id`` / ``utc_now_iso`` 仅供 BFF 与 CLI
调用；工作流只使用确定性函数（时间一律取 ``workflow.now()``）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

CONTRACT = "approval-evidence-v1"
GATES = ("approval", "second_approval")
DECISIONS = ("approve", "reject")
CHANNELS = ("console", "im", "feishu-card", "cli", "api")
MAX_COMMENT = 2000

_BINDING_KEYS = (
    "contract",
    "case_id",
    "patch_id",
    "diff_hash",
    "test_report_hash",
    "policy_sha256",
    "run_id",
)
# 测试报告参与哈希的字段固定在模块内：BFF 从 status dict 取、工作流从 dataclass 取，
# 两侧必须得到完全相同的规范化文本（新增字段需要显式加入并视为契约版本变更）。
_REPORT_FIELDS = (
    "patch_id",
    "passed",
    "unit_tests",
    "regression_tests",
    "sast",
    "details",
    "verification_mode",
)


def canonical_json(value: Any) -> str:
    """稳定 JSON（键排序 + 紧凑分隔 + 保留中文）；跨进程/跨语言复核同一算法。"""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _sha256(domain: str, text: str) -> str:
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + text.encode("utf-8")).hexdigest()


def diff_hash(diff: str | None) -> str:
    """补丁文本哈希（领域前缀 ``aiops-diff-v1``）。"""
    return _sha256("aiops-diff-v1", str(diff or ""))


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def test_report_hash(report: Any) -> str:
    """测试报告哈希（只取固定字段；None 与空报告可区分）。"""
    if report is None:
        payload: dict[str, Any] = {"present": False}
    else:
        payload = {"present": True}
        for name in _REPORT_FIELDS:
            value = _field(report, name)
            if isinstance(value, bool) or value is None:
                payload[name] = value
            else:
                payload[name] = str(value)
    return _sha256("aiops-test-report-v1", canonical_json(payload))


def build_binding(
    *,
    case_id: str,
    patch: Any,
    test_report: Any,
    policy_sha256: str,
    run_id: str,
) -> dict:
    """构造证据绑定（缺失字段保留空串，由 :func:`binding_problem` 统一判定）。"""
    return {
        "contract": CONTRACT,
        "case_id": str(case_id or ""),
        "patch_id": str(_field(patch, "patch_id", "") or ""),
        "diff_hash": diff_hash(str(_field(patch, "diff", "") or "")),
        "test_report_hash": test_report_hash(test_report),
        "policy_sha256": str(policy_sha256 or ""),
        "run_id": str(run_id or ""),
    }


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())


def binding_problem(binding: Any) -> str | None:
    """证据绑定是否可用于审批；返回中文原因或 None。"""
    if not isinstance(binding, dict):
        return "审批证据绑定缺失"
    if binding.get("contract") != CONTRACT:
        return "审批证据契约版本不匹配"
    for key, label in (
        ("case_id", "案件 ID"),
        ("patch_id", "补丁 ID"),
        ("diff_hash", "diff 哈希"),
        ("test_report_hash", "测试报告哈希"),
        ("policy_sha256", "策略哈希"),
        ("run_id", "run ID"),
    ):
        if not str(binding.get(key) or "").strip():
            return f"{label}缺失"
    for key, label in (
        ("diff_hash", "diff 哈希"),
        ("test_report_hash", "测试报告哈希"),
        ("policy_sha256", "策略哈希"),
    ):
        if not _is_hex64(str(binding.get(key) or "")):
            return f"{label}格式非法"
    return None


def normalize_binding(binding: Any) -> dict | None:
    """校验并裁剪为固定字段；不可用返回 None（调用方 fail-closed）。"""
    if binding_problem(binding) is not None:
        return None
    return {
        key: (CONTRACT if key == "contract" else str(binding.get(key) or ""))
        for key in _BINDING_KEYS
    }


def contract_of(status: Any) -> str:
    """流程 status/result 暴露的审批契约版本（旧流程为空串）。"""
    return str(_field(status, "approval_contract", "") or "")


def binding_from_status(status: Any) -> dict | None:
    """从运行中 status 派生可信绑定（仅 v2 流程；BFF 不信任客户端提交的哈希）。"""
    if contract_of(status) != CONTRACT:
        return None
    return normalize_binding(_field(status, "evidence_binding"))


def build_envelope(
    *,
    binding: dict,
    gate: str,
    decision: str,
    actor: str,
    channel: str,
    comment: str,
    command_id: str,
    submitted_at: str,
) -> dict:
    """构造审批信封（BFF/CLI 在认证后调用；工作流内不得生成命令 ID）。"""
    normalized = normalize_binding(binding) or {}
    return {
        "contract": CONTRACT,
        "command_id": str(command_id or ""),
        "gate": str(gate or ""),
        "decision": str(decision or ""),
        "actor": str(actor or ""),
        "channel": str(channel or ""),
        "comment": str(comment or ""),
        "submitted_at": str(submitted_at or ""),
        **{
            key: normalized.get(key, "")
            for key in (
                "case_id",
                "patch_id",
                "diff_hash",
                "test_report_hash",
                "policy_sha256",
                "run_id",
            )
        },
    }


def decision_problem(decision: str, comment: str) -> str | None:
    """决策与意见的基础校验（驳回意见必填；批准意见可选）。"""
    if decision not in DECISIONS:
        return "审批决策非法（需 approve / reject）"
    if decision == "reject" and not str(comment or "").strip():
        return "驳回必须填写审批意见"
    if len(str(comment or "")) > MAX_COMMENT:
        return f"审批意见过长（最多 {MAX_COMMENT} 字符）"
    return None


def _iso_problem(value: str) -> bool:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return False


def envelope_problem(envelope: Any) -> tuple[str, str] | None:
    """信封结构校验：返回 (错误码, 中文原因) 或 None。"""
    if not isinstance(envelope, dict):
        return (
            "missing-envelope",
            "审批信号缺少证据信封（新流程不接受旧字符串协议，请升级 BFF/控制台后重试）",
        )
    if envelope.get("contract") != CONTRACT:
        return ("contract-mismatch", "审批证据契约版本不匹配")
    command_id = str(envelope.get("command_id") or "").strip()
    if not command_id or len(command_id) > 128:
        return ("invalid-command-id", "审批命令 ID 缺失或格式非法")
    if envelope.get("gate") not in GATES:
        return ("invalid-gate", "审批闸门非法")
    if envelope.get("decision") not in DECISIONS:
        return ("invalid-decision", "审批决策非法（需 approve / reject）")
    if not str(envelope.get("actor") or "").strip():
        return ("missing-actor", "审批人身份缺失")
    if len(str(envelope.get("actor") or "")) > 200:
        return ("invalid-actor", "审批人身份过长")
    if envelope.get("channel") not in CHANNELS:
        return ("invalid-channel", "审批通道非法")
    if len(str(envelope.get("comment") or "")) > MAX_COMMENT:
        return ("comment-too-long", f"审批意见过长（最多 {MAX_COMMENT} 字符）")
    submitted_at = str(envelope.get("submitted_at") or "")
    if not submitted_at or _iso_problem(submitted_at):
        return ("invalid-submitted-at", "审批提交时间缺失或不是合法 ISO 时间")
    for key, label in (
        ("case_id", "案件 ID"),
        ("patch_id", "补丁 ID"),
        ("run_id", "run ID"),
        ("diff_hash", "diff 哈希"),
        ("test_report_hash", "测试报告哈希"),
        ("policy_sha256", "策略哈希"),
    ):
        if not str(envelope.get(key) or "").strip():
            return ("missing-evidence", f"审批信封缺少{label}")
    return None


def validate_envelope(
    envelope: Any,
    *,
    gate: str,
    binding: Any,
    requested_by: str = "",
    first_approval: Any = None,
    needs_second: bool = False,
    already_decided: bool = False,
    second_decided: bool = False,
) -> tuple[str, str] | None:
    """工作流侧二次校验：结构 → 闸门前提 → 证据绑定 → 职责分离。

    返回 ``(错误码, 中文原因)``；None 表示合法。任何非 None 结果都不得推进流程。
    """
    problem = envelope_problem(envelope)
    if problem is not None:
        return problem
    if envelope.get("gate") != gate:
        return (
            "wrong-gate",
            f"审批信号闸门不匹配（信号 {gate}，信封 {envelope.get('gate')}）",
        )
    binding_error = binding_problem(binding)
    if binding_error is not None:
        return ("invalid-binding", f"流程审批证据不可用：{binding_error}")
    if already_decided:
        return ("already-decided", "该闸门已作出决策，不能重复提交")
    if gate == "second_approval":
        if not needs_second:
            return ("second-not-required", "该流程不涉及受保护目录，无需二级审批")
        if second_decided:
            return ("already-decided", "二级审批已提交，不能重复提交")
        if not isinstance(first_approval, dict) or first_approval.get("decision") != "approve":
            return ("first-not-approved", "一级审批尚未批准，不能进行二级审批")

    for key, label, code in (
        ("run_id", "run ID", "run-mismatch"),
        ("case_id", "案件 ID", "case-mismatch"),
        ("patch_id", "补丁 ID", "patch-mismatch"),
        ("diff_hash", "diff 哈希", "diff-mismatch"),
        ("test_report_hash", "测试报告哈希", "test-mismatch"),
        ("policy_sha256", "策略哈希", "policy-mismatch"),
    ):
        if str(envelope.get(key) or "") != str(binding.get(key) or ""):
            return (
                code,
                f"{label}与当前流程不一致（可能基于过期页面或旧补丁），请刷新后重新审批",
            )

    actor = str(envelope.get("actor") or "")
    if requested_by and actor == str(requested_by):
        return ("self-approval", "职责分离：流程发起人不得审批自己的变更")
    if gate == "second_approval" and isinstance(first_approval, dict):
        first_actor = str(first_approval.get("actor") or "")
        if first_actor and first_actor == actor:
            return ("same-approver", "职责分离：一级与二级审批人不得相同")
    return None


def approval_record(envelope: dict, *, received_at: str) -> dict:
    """工作流内的审批记录（谁 / 何时 / 哪个通道 / 基于哪份证据 / 什么意见）。"""
    return {
        "gate": envelope["gate"],
        "decision": envelope["decision"],
        "actor": envelope["actor"],
        "channel": envelope["channel"],
        "comment": envelope["comment"],
        "command_id": envelope["command_id"],
        "submitted_at": envelope["submitted_at"],
        "received_at": str(received_at or ""),
        **{
            key: envelope.get(key, "")
            for key in (
                "contract",
                "case_id",
                "patch_id",
                "diff_hash",
                "test_report_hash",
                "policy_sha256",
                "run_id",
            )
        },
    }


def envelope_digest(envelope: dict) -> str:
    """同一 command_id 的内容指纹（检测重复投递是否被篡改）。"""
    return _sha256("aiops-approval-envelope-v1", canonical_json(envelope))


def expected_evidence_problem(
    binding: dict,
    expected_diff_hash: str = "",
    expected_run_id: str = "",
    expected_case_id: str = "",
) -> str | None:
    """BFF 侧过期页面保护：客户端回传的哈希必须与当前绑定一致。"""
    error = binding_problem(binding)
    if error is not None:
        return f"流程审批证据不可用：{error}"
    if expected_case_id and expected_case_id != str(binding.get("case_id") or ""):
        return "审批对象已变化（案件 ID 与当前流程不一致），请刷新审批页面后重新审批"
    if expected_diff_hash and expected_diff_hash != str(binding.get("diff_hash") or ""):
        return "审批依据已变化（diff 哈希与当前流程不一致），请刷新审批页面后重新审批"
    if expected_run_id and expected_run_id != str(binding.get("run_id") or ""):
        return "流程 run 已变化（可能已重启），请刷新审批页面后重新审批"
    return None


def new_command_id() -> str:
    """生成审批命令 ID（仅 BFF/CLI；工作流内不得调用）。"""
    import uuid

    return f"cmd-{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    """当前 UTC 时间（仅 BFF/CLI；工作流内使用 ``workflow.now()``）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
