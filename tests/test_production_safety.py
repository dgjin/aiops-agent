"""批次A安全回归；只用临时SQLite/文件与mock，不接触真实Docker/DB/数据。

入口：.venv/bin/python -B -m unittest discover -s tests -t . -p test_production_safety.py -v
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import REPO_ROOT, TEST_ROOT


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load_script("production_smoke_test", "production-smoke.py")
baseline = sys.modules["production_baseline"]
TEST_ID = "a" * 32
CID = "b" * 64
SERVER_UUID = "12345678-1234-1234-1234-123456789abc"
SECRET = "must-not-appear-secret-token"


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=f"aiops-smoke-{TEST_ID}-", dir=Path("/tmp").resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "fixture-repo"
        self.repo.mkdir()
        self.db = self.root / "fixture.sqlite"
        self.url_file = self.root / "database-url"
        self.url_file.write_text("sqlite:///" + str(self.db))
        self.url_file.chmod(0o600)
        self.config = {"test_id": TEST_ID, "namespace": "aiops-smoke-" + TEST_ID,
                       "backend": "sqlite", "app_port": 29881, "target_repo": str(self.repo)}
        self.save_config()
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE aiops_test_identity(test_id TEXT, purpose TEXT, server_uuid TEXT);
                CREATE TABLE monitored_apps(id TEXT, repo TEXT, url TEXT, service TEXT, enabled INTEGER);
                CREATE TABLE system_kv(kv_key TEXT, kv_value TEXT);
                CREATE TABLE console_tokens(token_hash TEXT, state TEXT);
                CREATE TABLE console_users(username TEXT, password_hash TEXT, state TEXT);
                CREATE TABLE audit_events(id INTEGER, detail TEXT);
                CREATE TABLE workflow_runs(workflow_id TEXT, stage TEXT, exec_status TEXT);
                CREATE TABLE escalations(id TEXT, status TEXT);
            """)
            conn.execute("INSERT INTO aiops_test_identity VALUES (?, ?, ?)", (TEST_ID, "production-smoke", "sqlite-fixture"))
            conn.execute("INSERT INTO monitored_apps VALUES (?, ?, ?, ?, ?)",
                         ("app-1", str(self.repo), "http://127.0.0.1:29881/", "fixture", 1))
            conn.execute("INSERT INTO console_tokens VALUES (?, 'active')", (SECRET,))
            conn.execute("INSERT INTO console_users VALUES ('alice', ?, 'active')", (SECRET,))
            conn.execute("INSERT INTO system_kv VALUES ('kill_switch', ?)", (SECRET,))
            conn.execute("INSERT INTO workflow_runs VALUES ('wf-1', 'WAIT_APPROVAL', 'RUNNING')")
            conn.execute("INSERT INTO escalations VALUES ('esc-1', 'open')")

    def save_config(self):
        (self.root / ".aiops-isolation.json").write_text(json.dumps(self.config))

    def inputs(self):
        return smoke.validate_inputs(TEST_ID, self.root, self.url_file)

    def mysql(self, *, port=29882, host="127.0.0.1", database=None, query=""):
        self.config.update(backend="mysql", compose_project="aiops-smoke-" + TEST_ID,
                           container_id=CID, server_uuid=SERVER_UUID)
        self.save_config()
        db = database or "aiops_test_" + TEST_ID
        self.url_file.write_text(f"mysql+pymysql://aiops_test:{SECRET}@{host}:{port}/{db}{query}")

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}


