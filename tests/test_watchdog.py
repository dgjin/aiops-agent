"""组件守护单测（P0-3）：崩溃冷却策略与单轮巡检编排（注入假判活/假拉起，零外部依赖）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

scripts/ 非 Python 包目录，按文件路径加载被测模块（与 python3 scripts/aiops-watchdog.py 运行一致）。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "aiops_watchdog", Path(__file__).resolve().parent.parent / "scripts" / "aiops-watchdog.py"
)
assert _SPEC is not None and _SPEC.loader is not None
watchdog = importlib.util.module_from_spec(_SPEC)
sys.modules["aiops_watchdog"] = watchdog  # dataclass 解析字符串注解需模块已注册（否则 sys.modules 查不到）
_SPEC.loader.exec_module(watchdog)

LOGGER = logging.getLogger("tests.watchdog")
LOGGER.addHandler(logging.NullHandler())

COMPS = (
    watchdog.Component("c1", "p1", ("cmd",), Path("/tmp/wd-c1.log")),
    watchdog.Component("c2", "p2", ("cmd",), Path("/tmp/wd-c2.log")),
)


class TestRestartGuard(unittest.TestCase):
    def test_cooldown_after_max_restarts_and_recover(self) -> None:
        guard = watchdog.RestartGuard(window_s=100, max_restarts=3, cooldown_s=50)
        now = 0.0
        for _ in range(3):
            self.assertTrue(guard.allow("worker", now))
            guard.record("worker", now)
            now += 1
        self.assertFalse(guard.allow("worker", now))  # 窗口内第 4 次：进入冷却
        self.assertGreater(guard.cooldown_remaining("worker", now), 0)
        self.assertTrue(guard.allow("worker", now + 60))  # 冷却期满恢复
        # 冷却解除后历史清零：再次记录 1 次不应立即重新冷却（上限为 3）
        guard.record("worker", now + 61)
        self.assertTrue(guard.allow("worker", now + 62))

    def test_old_restarts_slide_out_of_window(self) -> None:
        guard = watchdog.RestartGuard(window_s=10, max_restarts=3, cooldown_s=50)
        guard.record("w", 0.0)
        guard.record("w", 100.0)
        guard.record("w", 200.0)
        self.assertTrue(guard.allow("w", 201.0))  # 旧记录滚出窗口，未触发冷却

    def test_independent_per_component(self) -> None:
        guard = watchdog.RestartGuard(window_s=100, max_restarts=1, cooldown_s=50)
        guard.record("a", 0.0)
        self.assertFalse(guard.allow("a", 1.0))
        self.assertTrue(guard.allow("b", 1.0))  # b 不受 a 的冷却影响


class TestCheckOnce(unittest.TestCase):
    def test_starts_only_missing(self) -> None:
        started: list[str] = []

        def start(comp) -> int:
            started.append(comp.name)
            return 42

        guard = watchdog.RestartGuard()
        n = watchdog.check_once(
            COMPS, guard, 0.0, LOGGER, running_fn=lambda p: p == "p1", start_fn=start
        )
        self.assertEqual((n, started), (1, ["c2"]))

    def test_dry_run_starts_nothing(self) -> None:
        started: list[str] = []

        def start(comp) -> int:
            started.append(comp.name)
            return 1

        guard = watchdog.RestartGuard()
        n = watchdog.check_once(
            COMPS, guard, 0.0, LOGGER, dry_run=True, running_fn=lambda p: False, start_fn=start
        )
        self.assertEqual((n, started), (0, []))

    def test_cooldown_skips_restart(self) -> None:
        started: list[str] = []

        def start(comp) -> int:
            started.append(comp.name)
            return 1

        guard = watchdog.RestartGuard(window_s=100, max_restarts=1, cooldown_s=100)
        watchdog.check_once(COMPS, guard, 0.0, LOGGER, running_fn=lambda p: False, start_fn=start)
        watchdog.check_once(COMPS, guard, 5.0, LOGGER, running_fn=lambda p: False, start_fn=start)
        self.assertEqual(started, ["c1", "c2"])  # 第二轮两组件均在冷却中，跳过


class TestTerminatePids(unittest.TestCase):
    """真实子进程验证（POSIX）：僵尸收割、SIGTERM→SIGKILL 升级与「无残留」返回。"""

    @staticmethod
    def _spawn(cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_exited_child_is_reaped_and_dead(self) -> None:
        proc = self._spawn(["true"])
        time.sleep(0.3)  # 等子进程退出成僵尸（父进程未 wait，kill(0) 仍会成功）
        self.assertFalse(watchdog._pid_alive(proc.pid))
        with self.assertRaises(ChildProcessError):  # 已被收割，再次 wait 报 ECHILD
            os.waitpid(proc.pid, os.WNOHANG)
        proc.wait()  # 回填状态，避免 GC 时的 ResourceWarning（进程已被收割）

    def test_running_child_detected_alive(self) -> None:
        proc = self._spawn(["sleep", "30"])
        try:
            self.assertTrue(watchdog._pid_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_terminate_reaps_child_no_stuck(self) -> None:
        proc = self._spawn(["sleep", "30"])
        stuck = watchdog._terminate_pids([proc.pid], wait_s=2.0)
        self.assertEqual(stuck, [])  # SIGTERM 即退：不升级、不误报
        self.assertFalse(watchdog._pid_alive(proc.pid))
        proc.wait()  # 同上前述：回填状态消除 ResourceWarning

    def test_terminate_escalates_to_sigkill_without_false_report(self) -> None:
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        proc = self._spawn([sys.executable, "-c", code])
        time.sleep(0.5)  # 等 SIG_IGN 生效
        stuck = watchdog._terminate_pids([proc.pid], wait_s=0.6)
        self.assertEqual(stuck, [])  # SIGKILL 后收割确认：无残留误报
        self.assertFalse(watchdog._pid_alive(proc.pid))
        proc.wait()  # 同上前述：回填状态消除 ResourceWarning


class TestMemoryGuard(unittest.TestCase):
    """内存超限守护（P1）：在跑但 RSS 超阈值的组件就地重启，与崩溃冷却共用守卫。"""

    def _run(
        self,
        limit_mb: float = 100.0,
        rss_kb: int | None = 200 * 1024,
        pids: tuple[int, ...] = (11, 12),
        guard=None,
        dry_run: bool = False,
        now: float = 0.0,
    ):
        killed: list[list[int]] = []
        started: list[str] = []

        def kill(pids_arg: list[int]) -> list[int]:
            killed.append(list(pids_arg))
            return []

        def start(comp) -> int:
            started.append(comp.name)
            return 99

        g = guard if guard is not None else watchdog.RestartGuard()
        n = watchdog.check_memory_once(
            COMPS,
            g,
            now,
            LOGGER,
            limit_mb,
            dry_run=dry_run,
            pids_fn=lambda p: list(pids),
            rss_fn=lambda pid: rss_kb,
            kill_fn=kill,
            start_fn=start,
        )
        return n, killed, started

    def test_below_limit_no_action(self) -> None:
        n, killed, started = self._run(rss_kb=50 * 1024)
        self.assertEqual((n, killed, started), (0, [], []))

    def test_over_limit_restarts_with_kill_then_start(self) -> None:
        n, killed, started = self._run()
        self.assertEqual((n, sorted(started)), (2, ["c1", "c2"]))
        self.assertEqual(killed, [[11, 12], [11, 12]])

    def test_cooldown_blocks_memory_restart(self) -> None:
        guard = watchdog.RestartGuard(window_s=100, max_restarts=1, cooldown_s=100)
        n1, _, started1 = self._run(guard=guard, now=0.0)
        n2, killed2, started2 = self._run(guard=guard, now=5.0)
        self.assertEqual((n1, n2), (2, 0))  # 第二轮处于冷却：不杀不拉
        self.assertEqual((killed2, started2), ([], []))
        self.assertEqual(sorted(started1), ["c1", "c2"])

    def test_missing_component_skipped(self) -> None:
        n, killed, started = self._run(pids=())
        self.assertEqual((n, killed, started), (0, [], []))

    def test_unreadable_rss_skipped(self) -> None:
        n, killed, started = self._run(rss_kb=None)
        self.assertEqual((n, killed, started), (0, [], []))

    def test_dry_run_no_kill(self) -> None:
        n, killed, started = self._run(dry_run=True)
        self.assertEqual((n, killed, started), (0, [], []))

    def test_limit_zero_disables(self) -> None:
        called: list[str] = []
        n = watchdog.check_memory_once(
            COMPS,
            watchdog.RestartGuard(),
            0.0,
            LOGGER,
            0,
            pids_fn=lambda p: called.append(p) or [1],
        )
        self.assertEqual((n, called), (0, []))  # 关闭时不做任何探测


class TestComponentTable(unittest.TestCase):
    def test_six_components_match_demo_up(self) -> None:
        comps = watchdog.build_components()
        self.assertEqual(
            [c.name for c in comps],
            ["worker", "bff", "alert_webhook", "app_prober", "ship_app_logs", "log_surge_detector"],
        )
        for comp in comps:  # 拉起命令引用的入口脚本必须真实存在（防拼写漂移）
            for arg in comp.cmd:
                if arg.endswith(".py"):
                    self.assertTrue((watchdog.ROOT / arg).is_file(), arg)

    def test_surge_args_env_override(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"AIOPS_WATCHDOG_SURGE_ARGS": "--min-lines 100 --factor 3"}):
            comps = watchdog.build_components()
        surge = comps[-1]
        self.assertEqual(surge.cmd[-4:], ("--min-lines", "100", "--factor", "3"))


class TestModeAndLock(unittest.TestCase):
    """档位锁定与单实例锁（P1/P2）：防档位混用、防双守护。"""

    def test_env_mode_wins_over_dotenv(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            self.assertEqual(watchdog._effective_mode(), ("demo", "环境变量"))

    def test_fallback_chain_when_env_missing(self) -> None:
        import os
        from unittest import mock

        env = {k: v for k, v in os.environ.items() if k != "AIOPS_MODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(watchdog, "_read_env_mode", return_value="demo"):
                self.assertEqual(watchdog._effective_mode(), ("demo", ".env"))
            with mock.patch.object(watchdog, "_read_env_mode", return_value=None):
                self.assertEqual(watchdog._effective_mode(), ("production", "默认值"))

    def test_component_env_locks_mode_and_demo_policy(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}, clear=True):
            env = watchdog._component_env()
        self.assertEqual(env["AIOPS_MODE"], "demo")
        self.assertEqual(env["AIOPS_POLICY_PATH"], "./demo-policy.yaml")
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}, clear=True):
            env = watchdog._component_env()
        self.assertEqual(env["AIOPS_MODE"], "production")
        self.assertNotIn("AIOPS_POLICY_PATH", env)  # 生产档不注入演示策略

    def test_acquire_lock_single_instance(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wd.lock"
            first = watchdog.acquire_lock(LOGGER, path=path)
            self.assertIsNotNone(first)
            self.assertIsNone(watchdog.acquire_lock(LOGGER, path=path))  # 第二实例被拒
            first.close()  # 释放锁
            third = watchdog.acquire_lock(LOGGER, path=path)
            self.assertIsNotNone(third)
            third.close()


if __name__ == "__main__":
    unittest.main()
