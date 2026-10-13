"""批次H定向测试：先隔离tests包，所有材料均在临时目录，不连接真实服务。"""
from __future__ import annotations

import tests  # 先执行测试包的模式隔离，禁止提前导入应用配置。

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from aiops_agent import backup, release_journal

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup-data.py"
spec = importlib.util.spec_from_file_location("backup_cli_test", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def sql_fixture(value: str = "原始内容") -> bytes:
    return "".join(
        f"USE `{db}`;\nCREATE TABLE `records` (\n  `id` int NOT NULL,\n  `value` text\n) ENGINE=InnoDB;\n"
        f"INSERT INTO `records` (`id`, `value`) VALUES (1,'{value}');\n"
        for db in ("aiops", "temporal_prod", "temporal_visibility_prod")
    ).encode()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aiops-backup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / "repo"
        self.data = self.repo / "data"
        self.out = self.base / "archives"
        self.key = self.base / "keys" / "backup.key"
        self.config = self.base / "backup.json"
        for path in (self.repo, self.data, self.out, self.key.parent):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write(self.key, b"K" * 32)
        self.write(self.data / "notify" / "event.json", b'{"test_only":true}')
        self.raw = {"mode": "demo", "data_root": str(self.data), "backup_dir": str(self.out),
                    "key_file": str(self.key), "sources": {}, "offsite": {"enabled": False}}
        self.env = mock.patch.dict(os.environ, {"AIOPS_MODE": "demo", "AIOPS_DATA_ROOT": str(self.data)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        roots = mock.patch.object(backup, "ROOT", self.repo)
        roots.start()
        self.addCleanup(roots.stop)
        # H接线：备份生产的 release_lock/记录必须与发布控制目录一致；夹具重定位控制根。
        control = mock.patch.object(release_journal, "CONTROL_ROOT", self.base / "control")
        control.start()
        self.addCleanup(control.stop)
        # 所有测试默认拒绝任何子进程；需要验证命令构造时仅安装显式内存替身。
        self.process = mock.patch.object(backup.subprocess, "run", side_effect=AssertionError("禁止真实子进程"))
        self.process.start()
        self.addCleanup(self.process.stop)

    def write(self, path, data):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def save_config(self):
        self.write(self.config, json.dumps(self.raw).encode())
        return backup.load_config(self.config)

    def production(self):
        os.environ["AIOPS_MODE"] = "production"
        self.raw["mode"] = "production"
        sources = {}
        for role in backup.ROLES:
            path = self.base / "sources" / role
            if role in {"policy", "environment", "positions"}:
                self.write(path, b"TEST_ONLY=not-a-real-credential\n")
            else:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            sources[role] = str(path)
        tls = Path(sources["tls"])
        for name in ("ca/ca.crt", "ca/ca.key", "version-1/leaf.crt", "version-1/leaf.key"):
            self.write(tls / name, b"TEST_ONLY_TLS_MATERIAL")
        (tls / "current").symlink_to("version-1", target_is_directory=True)
        cnf = self.write(self.base / "mysql.cnf", b"[client]\nuser=test_only\npassword=not-real\nhost=127.0.0.1\nport=23317\n")
        binary = self.write(self.base / "mysqldump", b"TEST_ONLY_BINARY")
        lease = self.write(self.base / "lease.json", json.dumps({
            "mode": "production", "data_root": str(self.data), "writers_paused": True,
            "releases_idle": True, "expires_at": time.time() + 1800}).encode())
        guard = self.write(Path(release_journal.coordination_paths()["release_lock"]), b"")
        shared = release_journal.coordination_paths()
        sources["release_records"] = shared["release_records"]
        sources["release_recovery"] = shared["release_recovery"]
        self.raw.update(sources=sources, quiescence_file=str(lease), release_lock=str(guard),
                        mysql={"defaults_file": str(cnf), "dump_binary": str(binary),
                               "databases": ["aiops", "temporal_prod", "temporal_visibility_prod"]})
        return self.save_config()

    def fake_dump(self, cfg, path):
        self.write(path, sql_fixture())
        return {**backup.sql_stats(path), "server_version": "8-test-only"}

    def production_archive(self):
        cfg = self.production()
        with mock.patch.object(backup, "dump_mysql", side_effect=self.fake_dump):
            archive = backup.create_backup(cfg)
        return cfg, archive

    def test_demo_complete_encrypted_round_trip_and_permissions(self):
        cfg = self.save_config()
        archive = backup.create_backup(cfg)
        self.assertTrue(archive.read_bytes().startswith(backup.MAGIC))
        self.assertNotIn(b"test_only", archive.read_bytes())
        extracted = self.base / "extracted"
        manifest = backup.verify_archive(archive, b"K" * 32, self.out, extracted)
        self.assertEqual((extracted / "data/notify/event.json").read_bytes(), b'{"test_only":true}')
        self.assertFalse(any("backup.key" in k for k in manifest["files"]))
        for root in (self.out, extracted):
            for path in (root, *root.rglob("*")):
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))
        self.assertFalse(backup.status(self.out, current_mode="demo")["stale"])

    def test_production_three_databases_and_all_roles(self):
        cfg, archive = self.production_archive()
        manifest = backup.verify_archive(archive, b"K" * 32, self.out)
        self.assertEqual(manifest["mysql"]["rows"], 3)
        self.assertEqual(len(manifest["mysql"]["tables"]), 3)
        self.assertEqual(manifest["data_root"], str(self.data))
        self.assertTrue(backup.ROLES <= manifest["sources"].keys())
        self.assertIn("tls/current/leaf.key", manifest["files"])
        self.assertIn("legacy_notify/event.json", manifest["files"])
        self.assertIn("mysql_credentials/mysql.cnf", manifest["files"])
        self.assertEqual(manifest["runtime_acceptance"], "blocked")
        self.assertEqual(manifest["notes"]["tls/current"]["materialized_link"], "version-1")

    def test_default_mode_roots_follow_existing_module(self):
        self.write(self.config, json.dumps(self.raw).encode())
        os.environ.pop("AIOPS_DATA_ROOT")
        with mock.patch.object(backup.mode, "_REPO_ROOT", self.repo):
            self.assertEqual(backup.load_config(self.config)["data_root"], str(self.data))
            (self.data / "prod").mkdir(mode=0o700)
            cfg = self.save_config()
            archive = backup.create_backup(cfg)
            manifest = backup.verify_archive(archive, b"K" * 32, self.out)
            self.assertEqual(manifest["notes"]["data/prod"]["excluded"], "另一模式的数据根")

    def test_production_missing_config_fails_closed(self):
        self.raw["mode"] = "production"
        os.environ["AIOPS_MODE"] = "production"
        with self.assertRaises(backup.BackupError):
            self.save_config()
        cfg = self.production()
        for field in ("mysql", "release_lock", "quiescence_file"):
            broken = copy.deepcopy(self.raw)
            broken.pop(field)
            self.write(self.config, json.dumps(broken).encode())
            with self.subTest(field=field), self.assertRaises((backup.BackupError, OSError)):
                backup.load_config(self.config)
        self.assertEqual(cfg["mode"], "production")

    def test_mode_root_and_policy_mismatch_rejected(self):
        self.raw["mode"] = "production"
        with self.assertRaises(backup.BackupError):
            self.save_config()
        self.raw["mode"] = "demo"
        self.raw["data_root"] = str(self.base)
        with self.assertRaises(backup.BackupError):
            self.save_config()
        self.raw["data_root"] = str(self.data)
        self.production()
        os.environ["AIOPS_POLICY_PATH"] = str(self.base / "different-policy")
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_demo_never_connects_mysql_even_if_configured(self):
        self.raw["mysql"] = {"databases": ["aiops"]}
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_key_permissions_size_and_location_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(backup.BackupError):
            self.save_config()
        self.write(self.key, b"short")
        with self.assertRaises(backup.BackupError):
            self.save_config()
        unsafe = self.write(self.data / "key", b"K" * 32)
        self.raw["key_file"] = str(unsafe)
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_source_output_overlap_and_reserved_role_rejected(self):
        self.raw["backup_dir"] = str(self.data / "archives")
        with self.assertRaises(backup.BackupError):
            self.save_config()
        self.raw["backup_dir"] = str(self.out)
        self.raw["sources"] = {"data": str(self.base)}
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_offsite_cannot_be_enabled(self):
        self.raw["offsite"] = {"enabled": True, "target": "https://not-allowed"}
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_no_secret_body_read_in_dry_run_and_readiness_blocked(self):
        self.save_config()
        real_open = Path.open
        def guarded(path, *args, **kwargs):
            if path in {self.key, self.data / "notify/event.json"}:
                raise AssertionError("dry-run不应读取正文")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", guarded), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(self.config), "--dry-run"]), 0)
            self.assertEqual(cli.main(["--config", str(self.config), "--readiness"]), 2)
        self.assertIn('"runtime_acceptance": "blocked"', out.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_mutating_file_rejects_archive_and_records_failure(self):
        cfg = self.save_config()
        original = backup.copy_snapshot
        def mutate(source, target, expected):
            if source.name == "event.json":
                source.write_bytes(b"changed")
            return original(source, target, expected)
        with mock.patch.object(backup, "copy_snapshot", side_effect=mutate):
            with self.assertRaises(backup.BackupError):
                backup.create_backup(cfg)
        self.assertEqual(list(self.out.glob("*.aes")), [])
        state = backup.status(self.out, current_mode="demo")
        self.assertTrue(state["stale"])
        self.assertIsNotNone(state["last_failure"])

    def test_source_symlink_and_hardlink_rejected(self):
        cfg = self.save_config()
        (self.data / "outside").symlink_to(self.key)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)
        other = self.base / "hardlink-data"
        other.mkdir(mode=0o700)
        os.link(self.key, other / "key-copy")
        cfg["sources"] = {"data": str(other)}
        with self.assertRaises(backup.BackupError):
            backup.inventory(cfg)

    def test_tls_escape_rejected_and_missing_material_fails(self):
        cfg = self.production()
        tls = Path(cfg["sources"]["tls"])
        self.assertIn("tls/current/leaf.key", backup.inventory(cfg)[0])
        cfg["sources"]["tls"] = str(self.base / "sources" / "audit")
        with self.assertRaises(backup.BackupError):
            backup.inventory(cfg)
        alternate = self.base / "evil-tls"
        alternate.mkdir(mode=0o700)
        (alternate / "current").symlink_to(self.key.parent)
        cfg["sources"]["tls"] = str(alternate)
        with self.assertRaises(backup.BackupError):
            backup.inventory(cfg)
        self.assertTrue((tls / "current").is_symlink())

    def test_backup_and_release_locks_prevent_reentry(self):
        cfg = self.save_config()
        with backup.lock(self.out / "backup.lock"), self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)
        cfg = self.production()
        with backup.lock(Path(cfg["release_lock"]), existing=True), self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)

    def test_production_backup_refuses_nonterminal_release_record(self):
        # H接线：锁空闲但日志有未结束发布（崩溃残留）时，不得宣称发布空闲
        cfg = self.production()
        with release_journal.locked():
            release_journal.Journal("wf-1", "aiops-production", "p-1").claim(
                "hash", {"kind": "direct", "repo": "/tmp/x"}, ["repo:/tmp/x"])
        with self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)

    def test_production_decoy_release_lock_rejected(self):
        # H接线：release_lock 必须指向发布控制目录的共享锁，与发布执行方同锁
        self.production()
        decoy = self.write(self.base / "decoy.lock", b"")
        self.raw["release_lock"] = str(decoy)
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_quiescence_expiry_and_unsafe_release_rejected(self):
        cfg = self.production()
        for expires, idle in ((time.time() - 1, True), (time.time() + 9000, True), (time.time() + 600, False)):
            self.write(Path(cfg["quiescence_file"]), json.dumps({"mode": "production", "data_root": str(self.data),
                       "writers_paused": True, "releases_idle": idle, "expires_at": expires}).encode())
            with self.subTest(expires=expires, idle=idle), self.assertRaises(backup.BackupError):
                backup.create_backup(cfg)

    def test_aes_tamper_wrong_key_and_truncation_rejected(self):
        archive = backup.create_backup(self.save_config())
        with self.assertRaises(backup.BackupError):
            backup.verify_archive(archive, b"X" * 32, self.out)
        data = bytearray(archive.read_bytes())
        data[len(backup.MAGIC) + 20] ^= 1
        bad = self.write(self.out / "tampered.aes", data)
        with self.assertRaises(backup.BackupError):
            backup.verify_archive(bad, b"K" * 32, self.out)
        truncated = self.write(self.out / "truncated.aes", bytes(data[:10]))
        with self.assertRaises(backup.BackupError):
            backup.verify_archive(truncated, b"K" * 32, self.out)

    def test_manifest_checksum_mismatch_prevents_atomic_publish(self):
        cfg = self.save_config()
        original = backup.encrypt_archive
        def corrupt(staging, manifest, target, key):
            damaged = copy.deepcopy(manifest)
            damaged["files"]["data/notify/event.json"]["sha256"] = "0" * 64
            return original(staging, damaged, target, key)
        with mock.patch.object(backup, "encrypt_archive", side_effect=corrupt), self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)
        self.assertEqual(list(self.out.glob("*.aes")), [])

    def test_retention_seven_and_protected_never_deleted(self):
        cfg = self.save_config()
        protected = backup.create_backup(cfg, protected=True)
        for _ in range(9):
            latest = backup.create_backup(cfg)
        self.assertTrue(protected.exists())
        self.assertTrue(latest.exists())
        self.assertEqual(len(list(self.out.glob("*-daily-*.aes"))), 7)
        self.assertEqual(len(list(self.out.glob("*-protected-*.aes"))), 1)

    def test_retention_rejects_symlinks_keep_zero_and_path_escape(self):
        cfg = self.save_config()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(cfg, keep=0)
        with self.assertRaises(backup.BackupError):
            backup.retain(cfg, 0, b"K" * 32)
        evil = self.out / ("aiops-demo-daily-20261013T030000-" + "a" * 32 + ".aes")
        evil.symlink_to(self.key)
        with self.assertRaises(backup.BackupError):
            backup.retain(cfg, 1, b"K" * 32)
        self.assertEqual(self.key.read_bytes(), b"K" * 32)
        with self.assertRaises(backup.BackupError):
            backup.plain(self.out / ".." / "keys")

    def test_status_26h_future_corrupt_and_last_failure(self):
        cfg = self.save_config()
        backup.create_backup(cfg)
        ts = backup.status(self.out, current_mode="demo")["last_success"]["time"]
        self.assertFalse(backup.status(self.out, ts + 26 * 3600, "demo")["stale"])
        self.assertTrue(backup.status(self.out, ts + 26 * 3600 + 1, "demo")["stale"])
        self.assertTrue(backup.status(self.out, ts - 1, "demo")["stale"])
        with mock.patch.object(backup, "inventory", side_effect=backup.BackupError("测试失败")):
            with self.assertRaises(backup.BackupError):
                backup.create_backup(cfg)
        state = backup.status(self.out, current_mode="demo")
        self.assertEqual(state["last_success"]["time"], ts)
        self.assertIsNotNone(state["last_failure"])
        self.write(self.out / "status-demo.json", b"invalid")
        self.assertTrue(backup.status(self.out, current_mode="demo")["stale"])

    def test_sql_content_not_table_count(self):
        a = self.write(self.base / "a.sql", sql_fixture("one"))
        b = self.write(self.base / "b.sql", sql_fixture("two"))
        first, second = backup.sql_stats(a), backup.sql_stats(b)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(set(first["tables"]), set(second["tables"]))
        self.assertNotEqual(first, second)
        self.assertEqual(first["tables"]["aiops.records"]["rows"], 1)

    def test_three_database_single_dump_ddl_lock_and_stream_output(self):
        cfg = self.production()
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [(db, "records", "InnoDB", "BASE TABLE") for db in cfg["mysql"]["databases"]]
        cursor.fetchone.return_value = ("8-test",)
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        calls = []
        def process(argv, **kwargs):
            self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("not-real", " ".join(argv))
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            kwargs["stdout"].write(sql_fixture())
            calls.append(argv)
        with mock.patch("pymysql.connect", return_value=conn), mock.patch.object(backup.subprocess, "run", side_effect=process):
            result = backup.dump_mysql(cfg, self.base / "all.sql")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-4:], ["--databases", *cfg["mysql"]["databases"]])
        self.assertIn(mock.call("LOCK INSTANCE FOR BACKUP"), cursor.execute.call_args_list)
        self.assertIn(mock.call("UNLOCK INSTANCE"), cursor.execute.call_args_list)
        self.assertEqual(result["rows"], 3)
        conn.close.assert_called_once()

    def test_nontransactional_tables_and_export_fail_closed(self):
        cfg = self.production()
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [(db, "records", "MyISAM", "BASE TABLE") for db in cfg["mysql"]["databases"]]
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch("pymysql.connect", return_value=conn), self.assertRaises(backup.BackupError):
            backup.dump_mysql(cfg, self.base / "bad.sql")
        conn.close.assert_called_once()
        with mock.patch.object(backup.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "secret")):
            with self.assertRaisesRegex(backup.BackupError, "原始错误已隐藏"):
                backup.run_file(["test-only"], self.base / "failed-output")

    def test_unsafe_archive_paths_rejected(self):
        for name in ("../escape", "/absolute", "a/../b", "a//b", "a\\b"):
            with self.subTest(name=name), self.assertRaises(backup.BackupError):
                backup.safe_member(name)

    def target(self):
        marker, cid = "c" * 32, "d" * 64
        target = {"test_marker": marker, "container": "aiops-backup-test-mysql", "container_id": cid,
                  "volume": "aiops-backup-test-volume", "port": 23316}
        info = {"id": cid, "name": "/" + target["container"], "labels": {"io.aiops.backup-test": marker},
                "running": True, "network": "aiops-backup-test-net", "privileged": False,
                "networks": {"aiops-backup-test-net": {}}, "mounts": [
                    {"Type": "volume", "Name": target["volume"], "Destination": "/var/lib/mysql"}],
                "ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23316"}], "33060/tcp": None}}
        network = {"internal": True, "labels": info["labels"], "containers": {cid: {}}}
        return target, info, network

    def test_restore_target_requires_real_identity_checks(self):
        target, info, network = self.target()
        with mock.patch.object(backup, "docker_json", side_effect=[info, network, info["labels"]]), mock.patch.object(
                backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"dddddddddddd\n")):
            backup.validate_target(target)
        for field, value in (("container", "aiops-mysql"), ("volume", "mysql-data"), ("port", 3316), ("test_marker", "")):
            bad = {**target, field: value}
            with self.subTest(field=field), self.assertRaises(backup.BackupError):
                backup.validate_target(bad)

    def test_restore_shared_or_host_volume_and_external_network_rejected(self):
        target, info, network = self.target()
        bad = copy.deepcopy(info)
        bad["mounts"][0]["Type"] = "bind"
        with mock.patch.object(backup, "docker_json", return_value=bad), self.assertRaises(backup.BackupError):
            backup.validate_target(target)
        with mock.patch.object(backup, "docker_json", side_effect=[info, {**network, "internal": False}]), self.assertRaises(backup.BackupError):
            backup.validate_target(target)
        with mock.patch.object(backup, "docker_json", side_effect=[info, network, info["labels"]]), mock.patch.object(
                backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"dddddddddddd\nother\n")), self.assertRaises(backup.BackupError):
            backup.validate_target(target)

    def test_privileged_or_extra_network_target_rejected(self):
        target, info, _ = self.target()
        for change in ({"privileged": True}, {"networks": {"aiops-backup-test-net": {}, "production": {}}}):
            with self.subTest(change=change), mock.patch.object(backup, "docker_json", return_value={**info, **change}), self.assertRaises(backup.BackupError):
                backup.validate_target(target)

    def restore_runner(self, existing=False, changed=False):
        def run(argv, output, stdin=None):
            for forced in ("--protocol=TCP", "--host=127.0.0.1", "--port=3306", "--user=aiops_restore_test"):
                self.assertIn(forced, argv)
            if "SHOW GRANTS FOR CURRENT_USER" in argv:
                data = b"GRANT SHOW DATABASES ON *.* TO 'aiops_restore_test'@'localhost'\n"
            elif "SHOW DATABASES" in argv:
                data = b"mysql\nsys\ninformation_schema\nperformance_schema\n" + (b"real_business\n" if existing else b"")
            elif "mysqldump" in argv:
                data = sql_fixture("changed" if changed else "原始内容")
            else:
                self.assertIsNotNone(stdin)
                self.assertNotIsInstance(stdin, bytes)
                self.assertEqual(stdin.read(), sql_fixture())
                data = b""
            self.write(output, data)
        return run

    def test_isolated_restore_verifies_contents_but_temporal_stays_blocked(self):
        cfg, archive = self.production_archive()
        target, _, _ = self.target()
        with mock.patch.object(backup, "validate_target"), mock.patch.object(backup, "run_file", side_effect=self.restore_runner()):
            report = backup.restore_test(cfg, archive, target)
        self.assertTrue(report["files_verified"])
        self.assertTrue(report["mysql_content_verified"])
        self.assertEqual(report["runtime_acceptance"], "blocked")
        self.assertIn("未连接正式Server", report["temporal_restore"])
        self.assertTrue(report["target_preserved"])

    def test_restore_nonempty_and_same_count_wrong_content_rejected(self):
        cfg, archive = self.production_archive()
        target, _, _ = self.target()
        for existing, changed in ((True, False), (False, True)):
            with self.subTest(existing=existing, changed=changed), mock.patch.object(backup, "validate_target"), mock.patch.object(
                    backup, "run_file", side_effect=self.restore_runner(existing, changed)), self.assertRaises(backup.BackupError):
                backup.restore_test(cfg, archive, target)

    def test_legacy_restore_cli_is_removed_and_approval_required(self):
        self.save_config()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--config", str(self.config), "--restore-test", "--mysql-container", "aiops-mysql"])
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(self.config), "--restore-test", str(self.base / "x.aes")]), 1)
        self.assertIn("approve-test-import", out.getvalue())

    def test_recovery_does_not_require_original_sources_or_database(self):
        cfg, archive = self.production_archive()
        independent = {k: cfg[k] for k in ("mode", "key_file", "backup_dir")}
        self.write(self.config, json.dumps(independent).encode())
        with mock.patch.object(backup.mode, "data_root", side_effect=AssertionError("不应访问原数据根")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(self.config), "--verify", str(archive)]), 0)
        self.assertIn('"files_verified": true', out.getvalue())

    def test_key_revalidated_before_use(self):
        cfg = self.save_config()
        self.key.chmod(0o644)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(cfg)

    def test_mysql_implicit_endpoint_and_root_are_rejected(self):
        cfg = self.production()
        for content in (b"[client]\nuser=root\npassword=test\nhost=127.0.0.1\nport=23317\n",
                        b"[client]\nuser=test\npassword=test\n"):
            self.write(Path(cfg["mysql"]["defaults_file"]), content)
            with mock.patch("pymysql.connect") as connect, self.assertRaises(backup.BackupError):
                backup.dump_mysql(cfg, self.base / "implicit.sql")
            connect.assert_not_called()

    def test_authenticated_archive_cannot_escape_or_duplicate_members(self):
        for index, kind in enumerate(("escape", "link", "duplicate", "missing")):
            archive = self.out / f"hostile-{index}.aes"
            payload = b"test"
            digest = backup.hashlib.sha256(payload).hexdigest()
            manifest = {"format": 1, "files": {"data/test": {"size": len(payload), "sha256": digest}}}
            with backup.new_file(archive) as out:
                writer = backup.EncryptWriter(out, b"K" * 32)
                with tarfile.open(fileobj=writer, mode="w|gz") as tar:
                    content = json.dumps(manifest).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.mode, info.size = 0o600, len(content)
                    tar.addfile(info, io.BytesIO(content))
                    if kind != "missing":
                        member = tarfile.TarInfo("../escape" if kind == "escape" else "data/test")
                        member.mode, member.size = 0o600, len(payload)
                        if kind == "link":
                            member.type, member.linkname, member.size = tarfile.SYMTYPE, "../../keys/backup.key", 0
                        tar.addfile(member, io.BytesIO(payload))
                        if kind == "duplicate":
                            tar.addfile(member, io.BytesIO(payload))
                writer.finish()
            with self.subTest(kind=kind), self.assertRaises(backup.BackupError):
                backup.verify_archive(archive, b"K" * 32, self.out, self.base / f"extract-{index}")
            self.assertFalse((self.base / "escape").exists())

    def test_failed_or_unknown_archives_are_not_pruned(self):
        cfg = self.save_config()
        archive = backup.create_backup(cfg)
        unknown = self.write(self.out / "old-archive.tar.gz", b"old-format")
        bad = self.write(self.out / ("aiops-demo-daily-20200101T000000-" + "b" * 32 + ".aes"), b"corrupt")
        with self.assertRaises(backup.BackupError):
            backup.retain(cfg, 1, b"K" * 32)
        self.assertTrue(archive.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(bad.exists())

    def test_output_directory_symlink_is_rejected(self):
        alias = self.base / "alias-output"
        alias.symlink_to(self.out, target_is_directory=True)
        self.raw["backup_dir"] = str(alias)
        with self.assertRaises(backup.BackupError):
            self.save_config()

    def test_absent_global_visibility_cannot_claim_empty_target(self):
        cfg, archive = self.production_archive()
        target, _, _ = self.target()
        def no_grants(argv, output, stdin=None):
            self.assertIn("SHOW GRANTS FOR CURRENT_USER", argv)
            self.write(output, b"GRANT USAGE ON *.* TO 'test'@'localhost'\n")
        with mock.patch.object(backup, "validate_target"), mock.patch.object(backup, "run_file", side_effect=no_grants), self.assertRaises(backup.BackupError):
            backup.restore_test(cfg, archive, target)

    def test_launchd_template_is_daily_not_installed(self):
        path = SCRIPT.parents[1] / "deploy/launchd/com.aiops.backup.plist"
        with path.open("rb") as stream:
            plist = plistlib.load(stream)
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 3, "Minute": 0})
        self.assertEqual(plist["Umask"], 0o77)
        self.assertFalse(plist["RunAtLoad"])
        self.assertFalse(plist["KeepAlive"])
        self.assertEqual(plist["WorkingDirectory"], "__ROOT__")
        self.assertEqual(plist["ProgramArguments"][0], "__ROOT__/.venv/bin/python")


if __name__ == "__main__":
    unittest.main()
