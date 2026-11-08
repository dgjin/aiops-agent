"""窗口指令信封（``workflow-command-v1``，纯函数；BFF 与工作流共用）。

审批之外的写命令（deploy-command / queue-patch）也需要在「signal 成功后进程崩溃、
结果回写失败」的重放窗口内保持幂等：BFF 为每次操作生成 ``command_id``，工作流按
ID 去重（相同内容忽略、不同内容拒绝），从而允许 BFF outbox 安全重投。

旧协议（deploy 字符串 / queue 两参数）继续可用：工作流按参数形态分流，历史
SignalReceived 事件重放不改变行为。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from .change_evidence import canonical_json

CONTRACT = "workflow-command-v1"
TYPES = ("deploy-command", "queue-patch")
DEPLOY_COMMANDS = ("deploy_now", "cancel")
CHANNELS = ("console", "im", "feishu-card", "cli", "api")


def new_command_id() -> str:
    """生成写命令 ID（仅 BFF/CLI；工作流内不得调用）。"""
    return f"cmd-{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_envelope(
    *,
    command_type: str,
    command_id: str,
    actor: str,
    channel: str,
    submitted_at: str,
    payload: dict,
) -> dict:
    """构造窗口指令信封（BFF 在认证与守卫通过后调用）。"""
    return {
        "contract": CONTRACT,
        "command_id": str(command_id or ""),
        "type": str(command_type or ""),
        "actor": str(actor or ""),
        "channel": str(channel or ""),
        "submitted_at": str(submitted_at or ""),
        "payload": dict(payload or {}),
    }


def deploy_payload(command: str) -> dict:
    return {"command": str(command or "")}


def queue_payload(*, new_wf_id: str, service: str, alert_id: str, description: str = "") -> dict:
    return {
        "new_wf_id": str(new_wf_id or ""),
        "service": str(service or ""),
        "alert_id": str(alert_id or ""),
        "description": str(description or ""),
    }


def envelope_problem(envelope: Any, expected_type: str) -> tuple[str, str] | None:
    """结构校验：返回 (错误码, 中文原因) 或 None。"""
    if not isinstance(envelope, dict):
        return (
            "missing-envelope",
            "命令信号缺少信封（新 BFF 不接受旧参数形态，请升级后重试）",
        )
    if envelope.get("contract") != CONTRACT:
        return ("contract-mismatch", "命令契约版本不匹配")
    command_id = str(envelope.get("command_id") or "").strip()
    if not command_id or len(command_id) > 128:
        return ("invalid-command-id", "命令 ID 缺失或格式非法")
    if envelope.get("type") != expected_type:
        return (
            "wrong-type",
            f"命令类型不匹配（信号 {expected_type}，信封 {envelope.get('type')}）",
        )
    if envelope.get("type") not in TYPES:
        return ("invalid-type", "命令类型非法")
    if not str(envelope.get("actor") or "").strip():
        return ("missing-actor", "操作者身份缺失")
    if len(str(envelope.get("actor") or "")) > 200:
        return ("invalid-actor", "操作者身份过长")
    if envelope.get("channel") not in CHANNELS:
        return ("invalid-channel", "命令通道非法")
    submitted_at = str(envelope.get("submitted_at") or "")
    try:
        datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ("invalid-submitted-at", "命令提交时间缺失或不是合法 ISO 时间")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return ("missing-payload", "命令载荷缺失")
    if expected_type == "deploy-command":
        if payload.get("command") not in DEPLOY_COMMANDS:
            return ("invalid-command", "发布指令非法（需 deploy_now / cancel）")
    else:
        if not str(payload.get("new_wf_id") or "").strip():
            return ("missing-workflow-id", "排队命令缺少 new_wf_id")
        if not str(payload.get("alert_id") or "").strip():
            return ("missing-alert-id", "排队命令缺少 alert_id")
        if not str(payload.get("service") or "").strip():
            return ("missing-service", "排队命令缺少 service")
    return None


def envelope_digest(envelope: dict) -> str:
    """同一 command_id 的内容指纹（检测重投是否被篡改）。"""
    text = canonical_json(envelope)
    return hashlib.sha256(
        b"aiops-workflow-command-v1\0" + text.encode("utf-8")
    ).hexdigest()
