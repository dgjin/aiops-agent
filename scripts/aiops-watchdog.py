#!/usr/bin/env python3
"""组件守护（P0-3）：巡检 6 个常驻组件，异常退出自动拉起，崩溃风暴自动冷却。

背景：demo-up.sh 只负责启动、不负责守护——任一常驻组件（worker/BFF/告警接入/可用性巡检/
日志采集/突增检测）崩溃后链路静默断裂。本脚本按固定周期巡检（pgrep 判活），发现缺失即按
demo-up.sh 同款命令拉起，并写入日志。

不变量：
    - 档位锁定（P1）：守护启动时解析一次生效档位（环境变量 > .env > production，与
      config.load_dotenv(override=False) 语义一致），拉起的组件显式继承该档位；
      demo 档且未显式配置策略时自动补齐 ./demo-policy.yaml（与 demo-up.sh 成对语义），
      防止 .env 与启动环境不一致造成「档位混用」；
    - 单实例锁（P1）：常驻模式经 data/aiops-watchdog.lock（fcntl.flock）防双守护——
      launchd 托管与手动启动并存时，后到者检测到锁占用即退出（退出码 0，配合 launchd
      KeepAlive=SuccessfulExit:false 不触发重启），由在跑实例负责；
    - 组件以独立会话（start_new_session）脱离进程组运行，语义同手动 nohup，日志追加到各自
      *.log（与 demo-up.sh 相同文件）；
    - 停止全部组件的正确姿势是 `bash scripts/demo-up.sh down`（先停本守护再停组件，
      否则组件会被守护原地复活）；launchd 托管时用 `bash scripts/launchd-install.sh uninstall`。

用法：
    python3 scripts/aiops-watchdog.py              # 常驻守护（建议 nohup 后台运行）
    python3 scripts/aiops-watchdog.py --once       # 单轮巡检后退出（排查/CI 用）
    python3 scripts/aiops-watchdog.py --status     # 只看存活状态（有任何缺失退出码 1）
    bash scripts/launchd-install.sh install        # 开机自启（launchd 用户级托管，可卸载）
    环境变量：AIOPS_WATCHDOG_INTERVAL（巡检周期秒，默认 30）；
             AIOPS_WATCHDOG_SURGE_ARGS（突增检测器拉起参数覆盖，默认与 demo-up.sh 演示值一致）
"""

from __future__ import annotations

import argparse
import fcntl
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

# 单实例锁（P1）：常驻模式独占，防止 launchd 托管与手动启动双守护并存
LOCK_PATH = ROOT / "data" / "aiops-watchdog.lock"


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
            # 监听全部网卡（0.0.0.0）：本机 127.0.0.1 与局域网地址均可访问控制台
            (str(PY), "-m", "uvicorn", "bff.app:app", "--host", "0.0.0.0", "--port", "8600"),
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


def _read_env_mode() -> str | None:
    """工程根 .env 中的 AIOPS_MODE（惰性读取：测试以 importlib 加载本文件，模块级禁止 IO）。"""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() == "AIOPS_MODE":
                value = raw.strip().strip("'\"")
                if value:
                    return value
    except OSError:
        return None
    return None


def _effective_mode() -> tuple[str, str]:
    """生效档位与来源：环境变量 > .env > production（与 config.load_dotenv 优先级一致）。"""
    env_mode = os.environ.get("AIOPS_MODE")
    if env_mode:
        return env_mode, "环境变量"
    file_mode = _read_env_mode()
    if file_mode:
        return file_mode, ".env"
    return "production", "默认值"


def _component_env() -> dict[str, str]:
    """组件子进程环境：显式锁定生效档位；demo 档未配策略时补齐 demo-policy（同 demo-up.sh）。"""
    env = dict(os.environ)
    mode, _ = _effective_mode()
    env["AIOPS_MODE"] = mode
    if mode == "demo" and not env.get("AIOPS_POLICY_PATH"):
        env["AIOPS_POLICY_PATH"] = "./demo-policy.yaml"
    return env


def acquire_lock(logger: logging.Logger, path: Path | None = None) -> object | None:
    """单实例锁：返回持锁文件对象；被占用返回 None；锁文件不可建时返回 True 哨兵（放行并告警）。"""
    lock_path = path or LOCK_PATH
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        logger.warning("单实例锁不可用（%s）：%s —— 跳过加锁继续运行", lock_path, exc)
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


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
            env=_component_env(),
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
    # 常驻模式：单实例锁 + 档位锁定（P1/P2）
    lock = acquire_lock(logger)
    if lock is None:
        logger.warning("已有组件守护在运行（%s 被占用），本进程退出（组件保持现状）", LOCK_PATH)
        return
    mode, source = _effective_mode()
    logger.info("档位锁定：%s（来源 %s）——后续拉起的组件统一使用该档位", mode, source)
    file_mode = _read_env_mode()
    if file_mode and file_mode != mode:
        logger.warning(
            "档位不一致：生效 %s（%s）≠ .env %s —— 拉起的组件将使用生效档位（有意覆盖可忽略）",
            mode, source, file_mode,
        )
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
