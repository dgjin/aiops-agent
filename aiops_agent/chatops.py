"""ChatOps 命令引擎（P2-3「操作入口前移」）：IM 群内查询 / 审批指令的纯解析与渲染层。

定位：把「看待办、查状态、批准/驳回」从控制台前移到飞书/钉钉工作群——值班人
无需打开控制台即可处置待审批流程。本模块**零副作用**（无 IO、不依赖 BFF/网络），
供 ``bff/routes/chatops.py`` 装配与单测复用；信号发送 / 操作审计 / kill switch
校验等实际执行由路由层负责。

命令集（中英别名，发送「帮助」查看用法）：
    帮助                              命令一览
    待办                              列出待审批流程
    状态 <流程ID前缀>                 单流程状态（阶段/补丁/审批/截止）
    批准 / 驳回 <流程ID前缀>          一级审批决策
    二级批准 / 二级驳回 <流程ID前缀>  二级审批决策（受保护目录）

权限模型：``ROLE_ORDER`` 与控制台一致（viewer < operator < admin）；
查询类命令要求 viewer，审批类要求 operator。BFF 侧通过 AIOPS_IM_USERS 把发送者
映射到角色，未映射 = viewer（最小权限，仅可查询）。

入站消息规范化（:func:`normalize_inbound`）兼容三种载荷：
    - 通用网关：``{"sender": "alice", "text": "帮助"}``
    - 飞书事件 v2：``event.sender.sender_id.open_id`` + ``message.content``（JSON 字符串）
    - 钉钉机器人回调：``senderStaffId`` / ``senderNick`` + ``text.content``
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# 控制台角色等级（与 bff/auth.py ROLE_ORDER 对齐；test_chatops 有防漂移断言）
ROLE_ORDER: dict[str, int] = {"viewer": 1, "operator": 2, "admin": 3}

# 别名（小写）→ (规范命令, 是否必须携带参数)
_VERBS: dict[str, tuple[str, bool]] = {
    "帮助": ("help", False),
    "help": ("help", False),
    "待办": ("pending", False),
    "pending": ("pending", False),
    "状态": ("status", True),
    "status": ("status", True),
    "批准": ("approve", True),
    "同意": ("approve", True),
    "approve": ("approve", True),
    "驳回": ("reject", True),
    "拒绝": ("reject", True),
    "reject": ("reject", True),
    "二级批准": ("second_approve", True),
    "second-approve": ("second_approve", True),
    "二级驳回": ("second_reject", True),
    "second-reject": ("second_reject", True),
}

# 规范命令 → 所需最低角色（查询 viewer；审批 operator）
ROLE_BY_VERB: dict[str, str] = {
    "help": "viewer",
    "pending": "viewer",
    "status": "viewer",
    "approve": "operator",
    "reject": "operator",
    "second_approve": "operator",
    "second_reject": "operator",
}

# 规范命令 → 用法文本（缺参提示用）
USAGE: dict[str, str] = {
    "status": "状态 <流程ID前缀>",
    "approve": "批准 <流程ID前缀>",
    "reject": "驳回 <流程ID前缀>",
    "second_approve": "二级批准 <流程ID前缀>",
    "second_reject": "二级驳回 <流程ID前缀>",
}

# 决策命令 → (闸门, 决策)（与 notify.parse_callback 的回调契约同构）
_SIGNALS: dict[str, tuple[str, str]] = {
    "approve": ("approval", "approve"),
    "reject": ("approval", "reject"),
    "second_approve": ("second_approval", "approve"),
    "second_reject": ("second_approval", "reject"),
}

# 决策命令 → 成功回复动作词
DECISION_ACTIONS: dict[str, str] = {
    "approve": "一级审批已批准",
    "reject": "一级审批已驳回",
    "second_approve": "二级审批已批准",
    "second_reject": "二级审批已驳回",
}

# 流程阶段中文（与 workflows.py 的 stage 取值对齐；未知值回落原文）
_STAGE_LABELS: dict[str, str] = {
    "DETECTED": "已接警",
    "TRIAGING": "根因分析",
    "FIXING": "补丁生成",
    "TESTING": "沙箱验证",
    "SHADOWED": "影子模式（仅建议未执行）",
    "WAIT_APPROVAL": "等待人工审批",
    "NOTIFYING": "公告倒计时",
    "CANARY": "金丝雀发布",
    "DONE": "已完成（已发布）",
    "ESCALATED": "已转人工",
    "CANCELLED": "已取消",
    "FAILED": "硬失败（执行异常或超时）",
}

_EXEC_LABELS: dict[str, str] = {
    "RUNNING": "运行中",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "CANCELED": "已取消",
    "TERMINATED": "已终止",
    "TIMED_OUT": "已超时",
}

_DECISION_LABELS: dict[str, str] = {"approve": "已批准", "reject": "已驳回"}

_DEADLINE_LABELS: dict[str, str] = {
    "approval": "审批截止",
    "second_approval": "二级审批截止",
    "countdown": "发布倒计时截止",
}


@dataclass(frozen=True)
class Command:
    """解析后的命令：verb 为规范名，arg 保留原始大小写（wf_id 大小写敏感）。"""

    verb: str
    arg: str
    role: str


@dataclass(frozen=True)
class Inbound:
    """规范化后的入站消息。"""

    sender: str
    text: str


def parse_command(text: str) -> Command | None:
    """解析命令文本；无法识别返回 None（调用方提示「帮助」）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    parts = raw.split(maxsplit=1)
    head = parts[0].lower()
    if head in _VERBS:
        verb, _ = _VERBS[head]
        arg = parts[1].strip() if len(parts) > 1 else ""
        return Command(verb=verb, arg=arg, role=ROLE_BY_VERB[verb])
    # 无空格连写（如「批准abc-123」）：仅对有参命令做最长前缀匹配，
    # 避免「待办事项」被误解析为「待办」+ 垃圾参数
    for alias in sorted((a for a, (_, need) in _VERBS.items() if need), key=len, reverse=True):
        if lower.startswith(alias):
            verb, _ = _VERBS[alias]
            return Command(verb=verb, arg=raw[len(alias):].strip(), role=ROLE_BY_VERB[verb])
    return None


