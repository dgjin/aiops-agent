"""store 双后端单测：production（SQLite 模拟）分支覆盖审计 / 令牌 / 清单。

demo 分支行为由既有 test_bff / test_auth / test_monitored_apps 覆盖（conftest 固定 demo）；
本文件临时切换到 production + SQLite 临时库，验证 DB 分支与文件后端**接口同构**。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from aiops_agent import db

from bff import audit, auth, monitored_apps, runs_store


class _DbBackedTest(unittest.TestCase):
    """公共基类：production + SQLite 临时库 + 引擎/缓存重置。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self._tmp.name) / 'store.db'}"
        self._env = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": self.url}
        )
        self._env.start()
        db.reset_engine()
        auth.invalidate_registry_cache()

    def tearDown(self) -> None:
        db.reset_engine()
        auth.invalidate_registry_cache()
        self._env.stop()
        self._tmp.cleanup()


class AuditDbTest(_DbBackedTest):
    def test_write_read_roundtrip(self) -> None:
        record = audit.write_audit(
            actor="zhang",
            action="approval:approve",
            wf_id="aiops-fix-order-a-1",
            params={"decision": "approve"},
            result="signaled",
        )
        rows = audit.read_audit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "approval:approve")
        self.assertEqual(rows[0]["wf_id"], "aiops-fix-order-a-1")
        self.assertEqual(rows[0]["params"], {"decision": "approve"})
        self.assertEqual(rows[0]["result"], "signaled")
        self.assertEqual(rows[0]["target"], record["target"])

    def test_read_latest_first_and_limit(self) -> None:
        for index in range(3):
            audit.write_audit(actor="li", action=f"act-{index}", target="app-1")
        rows = audit.read_audit(limit=2)
        self.assertEqual([row["action"] for row in rows], ["act-2", "act-1"])


TOKENS = {
    "seed-view": {"user": "viewer01", "role": "viewer"},
    "seed-ops": {"user": "zhang", "role": "operator"},
    "seed-adm": {"user": "li", "role": "admin"},
}


