"""组件守护单测（P0-3）：崩溃冷却策略与单轮巡检编排（注入假判活/假拉起，零外部依赖）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

scripts/ 非 Python 包目录，按文件路径加载被测模块（与 python3 scripts/aiops-watchdog.py 运行一致）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
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


if __name__ == "__main__":
    unittest.main()
