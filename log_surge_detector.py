"""错误日志突增检测器（WP2 任务 3 交付物）：环比基线 → Alertmanager 标准告警。

检测逻辑（每 interval 秒一轮，对每个 service）：
    recent   = 近 window 分钟 ERROR 日志行数（Loki count_over_time）
    baseline = 1 小时前同一窗口的 ERROR 行数（环比基线）
    is_surge(recent, baseline, min_lines, factor) → 超限则注入标准告警：
        POST <alertmanager>/api/v2/alerts
        labels: {alertname: LogErrorSurge, severity: critical, service: <svc>,
                 aiops_demo: "true", alert_id: logsurge-<svc>-<HHMMSS>}
        （aiops_demo 标签经 Alertmanager 子路由自动转发至 AIOps 接入服务）
    防抖：同 service 在 cooldown 秒内仅告警一次。

service 来源（每轮解析 → 热生效）：
    --services 显式指定时按逗号分隔解析；缺省时从「被监控应用」清单
    （bff.monitored_apps.log_targets()：enabled 且配置了 log_path 的条目）
    动态提取，控制台新增/停用被监控应用无需重启本检测器。

用法：
    python log_surge_detector.py --once              # 单轮（调试/验证）
    python log_surge_detector.py                     # 守护运行（默认 15s 间隔）
    python log_surge_detector.py --services nl2sql   # 显式指定（调试用）
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.request

from aiops_agent import config as _config  # noqa: F401 - 触发工程根 .env 加载（与其他入口一致）
from aiops_agent import logs

logger = logging.getLogger("aiops.surge")

DEFAULT_ALERTMANAGER = "http://localhost:9093"


def resolve_services(explicit: str | None = None) -> list[str]:
    """解析本轮待检测的 service 列表（每轮调用 → 清单改动热生效）。

    显式传入（--services）时按逗号分隔解析；缺省时从「被监控应用」清单
    （bff.monitored_apps.log_targets()）动态提取 service 并去重排序。
    清单不可读时不中断守护循环，返回空列表等待下一轮。
    """
    if explicit:
        return [s.strip() for s in explicit.split(",") if s.strip()]
    try:
        from bff import monitored_apps

        return sorted({app["service"] for app in monitored_apps.log_targets() if app.get("service")})
    except Exception as exc:  # noqa: BLE001 - 清单不可读时本轮跳过，不中断检测循环
        logger.warning("读取被监控应用清单失败（本轮跳过）：%s", exc)
        return []


def build_surge_alert(service: str, recent: float, baseline: float) -> list[dict]:
    """构造 Alertmanager v2 API 告警载荷（纯函数，供单测）。"""
    alert_id = f"logsurge-{service}-{time.strftime('%H%M%S')}"
    return [
        {
            "labels": {
                "alertname": "LogErrorSurge",
                "severity": "critical",
                "service": service,
                "aiops_demo": "true",
                "alert_id": alert_id,
            },
            "annotations": {
                "summary": f"{service} 错误日志突增",
                "description": (
                    f"近 {int(recent)} 条 ERROR（1 小时前基线 {int(baseline)} 条），"
                    "超过环比阈值，已触发自动根因分析流程。"
                ),
            },
        }
    ]


def inject_alert(payload: list[dict], alertmanager: str = DEFAULT_ALERTMANAGER) -> None:
    """向 Alertmanager API 注入告警（由其路由至 AIOps 接入服务）。"""
    req = urllib.request.Request(
        f"{alertmanager.rstrip('/')}/api/v2/alerts",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"告警注入失败: HTTP {resp.status}")


def check_once(
    services: list[str],
    window_minutes: int,
    min_lines: int,
    factor: float,
    alertmanager: str,
    cooldown: int = 0,
    last_alert: dict[str, float] | None = None,
) -> dict[str, dict]:
    """单轮检测：返回 {service: {"recent","baseline","surge"}}；命中且过冷却期则注入告警。"""
    last_alert = last_alert if last_alert is not None else {}
    now_ns = time.time_ns()
    hour_ago_ns = now_ns - 3600 * 1_000_000_000
    results: dict[str, dict] = {}
    for service in services:
        query = f'sum(count_over_time({{service="{service}", level="error"}}[{window_minutes}m]))'
        recent = logs.query_metric(query)
        baseline = logs.query_metric(query, at_ns=hour_ago_ns)
        surge = logs.is_surge(recent, baseline, min_lines, factor)
        results[service] = {"recent": recent, "baseline": baseline, "surge": surge}
        logger.info(
            "[%s] recent=%d baseline=%d → %s",
            service,
            int(recent),
            int(baseline),
            "突增！" if surge else "正常",
        )
        if not surge:
            continue
        if now_ns - last_alert.get(service, 0) < cooldown * 1_000_000_000:
            logger.info("[%s] 突增持续，冷却期内跳过重复告警", service)
            continue
        inject_alert(build_surge_alert(service, recent, baseline), alertmanager)
        last_alert[service] = now_ns
        logger.info("[%s] 已注入告警 LogErrorSurge（→ Alertmanager → AIOps 接入服务）", service)
    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="错误日志突增检测器（→ Alertmanager）")
    parser.add_argument("--services", default=None, help="服务列表，逗号分隔（缺省：按「被监控应用」清单动态取）")
    parser.add_argument("--interval", type=int, default=15, help="轮询间隔秒（默认 15）")
    parser.add_argument("--window", type=int, default=1, help="统计窗口分钟（默认 1）")
    parser.add_argument("--min-lines", type=int, default=100, help="绝对门槛行数（默认 100）")
    parser.add_argument("--factor", type=float, default=3.0, help="环比倍数（默认 3）")
    parser.add_argument("--cooldown", type=int, default=300, help="同服务告警冷却秒（默认 300）")
    parser.add_argument("--alertmanager", default=DEFAULT_ALERTMANAGER)
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出（调试/验证）")
    args = parser.parse_args()

    last_alert: dict[str, float] = {}
    logger.info("突增检测器启动：services=%s interval=%ds window=%dm min_lines=%d factor=%.1f",
                args.services or "（清单动态）", args.interval, args.window, args.min_lines, args.factor)
    try:
        while True:
            try:
                services = resolve_services(args.services)  # 每轮解析 → 清单改动热生效
                if not services:
                    logger.info("本轮无待检测服务（清单为空或不可读），等待下一轮 ...")
                else:
                    check_once(
                        services,
                        args.window,
                        args.min_lines,
                        args.factor,
                        args.alertmanager,
                        cooldown=args.cooldown,
                        last_alert=last_alert,
                    )
            except Exception as exc:  # noqa: BLE001 - 单轮故障（Loki/Alertmanager 瞬态 5xx）不中断守护循环
                if args.once:
                    raise  # 调试模式保留异常传播，非零退出码可被脚本感知
                logger.warning("本轮检测失败（%s: %s），%ds 后重试", type(exc).__name__, exc, args.interval)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("突增检测器已停止")


if __name__ == "__main__":
    main()
