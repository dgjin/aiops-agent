"""kill switch 全链路单测：store 双后端 + BFF 中间件拦截 + Webhook 预检。

BFF 集成经 TestClient（真实中间件链）；demo 文件后端与 production SQLite 分支均覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aiops_agent import db, kill_switch

from bff import audit, monitored_apps

TOKENS = {
    "tok-view": {"user": "viewer01", "role": "viewer"},
    "tok-adm": {"user": "li", "role": "admin"},
}


class KillSwitchFileTest(unittest.TestCase):
    """demo 文件后端：读写 / 损坏 fail-closed / 状态结构。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"})
        self._env.start()
        self._path = Path(self._tmp.name) / "kill_switch.json"
        self._patch = mock.patch.object(kill_switch, "DATA_PATH", self._path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_default_inactive(self) -> None:
        state = kill_switch.get_state()
        self.assertFalse(state["active"])
        self.assertFalse(kill_switch.is_active())
        self.assertFalse(self._path.exists())  # 读取不产生副作用

    def test_activate_deactivate_roundtrip(self) -> None:
        state = kill_switch.set_state(active=True, actor="li", reason="紧急止血")
        self.assertTrue(state["active"])
        self.assertEqual(state["actor"], "li")
        self.assertIsNotNone(state["since"])
        self.assertTrue(kill_switch.is_active())
        self.assertTrue(self._path.is_file())

        state = kill_switch.set_state(active=False, actor="li", reason="恢复")
        self.assertFalse(state["active"])
        self.assertIsNone(state["since"])
        self.assertFalse(kill_switch.is_active())

    def test_corrupt_file_fail_closed(self) -> None:
        self._path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(kill_switch.KillSwitchError):
            kill_switch.get_state()


class KillSwitchDbTest(unittest.TestCase):
    """production DB 后端（SQLite 模拟）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{Path(self._tmp.name) / 'ks.db'}",
            },
        )
        self._env.start()
        db.reset_engine()

    def tearDown(self) -> None:
        db.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def test_activate_persists_across_reads(self) -> None:
        self.assertFalse(kill_switch.is_active())
        kill_switch.set_state(active=True, actor="li", reason="db 分支")
        state = kill_switch.get_state()
        self.assertTrue(state["active"])
        self.assertEqual(state["reason"], "db 分支")
        self.assertIsNotNone(state["since"])

        kill_switch.set_state(active=False, actor="li")
        self.assertFalse(kill_switch.is_active())
        state = kill_switch.get_state()
        self.assertEqual(state["reason"], "")
        self.assertIsNone(state["since"])


class KillSwitchApiTest(unittest.TestCase):
    """BFF 集成：激活 → 写 503 / 读放行 / 关闭恢复 / 角色与审计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
                "AIOPS_CONSOLE_TOKEN_FILE": str(Path(self._tmp.name) / "tokens.json"),
            },
        )
        self._env.start()
        self._patches = [
            mock.patch.object(kill_switch, "DATA_PATH", Path(self._tmp.name) / "ks.json"),
            mock.patch.object(audit, "DATA_DIR", Path(self._tmp.name)),
            mock.patch.object(monitored_apps, "DATA_PATH", Path(self._tmp.name) / "apps.json"),
        ]
        for patcher in self._patches:
            patcher.start()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}
        self.viewer = {"Authorization": "Bearer tok-view"}

    def tearDown(self) -> None:
        for patcher in self._patches:
            patcher.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _create_app(self, headers: dict) -> int:
        response = self.client.post(
            "/api/monitored-apps",
            json={"name": "ks-探针", "url": "http://localhost:19999/"},
            headers=headers,
        )
        return response.status_code

    def test_activate_blocks_writes_but_not_reads(self) -> None:
        response = self.client.post(
            "/api/system/kill-switch",
            json={"active": True, "reason": "演练"},
            headers=self.admin,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["kill_switch"]["active"])

        # 写操作被中间件 503 拒绝（含明确原因）
        blocked = self.client.post(
            "/api/monitored-apps",
            json={"name": "ks-探针", "url": "http://localhost:19999/"},
            headers=self.admin,
        )
        self.assertEqual(blocked.status_code, 503)
        self.assertIn("kill switch", blocked.json()["error"])
        # 读接口不受影响
        self.assertEqual(self.client.get("/api/auth/status", headers=self.admin).status_code, 200)

    def test_deactivate_restores_writes(self) -> None:
        self.client.post("/api/system/kill-switch", json={"active": True}, headers=self.admin)
        self.assertEqual(self._create_app(self.admin), 503)
        self.client.post("/api/system/kill-switch", json={"active": False}, headers=self.admin)
        self.assertEqual(self._create_app(self.admin), 200)

    def test_management_requires_admin(self) -> None:
        response = self.client.post(
            "/api/system/kill-switch", json={"active": True}, headers=self.viewer
        )
        self.assertEqual(response.status_code, 403)

    def test_activation_audited(self) -> None:
        self.client.post(
            "/api/system/kill-switch",
            json={"active": True, "reason": "演练"},
            headers=self.admin,
        )
        rows = audit.read_audit()
        self.assertEqual(rows[0]["action"], "system:kill-switch")
        self.assertEqual(rows[0]["target"], "activate")
        self.assertEqual(rows[0]["actor"], "li")
        self.assertEqual(rows[0]["params"]["reason"], "演练")


class WebhookKillSwitchTest(unittest.TestCase):
    """Webhook 预检（demo 文件后端）：关闭放行 / 激活拦截 / 损坏 fail-closed。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"})
        self._env.start()
        self._path = Path(self._tmp.name) / "ks.json"
        self._patch = mock.patch.object(kill_switch, "DATA_PATH", self._path)
        self._patch.start()
        from alert_webhook import check_kill_switch

        self._check = check_kill_switch

    def tearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_allows_when_inactive(self) -> None:
        self.assertIsNone(self._check())

    def test_blocks_when_active(self) -> None:
        kill_switch.set_state(active=True, actor="li", reason="演练")
        blocked = self._check()
        self.assertIsNotNone(blocked)
        self.assertIn("kill switch", blocked["error"])
        self.assertTrue(blocked["kill_switch"]["active"])

    def test_fails_closed_when_unreadable(self) -> None:
        self._path.write_text("{bad", encoding="utf-8")
        blocked = self._check()
        self.assertIsNotNone(blocked)
        self.assertIn("fail-closed", blocked["error"])


if __name__ == "__main__":
    unittest.main()
