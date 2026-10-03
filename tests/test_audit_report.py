"""审计报表聚合单测（批次 5 / P3-07）：demo 文件后端 + production SQLite 双路径。

- demo：直接写 JSONL（可控 ts），验证分组、排序、时间过滤、坏行跳过、采样截断；
- production：``write_audit`` 写库后 SQL GROUP BY 聚合断言；
- BFF：``GET /api/audit/summary`` 端点（鉴权 + 形状）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aiops_agent import db

from bff import audit, auth

TOKENS = {"tok-adm": {"user": "li", "role": "admin"}}


def _row(
    *,
    days_ago: int = 0,
    actor: str = "li",
    action: str = "approval:approve",
    result: str = "signaled",
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=5)).isoformat()
    return {
        "ts": ts,
        "actor": actor,
        "action": action,
        "wf_id": "aiops-fix-order-a-1",
        "target": None,
        "params": {},
        "result": result,
    }


def _write_events(data_dir: Path, rows: list[dict]) -> None:
    """按日期分文件写入 JSONL（复刻 write_audit 的落盘形态）。"""
    directory = data_dir / "web-audit"
    directory.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["ts"][:10].replace("-", ""), []).append(row)
    for day, items in by_day.items():
        with open(directory / f"audit-{day}.jsonl", "a", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")


class AuditAggregateDemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(audit, "DATA_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_groups_and_order(self) -> None:
        _write_events(
            Path(self._tmp.name),
            [
                _row(actor="li"),
                _row(actor="li"),
                _row(actor="zhang", action="deploy-command:deploy_now", result="ok"),
            ],
        )
        summary = audit.aggregate_audit(days=30, top=10)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_actor"][0], {"key": "li", "count": 2})
        self.assertEqual(summary["by_action"][0], {"key": "approval:approve", "count": 2})
        self.assertEqual(summary["by_result"][0], {"key": "signaled", "count": 2})
        self.assertEqual(len(summary["by_day"]), 1)
        self.assertFalse(summary["truncated"])
        self.assertIsNotNone(summary["latest_ts"])

    def test_days_filter_excludes_old_records(self) -> None:
        _write_events(Path(self._tmp.name), [_row(days_ago=40), _row(days_ago=1)])
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 1)

    def test_bad_lines_skipped(self) -> None:
        directory = Path(self._tmp.name) / "web-audit"
        directory.mkdir(parents=True)
        (directory / "audit-20261003.jsonl").write_text(
            "{bad json\n" + json.dumps(_row()) + "\n", encoding="utf-8"
        )
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 1)

    def test_truncated_flag_and_by_day_ascending(self) -> None:
        _write_events(
            Path(self._tmp.name),
            [_row(days_ago=2), _row(days_ago=1), _row(days_ago=0)],
        )
        summary = audit.aggregate_audit(days=30, sample_limit=2)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["total"], 2)  # 采样窗口内仅最近 2 条
        keys = [item["key"] for item in summary["by_day"]]
        self.assertEqual(keys, sorted(keys))  # 日期升序（趋势）

    def test_empty_directory(self) -> None:
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 0)
        self.assertIsNone(summary["latest_ts"])
        self.assertEqual(summary["by_day"], [])


class _DbBackedTest(unittest.TestCase):
    """production + SQLite 临时库（与 test_store_db 同构）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self._tmp.name) / 'audit.db'}"
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


class AuditAggregateDbTest(_DbBackedTest):
    def test_empty_table(self) -> None:
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 0)
        self.assertIsNone(summary["latest_ts"])
        self.assertEqual(summary["by_day"], [])
        self.assertFalse(summary["truncated"])

    def test_group_by_actor_action_day(self) -> None:
        audit.write_audit(actor="li", action="approval:approve", wf_id="w-1", result="signaled")
        audit.write_audit(actor="li", action="approval:approve", wf_id="w-2", result="signaled")
        audit.write_audit(actor="zhang", action="deploy-command:cancel", wf_id="w-3", result="ok")
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_actor"][0], {"key": "li", "count": 2})
        self.assertEqual(summary["by_action"][0], {"key": "approval:approve", "count": 2})
        self.assertEqual(summary["by_result"][0], {"key": "signaled", "count": 2})
        self.assertEqual(len(summary["by_day"]), 1)  # 同一天写入

    def test_days_filter(self) -> None:
        engine = db.get_engine()
        old_ts = db.to_dt(datetime.now(timezone.utc) - timedelta(days=40))
        with engine.begin() as conn:
            conn.execute(
                db.audit_events.insert().values(
                    ts=old_ts,
                    actor="old-user",
                    action="legacy:act",
                    workflow_id="",
                    target=None,
                    detail={},
                    result="ok",
                    mode="production",
                )
            )
        audit.write_audit(actor="li", action="approval:approve")
        summary = audit.aggregate_audit(days=30)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_actor"], [{"key": "li", "count": 1}])


class AuditSummaryApiTest(unittest.TestCase):
    """BFF 端点：GET /api/audit/summary（demo 文件后端）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {"AIOPS_MODE": "demo", "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS)},
        )
        self._env.start()
        self._patch = mock.patch.object(audit, "DATA_DIR", Path(self._tmp.name))
        self._patch.start()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}

    def tearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_summary_endpoint_shape(self) -> None:
        audit.write_audit(actor="li", action="approval:approve", wf_id="w-1", result="signaled")
        response = self.client.get("/api/audit/summary?days=7", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["days"], 7)
        self.assertEqual(payload["by_actor"][0]["key"], "li")
        self.assertIn("by_day", payload)
        self.assertIn("server_time", payload)

    def test_summary_requires_auth(self) -> None:
        self.assertEqual(self.client.get("/api/audit/summary").status_code, 401)


if __name__ == "__main__":
    unittest.main()
