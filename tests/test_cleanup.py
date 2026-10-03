"""P5-03：数据清理 TTL（cleanup）单测。

覆盖：TTL 表（demo / production）/ 审计文件名日期判定与 mtime 回退 / 工作区目录 /
通知与 ArgoCD 文件 / DB 行清理（SQLite 生产分支）/ dry-run / 单域失败隔离 / 报告结构。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiops_agent import cleanup, db as db_layer, mode


def _utime(path: Path, days_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


def _day_str(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


class _TmpRootsTest(unittest.TestCase):
    """所有文件域显式指向临时目录，避免测试触碰真实 data/ 数据。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in ("web-audit", "sandbox", "notify", "qoder", "argocd"):
            (self.root / name).mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *, dry_run: bool = False, ttl: dict | None = None, **overrides) -> dict:
        kwargs = {
            "audit_dir": self.root / "web-audit",
            "sandbox_root": self.root / "sandbox",
            "notify_dir": self.root / "notify",
            "qoder_root": self.root / "qoder",
            "argocd_dir": self.root / "argocd",
        }
        kwargs.update(overrides)
        return cleanup.run_cleanup(dry_run=dry_run, ttl=ttl, **kwargs)


class TtlTest(unittest.TestCase):
    def test_demo_ttl(self) -> None:
        # conftest 强制 AIOPS_MODE=demo
        self.assertEqual(cleanup.ttl_days(), cleanup.TTL_DEMO)
        self.assertEqual(cleanup.ttl_days()["sandbox"], 1)

    def test_production_ttl(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            self.assertEqual(cleanup.ttl_days(), cleanup.TTL_PRODUCTION)
            self.assertEqual(cleanup.ttl_days()["audit"], 365)


class ReportShapeTest(_TmpRootsTest):
    def test_report_shape_and_no_errors(self) -> None:
        report = self._run()
        self.assertEqual(report["mode"], "demo")
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            sorted(report["deleted"]),
            [
                "argocd_files",
                "audit_files",
                "audit_rows",
                "notify_files",
                "qoder_records",
                "qoder_workspaces",
                "sandbox_workspaces",
                "workflow_runs",
            ],
        )
        # demo 不触达数据库：DB 域恒为 0
        self.assertEqual(report["deleted"]["audit_rows"], 0)
        self.assertEqual(report["deleted"]["workflow_runs"], 0)

    def test_domain_failure_isolated(self) -> None:
        old_audit = self.root / "web-audit" / f"audit-{_day_str(40)}.jsonl"
        old_audit.write_text("{}\n", encoding="utf-8")
        with mock.patch.object(
            cleanup, "_cleanup_workspaces", side_effect=RuntimeError("boom")
        ):
            report = self._run()
        self.assertEqual(len(report["errors"]), 2)  # sandbox + qoder 两个工作区域
        self.assertEqual(report["deleted"]["audit_files"], 1)  # 其余域不受影响
        self.assertFalse(old_audit.exists())


