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
    - 事件聚合（P2-1 / C 批次）：同故障域（service）告警在窗口内（默认 90s，
      AIOPS_WEBHOOK_AGGREGATE_WINDOW 可配）聚合为单事件，仅事件首发告警开代表流程；
      - production：聚合状态持久化到 MySQL（alert_domains / alert_events / alert_members），
        跨实例成员去重 + 租约认领 + 到期补偿重试（同一代表 id），启动失败返回 5xx 交重投；
      - demo：进程内 AggregateWindow（零外部依赖），失败整域回滚保证不吞告警；
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from aiops_agent import alert_store, db, kill_switch, metrics, mode
from aiops_agent.config import load_policy, snapshot_for_workflow
from aiops_agent.models import Alert
from aiops_agent.workflows import AIOpsFixWorkflow

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

# ---- 事件聚合窗口（P2-1 修复风暴防护）----
AGGREGATE_WINDOW_ENV = "AIOPS_WEBHOOK_AGGREGATE_WINDOW"
DEFAULT_AGGREGATE_WINDOW = 90.0  # 秒；0 = 关闭聚合（production 退化为每告警一事件）

# ---- 事件补偿线程（C 批次）：到期未启动事件的周期重试 ----
COMPENSATE_INTERVAL_SECONDS = 15.0


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


@dataclass
class AggregateState:
    """同故障域聚合事件的状态（窗口自首条告警起算，不滑动）。"""

    event_key: str
    service: str
    first_at: float
    count: int = 1
    alert_ids: list[str] = field(default_factory=list)
    wf_id: str | None = None


