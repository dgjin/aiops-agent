"""M4 Temporal History 完整投影：分页 / 幂等 / 终态 / 重跑 / 断档 / 对账。

覆盖 demo 文件后端与 SQLite production 分支；Temporal 用假 handle 注入，
不连接任何真实服务。重点验证「超过 UI 200 条上限仍可完整投影与分页读取」。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import func, select
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent

from aiops_agent import db as db_layer
from bff import history_projector

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


class _Status:
    def __init__(self, name: str = "RUNNING") -> None:
        self.name = name


class _Desc:
    def __init__(
        self,
        run_id: str = "run-1",
        status: str = "RUNNING",
        close_time: datetime | None = None,
    ) -> None:
        self.run_id = run_id
        self.status = _Status(status)
        self.start_time = NOW
        self.close_time = close_time


class _FakeHandle:
    """最小 WorkflowHandle：describe + fetch_history_events（异步分页）。"""

    def __init__(self, events: list[HistoryEvent], desc: _Desc | None = None) -> None:
        self.events = events
        self.desc = desc or _Desc()

    async def describe(self, rpc_timeout=None):
        return self.desc

    def fetch_history_events(self, page_size=100, rpc_timeout=None):
        async def gen():
            for event in self.events:
                yield event

        return gen()


def _event(event_id: int, type_name: str = "EVENT_TYPE_WORKFLOW_TASK_STARTED") -> HistoryEvent:
    event = HistoryEvent(event_id=event_id, event_type=getattr(EventType, type_name))
    stamp = Timestamp()
    stamp.FromDatetime(NOW + timedelta(seconds=event_id))
    event.event_time.CopyFrom(stamp)
    return event


def _events(count: int, *, terminal: bool = False) -> list[HistoryEvent]:
    rows = [_event(index) for index in range(1, count + 1)]
    if terminal:
        rows.append(_event(count + 1, "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"))
    return rows


class _DemoProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": str(self.root)})
        self._env.start()
        history_projector.reset_for_tests()

    def tearDown(self) -> None:
        history_projector.reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()

    def test_more_than_ui_limit_is_fully_projected_and_paged(self) -> None:
        """250 条事件（> 200 UI 上限）全部落盘，游标分页可完整取回。"""
        handle = _FakeHandle(_events(250, terminal=True), _Desc(status="COMPLETED"))
        result = asyncio.run(history_projector.sync_workflow("wf-1", "run-1", handle=handle))
        self.assertEqual(result["error"], "")
        self.assertEqual(result["fetched"], 251)
        self.assertEqual(result["stored"], 251)
        self.assertEqual(result["last_event_id"], 251)
        self.assertTrue(result["complete"])

        page1 = history_projector.read_events("wf-1", "run-1", limit=100)
        self.assertEqual(len(page1["items"]), 100)
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["next_after"], 100)
        self.assertEqual(page1["items"][0]["event_id"], 1)

        page2 = history_projector.read_events("wf-1", "run-1", after_id=100, limit=100)
        self.assertEqual(len(page2["items"]), 100)
        self.assertEqual(page2["items"][0]["event_id"], 101)

        page3 = history_projector.read_events("wf-1", "run-1", after_id=200, limit=100)
        self.assertEqual([item["event_id"] for item in page3["items"]], list(range(201, 252)))
        self.assertFalse(page3["has_more"])

        status = history_projector.sync_status("wf-1", "run-1")
        self.assertEqual(status["event_count"], 251)
        self.assertTrue(status["complete"])
        self.assertFalse(status["gap"])

    def test_incremental_and_idempotent(self) -> None:
        first = _FakeHandle(_events(10), _Desc())
        result = asyncio.run(history_projector.sync_workflow("wf-2", "run-1", handle=first))
        self.assertEqual(result["stored"], 10)
        self.assertFalse(result["complete"])

        # 再次同步同一份历史：不重复写入
        again = asyncio.run(history_projector.sync_workflow("wf-2", "run-1", handle=first))
        self.assertEqual(again["stored"], 0)
        self.assertEqual(again["fetched"], 10)

        # 增量：新增 5 条只追加这 5 条
        second = _FakeHandle(_events(15), _Desc())
        result = asyncio.run(history_projector.sync_workflow("wf-2", "run-1", handle=second))
        self.assertEqual(result["stored"], 5)
        status = history_projector.sync_status("wf-2", "run-1")
        self.assertEqual(status["event_count"], 15)
        self.assertEqual(status["last_event_id"], 15)

    def test_run_switch_keeps_old_run_events(self) -> None:
        first = _FakeHandle(_events(6), _Desc(run_id="run-1"))
        asyncio.run(history_projector.sync_workflow("wf-3", "run-1", handle=first))

        second = _FakeHandle(_events(3), _Desc(run_id="run-2"))
        result = asyncio.run(history_projector.sync_workflow("wf-3", "run-2", handle=second))
        self.assertEqual(result["run_id"], "run-2")
        self.assertEqual(result["stored"], 3)

        old = history_projector.read_events("wf-3", "run-1")
        new = history_projector.read_events("wf-3", "run-2")
        self.assertEqual(len(old["items"]), 6)
        self.assertEqual(len(new["items"]), 3)
        self.assertEqual(history_projector.sync_status("wf-3")["run_id"], "run-2")

    def test_gap_detection_and_stats(self) -> None:
        handle = _FakeHandle(_events(8), _Desc())
        asyncio.run(history_projector.sync_workflow("wf-4", "run-1", handle=handle))
        _, meta_path = history_projector._paths("wf-4", "run-1")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["event_count"] = 6  # 模拟投影缺行
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        status = history_projector.sync_status("wf-4", "run-1")
        self.assertTrue(status["gap"])

        snapshot = history_projector.stats()
        self.assertGreaterEqual(snapshot["runs"], 1)
        self.assertGreaterEqual(snapshot["gaps"], 1)

    def test_reconcile_syncs_pending_runs(self) -> None:
        handle = _FakeHandle(_events(4), _Desc())
        asyncio.run(history_projector.sync_workflow("wf-5", "run-1", handle=handle))
        fake_result = {
            "wf_id": "wf-5",
            "run_id": "run-1",
            "stored": 1,
            "error": "",
        }
        fake_summary = {
            "targets": 1,
            "synced": 1,
            "stored": 1,
            "errors": [],
            "results": [fake_result],
            "executions": 1,
        }
        with (
            mock.patch.object(
                history_projector, "sync_all", new=mock.AsyncMock(return_value=fake_summary)
            ) as sync_all,
            mock.patch.object(history_projector, "sync_workflow", new=mock.AsyncMock()) as sync,
        ):
            summary = asyncio.run(history_projector.reconcile(limit=10))
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["synced"], 1)
        self.assertIn("stats", summary)
        sync_all.assert_awaited_once()
        sync.assert_not_awaited()


class _ProductionProjectionTest(unittest.TestCase):
    """SQLite production：workflow_events 幂等 + workflow_runs 同步位。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
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
        history_projector.reset_for_tests()

    def tearDown(self) -> None:
        history_projector.reset_for_tests()
        db_layer.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def _count_events(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(db_layer.workflow_events)
                ).scalar_one()
            )

    def test_events_and_sync_position(self) -> None:
        handle = _FakeHandle(_events(220, terminal=True), _Desc(status="COMPLETED"))
        result = asyncio.run(history_projector.sync_workflow("wf-db", "run-1", handle=handle))
        self.assertEqual(result["stored"], 221)
        self.assertEqual(self._count_events(), 221)

        with self.engine.connect() as conn:
            row = conn.execute(
                select(db_layer.workflow_runs).where(
                    db_layer.workflow_runs.c.workflow_id == "wf-db"
                )
            ).mappings().first()
        self.assertEqual(int(row["last_history_event_id"]), 221)
        self.assertTrue(row["history_complete"])

        # 幂等：重复同步不新增
        again = asyncio.run(history_projector.sync_workflow("wf-db", "run-1", handle=handle))
        self.assertEqual(again["stored"], 0)
        self.assertEqual(self._count_events(), 221)

        page = history_projector.read_events("wf-db", "run-1", after_id=200, limit=50)
        self.assertEqual([item["event_id"] for item in page["items"]], list(range(201, 222)))
        self.assertFalse(page["has_more"])

    def test_new_run_resets_position_but_keeps_old_events(self) -> None:
        asyncio.run(
            history_projector.sync_workflow(
                "wf-db2", "run-1", handle=_FakeHandle(_events(10), _Desc(run_id="run-1"))
            )
        )
        asyncio.run(
            history_projector.sync_workflow(
                "wf-db2", "run-2", handle=_FakeHandle(_events(3), _Desc(run_id="run-2"))
            )
        )
        self.assertEqual(self._count_events(), 13)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(db_layer.workflow_runs).where(
                    db_layer.workflow_runs.c.workflow_id == "wf-db2"
                )
            ).mappings().first()
        self.assertEqual(row["run_id"], "run-2")
        self.assertEqual(int(row["last_history_event_id"]), 3)

        old = history_projector.read_events("wf-db2", "run-1")
        new = history_projector.read_events("wf-db2", "run-2")
        self.assertEqual(len(old["items"]), 10)
        self.assertEqual(len(new["items"]), 3)


if __name__ == "__main__":
    unittest.main()
