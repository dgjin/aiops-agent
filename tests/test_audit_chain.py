"""M4 审计哈希链与归档：篡改 / 删除检测、归档复核、游标分页与权限。

覆盖范围：

- demo：JSONL 链写入、篡改与删除发现、归档（purge）后归档副本仍参与校验、
  归档副本被改写可发现、链损坏时拒绝归档、purge 后新写入继续接链；
- production：SQLite 审计表链写入与篡改发现、跨批次归档（前驱已 purge）、
  归档后全链仍可校验、游标分页与过滤；
- BFF：``/api/audit/ops`` 游标分页、``/api/audit/integrity`` 权限与篡改暴露、
  production 流程审计走 SQL 数据面。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select

from aiops_agent import audit_chain
from aiops_agent import db as db_layer
from bff import audit, auth

TOKENS = {
    "tok-adm": {"user": "li", "role": "admin"},
    "tok-view": {"user": "guest", "role": "viewer"},
}


def _record(
    ts: str,
    *,
    actor: str = "li",
    action: str = "approval:approve",
    wf_id: str = "wf-1",
    result: str = "ok",
) -> dict:
    return {
        "ts": ts,
        "actor": actor,
        "action": action,
        "wf_id": wf_id,
        "target": None,
        "params": {"note": action},
        "result": result,
    }


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class DemoChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self._patch = mock.patch.object(audit, "DATA_DIR", self.data)
        self._patch.start()
        self.directory = self.data / "web-audit"

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, count: int = 3) -> list[dict]:
        return [
            audit.write_audit(
                actor=f"u{index}",
                action="approval:approve",
                wf_id="wf-1",
                params={"index": index},
                result="ok",
            )
            for index in range(count)
        ]

    def test_chain_is_linked_and_verified(self) -> None:
        entries = self._write(3)
        result = audit.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["mode"], "demo")
        self.assertIn("tamper-evident", result["boundary"])
        self.assertEqual(result["head"], entries[-1]["entry_hash"])
        self.assertEqual(entries[1]["prev_hash"], entries[0]["entry_hash"])
        self.assertNotEqual(entries[0]["event_id"], entries[1]["event_id"])

    def test_modified_record_is_detected(self) -> None:
        entries = self._write(3)
        (path,) = audit_chain.audit_files(self.directory)
        lines = _lines(path)
        payload = json.loads(lines[1])
        payload["params"] = {"index": 999}
        lines[1] = json.dumps(payload, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = audit.verify()
        self.assertFalse(result["ok"])
        self.assertIn("entry_hash", result["error"])
        self.assertEqual(result["broken_at"]["event_id"], entries[1]["event_id"])

    def test_deleted_record_is_detected(self) -> None:
        self._write(3)
        (path,) = audit_chain.audit_files(self.directory)
        lines = _lines(path)
        path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

        result = audit.verify()
        self.assertFalse(result["ok"])
        self.assertIn("prev_hash", result["error"])

    def test_archive_purge_keeps_chain_verifiable(self) -> None:
        with audit_chain.file_lock(self.directory):
            audit_chain.append_demo_locked(
                self.directory,
                _record("2020-06-01T00:00:00+00:00", actor="legacy"),
                day="20200601",
            )
            audit_chain.append_demo_locked(
                self.directory,
                _record("2020-06-01T00:00:01+00:00", actor="legacy"),
                day="20200601",
            )
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        fresh = audit.write_audit(actor="li", action="approval:approve", wf_id="wf-1")

        result = audit_chain.archive_demo(self.directory, "20210101", purge=True)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["purged"], 1)
        self.assertFalse((self.directory / "audit-20200601.jsonl").exists())

        archived = self.directory / "archive" / "audit-20200601.jsonl"
        self.assertTrue(archived.is_file())
        manifest = audit_chain.load_manifest(self.directory)
        entry = next(item for item in manifest["entries"] if item["day"] == "20200601")
        self.assertEqual(entry["status"], "purged")
        self.assertEqual(entry["rows"], 2)
        self.assertEqual(
            entry["sha256"], hashlib.sha256(archived.read_bytes()).hexdigest()
        )

        verification = audit_chain.verify_demo(self.directory)
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 3)

        # purge 后新写入必须接在归档链尾之后，而不是从创世重启
        appended = audit.write_audit(actor="bob", action="approval:reject", wf_id="wf-2")
        self.assertEqual(appended["prev_hash"], fresh["entry_hash"])
        verification = audit_chain.verify_demo(self.directory)
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 4)
        self.assertTrue((self.directory / f"audit-{today}.jsonl").is_file())

    def test_chain_restarts_from_archive_after_full_purge(self) -> None:
        with audit_chain.file_lock(self.directory):
            first = audit_chain.append_demo_locked(
                self.directory,
                _record("2020-06-01T00:00:00+00:00", actor="legacy"),
                day="20200601",
            )
        result = audit_chain.archive_demo(self.directory, "20210101", purge=True)
        self.assertEqual(result["purged"], 1)
        self.assertEqual(audit_chain.audit_files(self.directory), [])
        self.assertEqual(audit_chain.chain_tail(self.directory)["last_hash"], first["entry_hash"])

        appended = audit.write_audit(actor="li", action="approval:approve", wf_id="wf-1")
        self.assertEqual(appended["prev_hash"], first["entry_hash"])
        verification = audit_chain.verify_demo(self.directory)
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 2)

    def test_tampered_archive_copy_is_detected(self) -> None:
        with audit_chain.file_lock(self.directory):
            audit_chain.append_demo_locked(
                self.directory,
                _record("2020-06-01T00:00:00+00:00"),
                day="20200601",
            )
        audit_chain.archive_demo(self.directory, "20210101", purge=True)
        archived = self.directory / "archive" / "audit-20200601.jsonl"
        archived.write_text(
            archived.read_text(encoding="utf-8").replace("approval:approve", "approval:approve "),
            encoding="utf-8",
        )
        result = audit_chain.verify_demo(self.directory)
        self.assertFalse(result["ok"])
        self.assertIn("SHA-256", result["error"])

    def test_broken_chain_refuses_archive(self) -> None:
        self._write(2)
        (path,) = audit_chain.audit_files(self.directory)
        lines = _lines(path)
        payload = json.loads(lines[0])
        payload["actor"] = "tampered"
        lines[0] = json.dumps(payload, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
        result = audit_chain.archive_demo(self.directory, cutoff, purge=True)
        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["purged"], 0)
        self.assertIn("拒绝归档", result["error"])
        self.assertTrue(path.is_file(), "校验失败时不得删除原始审计")

    def test_list_audit_cursor_and_filters(self) -> None:
        for index in range(5):
            audit.write_audit(
                actor="li" if index % 2 == 0 else "zhang",
                action="approval:approve" if index < 3 else "deploy-command:deploy_now",
                wf_id=f"wf-{index}",
                result="ok" if index % 2 == 0 else "failed",
            )

        first = audit.list_audit(limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        self.assertTrue(first["next_cursor"])

        second = audit.list_audit(limit=2, cursor=first["next_cursor"])
        third = audit.list_audit(limit=2, cursor=second["next_cursor"])
        self.assertFalse(third["has_more"])
        ids = [
            item["event_id"]
            for page in (first, second, third)
            for item in page["items"]
        ]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

        actors = audit.list_audit(actor="li")["items"]
        self.assertEqual({item["actor"] for item in actors}, {"li"})
        actions = audit.list_audit(action="deploy")["items"]
        self.assertEqual(len(actions), 2)
        failed = audit.list_audit(result="failed")["items"]
        self.assertEqual({item["wf_id"] for item in failed}, {"wf-1", "wf-3"})
        with self.assertRaises(ValueError):
            audit.list_audit(cursor="not-a-cursor")


class DbChainTest(unittest.TestCase):
    """production + SQLite：审计表链、跨批次归档、分页过滤。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{self.root / 'audit.db'}",
            },
        )
        self._env.start()
        db_layer.reset_engine()
        self.engine = db_layer.get_engine()
        self._patch = mock.patch.object(audit, "DATA_DIR", self.root / "data")
        self._patch.start()
        auth.invalidate_registry_cache()

    def tearDown(self) -> None:
        self._patch.stop()
        db_layer.reset_engine()
        auth.invalidate_registry_cache()
        self._env.stop()
        self._tmp.cleanup()

    def _tamper(self, event_id: str) -> None:
        table = db_layer.audit_events
        with self.engine.begin() as conn:
            conn.execute(
                table.update().where(table.c.event_id == event_id).values(detail={"x": 1})
            )

    def test_chain_verify_and_tamper_detection(self) -> None:
        entries = [
            audit.write_audit(actor="li", action="approval:approve", wf_id="wf-1")
            for _ in range(3)
        ]
        result = audit.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["mode"], "production")
        self.assertEqual(result["head"], entries[-1]["entry_hash"])

        self._tamper(entries[1]["event_id"])
        result = audit.verify()
        self.assertFalse(result["ok"])
        self.assertIn("entry_hash", result["error"])
        self.assertEqual(result["broken_at"]["event_id"], entries[1]["event_id"])

    def test_archives_survive_purge_and_second_batch(self) -> None:
        table = db_layer.audit_events
        with self.engine.begin() as conn:
            for index in range(2):
                audit_chain.append_db(
                    conn,
                    _record(f"2020-06-01T00:00:0{index}+00:00", actor="legacy", wf_id="wf-old"),
                )
        # 第二批：早于 1 天但晚于 2000 天，用于验证「前驱已 purge 的二次归档」
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.engine.begin() as conn:
            for _ in range(2):
                audit_chain.append_db(
                    conn,
                    _record(recent_ts, actor="li", wf_id="wf-new"),
                )

        first = audit.archive(days=2000, purge=True)
        self.assertEqual(first["error"], "")
        self.assertEqual(first["archived"], 1)
        self.assertEqual(first["purged"], 2)
        with self.engine.connect() as conn:
            remaining = conn.execute(select(table.c.id)).all()
        self.assertEqual(len(remaining), 2)

        verification = audit.verify()
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 4)

        # 前驱已被 purge：二次归档需要从归档副本恢复链尾
        second = audit.archive(days=1, purge=True)
        self.assertEqual(second["error"], "")
        self.assertEqual(second["archived"], 1, second)
        self.assertEqual(second["purged"], 2)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(table.c.id)).all(), [])
        verification = audit.verify()
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 4)

        appended = audit.write_audit(actor="bob", action="approval:reject", wf_id="wf-3")
        verification = audit.verify()
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked"], 5)
        self.assertEqual(verification["head"], appended["entry_hash"])

        with self.engine.connect() as conn:
            archives = conn.execute(select(db_layer.audit_archives)).all()
        self.assertEqual(len(archives), 2)

    def test_list_audit_cursor_and_filters(self) -> None:
        for index in range(5):
            audit.write_audit(
                actor="li" if index % 2 == 0 else "zhang",
                action="approval:approve" if index < 3 else "deploy-command:deploy_now",
                wf_id=f"wf-{index}",
                result="ok" if index % 2 == 0 else "failed",
            )
        first = audit.list_audit(limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        second = audit.list_audit(limit=2, cursor=first["next_cursor"])
        third = audit.list_audit(limit=2, cursor=second["next_cursor"])
        ids = [
            item["id"]
            for page in (first, second, third)
            for item in page["items"]
        ]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(
            {item["actor"] for item in audit.list_audit(actor="zhang")["items"]}, {"zhang"}
        )
        self.assertEqual(len(audit.list_audit(action="deploy")["items"]), 2)
        self.assertEqual(len(audit.list_audit(wf_id="wf-0")["items"]), 1)


class AuditApiTest(unittest.TestCase):
    """BFF：操作审计游标分页、完整性权限与篡改暴露（demo 文件后端）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {"AIOPS_MODE": "demo", "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS)},
        )
        self._env.start()
        self._patch = mock.patch.object(audit, "DATA_DIR", self.data)
        self._patch.start()
        auth.invalidate_registry_cache()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}
        self.viewer = {"Authorization": "Bearer tok-view"}

    def tearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        auth.invalidate_registry_cache()
        self._tmp.cleanup()

    def test_ops_pagination_and_invalid_cursor(self) -> None:
        for index in range(3):
            audit.write_audit(
                actor="li",
                action="approval:approve",
                wf_id=f"wf-{index}",
                params={"index": index},
            )
        first = self.client.get("/api/audit/ops?limit=2", headers=self.admin)
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertEqual(len(payload["items"]), 2)
        self.assertTrue(payload["has_more"])
        self.assertTrue(payload["next_cursor"])

        second = self.client.get(
            f"/api/audit/ops?limit=2&cursor={payload['next_cursor']}", headers=self.admin
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertFalse(second.json()["has_more"])

        bad = self.client.get("/api/audit/ops?cursor=not-a-cursor", headers=self.admin)
        self.assertEqual(bad.status_code, 400)

    def test_integrity_requires_admin_and_exposes_tamper(self) -> None:
        audit.write_audit(actor="li", action="approval:approve", wf_id="wf-1")
        self.assertEqual(
            self.client.get("/api/audit/integrity", headers=self.viewer).status_code, 403
        )
        response = self.client.get("/api/audit/integrity", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "demo")
        self.assertIn("algorithm", payload)
        self.assertIn("archives", payload)

        (path,) = audit_chain.audit_files(self.data / "web-audit")
        path.write_text(
            path.read_text(encoding="utf-8").replace("approval:approve", "approval:reject"),
            encoding="utf-8",
        )
        payload = self.client.get("/api/audit/integrity", headers=self.admin).json()
        self.assertFalse(payload["ok"])
        self.assertIn("entry_hash", payload["error"])


class ProductionFlowAuditApiTest(unittest.TestCase):
    """production 流程审计走 SQL 数据面（不依赖 Temporal）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{self.root / 'flow.db'}",
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
            },
        )
        self._env.start()
        db_layer.reset_engine()
        self.engine = db_layer.get_engine()
        self._patch = mock.patch.object(audit, "DATA_DIR", self.root / "data")
        self._patch.start()
        auth.invalidate_registry_cache()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}
        self._insert_runs()

    def tearDown(self) -> None:
        self._patch.stop()
        db_layer.reset_engine()
        auth.invalidate_registry_cache()
        self._env.stop()
        self._tmp.cleanup()

    def _insert_runs(self) -> None:
        table = db_layer.workflow_runs
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            for index in range(3):
                conn.execute(
                    table.insert().values(
                        workflow_id=f"wf-{index}",
                        run_id=f"run-{index}",
                        stage="DONE" if index != 2 else "FAILED",
                        exec_status="COMPLETED" if index != 2 else "FAILED",
                        kind="alert",
                        risk="low",
                        alert={"alert_id": f"a-{index}", "service": f"svc-{index}"},
                        confidence=0.9,
                        patch_id=f"p-{index}",
                        start_time=db_layer.to_dt(now - timedelta(minutes=10)),
                        close_time=db_layer.to_dt(now - timedelta(minutes=index)),
                        duration_seconds=60,
                        result={"gate_events": []},
                        updated_at=db_layer.now_dt(),
                    )
                )

    def test_flow_audit_uses_sql_data_plane_with_cursor(self) -> None:
        first = self.client.get("/api/audit?limit=2&days=30", headers=self.admin)
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertEqual(payload["source"], "db")
        self.assertEqual(len(payload["items"]), 2)
        self.assertTrue(payload["has_more"])
        self.assertTrue(payload["next_cursor"])
        self.assertEqual(payload["items"][0]["wf_id"], "wf-0")

        second = self.client.get(
            f"/api/audit?limit=2&days=30&cursor={payload['next_cursor']}", headers=self.admin
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertFalse(second.json()["has_more"])

        filtered = self.client.get("/api/audit?days=30&stage=FAILED", headers=self.admin).json()
        self.assertEqual([item["wf_id"] for item in filtered["items"]], ["wf-2"])

    def test_ops_endpoint_uses_sql_data_plane(self) -> None:
        audit.write_audit(actor="li", action="approval:approve", wf_id="wf-0")
        payload = self.client.get("/api/audit/ops?limit=10", headers=self.admin).json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["actor"], "li")


if __name__ == "__main__":
    unittest.main()