class AggregateWindow:
    """同故障域（service）聚合窗口：修复风暴防护（P2-1）。

    语义：窗口内同 service 的后续告警归并入事件（不再开第二条流程）；窗口过期后
    的新告警开新事件；``window_seconds<=0`` 关闭聚合（保持旧行为）。线程安全
    （ThreadingHTTPServer 并发请求）。
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = max(0.0, window_seconds)
        self._lock = threading.Lock()
        self._state: dict[str, AggregateState] = {}

    def route(
        self, alerts: list[Alert], now: float | None = None
    ) -> tuple[list[Alert], list[AggregateState]]:
        """分流一批告警 →（应启动列表，被归并事件的当前状态列表）。"""
        if self._window <= 0 or not alerts:
            return list(alerts), []
        now = time.monotonic() if now is None else now
        to_start: list[Alert] = []
        merged: dict[str, AggregateState] = {}
        with self._lock:
            for alert in alerts:
                state = self._state.get(alert.service)
                if state is not None and now - state.first_at < self._window:
                    state.count += 1
                    state.alert_ids.append(alert.alert_id)
                    merged[state.event_key] = state
                    metrics.observe_alert_aggregated(alert.service)
                    continue
                state = AggregateState(
                    event_key=f"agg-{alert.service}-{int(now)}",
                    service=alert.service,
                    first_at=now,
                    alert_ids=[alert.alert_id],
                )
                self._state[alert.service] = state
                to_start.append(alert)
            if len(self._state) > 1024:  # 内存兜底：清理过期事件
                self._state = {
                    k: v for k, v in self._state.items() if now - v.first_at < self._window
                }
        return to_start, list(merged.values())

    def attach_wf(self, service: str, wf_id: str) -> None:
        """启动后回填事件代表流程 id（重复投递同样回填既有流程 id）。"""
        with self._lock:
            state = self._state.get(service)
            if state is not None:
                state.wf_id = wf_id

    def discard(self, services: Iterable[str]) -> None:
        """启动失败回滚：移除未见流程 id 的事件状态（含已归并计数）。

        旧实现只回滚 count==1 的“干净”状态：同批多告警归并后代表启动失败，
        会残留 ``wf_id=None`` 的僵死事件——重投被判为“归并中”而不再开流程（吞告警）。
        """
        with self._lock:
            for service in services:
                state = self._state.get(service)
                if state is not None and state.wf_id is None:
                    del self._state[service]


# 进程内聚合窗口（demo；main 启动时按 env 重建；默认值供直接调用 handler 兜底）
_AGGREGATE_WINDOW = AggregateWindow(DEFAULT_AGGREGATE_WINDOW)
# 聚合窗口秒数（production 持久化路径使用；main 启动时按 env 重建）
_AGGREGATE_SECONDS = DEFAULT_AGGREGATE_WINDOW
# 补偿线程停止信号（测试与优雅停机使用；进程退出即失效）
_COMPENSATE_STOP = threading.Event()


def check_kill_switch() -> dict | None:
    """kill switch 检查：放行返回 None；拒绝返回响应体（含 error；fail-closed）。"""
    try:
        state = kill_switch.get_state()
    except kill_switch.KillSwitchError as exc:
        return {"error": f"kill switch 状态不可读，拒绝启动（fail-closed）：{exc}"}
    if state.get("active"):
        return {"error": "kill switch 已激活，拒绝启动新流程", "kill_switch": state}
    return None


async def _start_workflows(
    alerts: list[Alert], snapshot: dict
) -> tuple[list[str], list[str], list[tuple[Alert, Exception]]]:
    """逐条启动 workflow（demo / 无认领路径），返回 (started, duplicates, failures)。

    单条失败不阻断其余告警：部分成功保留（已开流程不回滚），失败者由 Alertmanager
    重投补齐；幂等冲突（REJECT_DUPLICATE + FAIL）不算失败。
    """
    try:
        client = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
            namespace=mode.temporal_namespace(),
        )
    except Exception as exc:  # noqa: BLE001 - 连接失败=整批未启动，交重投
        return [], [], [(alert, exc) for alert in alerts]
    started: list[str] = []
    duplicates: list[str] = []
    failures: list[tuple[Alert, Exception]] = []
    for alert in alerts:
        wf_id = workflow_id_for(alert)
        try:
            await client.start_workflow(
                AIOpsFixWorkflow.run,
                args=[alert, snapshot],
                id=wf_id,
                task_queue=mode.temporal_task_queue(),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            started.append(wf_id)
            metrics.observe_workflow_started(alert.service)
        except WorkflowAlreadyStartedError:
            duplicates.append(wf_id)
        except Exception as exc:  # noqa: BLE001 - 单条失败留给重投
            failures.append((alert, exc))
    return started, duplicates, failures


async def _start_claimed_events(
    engine, events: list[alert_store.EventView]
) -> tuple[list[str], list[str], list[tuple[alert_store.EventView, Exception]]]:
    """启动已认领事件的代表流程（绝不变更代表 id），并回报 mark_started / mark_failed。

    幂等冲突（同一代表 id 已被启动，如崩溃接管后重试）视为成功；
    全部 DB 回报都在 Temporal 调用之外进行，不在事务内做 RPC。
    """
    try:
        client = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
            namespace=mode.temporal_namespace(),
        )
    except Exception as exc:  # noqa: BLE001 - 连接失败=全部未启动，保留 pending 等重投/补偿
        for event in events:
            alert_store.mark_failed(
                engine, event.event_id, event.claim_token, f"Temporal 连接失败: {exc}"
            )
        return [], [], [(event, exc) for event in events]
    started: list[str] = []
    duplicates: list[str] = []
    failures: list[tuple[alert_store.EventView, Exception]] = []
    for event in events:
        try:
            await client.start_workflow(
                AIOpsFixWorkflow.run,
                args=[Alert(**event.representative_alert), event.policy_snapshot],
                id=event.wf_id,
                task_queue=mode.temporal_task_queue(),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            started.append(event.wf_id)
            metrics.observe_workflow_started(event.service)
        except WorkflowAlreadyStartedError:
            duplicates.append(event.wf_id)  # 已启动即成功（崩溃恢复重试同一代表 id）
        except Exception as exc:  # noqa: BLE001 - 失败释放租约等重投/补偿
            failures.append((event, exc))
            alert_store.mark_failed(engine, event.event_id, event.claim_token, str(exc))
            continue
        alert_store.mark_started(engine, event.event_id, event.claim_token)
    return started, duplicates, failures


def _handle_production(alerts: list[Alert], resolved: int) -> tuple[int, dict]:
    """production：持久化聚合（跨实例去重 + 租约认领）；失败/占用返回 5xx 交重投。"""
    engine = db.get_engine()
    outcome = alert_store.ingest(engine, alerts, _POLICY_SNAPSHOT, _AGGREGATE_SECONDS)
    for event in outcome.events:
        if not event.is_new:
            metrics.observe_alert_aggregated(event.service)
    started: list[str] = []
    duplicates: list[str] = []
    failures: list[tuple[alert_store.EventView, Exception]] = []
    if outcome.to_start:
        started, duplicates, failures = asyncio.run(
            _start_claimed_events(engine, outcome.to_start)
        )
    body = {
        "started": started,
        "duplicates": duplicates,
        "aggregated": [event.as_aggregated() for event in outcome.events],
        "ignored_resolved": resolved,
    }
    for wf_id in started:
        logger.info("已启动 workflow: %s", wf_id)
    for wf_id in duplicates:
        logger.info("重复告警（幂等拒绝，不重复开流程）: %s", wf_id)
    for event in outcome.events:
        if not event.is_new:
            logger.info(
                "故障域聚合：service=%s 归入事件 %s（当前 %d 条，代表流程 %s）",
                event.service, event.event_id, event.count, event.wf_id,
            )
    if failures:
        body["error"] = "部分代表流程启动失败，等待 Alertmanager 重投: " + "; ".join(
            f"{event.wf_id}: {exc}" for event, exc in failures
        )[:800]
        body["failed"] = [event.wf_id for event, _ in failures]
        return 500, body
    if outcome.busy:
        body["error"] = "事件代表流程正由其他实例启动，请稍后重投确认"
        body["busy"] = [event.event_id for event in outcome.busy]
        return 503, body
    return 200, body


def _handle_demo(alerts: list[Alert], resolved: int) -> tuple[int, dict]:
    """demo：进程内聚合（零外部依赖）；语义与 production 对齐——失败必重投、不吞告警。"""
    to_start, merged = _AGGREGATE_WINDOW.route(alerts)
    started: list[str] = []
    duplicates: list[str] = []
    failures: list[tuple[Alert, Exception]] = []
    if to_start:
        started, duplicates, failures = asyncio.run(_start_workflows(to_start, _POLICY_SNAPSHOT))
    failed_alerts = [alert for alert, _ in failures]
    for alert in to_start:  # 回填事件代表流程 id（started / duplicates 同一幂等键）
        if alert not in failed_alerts:
            _AGGREGATE_WINDOW.attach_wf(alert.service, workflow_id_for(alert))
    if failed_alerts:  # 失败服务整体回滚（含已归并计数）：重投后重新开事件，绝不吞告警
        _AGGREGATE_WINDOW.discard(alert.service for alert in failed_alerts)
    aggregated = [
        {
            "event_key": state.event_key,
            "service": state.service,
            "count": state.count,
            "wf_id": state.wf_id,
            "alert_ids": state.alert_ids[:8],  # 展示截断（窗口内可能持续追加）
        }
        for state in merged
    ]
    for wf_id in started:
        logger.info("已启动 workflow: %s", wf_id)
    for wf_id in duplicates:
        logger.info("重复告警（幂等拒绝，不重复开流程）: %s", wf_id)
    for item in aggregated:
        logger.info(
            "故障域聚合（修复风暴防护）：service=%s 归入事件 %s（窗口内第 %d 条，代表流程 %s）",
            item["service"], item["event_key"], item["count"], item["wf_id"],
        )
    body = {
        "started": started,
        "duplicates": duplicates,
        "aggregated": aggregated,
        "ignored_resolved": resolved,
    }
    if failures:
        body["error"] = "部分告警启动失败，等待 Alertmanager 重投: " + "; ".join(
            f"{alert.alert_id}: {exc}" for alert, exc in failures
        )[:800]
        body["failed"] = [alert.alert_id for alert in failed_alerts]
        return 500, body
    return 200, body


def _handle_alerts(alerts: list[Alert], resolved: int) -> tuple[int, dict]:
    """firing 告警主处理：kill switch →（production 持久化聚合 / demo 进程内聚合）。"""
    if not alerts:
        logger.info("收到载荷：0 条 firing（忽略 resolved=%d）", resolved)
        return 200, {
            "started": [], "duplicates": [], "aggregated": [], "ignored_resolved": resolved,
        }
    # kill switch：激活时拒绝启动新流程；状态不可读 fail-closed（503）
    blocked = check_kill_switch()
    if blocked:
        logger.warning("kill switch 拦截告警启动：%s", blocked["error"])
        return 503, blocked
    if mode.is_production():
        return _handle_production(alerts, resolved)
    return _handle_demo(alerts, resolved)


def _compensate_once(engine) -> int:
    """到期未启动事件的一轮补偿：同一代表 id 重试启动，绝不开新流程；返回认领数。"""
    if check_kill_switch():  # 每次补偿前检查 kill switch（激活时冻结补偿）
        logger.warning("kill switch 激活，跳过本轮事件补偿")
        return 0
    events = alert_store.due_events(engine)
    claimed: list[alert_store.EventView] = []
    for event in events:
        token = alert_store.claim_event(engine, event.event_id)
        if token is None:
            continue  # 已被其他实例认领
        claimed.append(replace(event, claim_token=token))
    if not claimed:
        return 0
    started, duplicates, failures = asyncio.run(_start_claimed_events(engine, claimed))
    logger.info(
        "事件补偿一轮：到期=%d 认领=%d 启动=%d 已存在=%d 失败=%d",
        len(events), len(claimed), len(started), len(duplicates), len(failures),
    )
    return len(claimed)


def _compensate_loop(engine) -> None:
    """补偿线程主体：周期扫描；单轮异常只记录，绝不退出线程。"""
    while not _COMPENSATE_STOP.wait(COMPENSATE_INTERVAL_SECONDS):
        try:
            _compensate_once(engine)
        except Exception:  # noqa: BLE001 - 下一轮重试
            logger.exception("事件补偿扫描失败（下一轮重试）")


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
        try:
            status, body = _handle_alerts(alerts, resolved)
        except Exception as exc:  # noqa: BLE001 - 5xx 让 Alertmanager 重试投递（DB/Temporal 断连）
            logger.exception("处理告警失败: %s", exc)
            self._respond(500, {"error": str(exc)})
            return
        self._respond(status, body)

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

    global _POLICY_SNAPSHOT, _AUTH_CONFIG, _RATE_LIMITER, _AGGREGATE_WINDOW, _AGGREGATE_SECONDS
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

    try:
        window_seconds = float(
            (os.environ.get(AGGREGATE_WINDOW_ENV) or str(DEFAULT_AGGREGATE_WINDOW)).strip()
        )
    except ValueError:
        window_seconds = DEFAULT_AGGREGATE_WINDOW
    _AGGREGATE_WINDOW = AggregateWindow(window_seconds)
    _AGGREGATE_SECONDS = window_seconds
    logger.info(
        "事件聚合姿态: 窗口=%.0fs%s",
        window_seconds,
        "（已关闭）" if window_seconds <= 0 else (
            "（持久化聚合 + 租约补偿，C 批次）" if mode.is_production() else "（demo 进程内聚合）"
        ),
    )

    if mode.is_production():
        engine = db.get_engine()  # fail-fast：建表/补列 + 连接探活；失败=启动失败
        logger.info(
            "聚合存储就绪（跨实例去重 + 租约补偿）：补偿线程每 %.0fs 一轮",
            COMPENSATE_INTERVAL_SECONDS,
        )
        threading.Thread(
            target=_compensate_loop,
            args=(engine,),
            name="aiops-webhook-compensate",
            daemon=True,
        ).start()

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
