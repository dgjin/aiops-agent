"""LAN IP 同步工具隔离测试：不导入应用、不读 .env、不触碰真实部署目录或 Docker/Caddy。"""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tests  # 必须先触发测试包隔离钩子。
from scripts import sync_lan_ip as sync

OLD = "10.1.112.39"
NEW = "192.168.0.6"

CADDY = f"""\
https://{OLD}:8643, https://127.0.0.1:8643, https://[::1]:8643 {{
    bind {OLD} 127.0.0.1 ::1
}}
"""


def settings(lan_ip: str = OLD) -> dict:
    return {"project": "aiops-sync-test", "lan_ip": lan_ip,
            "dns_names": ["host.docker.internal", "localhost"],
            "volumes": {key: "test-" + key for key in ("mysql", "redis", "loki", "temporal_dev")},
            "caddy_binary": "/not-installed/caddy"}


class TemporarySync(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aiops-sync-test-",
                                                     dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deploy = self.root / "production-deploy"
        self.deploy.mkdir(mode=0o700)
        (self.deploy / "settings.json").write_text(json.dumps(settings(), ensure_ascii=False, indent=2))
        (self.deploy / "Caddyfile").write_text(CADDY)
        self.mirror = self.root / "input-settings.json"
        self.mirror.write_text(json.dumps(settings(), ensure_ascii=False, indent=2))
        self.review = self.root / "review"
        self.renewed = []

    def renew(self, deploy_dir):
        self.renewed.append(Path(deploy_dir))
        return {"已续签": True}

    def run_sync(self, **kwargs):
        kwargs.setdefault("mirror_settings", (self.mirror,))
        kwargs.setdefault("review_dir", self.review)
        kwargs.setdefault("renew_fn", self.renew)
        kwargs.setdefault("bind_check", False)
        return sync.sync(self.deploy, NEW, **kwargs)


class SyncTests(TemporarySync):
    def test_sync_updates_all_address_surfaces(self):
        report = self.run_sync()
        caddy = (self.deploy / "Caddyfile").read_text()
        self.assertNotIn(OLD, caddy)
        self.assertEqual(caddy.count(NEW), 2)
        self.assertEqual(json.loads((self.deploy / "settings.json").read_text())["lan_ip"], NEW)
        self.assertEqual(json.loads(self.mirror.read_text())["lan_ip"], NEW)
        for name in ("prometheus.fragment.yml", "alertmanager.fragment.yml"):
            fragment = (self.deploy / name).read_text()
            self.assertIn(f"{NEW}:{sync.PORT}", fragment)
            self.assertNotIn(OLD, fragment)
        bundle = self.review / f"lan-ip-{NEW}"
        self.assertTrue((bundle / "prometheus.fragment.yml").is_file())
        self.assertTrue((bundle / "alertmanager.fragment.yml").is_file())
        self.assertIn(OLD, (bundle / "说明.md").read_text())
        self.assertEqual(self.renewed, [self.deploy])
        self.assertEqual(report["证书"], "已重签并 reload Caddy")

    def test_same_address_skips_files_but_still_renews(self):
        (self.deploy / "settings.json").write_text(json.dumps(settings(NEW), ensure_ascii=False, indent=2))
        (self.deploy / "Caddyfile").write_text(CADDY.replace(OLD, NEW))
        self.mirror.write_text(json.dumps(settings(NEW), ensure_ascii=False, indent=2))
        report = self.run_sync()
        self.assertNotIn(OLD, (self.deploy / "Caddyfile").read_text())
        self.assertIn("地址未变化", report["变更"]["说明"])
        self.assertEqual(report["后续人工步骤"], ["无需人工操作"])
        self.assertEqual(self.renewed, [self.deploy])

    def test_dry_run_writes_nothing(self):
        report = self.run_sync(dry_run=True)
        self.assertEqual((self.deploy / "Caddyfile").read_text(), CADDY)
        self.assertEqual(json.loads((self.deploy / "settings.json").read_text())["lan_ip"], OLD)
        self.assertEqual(json.loads(self.mirror.read_text())["lan_ip"], OLD)
        self.assertFalse(self.review.exists())
        self.assertEqual(self.renewed, [])
        self.assertIn("dry-run", report["变更"]["说明"])

    def test_caddyfile_missing_old_address_rejected(self):
        (self.deploy / "Caddyfile").write_text("# 无地址\n")
        with self.assertRaises(sync.DeployError):
            self.run_sync()
        self.assertEqual(json.loads((self.deploy / "settings.json").read_text())["lan_ip"], OLD)

    def test_missing_deploy_files_rejected(self):
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        with self.assertRaises(sync.DeployError):
            sync.sync(empty, NEW, renew_fn=self.renew, bind_check=False)

    def test_renew_failure_propagates(self):
        def failing(_deploy_dir):
            raise sync.DeployError("续签失败；可重跑本命令")

        with self.assertRaises(sync.DeployError):
            self.run_sync(renew_fn=failing)


class DetectionTests(unittest.TestCase):
    def test_detect_prefers_working_en0(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return "192.168.0.6\n"

        with mock.patch.object(sync, "run", side_effect=fake_run):
            self.assertEqual(sync.detect_lan_ip(), "192.168.0.6")
        self.assertEqual(calls[0], ["ipconfig", "getifaddr", "en0"])

    def test_detect_falls_back_to_en1(self):
        def fake_run(argv, **kwargs):
            if argv[2] == "en0":
                raise sync.DeployError("无地址")
            return "192.168.0.7\n"

        with mock.patch.object(sync, "run", side_effect=fake_run):
            self.assertEqual(sync.detect_lan_ip(), "192.168.0.7")

    def test_detect_without_any_address_fails(self):
        with mock.patch.object(sync, "run", side_effect=sync.DeployError("无地址")):
            with self.assertRaises(sync.DeployError):
                sync.detect_lan_ip()


class ValidationTests(unittest.TestCase):
    def test_validate_rejects_bad_addresses(self):
        for bad in ("127.0.0.1", "8.8.8.8", "999.1.1.1", "not-an-ip", "169.254.1.1", "0.0.0.0"):
            with self.assertRaises(sync.DeployError):
                sync.validate_lan_ip(bad)
        self.assertEqual(sync.validate_lan_ip(" 192.168.0.6 "), "192.168.0.6")

    def test_check_bind_rejects_absent_address(self):
        with self.assertRaises(sync.DeployError):
            sync.check_bind("192.0.2.1")

    def test_main_rejects_bad_ip(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(sync.main(["--ip", "8.8.8.8"]), 1)
        self.assertIn("私网段", output.getvalue())


if __name__ == "__main__":
    unittest.main()
