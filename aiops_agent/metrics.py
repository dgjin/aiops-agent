"""Prometheus 指标（优化方案 3.4，GAP-07）。

- 指标口径与优化方案对齐：工作流 / 修复 / 沙箱 / 发布 / BFF 请求 / 闸门事件；
- 暴露方式：BFF ``GET /metrics``；worker 独立 HTTP 服务（``start_worker_server``，默认 loopback 9091）；
- ``prometheus_client`` 缺失时全部指标降级为 no-op（零依赖兜底）；
- 所有 ``observe_*`` 入口均为纯旁路：不改变业务返回值，异常绝不影响主流程。

环境变量：
    AIOPS_METRICS_PORT   worker 指标服务端口（默认 loopback 9091；0 = 关闭）
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timezone

from . import mode

log = logging.getLogger("aiops.metrics")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover - 未安装时全量降级
    _AVAILABLE = False


class _NoopMetric:
    """no-op 指标：接口与 prometheus_client 对齐（labels/inc/observe）。"""

    def labels(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def inc(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass

    def observe(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass

    def set(self, *args, **kwargs):
        pass


if _AVAILABLE:
    WORKFLOW_STARTED = Counter(
        "aiops_workflow_started_total", "工作流启动数", ["service", "mode"]
    )
    WORKFLOW_COMPLETED = Counter(
        "aiops_workflow_completed_total", "工作流完成数", ["service", "stage", "mode"]
    )
    WORKFLOW_DURATION = Histogram(
        "aiops_workflow_duration_seconds",
        "工作流耗时",
        ["service", "mode"],
        buckets=[10, 30, 60, 120, 300, 600],
    )
    FIX_ATTEMPTS = Counter(
        "aiops_fix_attempts_total", "修复生成尝试数", ["provider", "degraded", "mode"]
    )
    FIX_DURATION = Histogram(
        "aiops_fix_duration_seconds", "修复生成耗时", ["provider", "mode"]
    )
    SANDBOX_RUNS = Counter("aiops_sandbox_runs_total", "沙箱执行数", ["passed", "mode"])
    SANDBOX_STATIC_RUNS = Counter(
        "aiops_sandbox_static_runs_total",
        "静态验收次数（不计入真实沙箱通过率）",
        ["passed", "verification", "mode"],
    )
    SANDBOX_DURATION = Histogram("aiops_sandbox_duration_seconds", "沙箱执行耗时", ["mode"])
    CANARY_DEPLOYS = Counter("aiops_canary_deploys_total", "金丝雀发布数", ["healthy", "mode"])
    CANARY_ERROR_RATE = Histogram(
        "aiops_canary_error_rate", "金丝雀观测错误率（%）", ["mode"], buckets=[0.5, 1, 2, 5, 10, 50, 100]
    )
    BFF_REQUESTS = Counter(
        "aiops_bff_requests_total", "BFF 请求数", ["method", "path", "status"]
    )
    BFF_REQUEST_DURATION = Histogram(
        "aiops_bff_request_duration_seconds", "BFF 请求耗时", ["method", "path"]
    )
    GATE_EVENTS = Counter("aiops_gate_events_total", "闸门事件数", ["gate", "decision", "mode"])
    AGGREGATED_ALERTS = Counter(
        "aiops_alert_aggregated_total", "同故障域聚合归并的告警数（P2-1）", ["service", "mode"]
    )
    CAPACITY_IN_USE = Gauge("aiops_capacity_in_use", "资源并发占用数（M6）", ["resource", "mode"])
    CAPACITY_QUEUE = Gauge(
        "aiops_capacity_queue_depth", "资源等待队列长度（M6）", ["resource", "mode"]
    )
    CAPACITY_WAIT = Histogram(
        "aiops_capacity_wait_seconds",
        "资源等待时长（M6）",
        ["resource", "mode"],
        buckets=[0.01, 0.1, 0.5, 1, 5, 30, 60, 300],
    )
    CAPACITY_RUN = Histogram(
        "aiops_capacity_run_seconds",
        "资源执行时长（M6）",
        ["resource", "mode"],
        buckets=[0.1, 1, 5, 15, 30, 60, 300, 900],
    )
    CAPACITY_REJECTED = Counter(
        "aiops_capacity_rejected_total",
        "资源等待超时/取消次数（M6）",
        ["resource", "reason", "mode"],
    )
else:  # pragma: no cover - 降级分支
    WORKFLOW_STARTED = _NoopMetric()
    WORKFLOW_COMPLETED = _NoopMetric()
    WORKFLOW_DURATION = _NoopMetric()
    FIX_ATTEMPTS = _NoopMetric()
    FIX_DURATION = _NoopMetric()
    SANDBOX_RUNS = _NoopMetric()
    SANDBOX_STATIC_RUNS = _NoopMetric()
    SANDBOX_DURATION = _NoopMetric()
    CANARY_DEPLOYS = _NoopMetric()
    CANARY_ERROR_RATE = _NoopMetric()
    BFF_REQUESTS = _NoopMetric()
    BFF_REQUEST_DURATION = _NoopMetric()
    GATE_EVENTS = _NoopMetric()
    AGGREGATED_ALERTS = _NoopMetric()
    CAPACITY_IN_USE = _NoopMetric()
    CAPACITY_QUEUE = _NoopMetric()
    CAPACITY_WAIT = _NoopMetric()
    CAPACITY_RUN = _NoopMetric()
    CAPACITY_REJECTED = _NoopMetric()


# 批次 I 只提供旁路接口；未接入的生产者不能声称已采集。
# 固定标签集合，不接收 service / Workflow ID / 错误文本 / 文件路径。
if _AVAILABLE:
    ALERT_PENDING = Gauge("aiops_alert_pending", "待补偿聚合事件数，-1 表示未采集")
    ALERT_PENDING.set(-1)
    COMPENSATION_FAILURES = Counter("aiops_compensation_failures_total", "聚合补偿失败次数")
    RELEASE_CONFLICTS = Counter("aiops_release_conflicts_total", "发布资源冲突次数")
    ROLLBACK_FAILURES = Counter("aiops_rollback_failures_total", "回滚失败次数")
    COMPONENT_RESTARTS = Counter("aiops_component_restarts_total", "组件重启次数", ["component"])
    BACKUP_LAST_SUCCESS = Gauge("aiops_backup_last_success_timestamp_seconds", "最后成功且校验通过的备份，0 未知")
    BACKUP_OVERDUE = Gauge("aiops_backup_overdue", "超过26小时无成功备份或状态未知")
    CERT_EXPIRES = Gauge("aiops_certificate_expiry_timestamp_seconds", "当前服务证书到期时间，0 未知")
    CERT_RENEWAL_DUE = Gauge("aiops_certificate_renewal_due", "证书距到期不超过30天或状态未知")
    # 写命令 outbox（M2）：积压/最老等待/近 24h 失败——只反映投递排队，不代表业务审批结果
    COMMAND_BACKLOG = Gauge("aiops_workflow_commands_pending", "待投递写命令数")
    COMMAND_OLDEST_WAIT = Gauge("aiops_workflow_commands_oldest_wait_seconds", "最老待投递命令等待秒数")
    COMMAND_FAILED_24H = Gauge("aiops_workflow_commands_failed_24h", "近 24 小时投递失败的写命令数")
    # Temporal History 投影（M4）：未完成同步 / 事件断档 / 最大滞后
    HISTORY_INCOMPLETE = Gauge(
        "aiops_history_sync_incomplete", "尚未完成最终 History 同步的流程数"
    )
    HISTORY_GAPS = Gauge("aiops_history_sync_gaps", "检测到事件断档的流程数")
    HISTORY_LAG = Gauge("aiops_history_sync_lag_seconds", "未完成同步流程的最大滞后秒数")
else:  # pragma: no cover
    ALERT_PENDING = COMPENSATION_FAILURES = RELEASE_CONFLICTS = ROLLBACK_FAILURES = _NoopMetric()
    COMPONENT_RESTARTS = BACKUP_LAST_SUCCESS = BACKUP_OVERDUE = CERT_EXPIRES = CERT_RENEWAL_DUE = _NoopMetric()
    COMMAND_BACKLOG = COMMAND_OLDEST_WAIT = COMMAND_FAILED_24H = _NoopMetric()
    HISTORY_INCOMPLETE = HISTORY_GAPS = HISTORY_LAG = _NoopMetric()

_backup_success = 0.0
_certificate_expiry = 0.0
if _AVAILABLE:
    # 在抓取时计算，防止旧的“未过期”样本随时间失真；默认未知即告警。
    BACKUP_OVERDUE.set_function(lambda: float(_backup_success <= 0 or time.time() - _backup_success > 26 * 3600))
    CERT_RENEWAL_DUE.set_function(lambda: float(_certificate_expiry <= 0 or _certificate_expiry - time.time() <= 30 * 86400))


def set_alert_pending(count: int | None) -> None:
    """None/非法值恢复未采集状态，不能保留上次的零积压冒充正常。"""
    ALERT_PENDING.set(count if type(count) is int and count >= 0 else -1)


def set_command_state(*, pending: int, oldest_seconds: float, failed_24h: int) -> None:
    """写命令 outbox 状态（由 bff.commands 在变更/调度后刷新）。"""
    COMMAND_BACKLOG.set(max(0, int(pending)))
    COMMAND_OLDEST_WAIT.set(max(0.0, float(oldest_seconds)))
    COMMAND_FAILED_24H.set(max(0, int(failed_24h)))


def set_history_state(*, incomplete: int, gaps: int, lag_seconds: float) -> None:
    """Temporal History 投影完整性（M4）：由 projector 对账后刷新。"""
    HISTORY_INCOMPLETE.set(max(0, int(incomplete)))
    HISTORY_GAPS.set(max(0, int(gaps)))
    HISTORY_LAG.set(max(0.0, float(lag_seconds)))


def observe_compensation_failure() -> None:
    COMPENSATION_FAILURES.inc()


def observe_release_conflict() -> None:
    RELEASE_CONFLICTS.inc()


def observe_rollback_failure() -> None:
    ROLLBACK_FAILURES.inc()


def observe_component_restart(component: str) -> None:
    allowed = {"bff", "worker", "webhook", "caddy", "collector", "prober", "detector", "backup"}
    COMPONENT_RESTARTS.labels(component=component if isinstance(component, str) and component in allowed else "other").inc()


def set_backup_last_success(timestamp: float | None) -> None:
    """H 的已校验成功记录时间；失败不能写当前时间冒充成功。None 表示未知。"""
    global _backup_success
    valid = isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and 0 < timestamp <= time.time()
    _backup_success = float(timestamp) if valid else 0.0
    BACKUP_LAST_SUCCESS.set(_backup_success)


def set_certificate_expiry(timestamp: float | None) -> None:
    """F 的当前服务证书 notAfter（UTC epoch）；不在指标采集时读取私钥。"""
    global _certificate_expiry
    valid = isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and timestamp > 0
    _certificate_expiry = float(timestamp) if valid else 0.0
    CERT_EXPIRES.set(_certificate_expiry)


def available() -> bool:
    """prometheus_client 是否可用（测试/自检用）。"""
    return _AVAILABLE


def current_mode() -> str:
    try:
        return mode.mode()
    except Exception:  # noqa: BLE001 - 指标打点不得干扰主流程
        return "unknown"


# 标签洗白：外部输入（service 等）可能带高基数/异常字符，统一清洗并截断。
# 保留 / {} 等路由模板字符（Prometheus 标签值无字符集限制，保持 /api/flows/{wf_id} 可读）
_LABEL_UNSAFE = re.compile(r"[^a-zA-Z0-9_.:/{}-]")


def _label(value: str | None, *, fallback: str = "unknown") -> str:
    """标签洗白：外部输入（service 等）可能带高基数/异常字符，统一清洗并截断。"""
    text = str(value or "").strip()
    if not text:
        return fallback
    return _LABEL_UNSAFE.sub("_", text)[:64]


def observe_workflow_started(service: str) -> None:
    WORKFLOW_STARTED.labels(service=_label(service), mode=current_mode()).inc()


def observe_workflow_completed(service: str, stage: str, duration: float | None = None) -> None:
    normalized = _label(stage, fallback="UNKNOWN").upper()
    WORKFLOW_COMPLETED.labels(service=_label(service), stage=normalized, mode=current_mode()).inc()
    if duration is not None and duration >= 0:
        WORKFLOW_DURATION.labels(service=_label(service), mode=current_mode()).observe(duration)


def observe_fix_attempt(provider: str, degraded: bool, duration: float | None = None) -> None:
    provider_label = _label(provider, fallback="unknown")
    FIX_ATTEMPTS.labels(
        provider=provider_label, degraded=str(bool(degraded)).lower(), mode=current_mode()
    ).inc()
    if duration is not None and duration >= 0:
        FIX_DURATION.labels(provider=provider_label, mode=current_mode()).observe(duration)


def observe_sandbox_run(passed: bool, duration: float | None = None) -> None:
    SANDBOX_RUNS.labels(passed=str(bool(passed)).lower(), mode=current_mode()).inc()
    if duration is not None and duration >= 0:
        SANDBOX_DURATION.labels(mode=current_mode()).observe(duration)


def observe_sandbox_static(passed: bool, verification: str) -> None:
    """静态验收计数（CONTRACT_TEST / STATIC_ONLY），与真实沙箱执行分开统计。

    真实沙箱通过率只允许使用 ``aiops_sandbox_runs_total``；静态验收单列，
    不得伪装成「测试通过」参与发布质量门槛。
    """
    SANDBOX_STATIC_RUNS.labels(
        passed=str(bool(passed)).lower(),
        verification=_label(verification, fallback="unknown"),
        mode=current_mode(),
    ).inc()


def observe_canary_deploy(healthy: bool, error_rate: float | None = None) -> None:
    CANARY_DEPLOYS.labels(healthy=str(bool(healthy)).lower(), mode=current_mode()).inc()
    if error_rate is not None and error_rate >= 0:
        CANARY_ERROR_RATE.labels(mode=current_mode()).observe(error_rate)


def observe_gate_event(gate: str, decision: str) -> None:
    GATE_EVENTS.labels(
        gate=_label(gate), decision=_label(decision), mode=current_mode()
    ).inc()


def observe_alert_aggregated(service: str) -> None:
    """同故障域聚合归并一条告警（webhook 接入层，P2-1 修复风暴防护）。"""
    AGGREGATED_ALERTS.labels(service=_label(service), mode=current_mode()).inc()


def set_capacity_state(resource: str, in_use: int, waiting: int) -> None:
    """容量占用与队列深度（M6）：进程内信号量的即时视图。

    ``waiting`` 是等待该资源的任务数，不等于跨副本总队列；多 Worker 部署必须
    接入集中限流后再据此告警。
    """
    resource_label = _label(resource, fallback="unknown")
    mode = current_mode()
    CAPACITY_IN_USE.labels(resource=resource_label, mode=mode).set(max(0, int(in_use)))
    CAPACITY_QUEUE.labels(resource=resource_label, mode=mode).set(max(0, int(waiting)))


def observe_capacity_wait(resource: str, seconds: float) -> None:
    if seconds >= 0:
        CAPACITY_WAIT.labels(
            resource=_label(resource, fallback="unknown"), mode=current_mode()
        ).observe(seconds)


def observe_capacity_run(resource: str, seconds: float) -> None:
    if seconds >= 0:
        CAPACITY_RUN.labels(
            resource=_label(resource, fallback="unknown"), mode=current_mode()
        ).observe(seconds)


def observe_capacity_rejected(resource: str, reason: str) -> None:
    """等待超时/取消计数：拒绝必须可观测，不能静默丢单。"""
    CAPACITY_REJECTED.labels(
        resource=_label(resource, fallback="unknown"),
        reason=_label(reason, fallback="unknown"),
        mode=current_mode(),
    ).inc()


def observe_bff_request(method: str, path: str, status: int, duration: float) -> None:
    method_label = _label(method, fallback="GET").upper()
    path_label = _label(path, fallback="unmatched")
    BFF_REQUESTS.labels(method=method_label, path=path_label, status=str(int(status))).inc()
    BFF_REQUEST_DURATION.labels(method=method_label, path=path_label).observe(max(0.0, duration))


def _escape_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_log_ship_health() -> bytes:
    """日志采集心跳（M1）：抓取时读取，避免把进程存活当成日志供给正常。

    指标按 service 标签输出；心跳缺失/过期/失败时 ``aiops_log_ship_healthy=0``。
    """
    try:
        from . import log_health

        items = log_health.snapshot()
    except Exception as exc:  # noqa: BLE001 - 指标旁路不得影响主服务
        log.warning("[metrics] 日志采集心跳读取失败：%s", exc)
        return b"# aiops_log_ship_health unavailable\n"
    if not items:
        return b"# aiops_log_ship_health unavailable (no heartbeat)\n"

    rows = [
        "# HELP aiops_log_ship_last_success_timestamp_seconds 最近一次成功采集（UTC 秒）",
        "# TYPE aiops_log_ship_last_success_timestamp_seconds gauge",
    ]
    for item in items:
        service = _escape_label(item.get("service") or "unknown")
        stamp = 0.0
        try:
            stamp = datetime.fromisoformat(str(item.get("last_success_at"))).timestamp()
        except (ValueError, TypeError):
            stamp = 0.0
        rows.append(
            f'aiops_log_ship_last_success_timestamp_seconds{{service="{service}"}} {stamp:.0f}'
        )
    rows.extend(
        [
            "# HELP aiops_log_ship_last_pushed_lines 最近一次成功采集推送行数",
            "# TYPE aiops_log_ship_last_pushed_lines gauge",
        ]
    )
    for item in items:
        service = _escape_label(item.get("service") or "unknown")
        rows.append(
            f'aiops_log_ship_last_pushed_lines{{service="{service}"}} '
            f'{int(item.get("last_pushed_lines") or 0)}'
        )
    rows.extend(
        [
            "# HELP aiops_log_ship_consecutive_failures 连续推送失败次数",
            "# TYPE aiops_log_ship_consecutive_failures gauge",
        ]
    )
    for item in items:
        service = _escape_label(item.get("service") or "unknown")
        rows.append(
            f'aiops_log_ship_consecutive_failures{{service="{service}"}} '
            f'{int(item.get("consecutive_failures") or 0)}'
        )
    rows.extend(
        [
            "# HELP aiops_log_ship_healthy 采集心跳新鲜且无失败/警告（1=健康，0=不健康）",
            "# TYPE aiops_log_ship_healthy gauge",
        ]
    )
    for item in items:
        service = _escape_label(item.get("service") or "unknown")
        rows.append(
            f'aiops_log_ship_healthy{{service="{service}"}} '
            f'{1 if item.get("healthy") else 0}'
        )
    return ("\n".join(rows) + "\n").encode()


def render() -> tuple[bytes, str]:
    """渲染指标文本，返回 (payload, content_type)。"""
    if not _AVAILABLE:
        return (
            b"# prometheus_client not installed; metrics disabled\n",
            "text/plain; charset=utf-8",
        )
    return generate_latest() + _render_log_ship_health(), CONTENT_TYPE_LATEST


def start_worker_server() -> int:
    """worker 进程独立指标服务；返回实际端口（0 = 未启动/失败，不影响 worker 主流程）。"""
    if not _AVAILABLE:
        return 0
    try:
        port = int(os.environ.get("AIOPS_METRICS_PORT", "9091") or 0)
    except ValueError:
        return 0
    if not 1 <= port <= 65535:
        return 0
    try:
        from prometheus_client import start_http_server

        start_http_server(port, addr="127.0.0.1")
        log.info("[metrics] worker 指标服务已启动：127.0.0.1:%d/metrics", port)
        return port
    except Exception as exc:  # noqa: BLE001 - 端口占用等不阻塞 worker
        log.warning("[metrics] worker 指标服务启动失败（端口 %d）：%s", port, exc)
        return 0