def signal_target(verb: str) -> tuple[str, str] | None:
    """决策命令 → (闸门, 决策)；非决策命令返回 None。"""
    return _SIGNALS.get(verb)


def usage(verb: str) -> str:
    """命令用法文本（缺参时提示）。"""
    return USAGE.get(verb, "")


def resolve_sender_role(sender: str, mapping: dict | None) -> str:
    """发送者 → 角色：命中映射取角色（非法值回落 viewer）；未映射 = viewer（仅可查询）。"""
    entry = (mapping or {}).get(sender)
    role = entry.get("role") if isinstance(entry, dict) else entry
    if isinstance(role, str) and role in ROLE_ORDER:
        return role
    return "viewer"


def has_permission(role: str, required: str) -> bool:
    """角色是否满足最低要求（未知角色不满足任何要求）。"""
    return ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(required, 99)


def render_help() -> str:
    """命令一览。"""
    return (
        "AIOps 助手 · 可用命令\n"
        "  待办                    待审批流程列表\n"
        "  状态 <ID前缀>           查看流程状态\n"
        "  批准 <ID前缀>           一级审批：批准\n"
        "  驳回 <ID前缀>           一级审批：驳回\n"
        "  二级批准 <ID前缀>       二级审批：批准（受保护目录）\n"
        "  二级驳回 <ID前缀>       二级审批：驳回\n"
        "  帮助                    本说明\n"
        "流程 ID 支持唯一前缀（如：批准 aiops-fix-order）"
    )


def render_unknown(text: str) -> str:
    """未识别命令的提示（截断回显，防超长文本回显爆炸）。"""
    return f"未识别命令「{text[:40]}」（发送「帮助」查看可用命令）"


def render_pending(items: list[dict]) -> str:
    """待审批列表（仅 RUNNING + WAIT_APPROVAL；空时给出明确占位）。"""
    pending = [
        item
        for item in items
        if item.get("exec_status") == "RUNNING" and item.get("stage") == "WAIT_APPROVAL"
    ]
    if not pending:
        return "当前没有待审批流程。"
    lines = [f"待审批流程（{len(pending)} 个）："]
    for index, item in enumerate(pending, start=1):
        alert = item.get("alert") or {}
        service = alert.get("service") or "未知服务"
        patch_id = item.get("patch_id")
        patch_desc = f"补丁 {patch_id}" if patch_id else "补丁生成中"
        lines.append(f"  {index}. {item.get('wf_id')}（{service}）· {patch_desc}{_pending_note(item)}")
    lines.append("发送「批准 <ID前缀>」或「驳回 <ID前缀>」处置。")
    return "\n".join(lines)