class SmokeSafetyTest(FixtureTest):
    def test_sqlite_smoke_is_readonly_and_does_not_import_application(self):
        before = self.snapshot()
        config, raw = self.inputs()
        with baseline.readonly_connection(raw) as conn:
            result = smoke.verify_database(conn, config)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(before, self.snapshot())

    def test_successful_main_never_imports_application_and_drops_real_env(self):
        import builtins
        original = builtins.__import__

        def no_application(name, *args, **kwargs):
            if name.split(".")[0] in {"aiops_agent", "bff", "dotenv"}:
                raise AssertionError("application import before isolation")
            return original(name, *args, **kwargs)

        before = self.snapshot()
        with mock.patch.dict(os.environ, {"AIOPS_CONSOLE_AUTH_TOKENS": SECRET, "OPENAI_API_KEY": SECRET}), \
                mock.patch.object(builtins, "__import__", side_effect=no_application), \
                redirect_stdout(io.StringIO()) as out:
            code = smoke.main(["--test-id", TEST_ID, "--data-root", str(self.root),
                               "--database-url-file", str(self.url_file)])
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            self.assertNotIn("AIOPS_CONSOLE_AUTH_TOKENS", os.environ)
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, out.getvalue())
        self.assertEqual(before, self.snapshot())

    def test_real_roots_and_fake_markers_rejected_before_reading_connection(self):
        for path in (REPO_ROOT, REPO_ROOT / "data", REPO_ROOT / "data/prod", self.repo):
            with self.subTest(path=path), mock.patch.object(smoke, "private_file") as read:
                with self.assertRaises(baseline.SafetyError):
                    smoke.validate_inputs(TEST_ID, path, self.url_file)
                read.assert_not_called()

    def test_missing_or_wrong_file_marker(self):
        self.config["test_id"] = "c" * 32
        self.save_config()
        with self.assertRaises(baseline.SafetyError):
            self.inputs()
        (self.root / ".aiops-isolation.json").unlink()
        with self.assertRaises(OSError):
            self.inputs()

    def test_stable_container_override_in_manifest_is_not_ignored(self):
        self.config["stable_container"] = "aiops-stable-order"
        self.save_config()
        with self.assertRaises(baseline.SafetyError):
            self.inputs()

    def test_all_known_production_ports_rejected_even_with_test_names(self):
        for port in smoke.REAL_PORTS:
            self.mysql(port=port)
            with self.subTest(port=port), self.assertRaises(baseline.SafetyError):
                self.inputs()

    def test_real_db_host_or_query_socket_cannot_be_overridden_by_name(self):
        for settings in ({"host": "localhost"}, {"host": "192.168.1.2"}, {"host": "aiops-smoke-test"},
                         {"database": "aiops"}, {"query": "?unix_socket=/tmp/mysql.sock"},
                         {"query": "?read_default_file=/tmp/credential"}, {"query": "?host=real"}):
            self.mysql(**settings)
            with self.subTest(settings=settings), self.assertRaises(baseline.SafetyError):
                self.inputs()

    def test_invalid_test_ids(self):
        for value in ("", "smoke", "../" + TEST_ID, "a" * 33):
            with self.subTest(value=value), self.assertRaises(baseline.SafetyError):
                smoke.validate_inputs(value, self.root, self.url_file)

    def test_external_repo_and_symlink_and_git_worktree_rejected(self):
        self.config["target_repo"] = str(REPO_ROOT)
        self.save_config()
        with self.assertRaises(baseline.SafetyError):
            self.inputs()
        self.config["target_repo"] = str(self.repo)
        self.save_config()
        (self.repo / "escape").symlink_to(REPO_ROOT)
        with self.assertRaises(baseline.SafetyError):
            self.inputs()
        (self.repo / "escape").unlink()
        (self.repo / ".git").write_text("gitdir: " + str(REPO_ROOT / ".git"))
        with self.assertRaises(baseline.SafetyError):
            self.inputs()

    def test_external_sqlite_path_rejected_before_reading_its_header(self):
        self.url_file.write_text("sqlite:///" + str(REPO_ROOT / "data/prod/real.sqlite"))
        with mock.patch.object(smoke, "database_url") as validate:
            with self.assertRaises(baseline.SafetyError):
                self.inputs()
        validate.assert_not_called()

    def test_database_path_permissions_and_hardlinks(self):
        self.url_file.chmod(0o644)
        with self.assertRaises(baseline.SafetyError):
            self.inputs()
        self.url_file.chmod(0o600)
        os.link(self.db, self.root / "alias.sqlite")
        with self.assertRaises(baseline.SafetyError):
            self.inputs()

    def test_database_marker_missing_wrong_or_duplicate_rejected_without_repair(self):
        for sql in ("DELETE FROM aiops_test_identity", "UPDATE aiops_test_identity SET test_id='wrong'",
                    "INSERT INTO aiops_test_identity SELECT * FROM aiops_test_identity"):
            with self.subTest(sql=sql):
                with sqlite3.connect(self.db) as conn:
                    conn.execute("DELETE FROM aiops_test_identity")
                    conn.execute("INSERT INTO aiops_test_identity VALUES (?, ?, ?)",
                                 (TEST_ID, "production-smoke", "sqlite-fixture"))
                    conn.execute(sql)
                before = self.snapshot()
                config, raw = self.inputs()
                with baseline.readonly_connection(raw) as conn, self.assertRaises(baseline.SafetyError):
                    smoke.verify_database(conn, config)
                self.assertEqual(before, self.snapshot())

    def test_missing_schema_refused_without_create_all(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE workflow_runs")
        before = self.snapshot()
        config, raw = self.inputs()
        with baseline.readonly_connection(raw) as conn, self.assertRaises(baseline.SafetyError):
            smoke.verify_database(conn, config)
        self.assertEqual(before, self.snapshot())

    def test_mysql_actual_server_identity_must_match_marker(self):
        self.mysql()
        config, _ = self.inputs()
        conn = mock.MagicMock()
        marker = {"test_id": TEST_ID, "purpose": "production-smoke", "server_uuid": SERVER_UUID}
        conn.execute.return_value.mappings.return_value.all.return_value = [marker]
        conn.execute.return_value.one.return_value = ("wrong-server", "aiops_test_" + TEST_ID)
        with mock.patch("sqlalchemy.inspect") as inspect:
            inspect.return_value.get_table_names.return_value = list(smoke.EXPECTED_TABLES | {"aiops_test_identity"})
            with self.assertRaises(baseline.SafetyError):
                smoke.verify_database(conn, config)

    def test_stored_real_target_rejected_even_in_marked_database(self):
        for repo, url in ((str(REPO_ROOT), "http://127.0.0.1:29881/"),
                          (str(self.repo), "http://127.0.0.1:18080/aiops-smoke-" + TEST_ID),
                          (str(self.repo), "http://aiops-smoke.example:29881/"),
                          (str(self.repo), "http://token:secret@127.0.0.1:29881/")):
            with sqlite3.connect(self.db) as conn:
                conn.execute("UPDATE monitored_apps SET repo=?, url=?", (repo, url))
            config, raw = self.inputs()
            with self.subTest(url=url), baseline.readonly_connection(raw) as conn:
                with self.assertRaises(baseline.SafetyError):
                    smoke.verify_database(conn, config)

    def test_main_refuses_before_database_connection_and_masks_errors(self):
        self.mysql()
        with mock.patch.dict(os.environ, {"AIOPS_CONSOLE_AUTH_TOKENS": SECRET}), \
                mock.patch.object(smoke, "verify_container", side_effect=RuntimeError(SECRET)), \
                mock.patch.object(smoke, "readonly_connection") as connect, \
                redirect_stderr(io.StringIO()) as err:
            code = smoke.main(["--test-id", TEST_ID, "--data-root", str(self.root),
                               "--database-url-file", str(self.url_file)])
            self.assertNotIn("AIOPS_CONSOLE_AUTH_TOKENS", os.environ)
        connect.assert_not_called()
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, err.getvalue())

    def test_cli_missing_explicit_args_never_loads_application_or_dotenv(self):
        script = REPO_ROOT / "scripts/production-smoke.py"
        probe = ("import builtins,runpy,sys; original=builtins.__import__; "
                 "builtins.__import__=lambda n,*a,**k: (_ for _ in ()).throw(RuntimeError('APP_IMPORT')) "
                 "if n.split('.')[0] in ('aiops_agent','bff','dotenv') else original(n,*a,**k); "
                 "sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0],run_name='__main__')")
        result = subprocess.run([sys.executable, "-B", "-c", probe, str(script)], capture_output=True, text=True,
                                env={**os.environ, "AIOPS_DATABASE_URL": SECRET, "AIOPS_CONSOLE_AUTH_TOKENS": SECRET})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("APP_IMPORT", result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)


