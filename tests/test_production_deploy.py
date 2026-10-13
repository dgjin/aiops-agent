"""E/F 部署产物隔离测试：不导入应用、不读 .env、不启动 Docker/Caddy 或接线外部服务。"""
from __future__ import annotations

import copy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import tests  # 必须先触发测试包隔离钩子。
from scripts import production_deploy as deploy
from scripts import production_tls as tls
import yaml

# 仅为格式夹具，不是有效生产口令或真实凭证哈希。
RUNTIME = 'isolated-runtime-"password:$value'
MIGRATION = "isolated-migration-password"
METRICS_HASH = "$2b$12$" + "A" * 53


def settings() -> dict:
    return {"project": "aiops-isolated-test", "lan_ip": "192.168.56.10",
            "dns_names": ["host.docker.internal", "gateway.internal"],
            "volumes": {key: "test-existing-" + key for key in deploy.VOLUME_KEYS},
            "caddy_binary": shutil.which("caddy") or "/not-installed/caddy"}


class OverrideLoader(yaml.SafeLoader):
    """仅供静态断言读取覆盖标签；真实合并必须另外由 Compose CLI 验证。"""


def load_override(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


OverrideLoader.add_constructor("!override", load_override)


class TemporaryDeployment(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aiops-deploy-test-",
                                                     dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "rendered"
        self.settings = settings()

    def render(self):
        return deploy.render(self.settings, self.destination, RUNTIME, MIGRATION,
                             METRICS_HASH, test_only=True)


class RenderTests(TemporaryDeployment):
    def test_runtime_migration_and_compose_secrets_are_separate(self):
        self.render()
        server_text = (self.destination / "temporal/production.yaml").read_text()
        server = yaml.safe_load(server_text)
        stores = server["persistence"]["datastores"]
        self.assertEqual({v["sql"]["databaseName"] for v in stores.values()},
                         {"temporal_prod", "temporal_visibility_prod"})
        self.assertTrue(all(v["sql"]["password"] == RUNTIME for v in stores.values()))
        self.assertTrue(all(v["sql"]["user"] == "temporal_runtime" for v in stores.values()))
        self.assertNotIn(MIGRATION, server_text)
        self.assertEqual((self.destination / "migration-password").read_text(), MIGRATION)
        env = (self.destination / "compose.env").read_text()
        self.assertNotIn(RUNTIME, env)
        self.assertNotIn(MIGRATION, env)
        self.assertNotIn("TOKEN", env)

    def test_monitoring_fragments_only_reference_dedicated_files_and_verified_tls(self):
        self.render()
        prometheus = yaml.safe_load((self.destination / "prometheus.fragment.yml").read_text())
        for job in prometheus["scrape_configs"]:
            self.assertEqual(job["scheme"], "https")
            self.assertEqual(job["basic_auth"]["username"], "aiops_metrics")
            self.assertIn("password_file", job["basic_auth"])
            self.assertNotIn("insecure_skip_verify", job["tls_config"])
        alertmanager = yaml.safe_load((self.destination / "alertmanager.fragment.yml").read_text())
        hook = alertmanager["receivers"][0]["webhook_configs"][0]
        self.assertEqual(hook["url"], "https://192.168.56.10:8643/webhook")
        self.assertIn("credentials_file", hook["http_config"]["authorization"])

    def test_permissions_all_files_600_directories_700(self):
        self.render()
        for path in [self.destination, *self.destination.rglob("*")]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)

    def test_plist_is_valid_and_only_caddy_is_managed(self):
        self.render()
        value = plistlib.loads((self.destination / "com.aiops.caddy.plist").read_bytes())
        self.assertEqual(value["ProgramArguments"][0], self.settings["caddy_binary"])
        self.assertEqual(value["WorkingDirectory"], str(deploy.ROOT))
        self.assertEqual(value["Umask"], 0o077)
        self.assertNotIn("bootstrap", value["ProgramArguments"])
        self.assertNotIn("__", str(value))

    def test_refuses_reused_password_bad_hash_or_injection(self):
        for runtime, migration, hashed in [(RUNTIME, RUNTIME, METRICS_HASH),
                                            (RUNTIME, MIGRATION, "plaintext"),
                                            (RUNTIME + "\n", MIGRATION, METRICS_HASH)]:
            with self.subTest(runtime=runtime == RUNTIME):
                with self.assertRaises(deploy.DeployError):
                    deploy.render(self.settings, self.destination, runtime, migration, hashed, test_only=True)
        self.assertFalse(self.destination.exists())

    def test_invalid_settings_are_rejected(self):
        cases = [{"lan_ip": "0.0.0.0"}, {"lan_ip": "127.0.0.1"}, {"lan_ip": "8.8.8.8"},
                 {"lan_ip": "bad"}, {"dns_names": ["*.internal"]}, {"dns_names": ["good\nbad"]},
                 {"caddy_binary": "caddy"}, {"project": "x;echo"}, {"volumes": {}}]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError):
                deploy.validate_settings({**self.settings, **bad})

    def test_no_overwrite_of_active_configuration(self):
        self.render()
        before = (self.destination / "Caddyfile").read_bytes()
        with self.assertRaises(deploy.DeployError):
            self.render()
        self.assertEqual((self.destination / "Caddyfile").read_bytes(), before)

    def test_no_secret_reads_before_explicit_approval(self):
        with patch.object(deploy, "read_secret") as read, redirect_stderr(io.StringIO()):
            result = deploy.main(["render", "--output", str(self.destination), "--test-only"])
        self.assertEqual(result, 1)
        read.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_secret_permissions_and_dotenv_guard(self):
        secret = self.root / "password"
        deploy.atomic_write(secret, RUNTIME + "\n")
        self.assertEqual(deploy.read_secret(secret), RUNTIME)
        secret.chmod(0o644)
        with self.assertRaises(deploy.DeployError):
            deploy.read_secret(secret)
        with self.assertRaises(deploy.DeployError):
            deploy.read_secret(deploy.ROOT / ".env")

    def test_symlink_and_unignored_output_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for output, test_only in [(alias / "config", True), (deploy.ROOT / "deploy/secrets", False),
                                  (deploy.ROOT / "data", False), (deploy.ROOT / "data/production-deploy", True)]:
            with self.subTest(output=output), self.assertRaises(deploy.DeployError):
                deploy.output_root(output, test_only)

    def test_atomic_write_failure_keeps_original(self):
        path = self.root / "atomic"
        deploy.atomic_write(path, "old")
        with patch.object(deploy.os, "replace", side_effect=OSError("模拟失败")):
            with self.assertRaises(OSError):
                deploy.atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertFalse(list(self.root.glob(".pending-*")))

    def test_subprocess_error_does_not_disclose_secret(self):
        with patch.object(deploy.subprocess, "run", side_effect=subprocess.CalledProcessError(
            1, ["tool"], stderr=RUNTIME
        )), self.assertRaises(deploy.DeployError) as error:
            deploy.run(["tool"])
        self.assertNotIn(RUNTIME, str(error.exception))

    def test_environment_does_not_inherit_application_or_compose_overrides(self):
        with patch.dict(os.environ, {"AIOPS_DATABASE_URL": RUNTIME, "COMPOSE_FILE": "/evil",
                                     "COMPOSE_PROFILES": "demo-history", "COMPOSE_ENV_FILES": ".env"}):
            env = deploy.clean_env()
        self.assertEqual(env["COMPOSE_DISABLE_ENV_FILE"], "1")
        for key in ("AIOPS_DATABASE_URL", "COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"):
            self.assertNotIn(key, env)

    def test_port_conflict_fails_without_killing_processes(self):
        with patch.object(deploy.socket, "socket") as factory:
            factory.return_value.__enter__.return_value.bind.side_effect = OSError("占用")
            with self.assertRaises(deploy.DeployError):
                deploy.check_ports(self.settings["lan_ip"])


