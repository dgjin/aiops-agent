"""模式层（aiops_agent.mode）与数据库层（aiops_agent.db）单测。

DB 层用 SQLite 临时库覆盖 production 分支逻辑（不依赖 MySQL 服务）；
conftest.py 已将全局测试环境固定为 demo 模式，此处按需临时切换。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import inspect

from aiops_agent import db, mode

EXPECTED_TABLES = {"system_kv", "console_tokens", "monitored_apps", "audit_events", "workflow_runs"}


class ModeTest(unittest.TestCase):
    def test_default_production(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_MODE", None)
            self.assertEqual(mode.mode(), "production")
            self.assertTrue(mode.is_production())
            self.assertFalse(mode.is_demo())

    def test_demo_never_touches_db(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo", "AIOPS_DATABASE_URL": "mysql+pymysql://x/y"}):
            self.assertTrue(mode.is_demo())
            self.assertIsNone(mode.db_url())  # 即使残留 DB 配置，演示也不连库

    def test_invalid_mode_raises(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "staging"}):
            with self.assertRaises(mode.ModeConfigError):
                mode.mode()

    def test_production_requires_db_url(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}, clear=False):
            os.environ.pop("AIOPS_DATABASE_URL", None)
            with self.assertRaises(mode.ModeConfigError) as ctx:
                mode.db_url()
        self.assertIn("AIOPS_DATABASE_URL", str(ctx.exception))

    def test_data_root_defaults(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}, clear=False):
            os.environ.pop("AIOPS_DATA_ROOT", None)
            self.assertEqual(mode.data_root().name, "data")  # 演示沿用既有 data/
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}, clear=False):
            os.environ.pop("AIOPS_DATA_ROOT", None)
            self.assertEqual(mode.data_root().name, "prod")  # 生产隔离到 data/prod/

    def test_data_root_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": tmp}):
                self.assertEqual(mode.data_root(), Path(tmp).resolve())


class DbLayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self._tmp.name) / 'test.db'}"
        self._env = mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": self.url}
        )
        self._env.start()
        db.reset_engine()

    def tearDown(self) -> None:
        db.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def test_schema_created_idempotent(self) -> None:
        engine = db.get_engine()
        tables = set(inspect(engine).get_table_names())
        self.assertTrue(EXPECTED_TABLES <= tables)
        db.get_engine()  # 重复调用幂等（checkfirst）

    def test_time_conversions(self) -> None:
        dt = db.to_dt("2026-10-03T10:00:00+00:00")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)  # 入库为 naive UTC
        self.assertEqual(db.to_iso(dt), "2026-10-03T10:00:00+00:00")
        self.assertEqual(db.to_iso(db.to_dt("2026-10-03T10:00:00")), "2026-10-03T10:00:00+00:00")
        self.assertIsNone(db.to_dt("not-a-time"))
        self.assertIsNone(db.to_dt(None))
        self.assertIsNone(db.to_iso(None))

    def test_demo_mode_engine_raises(self) -> None:
        db.reset_engine()
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            db.reset_engine()
            with self.assertRaises(mode.ModeConfigError):
                db.get_engine()
        db.reset_engine()

    def test_url_change_rebuilds_engine(self) -> None:
        engine1 = db.get_engine()
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.dict(os.environ, {"AIOPS_DATABASE_URL": f"sqlite:///{other}/b.db"}):
                engine2 = db.get_engine()
        self.assertIsNot(engine1, engine2)


if __name__ == "__main__":
    unittest.main()