class ContainerIdentityTest(FixtureTest):
    def setUp(self):
        super().setUp()
        self.mysql()
        self.cfg, _ = self.inputs()
        p = "aiops-smoke-" + TEST_ID
        self.info = {"id": CID, "name": "/" + p + "-mysql", "test_id": TEST_ID, "project": p,
                     "running": True, "privileged": False, "network": p + "-private",
                     "networks": {p + "-private": {}}, "mounts": [{"Type": "volume", "Name": p + "-mysql",
                                                                  "Destination": "/var/lib/mysql"}],
                     "ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "29882"}]}}
        self.net = {"internal": True, "test_id": TEST_ID, "containers": {CID: {}}}

    def run_identity(self, *, owners=CID, label=TEST_ID):
        with mock.patch.object(smoke, "run_readonly", side_effect=[json.dumps(self.info), json.dumps(self.net), label, owners]) as run:
            smoke.verify_container(self.cfg)
            commands = repr(run.call_args_list)
            self.assertNotIn(".Config.Env", commands)
            self.assertNotIn("exec", commands)

    def test_full_identity_passes(self):
        self.run_identity()

    def test_real_container_alias_privilege_ports_mounts_or_id_refused(self):
        changes = ({"name": "/aiops-stable-order"}, {"id": "c" * 64}, {"privileged": True},
                   {"network": "host"}, {"project": "aiops"}, {"running": False},
                   {"mounts": [{"Type": "bind", "Source": str(REPO_ROOT), "Destination": "/var/lib/mysql"}]},
                   {"ports": {"3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "29882"}]}},
                   {"ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3316"}]}})
        for change in changes:
            with self.subTest(change=change), mock.patch.dict(self.info, change):
                with self.assertRaises(baseline.SafetyError):
                    self.run_identity()

    def test_shared_network_and_shared_volume_refused(self):
        with mock.patch.dict(self.net, {"containers": {CID: {}, "c" * 64: {}}}):
            with self.assertRaises(baseline.SafetyError):
                self.run_identity()
        with self.assertRaises(baseline.SafetyError):
            self.run_identity(owners=CID + "\n" + "c" * 64)
        with self.assertRaises(baseline.SafetyError):
            self.run_identity(label="wrong")


