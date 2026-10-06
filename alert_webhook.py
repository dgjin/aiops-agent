"""告警接入服务（WP1 交付物）：Alertmanager webhook → 标准化 Alert → 幂等启动 Temporal workflow。

运行方式（独立终端）：
    .venv/bin/python alert_webhook.py --port 8099

Alertmanager 侧接入点（本机 nl2sql-monitoring 栈 deploy/alertmanager.yml 已配置）：
    子路由 matchers: ['aiops_demo = "true"'] → http://host.docker.internal:8099/webhook

设计要点（对齐实施计划 WP1）：
    - 标准化：仅处理 firing 告警 → Alert{alert_id, service, severity, description}；resolved 直接忽略；
    - alert_id：优先取 labels.alert_id（便于把业务告警 ID 透传），否则取「fingerprint 前 12 位
      + startsAt 秒级时间戳」——同一告警的重复投递/周期重发保持幂等，跨故障周期
      （resolved 后再次 firing）startsAt 刷新，避免 REJECT_DUPLICATE 永久拒绝新流程；
    - 幂等：workflow id = aiops-fix-{service}-{alert_id}，REJECT_DUPLICATE + FAIL，
      重复投递仅返回 duplicates，不产生第二个流程实例；
    - kill switch：启动前检查，激活时返回 503 拒绝新流程；状态不可读时 fail-closed；
    - 鉴权（P0-1）：来源 IP 白名单 + 共享密钥（Authorization: Bearer / X-AIOps-Token）+
      可选 HMAC 签名（X-AIOps-Signature）+ 限速；production 档未配置密钥时 fail-closed（503），
      demo 档未配置放行并打启动警告（保持演示零配置可跑）；
    - 策略快照在服务启动时加载一次（与 worker 语义一致，运行期不热修改）；
    - 启动失败返回 5xx，交由 Alertmanager 按重试策略再次投递。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from aiops_agent import kill_switch, metrics
from aiops_agent.config import load_policy, snapshot_for_workflow
from aiops_agent.models import Alert
from aiops_agent.workflows import AIOpsFixWorkflow

TASK_QUEUE = "aiops-tasks"
WEBHOOK_PATH = "/webhook"

logger = logging.getLogger("aiops.webhook")

# 服务启动时加载一次的策略快照（运行期不热修改）
_POLICY_SNAPSHOT: dict = {}

# ---- 接入鉴权（P0-1）----
# 背景：/webhook 是「AI 改代码 + 发布」链路的入口，必须具备防伪造能力。
# 检查顺序：IP 白名单（可选）→ 共享密钥 / HMAC 签名（未配置则按档位放行或 fail-closed）→ 单条通过即放行。
# Alertmanager 侧经 receiver 的 http_config.authorization 携带 Bearer 密钥
# （写入工具：monitoring/apply_alertmanager_route.py --set-webhook-token）。
AUTH_TOKEN_ENV = "AIOPS_WEBHOOK_TOKEN"
AUTH_HMAC_ENV = "AIOPS_WEBHOOK_HMAC_SECRET"
AUTH_IPS_ENV = "AIOPS_WEBHOOK_ALLOW_IPS"
AUTH_RATE_ENV = "AIOPS_WEBHOOK_RATE_LIMIT"
DEFAULT_RATE_LIMIT = 120  # 次/分钟/来源 IP；0 表示关闭限速
MAX_BODY_BYTES = 1_048_576  # 1 MiB：Alertmanager 载荷远小于此，超出直接 413


@dataclass(frozen=True)
class AuthConfig:
    """接入鉴权配置（服务启动时快照，与策略快照语义一致，运行期不热修改）。"""

    token: str = ""
    hmac_secret: str = ""
    allow_ips: tuple[str, ...] = ()
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT
    demo: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.token or self.hmac_secret)

    def describe(self) -> str:
        return (
            f"token={'已配置' if self.token else '未配置'} "
            f"hmac={'已配置' if self.hmac_secret else '未配置'} "
            f"ip白名单={len(self.allow_ips)}个 "
            f"限速={self.rate_limit_per_min}/分钟 "
            f"档位={'demo' if self.demo else 'production'}"
        )


def load_auth_config(env: Mapping[str, str] | None = None) -> AuthConfig:
    """从环境变量加载鉴权配置（env 可注入，供单测）。档位判定缺省为 production（与 mode.py 一致）。"""
    source = env if env is not None else os.environ
    raw_ips = (source.get(AUTH_IPS_ENV) or "").strip()
    try:
        rate = int((source.get(AUTH_RATE_ENV) or str(DEFAULT_RATE_LIMIT)).strip())
    except ValueError:
        rate = DEFAULT_RATE_LIMIT
    return AuthConfig(
        token=(source.get(AUTH_TOKEN_ENV) or "").strip(),
        hmac_secret=(source.get(AUTH_HMAC_ENV) or "").strip(),
        allow_ips=tuple(ip.strip() for ip in raw_ips.split(",") if ip.strip()),
        rate_limit_per_min=max(0, rate),
        demo=(source.get("AIOPS_MODE") or "production").strip().lower() == "demo",
    )


class RateLimiter:
    """固定窗口限速（进程内，按来源 IP 计数；limit<=0 关闭）。线程安全（ThreadingHTTPServer）。"""

    def __init__(self, limit_per_min: int) -> None:
        self._limit = limit_per_min
        self._lock = threading.Lock()
        self._window: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        if self._limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._window.get(key, (now, 0))
            if now - start >= 60.0:
                start, count = now, 0
            count += 1
            self._window[key] = (start, count)
            if len(self._window) > 1024:  # 内存兜底：窗口字典异常膨胀时清掉过期项
                self._window = {k: v for k, v in self._window.items() if now - v[0] < 60.0}
            return count <= self._limit


def check_request_auth(
    headers: Mapping[str, str], raw_body: bytes, client_ip: str, cfg: AuthConfig
) -> tuple[int, dict] | None:
    """鉴权准入检查：通过返回 None；拒绝返回 (HTTP 状态码, 错误响应体)。纯函数（除常时比较）。"""
    if cfg.allow_ips and client_ip not in cfg.allow_ips:
        return 403, {"error": f"来源 IP 不在白名单: {client_ip}"}
    if not cfg.configured:
        if cfg.demo:
            return None  # 演示档零配置放行（启动时已打警告）
        return 503, {"error": "webhook 鉴权未配置（fail-closed）: 请设置 AIOPS_WEBHOOK_TOKEN"}
    provided = ""
    auth_header = headers.get("Authorization") or ""
    if auth_header.startswith("Bearer "):
        provided = auth_header[len("Bearer "):].strip()
    if not provided:
        provided = (headers.get("X-AIOps-Token") or "").strip()
    if cfg.token and provided:
        if hmac.compare_digest(provided.encode(), cfg.token.encode()):
            return None
    if cfg.hmac_secret:
        signature = (headers.get("X-AIOps-Signature") or "").strip()
        if signature.startswith("sha256="):
            expected = hmac.new(cfg.hmac_secret.encode(), raw_body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature[len("sha256="):].lower(), expected):
                return None
    return 401, {
        "error": "鉴权失败: 缺少或无效凭证（Authorization: Bearer / X-AIOps-Token / X-AIOps-Signature）"
    }


def _starts_at_suffix(item: dict) -> str:
    """提取 startsAt 的秒级时间戳后缀（缺失/解析失败返回空串，保持旧行为）。

    fingerprint 对同一 labelset 恒定：接入 prometheus-alerts 业务规则后（无 alert_id 标签），
    若幂等键只用 fingerprint，告警 resolved 后再触发会命中 REJECT_DUPLICATE，永远开不出
    第二条流程。startsAt 在单次告警生命周期内稳定（重复投递幂等），跨周期刷新（可开新流程）。
    """
    raw = item.get("startsAt")
    if not raw:
        return ""
    try:
        return str(int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()))
    except (TypeError, ValueError):
        return ""


def parse_alertmanager_payload(payload: dict) -> tuple[list[Alert], int]:
    """Alertmanager v4 webhook 载荷 → 标准化 Alert 列表。

    返回 (firing_alerts, resolved_count)。纯函数，供单测覆盖。
    """
    alerts: list[Alert] = []
    resolved = 0
    for item in payload.get("alerts", []):
        if item.get("status") != "firing":
            resolved += 1
            continue
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        service = labels.get("service") or labels.get("job") or "unknown"
        fingerprint = item.get("fingerprint") or ""
        if labels.get("alert_id"):
            alert_id = labels["alert_id"]
        elif fingerprint:
            suffix = _starts_at_suffix(item)
            alert_id = f"{fingerprint[:12]}-{suffix}" if suffix else fingerprint[:12]
        else:  # 兜底：无 fingerprint 时用业务键拼装（保持确定性）
            alert_id = f"{labels.get('alertname', 'unknown')}-{service}"
        alerts.append(
            Alert(
                alert_id=alert_id,
                service=service,
                severity=labels.get("severity") or "critical",
                description=annotations.get("description")
                or annotations.get("summary")
                or labels.get("alertname", ""),
            )
        )
    return alerts, resolved


def workflow_id_for(alert: Alert) -> str:
    """与 demo_cli 保持一致的幂等键：aiops-fix-{service}-{alert_id}。"""
    return f"aiops-fix-{alert.service}-{alert.alert_id}"


def check_kill_switch() -> dict | None:
    """kill switch 检查：放行返回 None；拒绝返回响应体（含 error；fail-closed）。"""
    try:
        state = kill_switch.get_state()
    except kill_switch.KillSwitchError as exc:
        return {"error": f"kill switch 状态不可读，拒绝启动（fail-closed）：{exc}"}
    if state.get("active"):
        return {"error": "kill switch 已激活，拒绝启动新流程", "kill_switch": state}
    return None


async def _start_workflows(alerts: list[Alert], snapshot: dict) -> tuple[list[str], list[str]]:
    """逐条启动 workflow，返回 (started, duplicates)。幂等冲突不算失败。"""
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    started: list[str] = []
    duplicates: list[str] = []
    for alert in alerts:
        wf_id = workflow_id_for(alert)
        try:
            await client.start_workflow(
                AIOpsFixWorkflow.run,
                args=[alert, snapshot],
                id=wf_id,
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            started.append(wf_id)
            metrics.observe_workflow_started(alert.service)
        except WorkflowAlreadyStartedError:
            duplicates.append(wf_id)
    return started, duplicates


class _WebhookHandler(BaseHTTPRequestHandler):
    server_version = "AIOpsWebhook/0.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        if self.path != WEBHOOK_PATH:
            self._respond(404, {"error": f"仅支持 POST {WEBHOOK_PATH}"})
            return
        client_ip = self.client_address[0]
        if not _RATE_LIMITER.allow(client_ip):
            logger.warning(
                "接入限速拦截: %s 超过 %d 次/分钟", client_ip, _AUTH_CONFIG.rate_limit_per_min
            )
            self._respond(429, {"error": "请求过于频繁（超出限速配额），请稍后重试"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"error": "Content-Length 非法"})
            return
        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": f"请求体过大（>{MAX_BODY_BYTES} 字节）"})
            return
        raw_body = self.rfile.read(length) if length else b"{}"
        denied = check_request_auth(self.headers, raw_body, client_ip, _AUTH_CONFIG)
        if denied:
            status, body = denied
            logger.warning("接入鉴权拒绝（%d）: %s | %s", status, client_ip, body["error"])
            self._respond(status, body)
            return
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            self._respond(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._respond(400, {"error": "请求体必须是 JSON 对象（Alertmanager v4 载荷）"})
            return
        alerts_raw = payload.get("alerts", [])
        if not isinstance(alerts_raw, list) or any(not isinstance(item, dict) for item in alerts_raw):
            self._respond(400, {"error": "alerts 字段必须是对象数组（Alertmanager v4 载荷）"})
            return

        alerts, resolved = parse_alertmanager_payload(payload)
        if not alerts:
            logger.info("收到载荷：0 条 firing（忽略 resolved=%d）", resolved)
            self._respond(200, {"started": [], "duplicates": [], "ignored_resolved": resolved})
            return

        # kill switch：激活时拒绝启动新流程；状态不可读 fail-closed（503）
        blocked = check_kill_switch()
        if blocked:
            logger.warning(
                "kill switch 拦截告警启动（%d 条 firing）：%s", len(alerts), blocked["error"]
            )
            self._respond(503, blocked)
            return

        try:
            started, duplicates = asyncio.run(_start_workflows(alerts, _POLICY_SNAPSHOT))
        except Exception as exc:  # noqa: BLE001 - 返回 5xx 让 Alertmanager 重试投递
            logger.exception("启动 workflow 失败: %s", exc)
            self._respond(500, {"error": str(exc)})
            return

        for wf_id in started:
            logger.info("已启动 workflow: %s", wf_id)
        for wf_id in duplicates:
            logger.info("重复告警（幂等拒绝，不重复开流程）: %s", wf_id)
        self._respond(
            200,
            {"started": started, "duplicates": duplicates, "ignored_resolved": resolved},
        )

    def _respond(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # 交给 logging 统一输出
        logger.info("HTTP %s", fmt % args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="AIOps 告警接入服务（Alertmanager webhook → Temporal workflow，WP1）"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="监听地址（默认 0.0.0.0，供容器内 Alertmanager 经 host.docker.internal 访问）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AIOPS_WEBHOOK_PORT", "8099")),
        help="监听端口（默认 8099，或环境变量 AIOPS_WEBHOOK_PORT）",
    )
    args = parser.parse_args()

    global _POLICY_SNAPSHOT, _AUTH_CONFIG, _RATE_LIMITER
    policy = load_policy()
    _POLICY_SNAPSHOT = snapshot_for_workflow(policy)
    logger.info(
        "策略快照已加载（%s），%s",
        os.environ.get("AIOPS_POLICY_PATH", "默认"),
        policy.summary(),
    )

    _AUTH_CONFIG = load_auth_config()
    _RATE_LIMITER = RateLimiter(_AUTH_CONFIG.rate_limit_per_min)
    if not _AUTH_CONFIG.configured:
        if _AUTH_CONFIG.demo:
            logger.warning(
                "webhook 鉴权未配置（demo 档放行）: 生产环境请设置 %s 并同步更新 "
                "Alertmanager receiver 的 Authorization 头",
                AUTH_TOKEN_ENV,
            )
        else:
            logger.error(
                "webhook 鉴权未配置（production fail-closed）: 所有请求将返回 503，请设置 %s",
                AUTH_TOKEN_ENV,
            )
    logger.info("接入鉴权姿态: %s", _AUTH_CONFIG.describe())

    server = ThreadingHTTPServer((args.host, args.port), _WebhookHandler)
    logger.info("告警接入服务已启动: http://%s:%d%s", args.host, args.port, WEBHOOK_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
