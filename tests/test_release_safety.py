"""G批次离线门禁：临时文件/内存容器替身，禁止数据库/急停写入和部署工具。"""
from __future__ import annotations

import tests  # 必须先安装隔离钩子
import asyncio
import copy
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from aiops_agent import activities, backup, kill_switch, release, release_journal as journal
from aiops_agent.models import Alert, CanaryResult, Patch


def patch_value(name="patch-g", files=None, diff=None):
    return Patch(name, "alert-g", files or ["page.txt"], diff or
                 "--- a/page.txt\n+++ b/page.txt\n@@ -1 +1 @@\n-old\n+new\n",
                 "isolated", "low", "test", 1.0)


def old_container(name="aiops-test-g", port=28099):
    return {"Id": "old-id", "Name": "/" + name, "Image": "sha256:old",
            "State": {"Running": True}, "Mounts": [],
            "Config": {"Image": "old:image", "Env": ["CUSTOM=keep", "APP_VERSION=old"],
                       "Cmd": ["serve"], "Entrypoint": ["/entry"], "User": "1000",
                       "WorkingDir": "/app", "Labels": {"custom": "keep"}},
            "HostConfig": {"AutoRemove": False, "NetworkMode": "bridge", "Binds": [],
                           "RestartPolicy": {"Name": "unless-stopped"}, "Memory": 123456,
                           "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}},
            "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["service"]}}}}


class MemoryRunner(release.DockerRolloutRunner):
    """执行真正promote/restore代码，但所有容器读写只是内存字典。"""
    def __init__(self):
        super().__init__(stable_container="aiops-test-g", stable_port=28099)
        self.containers = {"old-id": old_container()}
        self.commands = []
        self.created_config = None
        self.fail_new = False
        self.after_command = None

    def inspect(self, name):
        for value in self.containers.values():
            if name in {value["Id"], value["Name"].lstrip("/")}:
                return copy.deepcopy(value)
        return None

    def _checked(self, args):
        release.checkpoint()
        self.commands.append(args)
        obj = self.inspect(args[1])
        if args[0] == "stop":
            self.containers[obj["Id"]]["State"]["Running"] = False
        elif args[0] == "start":
            self.containers[obj["Id"]]["State"]["Running"] = True
        elif args[0] == "rename":
            self.containers[obj["Id"]]["Name"] = "/" + args[2]
        elif args[0] == "rm":
            self.containers.pop(args[-1])
        else:
            raise AssertionError(args)
        if self.after_command:
            self.after_command(args)
        return b""

    def stop(self, container):
        value = self.inspect(container)
        if value:
            self.commands.append(["rm", value["Id"]])
            self.containers.pop(value["Id"])

    def _create_from_config(self, name, config):
        self.created_config = copy.deepcopy(config)
        value = old_container(name, self.stable_port)
        value.update(Id="new-id", Config={k: v for k, v in config.items()
                                          if k not in {"HostConfig", "NetworkingConfig"}},
                     HostConfig=config["HostConfig"], State={"Running": False})
        self.containers["new-id"] = value
        return "new-id"

    def build_image(self, *args, **kwargs):
        self.commands.append(["build"])

    def image_id(self, tag):
        return "sha256:" + "a" * 64

    def start_canary(self, name, image, **kwargs):
        self.containers["canary-id"] = {"Id": "canary-id", "Name": "/" + name}
        return "http://127.0.0.1:28098"

    def wait_ready(self, url):
        release.checkpoint()
        if self.fail_new and self.inspect(self.stable_container)["Id"] == "new-id":
            raise RuntimeError("new health failed")

    def observe(self, *args):
        release.checkpoint()
        return {"error_rate": 0, "p99_latency_ms": 2, "total": 5}


class IsolatedReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release-g-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "page.txt").write_text("old\n")
        self.patch = patch_value()
        self.publisher = mock.Mock()
        self.publisher.apply.return_value = {"mode": "recorded"}
        self.state = {"active": False}
        for p in (mock.patch.object(journal, "CONTROL_ROOT", self.root / "control"),
                  mock.patch.object(kill_switch, "get_state", side_effect=lambda: self.state),
                  mock.patch.object(release, "OBSERVE_INTERVAL_SECONDS", 0.001),
                  mock.patch.object(release, "_page_probe_once", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def direct(self, patch=None, **kwargs):
        return release.run_canary_direct(patch or self.patch, self.repo, "http://invalid.test", "", 5,
                                         .003, publisher=self.publisher, **kwargs)

    def record(self):
        with journal.locked():
            return copy.deepcopy(next(iter(journal._read()["records"].values())))

    def container(self, runner):
        workspace = self.root / "sandbox" / self.patch.patch_id
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "order_service.py").write_text("# isolated fixture\n")
        return release.run_canary(Alert("alert-g", "isolated"), self.patch, 5, .01,
                                  repo_dir=self.repo, sandbox_root=workspace.parent,
                                  runner=runner, publisher=self.publisher)

    def test_stage_redelivery_and_cross_stage_occupation(self):
        canary = self.direct()
        self.assertEqual(self.direct(), canary)
        self.publisher.apply.assert_called_once()
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "占用"):
            self.direct(patch_value("other"), workflow_id="another")
        with journal.locked(), self.assertRaises(journal.ReleaseSafetyError):
            journal.assert_idle_locked()
        result = release.run_finalize_direct(canary, True)
        self.assertEqual(release.run_finalize_direct(canary, True), result)
        with journal.locked():
            journal.assert_idle_locked()
        self.assertEqual(canary.traffic_percent, 100)

    def test_hash_and_stage_input_conflicts(self):
        canary = self.direct()
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "hash"):
            self.direct(dataclasses.replace(self.patch, diff=self.patch.diff + "\n"))
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "不符"):
            release.run_finalize_direct(dataclasses.replace(canary, healthy=False), True)
        release.run_finalize_direct(canary, True)
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "hash"):
            release.run_finalize_direct(canary, False)

    def test_same_resource_cross_namespace_and_mode(self):
        self.direct()
        with journal.locked(), mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            other = journal.Journal("production-workflow", "aiops-production", "p-other")
            with self.assertRaisesRegex(journal.ReleaseSafetyError, "占用"):
                other.claim("hash", {"kind": "direct", "repo": str(self.repo)}, ["repo:" + str(self.repo)])

    def test_lock_contends_across_process_and_backup(self):
        with journal.locked():
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    with journal.locked():
                        os.write(write_fd, b"unsafe")
                except journal.ReleaseSafetyError:
                    os.write(write_fd, b"blocked")
                finally:
                    os._exit(0)
            os.close(write_fd)
            try:
                self.assertEqual(os.read(read_fd, 100), b"blocked")
            finally:
                os.close(read_fd)
                os.waitpid(pid, 0)
            with self.assertRaises(backup.BackupError):
                with backup.lock(Path(journal.coordination_paths()["release_lock"]), existing=True):
                    self.fail("must contend")
        with backup.lock(Path(journal.coordination_paths()["release_lock"]), existing=True):
            with self.assertRaises(journal.ReleaseSafetyError):
                with journal.locked():
                    self.fail("must contend")

    def test_port_conflicts_even_with_different_container_names(self):
        with journal.locked():
            journal.Journal("w1", "demo", "p1").claim("h1", {"kind": "container"},
                                                          ["container:one", "port:28099"])
            with self.assertRaises(journal.ReleaseSafetyError):
                journal.Journal("w2", "production", "p2").claim("h2", {"kind": "container"},
                                                                ["container:two", "port:28099"])

    def test_publisher_exception_restores_and_blocks_retries(self):
        self.publisher.apply.side_effect = OSError("disk full")
        with self.assertRaises(journal.ReleaseSafetyError):
            self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.assertTrue(self.record()["recovery_verified"])
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "人工"):
            self.direct()

    def test_observation_exception_restores(self):
        with mock.patch.object(release, "_page_probe_once", side_effect=RuntimeError("observe")):
            with self.assertRaises(journal.ReleaseSafetyError):
                self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")

    def test_kill_switch_mid_observation_and_before_finalize_recovers(self):
        self.publisher.apply.side_effect = lambda *a: self.state.update(active=True)
        with self.assertRaises(journal.ReleaseSafetyError):
            self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.assertTrue(self.record()["recovery_verified"])

    def test_unreadable_kill_switch_before_finalize_still_restores(self):
        canary = self.direct()
        with mock.patch.object(kill_switch, "get_state", side_effect=kill_switch.KillSwitchError("db unreadable")):
            with self.assertRaises(journal.ReleaseSafetyError):
                release.run_finalize_direct(canary, True)
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")

    def test_all_paths_validated_before_any_write(self):
        diff = self.patch.diff + "--- /dev/null\n+++ b/../escape\n@@ -0,0 +1 @@\n+bad\n"
        with self.assertRaises(journal.ReleaseSafetyError):
            self.direct(dataclasses.replace(self.patch, diff=diff))
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.publisher.apply.assert_not_called()

    def test_declared_absolute_and_symlink_paths_rejected(self):
        outside = self.root / "outside"
        outside.write_text("old\n")
        (self.repo / "link").symlink_to(outside)
        for file in ("../outside", str(outside), "link"):
            with self.subTest(file=file), self.assertRaises(journal.ReleaseSafetyError):
                release._prepare_direct(dataclasses.replace(self.patch, files=[file]), self.repo)
        self.assertEqual(outside.read_text(), "old\n")

    def test_third_party_edit_never_overwritten(self):
        def third_party(*args):
            (self.repo / "page.txt").write_text("third-party\n")
            raise OSError("audit failed")
        self.publisher.apply.side_effect = third_party
        with self.assertRaises(journal.ReleaseSafetyError):
            self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "third-party\n")
        self.assertFalse(self.record()["recovery_verified"])
        self.assertIn("before_hash", self.record()["before"]["page.txt"])

    def test_crash_recovers_from_persisted_preimage_not_done(self):
        class Crash(BaseException):
            pass
        self.publisher.apply.side_effect = Crash()
        with self.assertRaises(Crash):
            self.direct()
        self.assertEqual(self.record()["status"], "running")
        self.assertEqual((self.repo / "page.txt").read_text(), "new\n")
        self.state["active"] = True
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "进程中断"):
            self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.assertEqual(self.record()["status"], "manual")

    def test_missing_or_corrupt_journal_never_reinitializes(self):
        self.direct()
        path = journal.CONTROL_ROOT / "journal.json"
        value = path.read_text()
        path.write_text("{")
        with self.assertRaises(journal.ReleaseSafetyError):
            self.direct()
        path.write_text(value)
        path.unlink()
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "丢失"):
            self.direct()

    def test_production_no_registry_fallback_and_direct_disabled(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}), mock.patch.object(
                release, "run_canary") as container:
            for entry in (None, {"repo": self.repo, "url": "http://invalid.test"},
                          {"repo": self.repo, "url": ""}):
                with mock.patch("aiops_agent.app_registry.resolve", return_value=entry):
                    with self.assertRaises(ApplicationError) as caught:
                        asyncio.run(activities.deploy_canary(Alert("a", "svc"), self.patch, 5, 1))
                    self.assertTrue(caught.exception.non_retryable)
            with mock.patch("aiops_agent.app_registry.resolve", side_effect=OSError("db unavailable")):
                with self.assertRaises(ApplicationError):
                    asyncio.run(activities.deploy_canary(Alert("a", "svc"), self.patch, 5, 1))
            with self.assertRaises(journal.ReleaseSafetyError):
                self.direct()
            container.assert_not_called()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")

    def test_container_promotion_retains_config_and_cleans_after_health(self):
        runner = MemoryRunner()
        original = copy.deepcopy(runner.containers["old-id"])
        canary = self.container(runner)
        result = release.run_finalize(canary, True, runner=runner)
        self.assertFalse(result.rolled_back)
        cfg = runner.created_config
        for key in ("Cmd", "Entrypoint", "User", "WorkingDir"):
            self.assertEqual(cfg[key], original["Config"][key])
        self.assertEqual(cfg["HostConfig"]["PortBindings"], original["HostConfig"]["PortBindings"])
        self.assertEqual(cfg["HostConfig"]["RestartPolicy"], original["HostConfig"]["RestartPolicy"])
        self.assertIn("CUSTOM=keep", cfg["Env"])
        self.assertEqual(set(runner.containers), {"new-id"})
        commands = copy.deepcopy(runner.commands)
        self.assertEqual(release.run_finalize(canary, True, runner=runner), result)
        self.assertEqual(commands, runner.commands)

    def test_new_stable_failure_restores_exact_old_container_ports(self):
        runner = MemoryRunner()
        original = copy.deepcopy(runner.containers["old-id"])
        canary = self.container(runner)
        runner.fail_new = True
        with self.assertRaises(journal.ReleaseSafetyError):
            release.run_finalize(canary, True, runner=runner)
        self.assertEqual(runner.containers, {"old-id": original})
        self.assertEqual(self.record()["status"], "manual")
        self.assertTrue(self.record()["recovery_verified"])

    def test_cleanup_health_failure_restores_retained_old(self):
        self._assert_cleanup_stop_restores("health")

    def test_cleanup_kill_switch_restores_retained_old(self):
        self._assert_cleanup_stop_restores("kill")

    def _assert_cleanup_stop_restores(self, failure):
        runner = MemoryRunner()
        original = copy.deepcopy(runner.containers["old-id"])
        canary = self.container(runner)
        finish = journal.Journal.finish
        def after_finish(record, stage, result):
            finish(record, stage, result)
            if stage == "finalize":
                if failure == "health":
                    runner.fail_new = True
                else:
                    self.state["active"] = True
        with mock.patch.object(journal.Journal, "finish", new=after_finish):
            with self.assertRaises(journal.ReleaseSafetyError):
                release.run_finalize(canary, True, runner=runner)
        self.assertEqual(runner.containers, {"old-id": original})
        self.assertEqual(self.record()["status"], "manual")
        self.assertTrue(self.record()["recovery_verified"])
        with self.assertRaises(journal.ReleaseSafetyError):
            release.run_finalize(canary, True, runner=runner)

    def test_restore_does_not_rename_third_party_modified_old(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        def edit_old(args):
            if args[0] == "rename":
                runner.containers["old-id"]["Config"]["Cmd"] = ["third-party"]
                raise RuntimeError("interrupted promotion")
        runner.after_command = edit_old
        with self.assertRaises(journal.ReleaseSafetyError):
            release.run_finalize(canary, True, runner=runner)
        self.assertIsNone(runner.inspect(runner.stable_container))
        self.assertFalse(self.record()["recovery_verified"])
        self.assertEqual(sum(args[0] == "rename" for args in runner.commands), 1)

    def test_cleanup_rejects_unknown_container_even_with_copied_label(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        finish = journal.Journal.finish
        def replace_new(record, stage, result):
            finish(record, stage, result)
            if stage == "finalize":
                replacement = runner.containers.pop("new-id")
                replacement["Id"] = "third-party-id"
                runner.containers["third-party-id"] = replacement
        with mock.patch.object(journal.Journal, "finish", new=replace_new):
            with self.assertRaises(journal.ReleaseSafetyError):
                release.run_finalize(canary, True, runner=runner)
        self.assertIn("third-party-id", runner.containers)
        self.assertIn("old-id", runner.containers)
        self.assertEqual(self.record()["status"], "manual")
        self.assertFalse(self.record()["recovery_verified"])

    def test_engine_api_honors_context_over_host_without_real_socket(self):
        runner = release.DockerRolloutRunner(stable_container="aiops-test-g", stable_port=28099)
        for context in ("isolated-context", ""):
            with self.subTest(context=context), mock.patch.dict(os.environ, {
                    "DOCKER_CONTEXT": context, "DOCKER_HOST": "unix:///unused-host.sock"}), mock.patch.object(
                    runner, "_checked", return_value=b'"unix:///unused-context.sock"') as checked, mock.patch.object(
                    release.http.client, "HTTPConnection") as connection, mock.patch.object(
                    release.socket, "socket") as sock:
                response = connection.return_value.getresponse.return_value
                response.status = 201
                response.read.return_value = b'{"Id":"isolated-id"}'
                self.assertEqual(runner._create_from_config("aiops-test-g", {}), "isolated-id")
                sock.return_value.connect.assert_called_once_with(
                    "/unused-context.sock" if context else "/unused-host.sock")
                if context:
                    self.assertEqual(checked.call_args.args[0][:3], ["context", "inspect", context])
                else:
                    checked.assert_not_called()

    def test_promotion_crash_unknown_target_is_manual(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        class Crash(BaseException):
            pass
        def crash(args):
            if args[0] == "rename":
                raise Crash()
        runner.after_command = crash
        with self.assertRaises(Crash):
            release.run_finalize(canary, True, runner=runner)
        runner.after_command = None
        runner.containers.pop("old-id")
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "恢复失败"):
            release.run_finalize(canary, True, runner=runner)
        self.assertEqual(self.record()["status"], "manual")
        self.assertFalse(self.record()["recovery_verified"])

    def test_kill_during_promotion_preserves_old(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        runner.after_command = lambda args: self.state.update(active=True) if args[0] == "stop" else None
        with self.assertRaises(journal.ReleaseSafetyError):
            release.run_finalize(canary, True, runner=runner)
        self.assertTrue(runner.containers["old-id"]["State"]["Running"])
        self.assertEqual(set(runner.containers), {"old-id"})

    def test_resource_flock_uses_canonical_repository_key(self):
        self.direct()
        name = "resource-" + journal.digest("repo:" + str(self.repo.resolve())) + ".lock"
        self.assertTrue((journal.CONTROL_ROOT / name).is_file())
        with journal.locked(), journal.resource_locks(["repo:" + str(self.repo)]):
            with self.assertRaises(journal.ReleaseSafetyError):
                with journal.resource_locks(["repo:" + str(self.repo)]):
                    self.fail("same target resource")

    def test_stage_result_write_failure_recovers_preimage(self):
        original = journal.write_json
        failed = False
        def disk_failure(path, value):
            nonlocal failed
            rec = next(iter(value["records"].values()))
            if not failed and rec["stages"].get("canary", {}).get("result") is not None:
                failed = True
                raise OSError("disk full on result")
            original(path, value)
        with mock.patch.object(journal, "write_json", side_effect=disk_failure):
            with self.assertRaises(journal.ReleaseSafetyError):
                self.direct()
        self.assertTrue(failed)
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.assertEqual(self.record()["status"], "manual")

    def test_real_child_process_exit_leaves_recoverable_record(self):
        pid = os.fork()
        if pid == 0:
            self.publisher.apply.side_effect = lambda *args: os._exit(17)
            try:
                self.direct()
            finally:
                os._exit(18)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 17)
        self.assertEqual(self.record()["status"], "running")
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "进程中断"):
            self.direct()
        self.assertEqual((self.repo / "page.txt").read_text(), "old\n")
        self.assertTrue(self.record()["recovery_verified"])

    def test_recovery_write_failure_never_reports_success(self):
        self.publisher.apply.side_effect = RuntimeError("publisher")
        replace = release._replace_file
        def fail_restore(root, rel, expected, content, permissions):
            if release._RECOVERING.get():
                raise OSError("restore failed")
            return replace(root, rel, expected, content, permissions)
        with mock.patch.object(release, "_replace_file", side_effect=fail_restore):
            with self.assertRaisesRegex(journal.ReleaseSafetyError, "恢复失败"):
                self.direct()
        self.assertEqual(self.record()["status"], "manual")
        self.assertFalse(self.record()["recovery_verified"])
        self.assertIsNone(self.record()["stages"]["canary"]["result"])

    def test_promotion_crash_restores_known_old_identity(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        class Crash(BaseException):
            pass
        def crash(args):
            if args[0] == "rename":
                raise Crash()
        runner.after_command = crash
        with self.assertRaises(Crash):
            release.run_finalize(canary, True, runner=runner)
        runner.after_command = None
        self.state["active"] = True
        with self.assertRaisesRegex(journal.ReleaseSafetyError, "进程中断"):
            release.run_finalize(canary, True, runner=runner)
        self.assertEqual(set(runner.containers), {"old-id"})
        self.assertTrue(runner.containers["old-id"]["State"]["Running"])
        self.assertTrue(self.record()["recovery_verified"])

    def test_container_third_party_config_change_is_not_overwritten(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        runner.containers["old-id"]["Config"]["Cmd"] = ["third-party"]
        with self.assertRaises(journal.ReleaseSafetyError):
            release.run_finalize(canary, True, runner=runner)
        self.assertEqual(runner.containers["old-id"]["Config"]["Cmd"], ["third-party"])
        self.assertNotIn("new-id", runner.containers)

    def test_cleanup_crash_keeps_occupation_until_reconciled(self):
        runner = MemoryRunner()
        canary = self.container(runner)
        original = release._cleanup_canary
        class Crash(BaseException):
            pass
        with mock.patch.object(release, "_cleanup_canary", side_effect=Crash):
            with self.assertRaises(Crash):
                release.run_finalize(canary, True, runner=runner)
        self.assertEqual(self.record()["status"], "occupied")
        with journal.locked(), self.assertRaises(journal.ReleaseSafetyError):
            journal.assert_idle_locked()
        result = release.run_finalize(canary, True, runner=runner)
        self.assertFalse(result.rolled_back)
        self.assertEqual(set(runner.containers), {"new-id"})
        self.assertEqual(self.record()["status"], "terminal")

    def test_cancelled_thread_holds_lock_until_it_exits_and_heartbeats(self):
        entered, leave = threading.Event(), threading.Event()
        beats = []
        def slow(**kwargs):
            with journal.locked():
                entered.set()
                leave.wait(5)
                self.assertTrue(kwargs["cancel_check"]())
        async def check():
            env = ActivityEnvironment()
            env.on_heartbeat = lambda *details: beats.append(details)
            task = asyncio.create_task(env.run(activities._release_thread, slow))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.01)
                task.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                with self.assertRaises(journal.ReleaseSafetyError):
                    with journal.locked():
                        self.fail("thread still owns target")
            finally:
                leave.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(check())
        self.assertTrue(beats)
        with journal.locked():
            pass


class KillSwitchReadSafetyTest(unittest.TestCase):
    def test_database_values_are_strictly_decoded_without_database_access(self):
        engine = mock.MagicMock()
        scalar = engine.connect.return_value.__enter__.return_value.execute.return_value.scalar
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}), mock.patch.object(
                kill_switch.db_layer, "get_engine", return_value=engine):
            for raw in ("", "null", "[]", "{}", '{"active":"false"}', '{"active":0}', 0):
                with self.subTest(raw=raw):
                    scalar.return_value = raw
                    with self.assertRaises(kill_switch.KillSwitchError):
                        kill_switch.require_release_allowed()
            for raw in (None, '{"active":false}'):
                scalar.return_value = raw
                kill_switch.require_release_allowed()
        engine.begin.assert_not_called()

    def test_unreadable_database_and_malformed_states_fail_closed(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}), mock.patch.object(
                kill_switch.db_layer, "get_engine", side_effect=OSError("unavailable")):
            with self.assertRaises(kill_switch.KillSwitchError):
                kill_switch.require_release_allowed()
        for state in ({}, {"active": "false"}, {"active": None}, {"active": True}):
            with self.subTest(state=state), mock.patch.object(kill_switch, "get_state", return_value=state):
                with self.assertRaises(kill_switch.KillSwitchError):
                    kill_switch.require_release_allowed()


if __name__ == "__main__":
    unittest.main()