class BaselineSafetyTest(FixtureTest):
    def test_readonly_connection_rejects_writes_at_sql_and_database_levels(self):
        before = self.snapshot()
        with baseline.readonly_connection(self.url_file.read_text()) as conn:
            for sql in ("CREATE TABLE unsafe(id INT)", "DELETE FROM console_tokens", "PRAGMA query_only=OFF",
                        "SELECT 1 INTO OUTFILE '/tmp/leak'", "SELECT GET_LOCK('unsafe', 1)"):
                with self.subTest(sql=sql), self.assertRaises(baseline.SafetyError):
                    conn.exec_driver_sql(sql)
            with self.assertRaises(sqlite3.OperationalError):
                conn.connection.driver_connection.execute("DELETE FROM console_tokens")
        self.assertEqual(before, self.snapshot())

    def test_missing_sqlite_is_not_created(self):
        path = self.root / "missing.sqlite"
        with self.assertRaises(baseline.SafetyError), baseline.readonly_connection("sqlite:///" + str(path)):
            pass
        self.assertFalse(path.exists())

    def test_wal_database_refused_to_avoid_shared_memory_side_effects(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        before = self.snapshot()
        with self.assertRaises(baseline.SafetyError), baseline.readonly_connection(self.url_file.read_text()):
            pass
        self.assertEqual(before, self.snapshot())

    def test_inventory_counts_ids_states_but_never_secrets_or_live_claim(self):
        before = self.snapshot()
        result = baseline.inventory_database(self.url_file.read_text(), baseline.Redactor())
        self.assertEqual(result["tables"]["console_tokens"]["count"], 1)
        self.assertEqual(result["tables"]["workflow_runs"]["states"]["stage:WAIT_APPROVAL"], 1)
        self.assertEqual(result["tables"]["console_sessions"]["status"], "absent")
        self.assertEqual(result["existing_table_count"], 8)
        text = json.dumps(result)
        for secret in (SECRET, "alice", "wf-1", str(self.repo)):
            self.assertNotIn(secret, text)
        self.assertIn("unknown", result["running_workflows"])
        self.assertEqual(before, self.snapshot())

    def test_dual_mode_comparison_and_no_seeding_missing_files(self):
        demo = self.root / "demo"
        demo.mkdir()
        (demo / "monitored_apps.json").write_text(json.dumps([{"id": "app-1"}, {"id": "app-2"}]))
        (demo / "console_tokens.json").write_text(json.dumps({"tokens": [{"token": SECRET}]}))
        redactor = baseline.Redactor()
        before = self.snapshot()
        files = baseline.inventory_files(demo, redactor)
        db = baseline.inventory_database(self.url_file.read_text(), redactor)
        delta = baseline.differences(files, db)
        self.assertEqual(delta["monitored_apps"]["count_delta_production_minus_demo"], -1)
        self.assertEqual(delta["monitored_apps"]["shared_count"], 1)
        self.assertEqual(delta["console_users"]["status"], "unknown")
        self.assertNotIn(SECRET, json.dumps(files))
        self.assertFalse((demo / "console_users.json").exists())
        self.assertEqual(before, self.snapshot())

    def test_dual_mode_detects_changed_config_without_revealing_urls(self):
        redactor = baseline.Redactor()
        row = {"id": "app-1", "url": "http://u:p@127.0.0.1:29881/?token=" + SECRET,
               "repo": str(self.repo), "service": "fixture", "enabled": True}
        a = baseline.summarize_records([row], "id", redactor)
        b = baseline.summarize_records([{**row, "service": "changed"}], "id", redactor)
        c = baseline.summarize_records([{**row, "url": "http://127.0.0.1:29881/"}], "id", redactor)
        self.assertEqual(a["config_fingerprints"], c["config_fingerprints"])
        delta = baseline.differences({"tables": {"monitored_apps": a}}, {"tables": {"monitored_apps": b}})
        self.assertEqual(len(delta["monitored_apps"]["changed_config_ids"]), 1)
        self.assertNotIn(SECRET, json.dumps(delta))
        self.assertNotIn(str(self.repo), json.dumps(a))

    def test_unsupported_or_symlink_file_is_unknown_not_empty(self):
        (self.root / "monitored_apps.json").write_text("{bad json: " + SECRET)
        (self.root / "console_tokens.json").symlink_to(self.url_file)
        result = baseline.inventory_files(self.root, baseline.Redactor())
        self.assertEqual(result["tables"]["monitored_apps"]["status"], "unknown")
        self.assertEqual(result["tables"]["console_tokens"]["status"], "unknown")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_mysql_sets_session_readonly_before_select_and_rolls_back(self):
        import sqlalchemy
        conn = mock.MagicMock()
        engine = mock.MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        with mock.patch.object(sqlalchemy, "create_engine", return_value=engine) as create, \
                mock.patch.object(sqlalchemy.event, "listen"):
            with baseline.readonly_connection("mysql+pymysql://readonly:fake@127.0.0.1:29882/fixture"):
                pass
        statements = [c.args[0] for c in conn.exec_driver_sql.call_args_list]
        self.assertEqual(statements, ["SET SESSION TRANSACTION READ ONLY", "SET SESSION MAX_EXECUTION_TIME=5000",
                                      "START TRANSACTION READ ONLY"])
        self.assertTrue(create.call_args.kwargs["hide_parameters"])
        conn.rollback.assert_called_once()
        engine.dispose.assert_called_once()

    def test_output_only_ignored_new_private_file_and_no_overwrite(self):
        fake_repo = self.root / "repo"
        fake_repo.mkdir()
        target = fake_repo / "data/production-baseline/result.json"
        with mock.patch.object(baseline, "ROOT", fake_repo), mock.patch.object(baseline, "run_readonly") as run:
            baseline.write_report(target, {"safe": True})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn("check-ignore", repr(run.call_args_list))
            with self.assertRaises(baseline.SafetyError):
                baseline.write_report(target, {"overwrite": True})
            with self.assertRaises(baseline.SafetyError):
                baseline.write_report(fake_repo / "leak.json", {})

    def test_unignored_output_refused_before_db_or_files(self):
        target = self.root / "repo/data/production-baseline/result.json"
        with mock.patch.object(baseline, "ROOT", self.root / "repo"), \
                mock.patch.object(baseline, "run_readonly", side_effect=baseline.SafetyError("not ignored")), \
                mock.patch.object(baseline, "private_file") as read, redirect_stderr(io.StringIO()):
            self.assertEqual(baseline.main(["--output", str(target), "--db-url-file", str(self.url_file)]), 1)
        read.assert_not_called()
        self.assertFalse(target.exists())

    def test_host_failures_are_unknown_and_never_leak_command_errors(self):
        with mock.patch.object(baseline, "run_readonly", side_effect=baseline.SafetyError(SECRET)):
            result = baseline.host_inventory(baseline.Redactor())
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["mysql"], {"status": "unknown"})

    def test_host_inventory_projects_mounts_ports_and_digest_are_sanitized(self):
        digest = "sha256:" + "d" * 64
        container = {"image": digest, "project": SECRET, "running": True,
                     "mounts": [{"Type": "bind", "Source": SECRET, "Destination": "/data", "RW": True}],
                     "ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3316"}]},
                     "Env": ["TOKEN=" + SECRET]}
        values = [item for _ in range(6) for item in (json.dumps(container), json.dumps(["mysql@" + digest]))]
        values += ["n127.0.0.1:8600\nn*:8643\n", "123 0 com.aiops.watchdog\n- 1 com.aiops.backup\n"]
        with mock.patch.object(baseline, "run_readonly", side_effect=values) as run:
            result = baseline.host_inventory(baseline.Redactor())
        self.assertEqual(result["mysql"]["digests"], [digest])
        self.assertEqual(result["listen_ports"], [8600, 8643])
        self.assertTrue(result["launchd"]["com.aiops.watchdog"]["running"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(".Config.Env", repr(run.call_args_list))

    def test_db_credentials_require_separate_confirmation(self):
        destination = self.root / "repo/data/production-baseline/out.json"
        with mock.patch.object(baseline, "validate_output", return_value=destination), \
                mock.patch.object(baseline, "private_file") as read, redirect_stderr(io.StringIO()):
            result = baseline.main(["--output", str(destination), "--db-url-file", str(self.url_file)])
        read.assert_not_called()
        self.assertEqual(result, 1)

    def test_child_env_is_allowlisted(self):
        with mock.patch.dict(os.environ, {"AIOPS_CONSOLE_AUTH_TOKENS": SECRET, "OPENAI_API_KEY": SECRET,
                                         "HTTPS_PROXY": SECRET, "DOCKER_HOST": SECRET, "GIT_CONFIG_COUNT": "1"}):
            child = baseline.clean_env()
        self.assertNotIn(SECRET, json.dumps(child))


class UnitEntrypointSafetyTest(unittest.TestCase):
    def test_clean_environment_temp_defaults_and_dotenv_disabled(self):
        import dotenv
        self.assertEqual(os.environ["AIOPS_MODE"], "demo")
        self.assertEqual(os.environ.get("AIOPS_DATABASE_URL"), "")
        self.assertTrue(Path(os.environ["AIOPS_DATA_ROOT"]).is_relative_to(TEST_ROOT))
        self.assertFalse(dotenv.load_dotenv(REPO_ROOT / ".env"))
        self.assertNotIn("AIOPS_CONSOLE_AUTH_TOKENS", os.environ)

    def test_production_root_and_dotenv_access_blocked(self):
        for path in (REPO_ROOT / ".env", REPO_ROOT / "data/console_tokens.json",
                     REPO_ROOT / "data/prod/kill_switch.json"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                path.read_text()

    def test_real_network_blocked_but_local_fixture_server_allowed(self):
        import socket
        with socket.socket() as listener, socket.socket() as client:
            with self.assertRaises(PermissionError):
                client.connect(("127.0.0.1", 29899))
            with self.assertRaises(PermissionError):
                listener.bind(("127.0.0.1", 8600))
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            peer, _ = listener.accept()
            peer.close()

    def test_sqlite_native_connection_cannot_bypass_production_file_guard(self):
        target = REPO_ROOT / "data/prod/forbidden-safety-test.sqlite"
        for database in (str(target), target.as_uri() + "?mode=rwc"):
            with self.subTest(uri=database), self.assertRaises(PermissionError):
                sqlite3.connect(database, uri=database.startswith("file:"))

    def test_real_container_tools_blocked(self):
        with self.assertRaises(PermissionError):
            subprocess.run(["docker", "ps"], check=True)

    def test_compose_exception_is_exact_offline_config_with_isolated_environment(self):
        from tests import _static_compose_only
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "compose.env"
            config.write_text("COMPOSE_PROJECT_NAME=fixture\n")
            argv = ["docker-compose", "--project-name", "fixture", "--env-file", str(config),
                    "-f", str(REPO_ROOT / "docker-compose.yml"),
                    "-f", str(REPO_ROOT / "docker-compose.production.yml"), "--profile", "*", "config", "--format", "json"]
            env = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
            env["COMPOSE_DISABLE_ENV_FILE"] = "1"
            self.assertTrue(_static_compose_only("docker-compose", argv, REPO_ROOT, env))
            self.assertTrue(_static_compose_only("docker", ["docker", "compose", *argv[1:]], REPO_ROOT, env))
            for index, value in ((11, "up"), (4, str(REPO_ROOT / ".env")), (6, str(config)), (13, "yaml")):
                changed = list(argv)
                changed[index] = value
                self.assertFalse(_static_compose_only("docker-compose", changed, REPO_ROOT, env))
            for changes in ({"DOCKER_HOST": "unix:///var/run/docker.sock"}, {"HOME": "/real"},
                            {"AIOPS_CONSOLE_AUTH_TOKENS": SECRET}, {"DOCKER_CONTEXT": "production"}):
                self.assertFalse(_static_compose_only("docker-compose", argv, REPO_ROOT, {**env, **changes}))

    def test_inherited_secrets_and_roots_cannot_reenter_child_tests(self):
        code = ("import tests,os; from pathlib import Path; import dotenv; "
                "assert os.environ.get('OPENAI_API_KEY') is None; "
                "assert os.environ.get('AIOPS_CONSOLE_AUTH_TOKENS') is None; "
                "assert os.environ['AIOPS_DATABASE_URL']==''; "
                "assert Path(os.environ['AIOPS_DATA_ROOT']).is_relative_to(tests.TEST_ROOT); "
                "assert not dotenv.load_dotenv(); print('isolated')")
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                                env={**os.environ, "OPENAI_API_KEY": SECRET, "AIOPS_CONSOLE_AUTH_TOKENS": SECRET,
                                     "AIOPS_DATABASE_URL": SECRET, "AIOPS_DATA_ROOT": str(REPO_ROOT / "data/prod")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "isolated")
        self.assertNotIn(SECRET, result.stderr)


if __name__ == "__main__":
    unittest.main()