class AuthDbTest(_DbBackedTest):
    def setUp(self) -> None:
        super().setUp()
        self._env_tokens = mock.patch.dict(
            os.environ, {"AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS)}
        )
        self._env_tokens.start()

    def tearDown(self) -> None:
        self._env_tokens.stop()
        super().tearDown()

    def test_seed_and_resolve_by_plaintext(self) -> None:
        tokens = auth.registry_tokens()
        self.assertEqual(len(tokens), 3)
        # 库内仅存指纹：明文令牌经 resolve_token 的指纹兜底仍可解析
        identity, _record = auth.resolve_token("Bearer seed-ops", tokens=tokens)
        self.assertEqual((identity.user, identity.role), ("zhang", "operator"))

    def test_rotate_new_token_works_old_in_grace(self) -> None:
        auth.registry_tokens()  # 播种
        result = auth.rotate(reason="manual")
        self.assertEqual(len(result["new_tokens"]), 3)
        tokens = auth.registry_tokens()
        for item in result["new_tokens"]:
            identity, _ = auth.resolve_token(f"Bearer {item['token']}", tokens=tokens)
            self.assertEqual(identity.user, item["user"])
        # 旧令牌转 previous 且在宽限期内仍有效
        identity, _ = auth.resolve_token("Bearer seed-view", tokens=tokens)
        self.assertEqual(identity.role, "viewer")

    def test_status_shape(self) -> None:
        with mock.patch.dict(
            os.environ, {"AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS": "0"}, clear=False
        ):
            auth.registry_tokens()  # 播种（关闭轮换：不设失效时间）
            status = auth.rotation_status()
        self.assertFalse(status["auto_rotation_enabled"])
        self.assertEqual(status["registry_path"], "mysql://console_tokens")
        self.assertEqual(len(status["tokens"]), 3)
        # 令牌清单不含明文（仅指纹 id）
        self.assertTrue(all(len(token["id"]) == 8 for token in status["tokens"]))
        self.assertTrue(all(token["expires_at"] is None for token in status["tokens"]))

    def test_rotation_due_and_rotate(self) -> None:
        with mock.patch.dict(
            os.environ, {"AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS": "3600"}, clear=False
        ):
            t0 = auth._now()
            auth.registry_tokens(now=t0)  # 播种（rotated_at = t0，meta 记录 interval=3600）
            self.assertFalse(auth.rotation_status(now=t0 + timedelta(seconds=60))["due"])
            self.assertTrue(auth.rotation_status(now=t0 + timedelta(seconds=3601))["due"])
            auth.rotate(now=t0 + timedelta(seconds=3601), reason="auto")
            self.assertFalse(auth.rotation_status(now=t0 + timedelta(seconds=3602))["due"])


class MonitoredAppsDbTest(_DbBackedTest):
    def test_seed_add_update_remove(self) -> None:
        seeded = monitored_apps.list_all()
        self.assertEqual(seeded[0]["id"], "default")

        app = monitored_apps.add(name="订单服务", url="http://localhost:9090/", log_path="/tmp/o.log")
        self.assertIn(app["id"], [item["id"] for item in monitored_apps.list_all()])

        before, after = monitored_apps.update(app["id"], note="改过备注")
        self.assertNotEqual(before["note"], after["note"])
        self.assertEqual(monitored_apps.get(app["id"])["note"], "改过备注")

        removed = monitored_apps.remove(app["id"])
        self.assertIsNotNone(removed)
        self.assertIsNone(monitored_apps.get(app["id"]))

    def test_seeded_marker_prevents_reseed_after_clear(self) -> None:
        monitored_apps.list_all()  # 触发播种
        for item in monitored_apps.list_all():
            monitored_apps.remove(item["id"])
        self.assertEqual(monitored_apps.list_all(), [])  # 全删后不再自动播种

    def test_log_targets_filters_enabled_with_path(self) -> None:
        monitored_apps.add(
            name="带路径", url="http://localhost:9091/", log_path="/tmp/a.log", enabled=True
        )
        monitored_apps.add(
            name="未启用", url="http://localhost:9092/", log_path="/tmp/b.log", enabled=False
        )
        targets = monitored_apps.log_targets()
        self.assertEqual([t["name"] for t in targets], ["带路径"])

    def test_unique_name_validation(self) -> None:
        monitored_apps.add(name="重复名", url="http://localhost:9093/")
        with self.assertRaises(monitored_apps.MonitorStoreError):
            monitored_apps.add(name="重复名", url="http://localhost:9094/")


class RunsStoreDbTest(_DbBackedTest):
    def setUp(self) -> None:
        super().setUp()
        runs_store._clear_cache()

    def _item(self, **overrides) -> dict:
        base = {
            "wf_id": "aiops-fix-order-a-1",
            "run_id": "run-1",
            "stage": "WAIT_APPROVAL",
            "exec_status": "RUNNING",
            "alert": {"alert_id": "a-1", "service": "order"},
            "start_time": "2026-10-03T10:00:00+00:00",
            "close_time": None,
            "confidence": None,
            "patch_id": None,
            "duration_seconds": None,
            "result": None,
        }
        base.update(overrides)
        return base

    def test_insert_then_delta_skip_then_update(self) -> None:
        self.assertEqual(runs_store.sync_runs([self._item()]), 1)  # 首轮插入
        self.assertEqual(runs_store.sync_runs([self._item()]), 0)  # 无变化 → 跳过
        self.assertEqual(
            runs_store.sync_runs(
                [self._item(stage="DONE", exec_status="COMPLETED", close_time="2026-10-03T10:05:00+00:00")]
            ),
            1,  # 终态变化 → 更新
        )

        from sqlalchemy import select

        engine = db.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(select(db.workflow_runs)).mappings().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow_id"], "aiops-fix-order-a-1")
        self.assertEqual(rows[0]["stage"], "DONE")
        self.assertEqual(rows[0]["exec_status"], "COMPLETED")

    def test_items_without_wf_id_skipped(self) -> None:
        self.assertEqual(runs_store.sync_runs([{"stage": "X"}]), 0)


class RunsStoreDemoNoopTest(unittest.TestCase):
    def test_demo_mode_noop(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            self.assertEqual(runs_store.sync_runs([{"wf_id": "w-1", "stage": "X"}]), 0)


if __name__ == "__main__":
    unittest.main()
