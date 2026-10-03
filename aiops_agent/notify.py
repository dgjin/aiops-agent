"""通知层（WP8 交付物）：飞书/钉钉交互卡片 + 真实投递 + 回调解析。

流程（对齐实施计划 WP8，替换日志桩卡片）：
    render_approval_card      审批卡片：根因分析 + 完整 diff + 测试报告 + 回滚预案 + 审批按钮
    render_broadcast_card     用户公告卡片（闸门 3 倒计时窗口）
    render_escalation_card    转人工升级卡片（值班负责人）
    NotificationSender.send   配置 webhook 时真实 POST（支持飞书/钉钉加签）；
                              未配置时留痕 data/notify/<msg_id>.json（recorded）；
                              投递失败降级为 failed（通知失败不阻断修复主流程）
    parse_callback            卡片按钮回调解析（飞书 action.value / 钉钉 actionURL 查询参数），
                              返回 (workflow_id, gate, decision)，供回调服务端校验后转发 signal

回调契约（按钮载荷）：{"wf_id": "aiops-fix-order-a-1", "gate": "approval", "decision": "approve"}

环境变量：
    AIOPS_NOTIFY_PROVIDER                       feishu / dingtalk（默认 feishu）
    AIOPS_FEISHU_WEBHOOK / AIOPS_DINGTALK_WEBHOOK   provider 专用 webhook（优先）
    AIOPS_NOTIFY_WEBHOOK                        群机器人 webhook（未设置则留痕模式）
    AIOPS_NOTIFY_SECRET                         加签密钥（可选；飞书秒级 / 钉钉毫秒级签名）
    AIOPS_NOTIFY_CALLBACK_BASE                  钉钉按钮回调基址（默认本机演示地址）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .models import Alert, Patch, RootCause, TestReport

BASE_DIR = Path(__file__).resolve().parent.parent
NOTIFY_DIR = BASE_DIR / "data" / "notify"

DEFAULT_PROVIDER = "feishu"
PROVIDERS = ("feishu", "dingtalk")
DEFAULT_CALLBACK_BASE = "http://127.0.0.1:8000/aiops/callback"
ROLLBACK_PLAN = "保留上一稳定版本，金丝雀劣化自动回滚"
BROADCAST_MESSAGE = (
    "【系统通知】新版本已完成验证，将于倒计时结束后部署。"
    "期间可能出现短暂抖动，如有异常请点击反馈。"
)

_ALLOWED_GATES = ("approval", "second_approval")
_ALLOWED_DECISIONS = ("approve", "reject")


def _workflow_id(alert: Alert) -> str:
    """回调契约中的 workflow_id（与 demo_cli 的 ID 公式一致）。"""
    return f"aiops-fix-{alert.service}-{alert.alert_id}"


def _callback_url(base: str, wf_id: str, gate: str, decision: str) -> str:
    """钉钉 actionURL：按钮点击回调地址（查询参数携带回调契约）。"""
    query = urllib.parse.urlencode({"wf_id": wf_id, "gate": gate, "decision": decision})
    return f"{base.rstrip('/')}?{query}"


def render_approval_card(
    alert: Alert,
    root_cause: RootCause,
    patch: Patch,
    test_report: TestReport,
    needs_second: bool,
    *,
    provider: str = DEFAULT_PROVIDER,
    callback_base: str = DEFAULT_CALLBACK_BASE,
) -> dict:
    """审批卡片：根因分析 + 完整 diff + 测试报告 + 回滚预案 + 审批按钮。"""
    wf_id = _workflow_id(alert)
    title = f"AI 修复待审批 · {alert.service}/{alert.alert_id}"
    test_line = (
        f"单测 {test_report.unit_tests} / 回归 {test_report.regression_tests} / SAST {test_report.sast}"
    )
    if provider == "dingtalk":
        lines = [
            f"**根因**（confidence={root_cause.confidence:.2f}）：{root_cause.summary}",
            f"**补丁**：{patch.patch_id}（风险：{patch.risk}） 文件：{', '.join(patch.files)}",
            f"**测试**：{test_line}",
            f"**回滚预案**：{ROLLBACK_PLAN}",
        ]
        if needs_second:
            lines.append("**需二级审批**：命中受保护目录，请二级审批人确认")
        lines.append(f"**Diff**：\n```diff\n{patch.diff}\n```")
        return {
            "msgtype": "actionCard",
            "actionCard": {
                "title": title,
                "text": f"### {title}\n\n" + "\n\n".join(lines),
                "btnOrientation": "0",
                "btns": [
                    {
                        "title": "批准",
                        "actionURL": _callback_url(callback_base, wf_id, "approval", "approve"),
                    },
                    {
                        "title": "驳回",
                        "actionURL": _callback_url(callback_base, wf_id, "approval", "reject"),
                    },
                ],
            },
        }
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**根因**（confidence={root_cause.confidence:.2f}）\n{root_cause.summary}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**补丁**：{patch.patch_id}（风险：{patch.risk}）\n文件：{', '.join(patch.files)}",
            },
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**测试**\n{test_line}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**回滚预案**\n{ROLLBACK_PLAN}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Diff**\n```diff\n{patch.diff}\n```"}},
    ]
    if needs_second:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**需二级审批**：命中受保护目录，请二级审批人确认",
                },
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准"},
                    "type": "primary",
                    "value": {"wf_id": wf_id, "gate": "approval", "decision": "approve"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "驳回"},
                    "type": "danger",
                    "value": {"wf_id": wf_id, "gate": "approval", "decision": "reject"},
                },
            ],
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "orange"},
            "elements": elements,
        },
    }


def render_broadcast_card(
    alert: Alert, patch: Patch, version: str, *, provider: str = DEFAULT_PROVIDER
) -> dict:
    """用户公告卡片（闸门 3：倒计时期间向全部在线会话推送）。"""
    title = f"版本发布公告 · {version}"
    body = f"{BROADCAST_MESSAGE}\n服务：{alert.service} · 补丁：{patch.patch_id}"
    if provider == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }


def render_escalation_card(alert: Alert, reason: str, *, provider: str = DEFAULT_PROVIDER) -> dict:
    """转人工升级卡片（值班负责人；电话/短信升级语义对齐）。"""
    title = f"转人工升级 · {alert.service}/{alert.alert_id}"
    body = f"**原因**：{reason}\n**严重级别**：{alert.severity}\n**动作**：请联系值班负责人人工处理"
    if provider == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**原因**\n{reason}"}},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**严重级别**：{alert.severity}\n**动作**：请联系值班负责人人工处理",
                    },
                },
            ],
        },
    }


def _feishu_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名：key = f'{timestamp}\\n{secret}'（秒级时间戳）。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dingtalk_sign(timestamp_ms: str, secret: str) -> str:
    """钉钉加签：key = secret，message = f'{timestamp}\\n{secret}'（毫秒级时间戳）。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class NotificationSender:
    """通知投递器：配置 webhook 时真实发送；否则留痕（recorded）。投递失败降级不抛错。"""

    def __init__(
        self,
        provider: str | None = None,
        webhook: str | None = None,
        secret: str | None = None,
        callback_base: str | None = None,
    ) -> None:
        configured = (provider or os.environ.get("AIOPS_NOTIFY_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
        self.provider = configured if configured in PROVIDERS else DEFAULT_PROVIDER
        env_webhook = os.environ.get(f"AIOPS_{self.provider.upper()}_WEBHOOK") or os.environ.get(
            "AIOPS_NOTIFY_WEBHOOK", ""
        )
        self.webhook = (webhook if webhook is not None else env_webhook).strip()
        self.secret = secret if secret is not None else os.environ.get("AIOPS_NOTIFY_SECRET", "")
        self.callback_base = (
            callback_base
            if callback_base is not None
            else os.environ.get("AIOPS_NOTIFY_CALLBACK_BASE", DEFAULT_CALLBACK_BASE)
        ).rstrip("/")

    def _prepare(self, message: dict) -> tuple[str, dict]:
        """按 provider 构造请求 URL 与请求体（含加签）。"""
        url = self.webhook
        body = dict(message)
        if self.provider == "dingtalk":
            if self.secret:
                timestamp_ms = str(round(time.time() * 1000))
                sign = _dingtalk_sign(timestamp_ms, self.secret)
                separator = "&" if "?" in url else "?"
                query = urllib.parse.urlencode({"timestamp": timestamp_ms, "sign": sign})
                url = f"{url}{separator}{query}"
            return url, body
        if self.secret:  # feishu：加签字段置于请求体顶层
            timestamp = int(time.time())
            body["timestamp"] = str(timestamp)
            body["sign"] = _feishu_sign(timestamp, self.secret)
        return url, body

    def send(self, message: dict, msg_id: str) -> dict:
        """投递卡片：始终留痕；配置 webhook 时真实 POST。失败降级（通知不阻断主流程）。"""
        NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
        record = NOTIFY_DIR / f"{msg_id}.json"
        record.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.webhook:
            return {"mode": "recorded", "msg_id": msg_id, "path": str(record)}
        url, body = self._prepare(message)
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return {
                    "mode": "live",
                    "msg_id": msg_id,
                    "status_code": resp.status,
                    "path": str(record),
                }
        except (urllib.error.HTTPError, OSError) as exc:
            # 通知失败不影响修复主流程：降级记录，留痕兜底审计
            return {"mode": "failed", "msg_id": msg_id, "error": str(exc), "path": str(record)}


def parse_callback(payload: dict | str) -> tuple[str, str, str] | None:
    """解析卡片按钮回调（回调服务端入口）。

    支持两种结构：
        飞书：{"action": {"value": {"wf_id": ..., "gate": ..., "decision": ...}}}
        钉钉：actionURL 字符串（查询参数携带同构字段）
    返回 (wf_id, gate, decision)；字段缺失或取值非法返回 None（服务端据此拒绝）。
    """
    data: dict | None = None
    if isinstance(payload, str):
        data = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(payload).query))
    elif isinstance(payload, dict):
        action = payload.get("action", payload)
        if isinstance(action, dict):
            value = action.get("value", action)
            if isinstance(value, dict):
                data = value
            elif isinstance(value, str):
                data = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(value).query))
    if not data:
        return None
    wf_id = str(data.get("wf_id") or "")
    gate = str(data.get("gate") or "")
    decision = str(data.get("decision") or "")
    if not wf_id or gate not in _ALLOWED_GATES or decision not in _ALLOWED_DECISIONS:
        return None
    return wf_id, gate, decision
