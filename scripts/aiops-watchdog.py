#!/usr/bin/env python3
"""组件守护（P0-3）：巡检 6 个常驻组件，异常退出自动拉起，崩溃风暴自动冷却。

背景：demo-up.sh 只负责启动、不负责守护——任一常驻组件（worker/BFF/告警接入/可用性巡检/
日志采集/突增检测）崩溃后链路静默断裂。本脚本按固定周期巡检（pgrep 判活），发现缺失即按
demo-up.sh 同款命令拉起，并写入日志。

不变量：
    - 不覆盖任何环境变量：完全继承启动时的环境（demo-up.sh 以 demo 档启动本守护，则被拉起的
      组件保持 demo 档；单独启动时组件各自加载工程根 .env，与手动启动行为一致）；
    - 组件以独立会话（start_new_session）脱离进程组运行，语义同手动 nohup，日志追加到各自
      *.log（与 demo-up.sh 相同文件）；
    - 停止全部组件的正确姿势是 `bash scripts/demo-up.sh down`（先停本守护再停组件，
      否则组件会被守护原地复活）。

用法：
    python3 scripts/aiops-watchdog.py              # 常驻守护（建议 nohup 后台运行）
    python3 scripts/aiops-watchdog.py --once       # 单轮巡检后退出（排查/CI 用）
    python3 scripts/aiops-watchdog.py --status     # 只看存活状态（有任何缺失退出码 1）
    环境变量：AIOPS_WATCHDOG_INTERVAL（巡检周期秒，默认 30）；
             AIOPS_WATCHDOG_SURGE_ARGS（突增检测器拉起参数覆盖，默认与 demo-up.sh 演示值一致）
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
if not PY.exists():  # 无 venv 时退回当前解释器（与 security-scan.sh 同款降级）
    PY = Path(sys.executable)

DEFAULT_INTERVAL = 30.0
# 与 demo-up.sh 演示灵敏度一致（生产建议 --min-lines 100 --factor 3，可用环境变量覆盖）
DEFAULT_SURGE_ARGS = "--min-lines 5 --factor 2 --cooldown 300"


@dataclass(frozen=True)
class Component:
    name: str
    pattern: str          # pgrep -f 判活模式（与 demo-up.sh 一致）
    cmd: tuple[str, ...]  # 拉起命令
    log_path: Path        # 日志文件（追加）


def build_components() -> tuple[Component, ...]:
    surge_args = (os.environ.get("AIOPS_WATCHDOG_SURGE_ARGS") or DEFAULT_SURGE_ARGS).split()
    return (
        Component(
            "worker", "aiops_agent.worker",
            (str(PY), "-m", "aiops_agent.worker"), ROOT / "worker.log",
        ),
        Component(
            "bff", "uvicorn bff.app:app",
            (str(PY), "-m", "uvicorn", "bff.app:app", "--host", "127.0.0.1", "--port", "8600"),
            ROOT / "bff.log",
        ),
        Component(
            "alert_webhook", "alert_webhook.py",
            (str(PY), "alert_webhook.py", "--port", "8099"), ROOT / "alert_webhook.log",
        ),
        Component(
            "app_prober", "app_prober.py",
            (str(PY), "app_prober.py"), ROOT / "app_prober.log",
        ),
        Component(
            "ship_app_logs", "ship_app_logs.py",
            (str(PY), "ship_app_logs.py", "--follow"), ROOT / "ship_app_logs.log",
        ),
        Component(
            "log_surge_detector", "log_surge_detector.py",
            (str(PY), "log_surge_detector.py", *surge_args), ROOT / "log_surge_detector.log",
        ),
    )


def is_running(pattern: str) -> bool:
    """pgrep -f 判活（与 demo-up.sh 的 running() 等价）。"""
    return (
        subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def start_component(comp: Component) -> int:
    """以独立会话拉起组件（语义同 nohup：stdin 关闭、日志追加），返回 pid。"""
    with open(comp.log_path, "ab") as log:
        proc = subprocess.Popen(
            comp.cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


class RestartGuard:
    """崩溃风暴保护：滑动窗口内重启次数达到上限即进入冷却，冷却期满自动解除并清零历史。"""

    def __init__(
        self, window_s: float = 900.0, max_restarts: int = 5, cooldown_s: float = 600.0
    ) -> None:
        self._window_s = window_s
        self._max = max_restarts
        self._cooldown_s = cooldown_s
        self._history: dict[str, list[float]] = {}
        self._cooldown_until: dict[str, float] = {}

    def allow(self, name: str, now: float) -> bool:
        until = self._cooldown_until.get(name)
        if until is None:
            return True
        if now >= until:
            self._cooldown_until.pop(name, None)
            self._history.pop(name, None)
            return True
        return False

    def record(self, name: str, now: float) -> None:
        history = [t for t in self._history.get(name, []) if now - t < self._window_s]
        history.append(now)
        self._history[name] = history
        if len(history) >= self._max:
            self._cooldown_until[name] = now + self._cooldown_s

    def cooldown_remaining(self, name: str, now: float) -> float:
        return max(0.0, self._cooldown_until.get(name, 0.0) - now)


def check_once(
    components: tuple[Component, ...],
    guard: RestartGuard,
    now: float,
    logger: logging.Logger,
    dry_run: bool = False,
    running_fn=is_running,
    start_fn=start_component,
) -> int:
    """单轮巡检：缺失的组件按守卫策略拉起，返回本轮拉起数（running_fn/start_fn 可注入，供单测）。"""
    restarted = 0
    for comp in components:
        if running_fn(comp.pattern):
            continue
        if not guard.allow(comp.name, now):
            logger.warning(
                "%s 缺失，但处于崩溃冷却中（剩余 %.0fs），本轮跳过",
                comp.name,
                guard.cooldown_remaining(comp.name, now),
            )
            continue
        if dry_run:
            logger.info("%s 缺失（dry-run，未拉起）", comp.name)
            continue
        pid = start_fn(comp)
        guard.record(comp.name, now)
        restarted += 1
        logger.warning("%s 未在运行，已自动拉起（pid %d），日志 %s", comp.name, pid, comp.log_path)
    return restarted


def run_status(components: tuple[Component, ...], logger: logging.Logger) -> int:
    """输出各组件存活状态；有缺失返回 1。"""
    missing = 0
    for comp in components:
        if is_running(comp.pattern):
            logger.info("[运行中] %s", comp.name)
        else:
            logger.warning("[已停止] %s", comp.name)
            missing += 1
    return 1 if missing else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AIOps 组件守护：巡检 6 个常驻组件，异常自动拉起（P0-3）"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("AIOPS_WATCHDOG_INTERVAL", DEFAULT_INTERVAL)),
        help="巡检周期秒（默认 30，或环境变量 AIOPS_WATCHDOG_INTERVAL）",
    )
    parser.add_argument("--once", action="store_true", help="单轮巡检后退出（排查/CI 用）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不拉起（配合 --once）")
    parser.add_argument("--status", action="store_true", help="只输出各组件存活状态（有缺失退出码 1）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logger = logging.getLogger("aiops.watchdog")
    components = build_components()

    if args.status:
        sys.exit(run_status(components, logger))

    guard = RestartGuard()
    logger.info(
        "组件守护已启动：周期 %.0fs，监控 %d 个组件（%s）",
        args.interval,
        len(components),
        "、".join(c.name for c in components),
    )
    if args.once:
        restarted = check_once(components, guard, time.monotonic(), logger, dry_run=args.dry_run)
        logger.info("单轮巡检完成：拉起 %d 个组件", restarted)
        return
    try:
        while True:
            try:
                check_once(components, guard, time.monotonic(), logger)
            except Exception:  # noqa: BLE001 - 单轮故障不中断守护循环（与 ship/surge 同款语义）
                logger.exception("本轮巡检异常，等待下一轮")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("组件守护已退出（组件保持运行）")


if __name__ == "__main__":
    main()