def _pending_note(item: dict) -> str:
    """待办备注：二级审批进度提示（受保护目录流程）。"""
    if item.get("needs_second"):
        if item.get("approval") == "approve" and not item.get("second_approval"):
            return "（一级已批准，待二级审批）"
        if not item.get("approval"):
            return "（受保护目录，需二级审批）"
    return ""


def render_status(item: dict) -> str:
    """单流程状态渲染（输入为 list_flows 的聚合条目结构）。"""
    stage = item.get("stage")
    exec_status = item.get("exec_status") or "未知"
    lines = [
        f"流程 {item.get('wf_id') or '未知'}",
        f"执行：{_EXEC_LABELS.get(exec_status, exec_status)}"
        f" · 阶段：{_STAGE_LABELS.get(stage, stage or '未知')}",
    ]
    alert = item.get("alert") or {}
    if alert.get("service"):
        lines.append(f"服务：{alert['service']}（告警 {alert.get('alert_id') or '未知'}）")
    root_cause = item.get("root_cause")
    if isinstance(root_cause, dict) and root_cause.get("summary"):
        confidence = root_cause.get("confidence")
        suffix = f"（置信度 {confidence:.2f}）" if isinstance(confidence, (int, float)) else ""
        lines.append(f"根因：{root_cause['summary']}{suffix}")
    patch_id = item.get("patch_id")
    if patch_id:
        lines.append(f"补丁：{patch_id}")
    if stage == "WAIT_APPROVAL":
        lines.extend(_approval_lines(item))
    deadline = item.get("deadline")
    if isinstance(deadline, dict) and deadline.get("at"):
        label = _DEADLINE_LABELS.get(str(deadline.get("kind") or ""), "截止")
        lines.append(f"{label}：{deadline['at']}")
    if item.get("start_time"):
        lines.append(f"开始：{item['start_time']}")
    return "\n".join(lines)


def _approval_lines(item: dict) -> list[str]:
    """待审批期的审批进度行。"""
    lines: list[str] = []
    approval = item.get("approval")
    if approval:
        lines.append(f"一级审批：{_DECISION_LABELS.get(approval, approval)}")
    else:
        lines.append("一级审批：待处理（发送「批准 <ID前缀>」或「驳回 <ID前缀>」）")
    if item.get("needs_second"):
        second = item.get("second_approval")
        if second:
            lines.append(f"二级审批：{_DECISION_LABELS.get(second, second)}")
        else:
            lines.append("二级审批：待处理（受保护目录，需「二级批准/二级驳回」）")
    return lines


def normalize_inbound(payload: dict) -> Inbound | None:
    """入站载荷 → Inbound；无法识别返回 None（路由据此 400）。"""
    if not isinstance(payload, dict):
        return None
    # 通用网关：{"sender": ..., "text": ...}（text 为 dict 时是钉钉文本结构，跳过）
    sender = payload.get("sender")
    text = payload.get("text")
    if isinstance(sender, str) and isinstance(text, str):
        result = _make(sender, text)
        if result is not None:
            return result
    # 飞书事件 v2（im.message.receive_v1）
    event = payload.get("event")
    if isinstance(event, dict):
        result = _make(_feishu_sender(event), _feishu_text(event))
        if result is not None:
            return result
    # 钉钉机器人回调（outgoing 群消息）
    dingtalk_sender = (
        payload.get("senderStaffId") or payload.get("senderId") or payload.get("senderNick")
    )
    text_field = payload.get("text")
    dingtalk_text = text_field.get("content") if isinstance(text_field, dict) else None
    return _make(dingtalk_sender, dingtalk_text)


def _make(sender, text) -> Inbound | None:  # noqa: ANN001 - 宽容接收任意 JSON 值
    """字段类型/空值清洗 + 去 @提及（@机器人 后跟命令的常见形态）。"""
    if not isinstance(sender, str) or not isinstance(text, str):
        return None
    sender, text = sender.strip(), _strip_mention(text.strip())
    if not sender or not text:
        return None
    return Inbound(sender=sender, text=text)


def _strip_mention(text: str) -> str:
    """去掉开头的 @提及 token（如「@_user_1 批准 abc」→「批准 abc」）。"""
    if text.startswith("@"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    return text


def _feishu_sender(event: dict) -> str | None:
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return None
    ids = sender.get("sender_id")
    if not isinstance(ids, dict):
        return None
    for key in ("open_id", "user_id", "union_id"):
        value = ids.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _feishu_text(event: dict) -> str | None:
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text if isinstance(text, str) else None
