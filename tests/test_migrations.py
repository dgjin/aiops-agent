"""Alembic 基线：空库升级、历史缺列升级与隔离回退；不得触达真实 MySQL。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from aiops_agent import db


ROOT = Path(__file__).resolve().parent.parent


class MigrationBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        database = Path(self.tmp.name) / "migration.db"
        self.env = mock.patch.dict(
            "os.environ",
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{database}",
                "PYTHON_DOTENV_DISABLED": "1",
            },
            clear=False,
        )
        self.env.start()
        db.reset_engine()

    def tearDown(self) -> None:
        db.reset_engine()
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _config() -> Config:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        return config

    def test_empty_upgrade_and_downgrade(self) -> None:
        command.upgrade(self._config(), "head")
        engine = db.get_engine()
        names = set(inspect(engine).get_table_names())
        self.assertIn("workflow_runs", names)
        self.assertIn("log_evidence", names)
        self.assertIn("alembic_version", names)

        command.downgrade(self._config(), "base")
        verification = create_engine(os.environ["AIOPS_DATABASE_URL"])
        self.assertNotIn("workflow_runs", inspect(verification).get_table_names())
        with verification.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar(), 0)
        verification.dispose()

    def test_existing_schema_gets_missing_columns(self) -> None:
        engine = db.get_engine()
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE monitored_apps DROP COLUMN depends_on"))
        command.upgrade(self._config(), "head")
        columns = {item["name"] for item in inspect(engine).get_columns("monitored_apps")}
        self.assertIn("depends_on", columns)

    def test_workflow_commands_added_and_downgraded(self) -> None:
        """M2：命令 outbox 表与 audit_params 列；降级到 0005 移除，重放升级可重建。"""
        command.upgrade(self._config(), "head")
        engine = db.get_engine()
        self.assertIn("workflow_commands", inspect(engine).get_table_names())
        columns = {item["name"] for item in inspect(engine).get_columns("workflow_commands")}
        self.assertIn("command_id", columns)
        self.assertIn("audit_params", columns)
        indexes = {item["name"] for item in inspect(engine).get_indexes("workflow_commands")}
        self.assertIn("idx_commands_status_next", indexes)
        self.assertIn("idx_commands_wf", indexes)

        command.downgrade(self._config(), "20261101_0005")
        self.assertNotIn("workflow_commands", inspect(engine).get_table_names())

        command.upgrade(self._config(), "head")
        self.assertIn("workflow_commands", inspect(engine).get_table_names())
        columns = {item["name"] for item in inspect(engine).get_columns("workflow_commands")}
        self.assertIn("audit_params", columns)

    def test_runs_kind_risk_added_and_downgraded(self) -> None:
        """M5：kind/risk 列与联合索引；降级到 0003 移除，重放升级幂等。"""
        command.upgrade(self._config(), "head")
        engine = db.get_engine()
        columns = {item["name"] for item in inspect(engine).get_columns("workflow_runs")}
        self.assertIn("kind", columns)
        self.assertIn("risk", columns)
        indexes = {item["name"] for item in inspect(engine).get_indexes("workflow_runs")}
        self.assertIn("idx_runs_close_kind", indexes)

        command.downgrade(self._config(), "20261101_0003")
        columns = {item["name"] for item in inspect(engine).get_columns("workflow_runs")}
        self.assertNotIn("kind", columns)
        self.assertNotIn("risk", columns)

        command.upgrade(self._config(), "head")
        columns = {item["name"] for item in inspect(engine).get_columns("workflow_runs")}
        self.assertIn("kind", columns)
        self.assertIn("risk", columns)


if __name__ == "__main__":
    unittest.main()
