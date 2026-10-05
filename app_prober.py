"""被监控应用可用性巡检（控制塔主动巡检）：探测 → 连续失败 → 注入标准告警 → 自动修复流程。

巡检逻辑（每 interval 秒一轮，对 monitored_apps 清单中 enabled 的每一项）：
    探测 清单 URL + health_path（Manifest 预填，默认根地址）；判定与控制台实时探测一致：
    收到任何 HTTP 响应即在线的；连接失败/超时才不可达；配置了页面关键字的条目还要求响应内容
    包含该关键字，否则同样判失败——覆盖「端口活着但页面白屏」）
    - 连续失败达到 threshold 次 → 向 Alertmanager 注入标准告警：
        POST <alertmanager>/api/v2/alerts
        labels: {alertname: AppUnreachable, severity: critical, service: <svc>,
                 aiops_demo: "true", alert_id: probe-<app_id>-<fail_since 秒级时间戳>}
        （aiops_demo 标签经 Alertmanager 子路由自动转发至 AIOps 接入服务 → 幂等启动修复流程）
    - 恢复（或条目被停用/移除）→ 注入 resolved（同 labelset + endsAt 置过去），复位状态
    防抖：同一次故障周期（fail_since 不变）只注入一次告警；恢复后再次故障自然生成新 alert_id。

状态落盘 data/probe_status.json（原子替换），BFF 在「被监控应用」页透出巡检结果。

用法：
    python app_prober.py --once      # 单轮（调试/验证）
    python app_prober.py             # 守护运行（默认 15s 间隔，连续 3 次失败触发）

环境变量（CLI 参数优先）：AIOPS_PROBE_INTERVAL_SECONDS / AIOPS_PROBE_FAILURE_THRESHOLD /
AIOPS_PROBE_TIMEOUT_SECONDS / AIOPS_ALERTMANAGER_URL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from aiops_agent import config as _config  # noqa: F401 - 触发工程根 .env 加载（与其他入口一致）
from bff import monitored_apps as store
from bff.aggregator import probe_monitored_app

logger = logging.getLogger("aiops.prober")

DEFAULT_ALERTMANAGER = "http://localhost:9093"
STATUS_FILE_NAME = "probe_status.json"


def _iso(ts: float) -> str:
    """秒级 UTC ISO 时间（与清单存储的时间格式一致）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def alert_id_for(app_id: str, fail_since: float) -> str:
    """同一次故障周期内稳定的幂等键；恢复后再次故障自动生成新键。"""
    return f"probe-{app_id}-{int(fail_since)}"


def build_unreachable_alert(app: dict, *, failures: int, error: str, fail_since: float) -> list[dict]:
    """构造「服务不可达」告警载荷（Alertmanager v2 API；纯函数供单测）。"""
    name = app.get("name") or app.get("id")
    url = app.get("url")
    return [
        {
            "labels": {
                "alertname": "AppUnreachable",
                "severity": "critical",
                "service": str(app.get("service") or store.DEFAULT_SERVICE),
                "aiops_demo": "true",
                "alert_id": alert_id_for(app["id"], fail_since),
            },
            "annotations": {
                "summary": f"{name} 服务不可达",
                "description": (
                    f"{url} 连续 {failures} 次探测失败（最近错误：{error}），"
                    "已触发自动修复流程。"
                ),
            },
        }
    ]


def build_resolved_alert(labels: dict) -> list[dict]:
    """构造 resolved 载荷：同 labelset（同 fingerprint）+ endsAt 置过去即立即置为 resolved。"""
    ends_at = datetime.fromtimestamp(time.time() - 1, tz=timezone.utc).isoformat(timespec="seconds")
    return [{"labels": dict(labels), "annotations": {}, "endsAt": ends_at}]


def inject_alerts(payload: list[dict], alertmanager: str) -> None:
    """向 Alertmanager API 注入告警（与 log_surge_detector 同款约定，由其路由至 AIOps 接入服务）。"""
    req = urllib.request.Request(
        f"{alertmanager.rstrip('/')}/api/v2/alerts",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"告警注入失败: HTTP {resp.status}")


def advance(record: dict, ok: bool, *, ts: float, threshold: int) -> str | None:
    """推进单应用巡检状态机（原地更新 record），返回应执行的动作：'alert' / 'resolve' / None。"""
    record["last_check_at"] = _iso(ts)
    if ok:
        record["ok"] = True
        record["failures"] = 0
        record["fail_since"] = None
        record["last_ok_at"] = record["last_check_at"]
        if record.get("alerted"):
            record["alerted"] = False  # 调用方负责注入 resolved
            return "resolve"
        return None
    record["ok"] = False
    record["failures"] = int(record.get("failures") or 0) + 1
    if record["failures"] == 1:
        record["fail_since"] = ts
    if not record.get("alerted") and record["failures"] >= threshold:
        record["alerted"] = True
        record["alerted_at"] = record["last_check_at"]
        record["alert_id"] = alert_id_for(record["id"], record["fail_since"])
        return "alert"
    return None


def _resolve_pending(record: dict, alertmanager: str) -> None:
    """注入 resolved 收尾 Alertmanager 中未决告警（失败不影响巡检主流程）。"""
    labels = record.get("alert_labels")
    record["alert_labels"] = None
    if not labels:
        return
    try:
        inject_alerts(build_resolved_alert(labels), alertmanager)
        logger.info("[%s] 已注入 resolved（%s 恢复在线）", record.get("name"), record.get("url"))
    except Exception as exc:  # noqa: BLE001 - resolved 失败仅告警留痕，等下轮或由 Alertmanager 侧过期
        logger.warning("[%s] resolved 注入失败（不影响巡检）：%s", record.get("name"), exc)


