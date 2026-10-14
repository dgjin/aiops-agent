"""通知层（WP8 交付物）：飞书/钉钉交互卡片 + 真实投递 + 回调解析。

流程（对齐实施计划 WP8，替换日志桩卡片）：
    render_intake_card        接收卡片：监控到告警/日志 → 处理流程说明（IM 交互补齐）
    render_requirement_intake_card  需求接收卡片：已批准需求任务 → 直接进入修复流程
    render_fix_started_card   进入修复卡片：分析结论 + 修复链路（含影子档/全链路模式）
    render_approval_card      审批卡片：根因分析 + 完整 diff + 测试报告 + 回滚预案 + 审批按钮
    render_broadcast_card     用户公告卡片（闸门 3 倒计时窗口）
    render_escalation_card    转人工升级卡片（值班负责人）
    NotificationSender.send   配置 webhook 时真实 POST（支持飞书/钉钉加签）；
                              未配置时留痕 data/notify/<msg_id>.json（recorded）；
                              投递失败或机器人应答非零（如加签不符）降级为 failed，不阻断修复主流程
    parse_callback            卡片按钮回调解析（飞书 v2 卡片动作事件 / v1 action.value /
                              钉钉 actionURL 查询参数），返回 (workflow_id, gate, decision)，
                              供回调服务端校验后转发 signal
    extract_callback_operator 回调触发者提取（飞书 v2 event.operator / v1 open_id），
                              用于审批人身份与命令级权限判定
    render_approval_result_card  审批处置结果卡片（卡片回调响应：按钮区替换为处置结果）

回调契约（按钮载荷）：{"wf_id": "aiops-fix-order-a-1", "gate": "approval", "decision": "approve"}

回调网关：BFF ``POST /api/im/card-callback`` 接收飞书卡片动作回调（Verification Token
校验 fail-closed）→ 解析载荷与操作者 → 转发 Temporal signal → 3 秒内响应 toast + 更新卡片。

环境变量：
    AIOPS_NOTIFY_PROVIDER                       feishu / dingtalk（默认 feishu）
    AIOPS_FEISHU_WEBHOOK / AIOPS_DINGTALK_WEBHOOK   provider 专用 webhook（优先）
    AIOPS_NOTIFY_WEBHOOK                        群机器人 webhook（未设置则留痕模式）
    AIOPS_NOTIFY_SECRET                         加签密钥（可选；飞书秒级 / 钉钉毫秒级签名）
    AIOPS_NOTIFY_CALLBACK_BASE                  钉钉按钮回调基址（默认本机演示地址）
    AIOPS_FEISHU_VERIFY_TOKEN                   飞书卡片回调 Verification Token
                                                （未配置＝回调通道关闭，BFF 侧读取）
    AIOPS_FEISHU_APP_ID / AIOPS_FEISHU_APP_SECRET  应用机器人凭证（与接收方三要素
                                                齐全时飞书卡片经应用通道发送，按钮
                                                可回传交互；自定义机器人 webhook
                                                发送的卡片不支持回传）
    AIOPS_FEISHU_RECEIVE_ID                     应用通道接收方（群 chat_id 或 open_id）
    AIOPS_FEISHU_RECEIVE_ID_TYPE                接收方类型（chat_id / open_id / email，
                                                默认 chat_id）
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

from . import feishu_app
from .models import Alert, Patch, RequirementTask, RootCause, TestReport

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
    wf_id: str | None = None,
) -> dict:
    """审批卡片：根因分析 + 完整 diff + 测试报告 + 回滚预案 + 审批按钮。

    ``wf_id`` 为按钮回调载荷中的流程标识：活动层应传入运行中的真实 workflow id
    （需求流程 / 重跑后缀流程的公式值与实际 ID 不一致，否则点击回调无法定位流程）；
    缺省按 ``aiops-fix-{service}-{alert_id}`` 公式生成（兼容直接调用场景）。
    """
    wf_id = wf_id or _workflow_id(alert)
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


def render_shadow_card(
    alert: Alert,
    root_cause: RootCause,
    patch: Patch,
    test_report: TestReport,
    *,
    provider: str = DEFAULT_PROVIDER,
) -> dict:
    """Shadow 模式建议卡片（P1-2）：只建议不执行，仅供人工参考，无审批按钮。"""
    title = f"AI 修复建议（shadow）· {alert.service}/{alert.alert_id}"
    test_line = (
        f"单测 {test_report.unit_tests} / 回归 {test_report.regression_tests} / SAST {test_report.sast}"
    )
    note = "shadow 模式：未创建 MR、未进入审批与发布，仅生成建议供人工参考。"
    if provider == "dingtalk":
        lines = [
            f"**根因**（confidence={root_cause.confidence:.2f}）：{root_cause.summary}",
            f"**建议补丁**：{patch.patch_id}（风险：{patch.risk}） 文件：{', '.join(patch.files)}",
            f"**沙箱测试**：{test_line}",
            f"**说明**：{note}",
            f"**Diff**：\n```diff\n{patch.diff}\n```",
        ]
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n" + "\n\n".join(lines)},
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
                "content": f"**建议补丁**：{patch.patch_id}（风险：{patch.risk}）\n文件：{', '.join(patch.files)}",
            },
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**沙箱测试**\n{test_line}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**说明**\n{note}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Diff**\n```diff\n{patch.diff}\n```"}},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "turquoise"},
            "elements": elements,
        },
    }


def render_intake_card(alert: Alert, *, provider: str = DEFAULT_PROVIDER) -> dict:
    """接收卡片（告警流程）：监控到告警/日志信息进入处理流程，附全链路阶段说明。"""
    title = f"已接收告警 · {alert.service}/{alert.alert_id}"
    desc = " ".join(alert.description.split())[:120] or "-"
    pipeline = (
        "① 日志取证（Loki 近 10 分钟）→ ② Drain3 聚类降噪 → ③ LLM 根因分析 → "
        "④ 闸门1 置信度判定；通过后进入修复流程（补丁生成 → 沙箱验证 → 审批/发布）"
    )
    note = "本消息为流程启动通知，后续将推送「进入修复流程」及审批/发布/终态消息。"
    if provider == "dingtalk":
        lines = [
            f"**服务**：{alert.service} · 告警：{alert.alert_id} · 级别：{alert.severity}",
            f"**描述**：{desc}",
            f"**处理流程**：{pipeline}",
            f"**说明**：{note}",
        ]
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n" + "\n\n".join(lines)},
        }
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**服务**：{alert.service} · 告警：{alert.alert_id} · 级别：{alert.severity}",
            },
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**描述**\n{desc}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**处理流程**\n{pipeline}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**说明**\n{note}"}},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "wathet"},
            "elements": elements,
        },
    }


def render_requirement_intake_card(
    task: RequirementTask, *, provider: str = DEFAULT_PROVIDER
) -> dict:
    """需求接收卡片（需求流程）：已批准的需求任务进入修复流程（跳过日志诊断）。"""
    title = f"已接收需求实现任务 · {task.service}/{task.entry_id}"
    requirement = " ".join(task.requirement.split())[:150] or "-"
    acceptance = "；".join(task.acceptance)[:200] or "（未列出）"
    pipeline = (
        "需求分析已由管理员批准（跳过日志诊断与闸门1）→ 直接进入修复流程："
        "① 生成最小补丁 → ② 沙箱隔离验证 → ③ 审批与发布（按当前模式执行）"
    )
    if provider == "dingtalk":
        lines = [
            f"**需求标题**：{task.title}",
            f"**来源分析**：{task.analysis_id} v{task.version} · 批准人：{task.approved_by}",
            f"**需求原文**：{requirement}",
            f"**验收要点**：{acceptance}",
            f"**处理流程**：{pipeline}",
        ]
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n" + "\n\n".join(lines)},
        }
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**需求标题**\n{task.title}"}},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**来源分析**：{task.analysis_id} v{task.version} · 批准人：{task.approved_by}",
            },
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**需求原文**\n{requirement}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**验收要点**\n{acceptance}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**处理流程**\n{pipeline}"}},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "wathet"},
            "elements": elements,
        },
    }


def render_fix_started_card(
    alert: Alert,
    root_cause: RootCause,
    shadow_mode: bool,
    *,
    provider: str = DEFAULT_PROVIDER,
) -> dict:
    """进入修复卡片：分析结论 + 修复链路；按 shadow_mode 标注影子档/全链路模式。"""
    title = f"进入修复流程 · {alert.service}/{alert.alert_id}"
    files = ", ".join(root_cause.suspect_files) or "（未定位）"
    pipeline = "① 生成最小补丁（Code RAG 上下文）→ ② 沙箱隔离验证 → ③ 审批与发布（按模式执行）"
    mode = (
        "影子档（仅建议不执行）：测试通过后仅推送修复建议，不创建 MR、不审批、不发布"
        if shadow_mode
        else "全链路：测试通过后创建 MR → IM 审批（受保护目录二级）→ 用户公告倒计时 → 金丝雀发布（劣化自动回滚）"
    )
    if provider == "dingtalk":
        lines = [
            f"**分析结论**（confidence={root_cause.confidence:.2f}）：{root_cause.summary}",
            f"**疑似文件**：{files}",
            f"**修复链路**：{pipeline}",
            f"**模式**：{mode}",
        ]
        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n" + "\n\n".join(lines)},
        }
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**分析结论**（confidence={root_cause.confidence:.2f}）\n{root_cause.summary}",
            },
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**疑似文件**\n{files}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**修复链路**\n{pipeline}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**模式**\n{mode}"}},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "indigo"},
            "elements": elements,
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
        # 应用机器人通道（三要素齐全时优先生效）：卡片回传交互（审批按钮）只能由
        # 应用发送的卡片承载——自定义群机器人 webhook 发送的卡片仅支持跳转 URL。
        self.feishu_app_id = (os.environ.get("AIOPS_FEISHU_APP_ID") or "").strip()
        self.feishu_app_secret = (os.environ.get("AIOPS_FEISHU_APP_SECRET") or "").strip()
        self.feishu_receive_id = (os.environ.get("AIOPS_FEISHU_RECEIVE_ID") or "").strip()
        self.feishu_receive_id_type = (
            (os.environ.get("AIOPS_FEISHU_RECEIVE_ID_TYPE") or "").strip() or "chat_id"
        )

    def _feishu_app_ready(self) -> bool:
        """应用机器人通道是否可用（provider=feishu 且凭证与接收方三要素齐全）。"""
        return bool(self.feishu_app_id and self.feishu_app_secret and self.feishu_receive_id)

    def _send_via_feishu_app(self, message: dict, msg_id: str, record: Path) -> dict:
        """应用机器人通道投递（卡片可回传交互）；失败降级不抛错。

        与 webhook 通道的差异：应答判定由 feishu_app 内部完成（code==0 才成功），
        本方法只负责渠道衔接、留痕关联与降级包装。
        """
        card = message.get("card")
        if not isinstance(card, dict):
            return {
                "mode": "failed", "msg_id": msg_id, "error": "消息非交互卡片结构，应用通道跳过",
                "path": str(record),
            }
        try:
            message_id = feishu_app.send_card(card, self.feishu_receive_id, self.feishu_receive_id_type)
        except feishu_app.FeishuAppError as exc:
            return {"mode": "failed", "msg_id": msg_id, "error": str(exc), "path": str(record)}
        result = {"mode": "live", "msg_id": msg_id, "path": str(record), "channel": "feishu-app"}
        if message_id:
            result["message_id"] = message_id
        return result

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

    def _reply_code(self, resp) -> tuple[int | None, str]:
        """解析群机器人应答业务码：飞书 code / 钉钉 errcode（兼容飞书旧版 StatusCode）。

        机器人拒收（加签不符、参数错误等）以 HTTP 200 + 非零应答码返回；读取/解析
        异常一律回退 (None, "")，由 HTTP 状态码兜底判定（通知层不因解析失败抛错）。
        """
        try:
            data = json.loads(resp.read().decode("utf-8"))
            code = data["errcode"] if self.provider == "dingtalk" else data.get("code", data.get("StatusCode"))
            reply = data.get("errmsg") or data.get("msg") or data.get("StatusMessage") or ""
            return int(code), str(reply)
        except Exception:  # noqa: BLE001 - 应答读取/解析异常回退 HTTP 判据
            return None, ""

    def send(self, message: dict, msg_id: str) -> dict:
        """投递卡片：始终留痕；应用通道或 webhook 真实发送。失败降级（不阻断主 流程）。
        成功 = HTTP 2xx 且业务应答码为 0；仅看 HTTP 状态会把机器人拒收误报为 live。
        通道优先级：应用机器人（三要素配置齐全；卡片可回传交互）→ 群机器人 webhook。
        """
        NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
        record = NOTIFY_DIR / f"{msg_id}.json"
        record.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.provider == "feishu" and self._feishu_app_ready():
            return self._send_via_feishu_app(message, msg_id, record)
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
                status_code = resp.status
                code, reply = self._reply_code(resp)
        except (urllib.error.HTTPError, OSError) as exc:
            # 通知失败不影响修复主流程：降级记录，留痕兜底审计
            return {"mode": "failed", "msg_id": msg_id, "error": str(exc), "path": str(record)}
        if code is not None and code != 0:
            return {
                "mode": "failed",
                "msg_id": msg_id,
                "status_code": status_code,
                "error": f"机器人应答非零 code={code}：{reply}",
                "path": str(record),
            }
        result = {"mode": "live", "msg_id": msg_id, "status_code": status_code, "path": str(record)}
        if code is not None:
            result["provider_code"] = code
        return result


def parse_callback(payload: dict | str) -> tuple[str, str, str] | None:
    """解析卡片按钮回调（回调服务端入口）。

    支持三种结构：
        飞书 v2：{"event": {"action": {"value": {"wf_id": ..., ...}}}}（card.action.trigger）
        飞书 v1：{"action": {"value": {"wf_id": ..., ...}}}
        钉钉：actionURL 字符串（查询参数携带同构字段）
    返回 (wf_id, gate, decision)；字段缺失或取值非法返回 None（服务端据此拒绝）。
    """
    data: dict | None = None
    if isinstance(payload, str):
        data = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(payload).query))
    elif isinstance(payload, dict):
        # 飞书 v2 卡片动作事件：action.value 位于 event 内；v1 / 通用结构 action 在顶层
        event = payload.get("event")
        source = event if isinstance(event, dict) else payload
        action = source.get("action", source)
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


def extract_callback_operator(payload: dict) -> str:
    """提取卡片回调触发者标识（审批人身份与命令级权限判定的输入）。

    飞书 v2：``event.operator.open_id / user_id / union_id``；
    旧版 / 通用：顶层 ``open_id / user_id``。未命中返回空串（调用方拒绝并提示）。
    """
    if not isinstance(payload, dict):
        return ""
    event = payload.get("event")
    containers = [item for item in (event, payload) if isinstance(item, dict)]
    for container in containers:
        operator = container.get("operator")
        for source in (operator, container):
            if not isinstance(source, dict):
                continue
            for key in ("open_id", "user_id", "union_id"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


# 闸门 / 决策中文标签（回调处置结果卡片用）
_GATE_LABELS = {"approval": "一级审批", "second_approval": "二级审批"}
_RESULT_LABELS = {"approve": "已批准", "reject": "已驳回"}


def render_approval_result_card(
    wf_id: str, gate: str, decision: str, operator: str, at: str
) -> dict:
    """审批处置结果卡片（卡片回调响应：按钮区替换为处置结果，防重复点击与状态不明）。

    返回飞书 1.0 交互卡片消息结构；回调端点取 ``["card"]`` 作为响应 ``type=raw`` 数据。
    ``at`` 为处置时间（调用方格式化）；渲染层保持纯函数（无时钟依赖）。
    """
    icon = "✅" if decision == "approve" else "❌"
    action_label = f"{_GATE_LABELS.get(gate, gate)}{_RESULT_LABELS.get(decision, decision)}"
    title = f"AI 修复审批处置结果 · {wf_id}"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if decision == "approve" else "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"{icon} **{action_label}**"}},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**流程**：{wf_id}"}},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**操作者**：{operator} · {at}"},
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "流程将继续推进；后续节点（公告/发布/终态）将另行通知。",
                    },
                },
            ],
        },
    }