class AuditFilesTest(_TmpRootsTest):
    def test_expired_by_filename_date(self) -> None:
        old = self.root / "web-audit" / f"audit-{_day_str(40)}.jsonl"
        keep = self.root / "web-audit" / f"audit-{_day_str(0)}.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        keep.write_text("{}\n", encoding="utf-8")
        report = self._run()
        self.assertEqual(report["deleted"]["audit_files"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_unknown_name_falls_back_to_mtime(self) -> None:
        stale = self.root / "web-audit" / "legacy.jsonl"
        notes = self.root / "web-audit" / "notes.txt"
        stale.write_text("", encoding="utf-8")
        notes.write_text("", encoding="utf-8")
        _utime(stale, 90)
        _utime(notes, 90)
        report = self._run()
        # 无日期前缀的 jsonl 按 mtime 判定（90 天 > demo 30 天）→ 删除；
        # 非 jsonl 文件不属审计留痕形态 → 不受影响
        self.assertEqual(report["deleted"]["audit_files"], 1)
        self.assertFalse(stale.exists())
        self.assertTrue(notes.exists())

    def test_dry_run_counts_without_deleting(self) -> None:
        old = self.root / "web-audit" / f"audit-{_day_str(40)}.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        report = self._run(dry_run=True)
        self.assertEqual(report["deleted"]["audit_files"], 1)
        self.assertTrue(old.exists())


class WorkspacesTest(_TmpRootsTest):
    def test_sandbox_expired_dir_removed_only(self) -> None:
        old = self.root / "sandbox" / "p-old"
        fresh = self.root / "sandbox" / "p-new"
        stray = self.root / "sandbox" / "stray.txt"
        for path in (old, fresh):
            path.mkdir()
            (path / "file.py").write_text("x = 1", encoding="utf-8")
        stray.write_text("", encoding="utf-8")
        _utime(old, 3)
        _utime(stray, 3)
        report = self._run()
        self.assertEqual(report["deleted"]["sandbox_workspaces"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(stray.exists())  # 根下散文件不属于工作区，不删

    def test_qoder_workspaces_and_records(self) -> None:
        old_ws = self.root / "qoder" / "p-wp5-old"
        fresh_ws = self.root / "qoder" / "p-wp5-new"
        old_record = self.root / "qoder" / "p-wp5-old.run.json"
        fresh_record = self.root / "qoder" / "p-wp5-new.run.json"
        for path in (old_ws, fresh_ws):
            path.mkdir()
        for path in (old_record, fresh_record):
            path.write_text("{}", encoding="utf-8")
        _utime(old_ws, 3)
        _utime(old_record, 3)
        report = self._run()
        self.assertEqual(report["deleted"]["qoder_workspaces"], 1)
        self.assertEqual(report["deleted"]["qoder_records"], 1)
        self.assertFalse(old_ws.exists())
        self.assertFalse(old_record.exists())
        self.assertTrue(fresh_ws.exists())
        self.assertTrue(fresh_record.exists())


class NotifyArgocdTest(_TmpRootsTest):
    def test_notify_and_argocd_expired_files(self) -> None:
        notify_old = self.root / "notify" / "approval-p-1.json"
        notify_new = self.root / "notify" / "approval-p-2.json"
        argocd_old = self.root / "argocd" / "demo-app-p-1.json"
        argocd_new = self.root / "argocd" / "demo-app-p-2.json"
        keep_txt = self.root / "notify" / "readme.txt"
        for path in (notify_old, notify_new, argocd_old, argocd_new, keep_txt):
            path.write_text("{}", encoding="utf-8")
        _utime(notify_old, 10)   # demo notify TTL=7 天 → 过期
        _utime(argocd_old, 10)   # demo argocd TTL=7 天 → 过期
        _utime(keep_txt, 365)
        report = self._run()
        self.assertEqual(report["deleted"]["notify_files"], 1)
        self.assertEqual(report["deleted"]["argocd_files"], 1)
        self.assertFalse(notify_old.exists())
        self.assertFalse(argocd_old.exists())
        self.assertTrue(notify_new.exists())
        self.assertTrue(argocd_new.exists())
        self.assertTrue(keep_txt.exists())  # 非 json 不回清理


class DbCleanupTest(unittest.TestCase):
    """production 分支：SQLite 临时库覆盖 audit_events / workflow_runs 行删除。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in ("web-audit", "sandbox", "notify", "qoder", "argocd"):
            (self.root / name).mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{self.root / 'test.db'}",
            },
        )
        self._env.start()
        db_layer.reset_engine()
        self.engine = db_layer.get_engine()
        self._seed()

    def tearDown(self) -> None:
        db_layer.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def _seed(self) -> None:
        old = db_layer.now_dt() - timedelta(days=400)
        new = db_layer.now_dt() - timedelta(days=1)
        with self.engine.begin() as conn:
            conn.execute(
                db_layer.audit_events.insert().values(
                    ts=old, actor="t", action="x", workflow_id="", target=None,
                    detail={}, result="ok", mode="production",
                )
            )
            conn.execute(
                db_layer.audit_events.insert().values(
                    ts=new, actor="t", action="x", workflow_id="", target=None,
                    detail={}, result="ok", mode="production",
                )
            )
            conn.execute(
                db_layer.workflow_runs.insert().values(
                    workflow_id="w-old", run_id="r", stage="DONE", exec_status="COMPLETED",
                    updated_at=old,
                )
            )
            conn.execute(
                db_layer.workflow_runs.insert().values(
                    workflow_id="w-new", run_id="r", stage="DONE", exec_status="COMPLETED",
                    updated_at=new,
                )
            )

    def _run(self, *, dry_run: bool = False) -> dict:
        return cleanup.run_cleanup(
            dry_run=dry_run,
            audit_dir=self.root / "web-audit",
            sandbox_root=self.root / "sandbox",
            notify_dir=self.root / "notify",
            qoder_root=self.root / "qoder",
            argocd_dir=self.root / "argocd",
        )

    def _count(self, table) -> int:
        from sqlalchemy import func, select

        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar_one())

    def test_deletes_expired_rows_only(self) -> None:
        report = self._run()
        self.assertEqual(report["mode"], "production")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["deleted"]["audit_rows"], 1)
        self.assertEqual(report["deleted"]["workflow_runs"], 1)
        self.assertEqual(self._count(db_layer.audit_events), 1)
        self.assertEqual(self._count(db_layer.workflow_runs), 1)

    def test_dry_run_counts_without_deleting(self) -> None:
        report = self._run(dry_run=True)
        self.assertEqual(report["deleted"]["audit_rows"], 1)
        self.assertEqual(report["deleted"]["workflow_runs"], 1)
        self.assertEqual(self._count(db_layer.audit_events), 2)
        self.assertEqual(self._count(db_layer.workflow_runs), 2)

    def test_custom_ttl_respected(self) -> None:
        # TTL 调为 500 天：400 天前的行不再过期
        report = self._run_with_ttl({"audit": 500, "sandbox": 7, "notify": 90, "qoder": 7, "argocd": 90, "workflow_runs": 500})
        self.assertEqual(report["deleted"]["audit_rows"], 0)
        self.assertEqual(report["deleted"]["workflow_runs"], 0)

    def _run_with_ttl(self, ttl: dict) -> dict:
        return cleanup.run_cleanup(
            ttl=ttl,
            audit_dir=self.root / "web-audit",
            sandbox_root=self.root / "sandbox",
            notify_dir=self.root / "notify",
            qoder_root=self.root / "qoder",
            argocd_dir=self.root / "argocd",
        )


class CliTest(unittest.TestCase):
    def test_main_dry_run_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("web-audit", "sandbox", "notify", "qoder", "argocd"):
                (root / name).mkdir()
            with mock.patch.object(
                cleanup,
                "run_cleanup",
                return_value={
                    "mode": "demo",
                    "dry_run": True,
                    "run_at": "t",
                    "ttl_days": {},
                    "deleted": {},
                    "errors": [],
                },
            ) as fn:
                code = cleanup.main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(fn.call_args.kwargs["dry_run"])

    def test_main_errors_return_one(self) -> None:
        with mock.patch.object(
            cleanup,
            "run_cleanup",
            return_value={
                "mode": "demo",
                "dry_run": False,
                "run_at": "t",
                "ttl_days": {},
                "deleted": {},
                "errors": ["x: boom"],
            },
        ):
            self.assertEqual(cleanup.main([]), 1)


if __name__ == "__main__":
    unittest.main()
