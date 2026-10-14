"""组件守护单测（P0-3）：崩溃冷却策略与单轮巡检编排（注入假判活/假拉起，零外部依赖）。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

scripts/ 非 Python 包目录，按文件路径加载被测模块（与 python3 scripts/aiops-watchdog.py 运行一致）。
"""

from __future__ import annotations

import tests  # noqa: F401 - 先导入隔离钩子
import importlib.util
import json
import plistlib
import tempfile
from types import SimpleNamespace
from unittest import mock
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
    def setUp(self):
        patch = mock.patch.object(watchdog, "maintenance_active", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

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
    """信号操作全部 mock；未知身份不杀，拒绝用 SIGKILL 掩盖业务退出失败。"""

    def test_unknown_pid_never_signalled(self):
        with mock.patch.object(watchdog.os, "kill") as kill:
            with self.assertRaises(watchdog.StartupError):
                watchdog._terminate_pids([42], identities={})
            kill.assert_not_called()

    def test_identity_rechecked_before_signal(self):
        record = {"pid": 42}
        with mock.patch.object(watchdog, "verify_identity", side_effect=[True, False]), mock.patch.object(watchdog.os, "kill") as kill:
            self.assertEqual(watchdog._terminate_pids([42], identities={42: record}), [])
            kill.assert_called_once_with(42, watchdog.signal.SIGTERM)

    def test_stuck_process_not_force_killed(self):
        with mock.patch.object(watchdog, "verify_identity", return_value=True), mock.patch.object(watchdog.os, "kill") as kill:
            self.assertEqual(watchdog._terminate_pids([42], wait_s=0, identities={42: {}}), [42])
            kill.assert_called_once_with(42, watchdog.signal.SIGTERM)

    def test_changed_identity_never_signalled(self):
        with mock.patch.object(watchdog, "verify_identity", side_effect=watchdog.StartupError("PID reused")), mock.patch.object(watchdog.os, "kill") as kill:
            with self.assertRaises(watchdog.StartupError):
                watchdog._terminate_pids([42], identities={42: {}})
            kill.assert_not_called()


class TestMemoryGuard(unittest.TestCase):
    """内存超限守护（P1）：在跑但 RSS 超阈值的组件就地重启，与崩溃冷却共用守卫。"""

    def setUp(self):
        patch = mock.patch.object(watchdog, "maintenance_active", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

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

    def test_feishu_gateway_registered_only_with_app_credentials(self) -> None:
        """应用凭证齐全才注册长连接网关（半套/空值不注册，防拉起-退出循环）；surge 恒为末位。"""
        base = ["worker", "bff", "alert_webhook", "app_prober", "ship_app_logs", "log_surge_detector"]
        self.assertEqual([c.name for c in watchdog.build_components()], base)
        for env in ({"AIOPS_FEISHU_APP_ID": "cli_x"},
                    {"AIOPS_FEISHU_APP_ID": "  ", "AIOPS_FEISHU_APP_SECRET": "sec"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual([c.name for c in watchdog.build_components()], base)
        with mock.patch.dict(os.environ, {"AIOPS_FEISHU_APP_ID": "cli_x",
                                          "AIOPS_FEISHU_APP_SECRET": "sec"}):
            comps = watchdog.build_components()
        self.assertEqual(
            [c.name for c in comps],
            ["worker", "bff", "alert_webhook", "app_prober", "ship_app_logs",
             "feishu_gateway", "log_surge_detector"],
        )
        gateway_comp = comps[-2]  # surge 保持末位（现有断言不破坏）
        self.assertEqual(gateway_comp.cmd, (str(comps[0].cmd[0]), str(watchdog.ROOT / "feishu_gateway.py")))
        self.assertTrue((watchdog.ROOT / "feishu_gateway.py").is_file())


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
        self.assertNotIn("AIOPS_POLICY_PATH", env)  # 由 config 唯一解析，不在启动器注入
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


class WatchdogSafetyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="aiops-watchdog-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.patch = mock.patch.multiple(watchdog, ROOT=self.root, LOCK_PATH=self.root / "data/watchdog.lock")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        env = mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.io = mock.patch.object(watchdog, "_run", side_effect=AssertionError("unexpected real process command"))
        self.run = self.io.start()
        self.addCleanup(self.io.stop)
        self.spawn_patch = mock.patch.object(watchdog.subprocess, "Popen", side_effect=AssertionError("unexpected service start"))
        self.spawn = self.spawn_patch.start()
        self.addCleanup(self.spawn_patch.stop)
        kill = mock.patch.object(watchdog.os, "kill", side_effect=AssertionError("unexpected real signal"))
        kill.start()
        self.addCleanup(kill.stop)
        self.snapshot = {"fingerprint": "safe-fingerprint", "mode": "demo", "listeners": {}}

    def record(self, pid=424242):
        return {"pid": pid, "uid": os.getuid(), "started": "Tue Oct 13 12:00:00 2026",
                "command": "python -m aiops_agent.worker", "cwd": str(self.root), "repository": str(self.root)}

    def test_import_does_not_load_dotenv(self):
        self.assertNotIn("AIOPS_DATABASE_URL", os.environ)
        self.run.assert_not_called()
        self.spawn.assert_not_called()

    def test_production_table_and_missing_venv_failclosed(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            with self.assertRaises(watchdog.StartupError):
                watchdog.build_components()
            with mock.patch.object(watchdog.os, "access", return_value=True):
                comps = watchdog.build_components()
        self.assertEqual(comps[-1].cmd[-6:], ("--min-lines", "100", "--factor", "3", "--cooldown", "300"))
        self.assertEqual(comps[0].cmd[0], str(self.root / ".venv/bin/python"))
        self.assertIn("127.0.0.1", comps[1].cmd)
        self.assertIn("--host", comps[2].cmd)

    def test_locks_fail_closed_for_creation_and_flock_errors(self):
        with mock.patch.object(watchdog.os, "open", side_effect=PermissionError):
            with self.assertRaises(watchdog.StartupError):
                watchdog.acquire_lock(LOGGER)
        with mock.patch.object(watchdog.fcntl, "flock", side_effect=OSError(5, "I/O")):
            with self.assertRaises(watchdog.StartupError):
                watchdog.acquire_lock(LOGGER)

    def test_lock_symlink_rejected(self):
        target = self.root / "real"
        target.write_text("unchanged")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(watchdog.StartupError):
            watchdog.acquire_lock(LOGGER, link)
        self.assertEqual(target.read_text(), "unchanged")

    def test_maintenance_blocks_liveness_and_memory_restarts(self):
        watchdog.set_maintenance("backup")
        start = mock.Mock()
        self.assertEqual(watchdog.check_once(COMPS, watchdog.RestartGuard(), 0, LOGGER,
                         running_fn=lambda p: False, start_fn=start), 0)
        self.assertEqual(watchdog.check_memory_once(COMPS, watchdog.RestartGuard(), 0, LOGGER, 1,
                         pids_fn=lambda p: [42], rss_fn=lambda p: 99999, start_fn=start), 0)
        start.assert_not_called()
        self.assertEqual(watchdog.main([]), 0)
        self.spawn.assert_not_called()

    def test_identity_checks_repository_start_time_uid_command(self):
        record = self.record()
        for field, changed in (("repository", "/another"), ("cwd", "/another"), ("uid", -1)):
            with self.subTest(field=field), self.assertRaises(watchdog.StartupError):
                watchdog.verify_identity({**record, field: changed})
        for field, value in (("started", "new time"), ("command", "different command"), ("cwd", "/other")):
            with self.subTest(field=field), mock.patch.object(watchdog, "process_identity", return_value={**record, field: value}):
                with self.assertRaises(watchdog.StartupError):
                    watchdog.verify_identity(record)
        with mock.patch.object(watchdog, "process_identity", return_value=record):
            self.assertTrue(watchdog.verify_identity(record))
        with mock.patch.object(watchdog, "process_identity", return_value=None):
            self.assertFalse(watchdog.verify_identity(record))

    def test_process_identity_parses_ps_and_cwd(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0,
            stdout=f"{os.getuid()} Tue Oct 13 12:00:00 2026 S python -m aiops_agent.worker\n")
        with mock.patch.object(watchdog, "_cwd", return_value=str(self.root)):
            self.assertEqual(watchdog.process_identity(424242), self.record())

    def test_unregistered_same_repo_refused_other_repo_untouched(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0, stdout="424242 python -m aiops_agent.worker\n")
        with mock.patch.object(watchdog, "_cwd", return_value=str(self.root)):
            with self.assertRaises(watchdog.StartupError):
                watchdog.reject_unregistered(watchdog.read_state(), watchdog.build_components())
        with mock.patch.object(watchdog, "_cwd", return_value="/different-project"):
            watchdog.reject_unregistered(watchdog.read_state(), watchdog.build_components())

    def test_unregistered_old_watchdog_is_also_refused(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0, stdout="424242 python scripts/aiops-watchdog.py\n")
        with mock.patch.object(watchdog, "_cwd", return_value=str(self.root)):
            with self.assertRaises(watchdog.StartupError):
                watchdog.reject_unregistered(watchdog.read_state(), watchdog.build_components())

    def test_wrong_mode_running_state_rejected_before_dependencies(self):
        state = watchdog.read_state()
        state.update(fingerprint="old-mode", processes={"worker": self.record()})
        watchdog.save_state(state)
        with mock.patch.object(watchdog, "verify_identity", return_value=True), mock.patch.object(watchdog, "check_dependencies") as deps:
            with self.assertRaises(watchdog.StartupError):
                watchdog.reconcile(self.snapshot, COMPS, watchdog.RestartGuard(), 0)
            deps.assert_not_called()
        self.spawn.assert_not_called()

    def test_dependency_failure_prevents_every_component_start(self):
        with mock.patch.object(watchdog, "reject_unregistered"), mock.patch.object(watchdog, "check_dependencies",
                side_effect=watchdog.StartupError("unavailable")), mock.patch.object(watchdog, "check_once") as start:
            with self.assertRaises(watchdog.StartupError):
                watchdog.reconcile(self.snapshot, COMPS, watchdog.RestartGuard(), 0)
            start.assert_not_called()
        self.spawn.assert_not_called()

    def test_docker_failure_does_not_contact_any_other_dependency(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=1)
        with mock.patch.object(watchdog, "check_temporal", new=mock.AsyncMock()) as temporal:
            with self.assertRaises(watchdog.StartupError):
                watchdog.check_dependencies(self.snapshot)
            temporal.assert_not_awaited()
        self.assertEqual(self.run.call_args.args[0], ["docker", "info"])

    def test_port_conflict_prevents_popen(self):
        snapshot = {**self.snapshot, "listeners": {"bff": {"host": "127.0.0.1", "port": 18600}}}
        with mock.patch.object(watchdog.socket, "socket") as socket:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError("in use")
            with self.assertRaises(watchdog.StartupError):
                watchdog.check_ports(snapshot, watchdog.read_state())
            # 探测必须携带 SO_REUSEADDR（否则 TIME_WAIT 残留会在 restart 后误报端口占用）。
            socket.return_value.__enter__.return_value.setsockopt.assert_called_once_with(
                watchdog.socket.SOL_SOCKET, watchdog.socket.SO_REUSEADDR, 1)
        self.spawn.assert_not_called()

    def test_start_failure_rolls_back_only_this_batch(self):
        start = mock.Mock(side_effect=[42, RuntimeError("failed")])
        with mock.patch.object(watchdog, "_terminate_pids", return_value=[]) as stop:
            with self.assertRaises(RuntimeError):
                watchdog.check_once(COMPS, watchdog.RestartGuard(), 0, LOGGER, running_fn=lambda p: False, start_fn=start)
            stop.assert_called_once_with([42])
        self.assertTrue(watchdog.maintenance_active())

    def test_stuck_old_process_does_not_restart(self):
        start = mock.Mock()
        with self.assertRaises(watchdog.StartupError):
            watchdog.check_memory_once(COMPS, watchdog.RestartGuard(), 0, LOGGER, 1,
                pids_fn=lambda p: [42], rss_fn=lambda p: 99999, kill_fn=lambda p: [42], start_fn=start)
        start.assert_not_called()

    def test_stop_preserves_registration_and_maintenance(self):
        state = watchdog.read_state()
        state.update(processes={"worker": self.record()}, watchdog=self.record(424243))
        watchdog.save_state(state)
        with mock.patch.object(watchdog, "verify_identity", return_value=True), mock.patch.object(
                watchdog, "reject_unregistered"), mock.patch.object(watchdog, "_terminate_pids", return_value=[]) as kill, mock.patch.object(watchdog, "launchctl") as launch:
            watchdog.stop_services()
            self.assertEqual([c.args[0] for c in kill.call_args_list], [[424243], [424242]])
            launch.assert_not_called()
        self.assertTrue(watchdog.maintenance_active())
        self.assertEqual(watchdog.read_state()["processes"], {})

    def test_uninstall_is_only_action_that_removes_plist(self):
        path = self.root / "test.plist"
        path.write_text("fixture")
        with mock.patch.object(watchdog, "validate_plist"), mock.patch.object(watchdog, "plist_path", return_value=path), mock.patch.object(
                watchdog, "stop_services") as stop, mock.patch.object(watchdog, "launchctl", return_value=True) as launch:
            self.assertEqual(watchdog.control("uninstall"), 0)
        stop.assert_called_once()
        self.assertFalse(path.exists())
        launch.assert_any_call("bootout", watchdog.launch_target())

    def test_foreign_launchd_registration_rejected(self):
        path = self.root / "test.plist"
        path.write_bytes(plistlib.dumps({"Label": watchdog.LABEL, "WorkingDirectory": "/other"}))
        with mock.patch.object(watchdog, "plist_path", return_value=path):
            with self.assertRaises(watchdog.StartupError):
                watchdog.validate_plist()

    def test_managed_supervisor_uses_kickstart_not_manual_spawn(self):
        with mock.patch.object(watchdog.sys, "platform", "darwin"), mock.patch.object(watchdog, "validate_plist",
                return_value={"EnvironmentVariables": {"AIOPS_LAUNCH_FINGERPRINT": self.snapshot["fingerprint"]}}), mock.patch.object(
                watchdog, "launchctl", return_value=True) as launch:
            watchdog.start_supervisor(self.snapshot)
            launch.assert_any_call("kickstart", watchdog.launch_target())
        self.spawn.assert_not_called()

    def test_up_does_not_seed_and_requires_supervisor_ack(self):
        with mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(watchdog, "validate_plist", return_value=None), mock.patch.object(
                watchdog, "check_dependencies"), mock.patch.object(watchdog, "check_ports"), mock.patch.object(watchdog, "reconcile"), mock.patch.object(
                watchdog, "start_supervisor") as start, mock.patch.object(watchdog, "await_supervisor") as ack, mock.patch.object(watchdog, "demo_seed") as seed:
            self.assertEqual(watchdog.control("up"), 0)
        start.assert_called_once()
        ack.assert_called_once()
        seed.assert_not_called()

    def test_restart_restores_registered_supervisor(self):
        registered = {"EnvironmentVariables": {"AIOPS_LAUNCH_FINGERPRINT": "old"}}
        with mock.patch.object(watchdog.sys, "platform", "darwin"), mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(
                watchdog, "validate_plist", return_value=registered), mock.patch.object(watchdog, "check_dependencies"), mock.patch.object(watchdog, "check_ports"), mock.patch.object(
                watchdog, "stop_services") as stop, mock.patch.object(watchdog, "write_plist") as write, mock.patch.object(watchdog, "launchctl", return_value=True) as launch, mock.patch.object(
                watchdog, "reconcile"), mock.patch.object(watchdog, "start_supervisor") as start, mock.patch.object(watchdog, "await_supervisor"):
            self.assertEqual(watchdog.control("restart"), 0)
        stop.assert_called_once()
        write.assert_called_once_with(self.snapshot, registered)
        start.assert_called_once_with(self.snapshot)
        launch.assert_any_call("bootout", watchdog.launch_target())

    def test_restart_dependency_failure_does_not_stop_existing_services(self):
        with mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(watchdog, "validate_plist", return_value=None), mock.patch.object(
                watchdog, "check_dependencies", side_effect=watchdog.StartupError("down")), mock.patch.object(watchdog, "stop_services") as stop:
            with self.assertRaises(watchdog.StartupError):
                watchdog.control("restart")
            stop.assert_not_called()

    def test_backup_maintenance_requires_explicit_resume(self):
        watchdog.set_maintenance("backup")
        with mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(watchdog, "validate_plist", return_value=None):
            with self.assertRaises(watchdog.StartupError):
                watchdog.control("up")
        self.assertTrue(watchdog.maintenance_active())

    def test_production_seed_rejected_without_process_operations(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            with self.assertRaises(watchdog.StartupError):
                watchdog.demo_seed()
        self.spawn.assert_not_called()
        self.run.assert_not_called()

    def test_controller_process_not_mistaken_for_old_daemon(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0, stdout="424242 python scripts/aiops-watchdog.py up\n")
        with mock.patch.object(watchdog, "_cwd") as cwd:
            watchdog.reject_unregistered(watchdog.read_state(), watchdog.build_components())
        cwd.assert_not_called()

    def test_live_foreign_launchd_identity_never_booted_out(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0, stdout="program = /other/python\nworking directory = /other\n")
        with self.assertRaises(watchdog.StartupError):
            watchdog.launchctl("bootout", watchdog.launch_target())
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][1], "print")

    def test_launchd_snapshot_contains_no_credentials_and_xml_is_valid(self):
        path = self.root / "agents/test.plist"
        env = {"AIOPS_MODE": "production", "AIOPS_POLICY_PATH": str(self.root / "a & b.yaml"),
               "AIOPS_DATA_ROOT": str(self.root / "prod"), "TEMPORAL_NAMESPACE": "aiops-production",
               "TEMPORAL_TASK_QUEUE": "aiops-production-tasks", "AIOPS_DATABASE_URL": "private-secret"}
        for name in ("BFF", "WEBHOOK", "METRICS"):
            env[f"AIOPS_{name}_HOST"] = "127.0.0.1"
            env[f"AIOPS_{name}_PORT"] = "20001"
        with mock.patch.dict(os.environ, env), mock.patch.object(watchdog, "plist_path", return_value=path):
            watchdog.write_plist(self.snapshot, {"Label": watchdog.LABEL})
        self.assertNotIn("private-secret", path.read_text())
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["EnvironmentVariables"]["AIOPS_POLICY_PATH"], env["AIOPS_POLICY_PATH"])
        self.assertIn("/usr/bin", data["EnvironmentVariables"]["PATH"])

    def test_supervisor_ack_timeout_is_failure(self):
        with self.assertRaises(watchdog.StartupError):
            watchdog.await_supervisor(self.snapshot, timeout=0)

    def test_temporal_preflight_uses_namespace_rpc(self):
        fake = SimpleNamespace(workflow_service=SimpleNamespace(describe_namespace=mock.AsyncMock(
            return_value=SimpleNamespace(namespace_info=SimpleNamespace(name="default")))))
        with mock.patch("temporalio.client.Client.connect", new=mock.AsyncMock(return_value=fake)) as connect:
            watchdog.asyncio.run(watchdog.check_temporal())
        connect.assert_awaited_once_with("localhost:7233", namespace="default")
        fake.workflow_service.describe_namespace.assert_awaited_once()

    def test_mysql_check_is_select_only_without_schema_or_business_engine(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0)
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        engine = mock.MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        with mock.patch.dict(os.environ, {"AIOPS_DATABASE_URL": "mysql+pymysql://test@invalid/test", "AIOPS_REDIS_URL": "redis://invalid"}), mock.patch.object(
                watchdog, "check_temporal", new=mock.AsyncMock()), mock.patch("urllib.request.build_opener") as opener, mock.patch(
                "sqlalchemy.create_engine", return_value=engine), mock.patch("aiops_agent.db.get_engine", side_effect=AssertionError("DDL forbidden")), mock.patch("redis.Redis.from_url") as redis:
            opener.return_value.open.return_value = response
            watchdog.check_dependencies({"mode": "production"})
            self.assertEqual(str(connection.execute.call_args.args[0]), "SELECT 1")
            engine.dispose.assert_called_once()
            redis.return_value.ping.assert_called_once()
            redis.return_value.close.assert_called_once()

    def test_duplicate_daemon_with_verified_matching_identity_exits_cleanly(self):
        watchdog.save_state({**watchdog.read_state(), "fingerprint": self.snapshot["fingerprint"], "watchdog": self.record()})
        with mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(watchdog, "acquire_lock", return_value=None), mock.patch.object(
                watchdog, "verify_identity", return_value=True):
            self.assertEqual(watchdog.main(["--once"]), 0)
        self.spawn.assert_not_called()

    def test_spawn_uses_single_component_command_and_records_identity(self):
        comp = watchdog.build_components()[0]
        fake = mock.Mock(pid=424242)
        fake.poll.return_value = None
        self.spawn.side_effect = None
        self.spawn.return_value = fake
        with mock.patch.object(watchdog, "process_identity", return_value=self.record()), mock.patch.object(watchdog.time, "sleep"):
            self.assertEqual(watchdog.start_component(comp), 424242)
        self.assertEqual(self.spawn.call_args.args[0], comp.cmd)
        self.assertEqual(self.spawn.call_args.kwargs["cwd"], self.root)
        self.assertEqual(self.spawn.call_args.kwargs["env"]["AIOPS_MODE"], "demo")
        self.assertEqual(watchdog.read_state()["processes"]["worker"], self.record())

    def test_prepare_snapshot_is_stable_and_locks_child_configuration(self):
        policy = self.root / "policy.yaml"
        policy.write_text("release_gate: {shadow_mode: true}")
        with mock.patch.dict(os.environ, {"AIOPS_POLICY_PATH": str(policy), "AIOPS_DATA_ROOT": str(self.root / "data")}):
            first = watchdog.prepare_snapshot()
            second = watchdog.prepare_snapshot()
            self.assertEqual(first, second)
            self.assertEqual(watchdog._component_env()["AIOPS_POLICY_SHA256"], first["policy_sha256"])
            with mock.patch.dict(os.environ, {"AIOPS_LAUNCH_FINGERPRINT": "wrong"}):
                with self.assertRaises(watchdog.StartupError):
                    watchdog.prepare_snapshot()

    def test_duplicate_daemon_validates_snapshot_without_start(self):
        watchdog.save_state({**watchdog.read_state(), "fingerprint": "other"})
        with mock.patch.object(watchdog, "prepare_snapshot", return_value=self.snapshot), mock.patch.object(watchdog, "acquire_lock", return_value=None):
            self.assertEqual(watchdog.main(["--once"]), 1)
        self.spawn.assert_not_called()


class ShellEntrypointTest(unittest.TestCase):
    def test_shell_syntax_and_mode_passthrough_in_temporary_repo(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aiops-shell-test-") as temp:
            root = Path(temp) / "repo with spaces"
            (root / "scripts").mkdir(parents=True)
            (root / ".venv/bin").mkdir(parents=True)
            for name in ("start.sh", "scripts/demo-up.sh", "scripts/launchd-install.sh"):
                script = root / name
                script.write_text((source / name).read_text())
                subprocess.run(["/bin/bash", "-n", str(script)], check=True)
            python = root / ".venv/bin/python"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "${AIOPS_MODE-unset}" "$@"\n')
            python.chmod(0o700)
            for command, expected in (([], "up"), (["back"], "up"), (["restart"], "restart"), (["demo"], "demo")):
                result = subprocess.run(["/bin/bash", str(root / "start.sh"), *command],
                    env={"PATH": "/usr/bin:/bin", "AIOPS_MODE": "production"}, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.splitlines(), ["production", str(root / "scripts/aiops-watchdog.py"), expected])
            result = subprocess.run(["/bin/bash", str(root / "start.sh")], env={"PATH": "/usr/bin:/bin"},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines()[0], "unset")


if __name__ == "__main__":
    unittest.main()