def run_round(
    state: dict,
    *,
    probe_fn=probe_monitored_app,
    timeout: float = 2.0,
    threshold: int = 3,
    alertmanager: str = DEFAULT_ALERTMANAGER,
    ts: float | None = None,
) -> dict:
    """单轮巡检：对全部启用项并发探测并推进状态机；``state`` 为跨轮内存态（原地更新并返回）。"""
    now = ts if ts is not None else time.time()
    apps = [app for app in store.list_all() if app.get("enabled")]
    current_ids = {app["id"] for app in apps}

    # 清单中被移除/停用的条目：如有未决告警先发 resolved（避免 Alertmanager 挂着过期告警）
    for stale_id in [app_id for app_id in state if app_id not in current_ids]:
        _resolve_pending(state.pop(stale_id), alertmanager)

    with ThreadPoolExecutor(max_workers=8) as pool:
        # keyword / health_path 透传（未配置为空串 → 纯连接级探测、根路径，与旧行为一致）
        results = [
            pool.submit(
                probe_fn,
                app.get("url"),
                timeout=timeout,
                keyword=str(app.get("probe_keyword") or ""),
                health_path=str(app.get("health_path") or ""),
            )
            for app in apps
        ]
        probes = [future.result() for future in results]

    for app, result in zip(apps, probes):
        record = state.setdefault(app["id"], {"id": app["id"]})
        record["name"] = app.get("name")
        record["url"] = app.get("url")
        record["service"] = app.get("service") or store.DEFAULT_SERVICE
        if result.get("running"):
            record["status_code"] = result.get("status_code")
            record["latency_ms"] = result.get("latency_ms")
            record["last_error"] = ""
        else:
            record["last_error"] = str(result.get("error") or "不可达")

        action = advance(record, bool(result.get("running")), ts=now, threshold=threshold)
        if action == "alert":
            payload = build_unreachable_alert(
                app,
                failures=record["failures"],
                error=record["last_error"],
                fail_since=record["fail_since"],
            )
            try:
                inject_alerts(payload, alertmanager)
                record["alert_labels"] = payload[0]["labels"]
                logger.warning(
                    "[%s] %s 连续 %d 次不可达 → 已注入告警 %s（→ Alertmanager → AIOps 修复流程）",
                    record.get("name"),
                    record.get("url"),
                    record["failures"],
                    record["alert_id"],
                )
            except Exception as exc:  # noqa: BLE001 - 注入失败复位，下轮重试
                record["alerted"] = False
                logger.warning("[%s] 告警注入失败（下轮重试）：%s", record.get("name"), exc)
        elif action == "resolve":
            _resolve_pending(record, alertmanager)

        logger.info(
            "[%s] 探测 %s → %s",
            record.get("name"),
            record.get("url"),
            (
                f"在线 {record.get('status_code')}（{record.get('latency_ms')}ms）"
                if result.get("running")
                else f"失败（连续 {record['failures']} 次，阈值 {threshold}）：{record['last_error']}"
            ),
        )
    return state


def write_status(path: Path, state: dict) -> None:
    """原子写巡检状态（BFF 读取展示；临时文件 + 替换，读侧永远拿到完整 JSON）。"""
    payload = {"updated_at": _iso(time.time()), "apps": state}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="被监控应用可用性巡检（→ Alertmanager → AIOps）")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("AIOPS_PROBE_INTERVAL_SECONDS", "15")),
        help="巡检间隔秒（默认 15，或环境变量 AIOPS_PROBE_INTERVAL_SECONDS）",
    )
    parser.add_argument(
        "--failures",
        type=int,
        default=int(os.environ.get("AIOPS_PROBE_FAILURE_THRESHOLD", "3")),
        help="连续失败触发阈值（默认 3，或环境变量 AIOPS_PROBE_FAILURE_THRESHOLD）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("AIOPS_PROBE_TIMEOUT_SECONDS", "2")),
        help="单次探测超时秒（默认 2，或环境变量 AIOPS_PROBE_TIMEOUT_SECONDS）",
    )
    parser.add_argument(
        "--alertmanager",
        default=os.environ.get("AIOPS_ALERTMANAGER_URL", DEFAULT_ALERTMANAGER),
        help="Alertmanager 地址（默认 http://localhost:9093，或环境变量 AIOPS_ALERTMANAGER_URL）",
    )
    parser.add_argument(
        "--status-file",
        default=str(store.DATA_DIR / STATUS_FILE_NAME),
        help=f"巡检状态落盘路径（默认 {store.DATA_DIR / STATUS_FILE_NAME}）",
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出（调试/验证）")
    args = parser.parse_args()

    state: dict = {}
    logger.info(
        "可用性巡检启动：interval=%ds 连续失败阈值=%d 探测超时=%.1fs 告警目标=%s",
        args.interval,
        args.failures,
        args.timeout,
        args.alertmanager,
    )
    try:
        while True:
            try:
                run_round(
                    state,
                    timeout=args.timeout,
                    threshold=args.failures,
                    alertmanager=args.alertmanager,
                )
                write_status(Path(args.status_file), state)
            except Exception as exc:  # noqa: BLE001 - 单轮故障（如存储层瞬断）不中断守护循环
                if args.once:
                    raise  # 调试模式保留异常传播，非零退出码可被脚本感知
                logger.warning("本轮巡检失败（%s: %s），%ds 后重试", type(exc).__name__, exc, args.interval)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("可用性巡检已停止")


if __name__ == "__main__":
    main()