class ManifestTests(TemporaryDeployment):
    def setUp(self):
        super().setUp()
        # 只使用 SafeLoader 子类；新增标签仅构造标准序列或映射，不构造 Python 对象。
        loader = OverrideLoader((deploy.ROOT / "docker-compose.production.yml").read_text())
        try:
            self.overlay = loader.get_single_data()
        finally:
            loader.dispose()

    def test_versions_schema_and_default_services(self):
        services = self.overlay["services"]
        for name in ("temporal-prod", "temporal-schema", "temporal-namespace"):
            self.assertIn(":1.32.1@sha256:", services[name]["image"])
            self.assertNotIn("latest", services[name]["image"])
        self.assertEqual(services["temporal-prod"]["entrypoint"], ["temporal-server"])
        self.assertEqual(services["temporal-prod"]["command"],
                         ["--config-file", "/etc/aiops-temporal/production.yaml", "start"])
        self.assertNotIn("temporal", services["temporal-prod"]["depends_on"])
        self.assertNotIn("secrets", services["temporal-prod"])
        self.assertEqual(services["temporal"]["profiles"], ["demo-history"])
        self.assertEqual(services["temporal-schema"]["profiles"], ["maintenance"])
        self.assertNotIn("temporal-schema", deploy.SERVICES)
        self.assertNotIn("temporal", deploy.SERVICES)

    def test_schema_script_syntax_and_approval_gates(self):
        script = str(deploy.ROOT / "deploy/temporal/schema.sh")
        subprocess.run(["sh", "-n", script], check=True)
        for action in ("schema", "namespace-create", "namespace-update"):
            result = subprocess.run(["sh", script, action], env={"PATH": "/usr/bin:/bin"},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("尚未单独确认", result.stderr)

    def test_bootstrap_contains_no_accounts_passwords_or_global_privileges(self):
        sql = (deploy.ROOT / "deploy/temporal/bootstrap.sql").read_text()
        statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        self.assertNotIn("CREATE USER", statements)
        self.assertNotIn("IDENTIFIED BY", statements)
        self.assertNotIn("*.*", statements)
        self.assertNotIn("GRANT OPTION", statements)
        runtime = [line for line in statements.splitlines() if "temporal_runtime" in line]
        self.assertEqual(len(runtime), 2)
        self.assertTrue(all("CREATE" not in line and "DROP" not in line for line in runtime))

    def test_caddy_routes_exact_authentication_and_forwarded_headers(self):
        self.render()
        text = (self.destination / "Caddyfile").read_text()
        for value in ("auto_https off", "persist_config off", "127.0.0.1:8600", "127.0.0.1:8099", "127.0.0.1:9091"):
            self.assertIn(value, text)
        for route in ("/internal-metrics/bff", "/internal-metrics/worker"):
            self.assertIn("path " + route + "\n", text)
            self.assertNotIn("path " + route + "*", text)
        self.assertEqual(text.count("basic_auth {"), 2)
        self.assertEqual(text.count("header_up -Authorization"), 2)
        self.assertIn("method POST\n        path /webhook", text)
        # 帮助中心「系统视图」iframe 加载 /diagrams/*.html：生产入口必须放行该静态前缀。
        self.assertIn("/diagrams/*", text)
        for internal in ("metrics", "readyz", "livez", "docs", "openapi[.]json"):
            self.assertIn(internal, text.split("@private", 1)[1].splitlines()[0])
        self.assertIn("header_up X-Forwarded-For {http.request.remote.host}", text)
        self.assertIn("request_header -X-Forwarded-*", text)
        self.assertNotIn("trusted_proxies", text)
        self.assertNotIn("tls internal", text)
        self.assertNotIn("__", text)

    def test_real_compose_merge_without_dotenv_or_daemon_access(self):
        self.render()
        command = deploy.compose_command(self.destination, self.settings["project"])
        try:
            # 只运行配置合并；无 up、pull、run、exec、down 或容器 Env inspect。
            model = json.loads(deploy.run([*command, "--profile", "*", "config", "--format", "json"]))
        except deploy.DeployError:
            self.fail("Compose 配置合并未通过；需要已安装且支持 !override 的 Compose >= 2.24.4")
        deploy.validate_compose(model, deploy.validate_settings(self.settings))
        self.assertNotIn(RUNTIME, json.dumps(model))
        self.assertNotIn(MIGRATION, json.dumps(model))
        # 模拟旧版错误追加端口，断言门禁能拒绝。
        broken = copy.deepcopy(model)
        broken["services"]["mysql"]["ports"].append({"published": "3316", "target": 3306, "host_ip": "0.0.0.0"})
        with self.assertRaises(deploy.DeployError):
            deploy.validate_compose(broken, self.settings)
        broken = copy.deepcopy(model)
        broken["services"]["temporal-prod"]["image"] = "temporalio/server:latest"
        with self.assertRaises(deploy.DeployError):
            deploy.validate_compose(broken, self.settings)
        broken = copy.deepcopy(model)
        broken["services"]["temporal"]["profiles"] = []
        with self.assertRaises(deploy.DeployError):
            deploy.validate_compose(broken, self.settings)

    @unittest.skipUnless(shutil.which("caddy"), "未安装 Caddy；仅完成静态路由断言，未伪称官方解析通过")
    def test_caddy_official_adapter_without_starting_service(self):
        self.render()
        adapted = json.loads(deploy.run([self.settings["caddy_binary"], "adapt", "--config",
                                         str(self.destination / "Caddyfile"), "--adapter", "caddyfile"]))
        self.assertIn("http", adapted["apps"])


@unittest.skipUnless(shutil.which("openssl"), "需要已安装 OpenSSL，不自动安装")
class CertificateTests(TemporaryDeployment):
    def setUp(self):
        super().setUp()
        self.render()
        self.tls_root = self.destination / "tls"

    def operate(self, action, approved=True, reload_fn=None):
        return tls.operate(action, self.destination, self.settings, approved,
                           test_only=True, reload_fn=reload_fn)

    def issue(self):
        self.operate("init-ca")
        self.operate("issue")
        return tls.current_leaf(self.tls_root)

    def test_requires_approval_and_check_is_read_only(self):
        for action in ("init-ca", "issue", "renew"):
            with self.assertRaises(deploy.DeployError):
                self.operate(action, approved=False)
        with self.assertRaises(deploy.DeployError):
            self.operate("check", approved=False)
        self.assertFalse(self.tls_root.exists())

    def test_real_openssl_san_expiry_chain_and_permissions(self):
        leaf = self.issue()
        tls.validate_leaf(leaf, self.tls_root / "ca", tls.desired_sans(deploy.validate_settings(self.settings)))
        self.assertGreater(tls.days_remaining(leaf / "leaf.crt"), 89)
        self.assertLessEqual(tls.days_remaining(leaf / "leaf.crt"), 90)
        for path in [self.tls_root, *self.tls_root.rglob("*")]:
            if not path.is_symlink():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)
        report = self.operate("check", approved=False)
        self.assertFalse(report["需要续签"])
        self.assertIn("Fingerprint", report["CA指纹"])
        self.assertNotIn("PRIVATE KEY", json.dumps(report))
        with self.assertRaises(deploy.DeployError):
            self.operate("init-ca")
        with self.assertRaises(deploy.DeployError):
            self.operate("issue")

    def test_not_due_does_not_issue_or_reload(self):
        previous = self.issue()
        reload_fn = Mock()
        with patch.object(tls, "make_leaf", side_effect=AssertionError("不应重新签发")):
            self.assertEqual(self.operate("renew", reload_fn=reload_fn), {"已续签": False})
        reload_fn.assert_not_called()
        self.assertEqual(tls.current_leaf(self.tls_root), previous)

    def test_30_day_boundary_and_successful_reload(self):
        previous = self.issue()
        original = tls.days_remaining
        reload_fn = Mock()
        with patch.object(tls, "days_remaining", side_effect=lambda p: 30 if p == previous / "leaf.crt" else original(p)):
            self.assertEqual(self.operate("renew", reload_fn=reload_fn), {"已续签": True})
        reload_fn.assert_called_once()
        self.assertNotEqual(tls.current_leaf(self.tls_root), previous)
        self.assertTrue((previous / "leaf.key").exists())
        self.assertFalse((self.tls_root / "transition.json").exists())

    def test_ip_change_forces_reissue_without_ca_rotation(self):
        previous = self.issue()
        ca = (self.tls_root / "ca/ca.crt").read_bytes()
        self.settings["lan_ip"] = "192.168.56.11"
        self.assertEqual(self.operate("renew", reload_fn=Mock()), {"已续签": True})
        current = tls.current_leaf(self.tls_root)
        self.assertNotEqual(current, previous)
        self.assertIn("IP:192.168.56.11", tls.certificate_sans(current / "leaf.crt"))
        self.assertNotIn("IP:192.168.56.10", tls.certificate_sans(current / "leaf.crt"))
        self.assertEqual((self.tls_root / "ca/ca.crt").read_bytes(), ca)

    def test_reload_failure_restores_old_certificate_and_key(self):
        previous = self.issue()
        before = (previous / "leaf.crt").read_bytes(), (previous / "leaf.key").read_bytes()
        self.settings["lan_ip"] = "192.168.56.11"
        reload_fn = Mock(side_effect=[deploy.DeployError("失败"), None])
        with self.assertRaisesRegex(deploy.DeployError, "已恢复旧证书"):
            self.operate("renew", reload_fn=reload_fn)
        self.assertEqual(reload_fn.call_count, 2)
        self.assertEqual(tls.current_leaf(self.tls_root), previous)
        self.assertEqual(before, ((previous / "leaf.crt").read_bytes(), (previous / "leaf.key").read_bytes()))

    def test_double_reload_failure_requires_explicit_recovery(self):
        previous = self.issue()
        self.settings["lan_ip"] = "192.168.56.11"
        with self.assertRaisesRegex(deploy.DeployError, "recover"):
            self.operate("renew", reload_fn=Mock(side_effect=deploy.DeployError("失败")))
        self.assertEqual(tls.current_leaf(self.tls_root), previous)
        self.assertTrue((self.tls_root / "transition.json").exists())
        with self.assertRaises(deploy.DeployError):
            self.operate("check")
        self.operate("recover", reload_fn=Mock())
        self.assertFalse((self.tls_root / "transition.json").exists())

    def test_bad_san_key_and_expiring_ca_are_rejected(self):
        leaf = self.issue()
        with self.assertRaises(deploy.DeployError):
            tls.validate_leaf(leaf, self.tls_root / "ca", {"DNS:wrong.internal"})
        with patch.object(tls, "days_remaining", return_value=80), self.assertRaises(deploy.DeployError):
            tls.make_leaf(self.tls_root, self.settings)
        (leaf / "leaf.key").write_bytes((self.tls_root / "ca/ca.key").read_bytes())
        with self.assertRaises(deploy.DeployError):
            tls.validate_leaf(leaf, self.tls_root / "ca")

    def test_concurrent_rotation_and_escaping_symlink_are_rejected(self):
        self.issue()
        with tls.exclusive(self.tls_root), self.assertRaises(deploy.DeployError):
            self.operate("renew", reload_fn=Mock())
        (self.tls_root / "current").unlink()
        (self.tls_root / "current").symlink_to("../outside")
        with self.assertRaises(deploy.DeployError):
            self.operate("check")

    def test_failed_leaf_validation_does_not_switch_current(self):
        previous = self.issue()
        self.settings["lan_ip"] = "192.168.56.11"
        reload_fn = Mock()
        validate = tls.validate_leaf

        def reject_new(leaf, *args, **kwargs):
            if leaf != previous:
                raise deploy.DeployError("模拟新证书校验失败")
            return validate(leaf, *args, **kwargs)

        with patch.object(tls, "validate_leaf", side_effect=reject_new):
            with self.assertRaises(deploy.DeployError):
                self.operate("renew", reload_fn=reload_fn)
        self.assertEqual(tls.current_leaf(self.tls_root), previous)
        reload_fn.assert_not_called()

    def test_test_mode_cannot_reload_real_caddy(self):
        with self.assertRaises(deploy.DeployError):
            self.operate("renew")

    def test_reload_forces_reading_changed_static_certificate(self):
        with patch.object(tls, "run", return_value="") as run:
            tls.reload_caddy(self.destination, self.settings["caddy_binary"])
        self.assertEqual(run.call_count, 2)
        command = run.call_args.args[0]
        self.assertIn("--force", command)
        self.assertIn("unix/" + str(self.destination / "caddy-admin.sock"), command)


if __name__ == "__main__":
    unittest.main()
