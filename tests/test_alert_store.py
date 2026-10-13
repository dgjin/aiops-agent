"""C 批次聚合存储测试：SQLite 夹具覆盖 schema 升级、去重、窗口、租约与补偿语义。

运行：.venv/bin/python -m unittest discover -s tests -t . -v

真实 MySQL 的多进程并发/行锁契约由 scripts/ci_integration.py 在隔离环境验证；
本文件的“双实例”场景用顺序交错 + 显式时间模拟（SQLite 无 FOR UPDATE，不做真并发）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlalchemy import MetaData, create_engine, func, select

from aiops_agent import alert_store, db, readiness
from aiops_agent.models import Alert

# 固定基准时间：窗口/退避断言不依赖系统时钟
BASE = datetime(2026, 10, 13, 8, 0, 0)


def _alert(alert_id: str, service: str = "nl2sql") -> Alert:
    return Alert(alert_id=alert_id, service=service)


class SchemaUpgradeTest(unittest.TestCase):
    """ensure_schema：老库缺列显式补齐、幂等；metadata 契约含聚合三表。"""

    def test_aggregation_tables_in_metadata_contract(self) -> None:
        self.assertLessEqual(
            {"alert_domains", "alert_events", "alert_members"}, set(db.metadata.tables)
        )

    def test_ensure_schema_upgrades_legacy_and_is_idempotent(self) -> None:
        engine = create_engine("sqlite://")
        legacy = db.monitored_apps.to_metadata(MetaData())
        legacy._columns.remove(legacy.c.depends_on)  # 老库夹具：缺 depends_on 列
        legacy.create(engine)
        statements = db.ensure_schema(engine)
        self.assertTrue(any("depends_on" in s for s in statements), statements)
        self.assertEqual(db.ensure_schema(engine), [])  # 幂等：无重复 DDL
        self.assertTrue(readiness.inspect_schema(engine)["ok"])


class IngestTest(unittest.TestCase):
    """ingest / 去重 / 窗口 / 认领语义（顺序交错模拟双实例）。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        db.metadata.create_all(self.engine)
        self.snapshot = {"release_gate": {"shadow_mode": True}}

    def _ingest(self, alerts: list[Alert], now: datetime | None = None, window: float = 90.0):
        return alert_store.ingest(
            self.engine, alerts, self.snapshot, window, now=now or BASE
        )

    def _counts(self) -> tuple[int, int]:
        with self.engine.connect() as conn:
            events = conn.execute(select(func.count()).select_from(db.alert_events)).scalar_one()
            members = conn.execute(select(func.count()).select_from(db.alert_members)).scalar_one()
        return int(events), int(members)

    def _event_rows(self):
        with self.engine.connect() as conn:
            return conn.execute(select(db.alert_events)).mappings().all()

    def test_same_batch_aggregates_to_single_representative(self) -> None:
        outcome = self._ingest([_alert("a-1"), _alert("a-2")])
        self.assertEqual(len(outcome.events), 1)
        self.assertEqual(len(outcome.to_start), 1)
        event = outcome.to_start[0]
        self.assertTrue(event.is_new)
        self.assertEqual(event.wf_id, "aiops-fix-nl2sql-a-1")
        self.assertEqual(event.count, 2)
        self.assertEqual(event.alert_ids, ["a-1", "a-2"])
        self.assertEqual(self._counts(), (1, 2))

    def test_duplicate_member_not_counted_then_busy_until_started(self) -> None:
        first = self._ingest([_alert("a-1")])
        event = first.to_start[0]
        again = self._ingest([_alert("a-1")], now=BASE + timedelta(seconds=5))
        self.assertEqual(again.to_start, [])
        self.assertEqual(len(again.busy), 1)  # 有效租约中：需重投确认
        self.assertEqual(again.events[0].count, 1)  # 重复投递不重复计数
        self.assertTrue(
            alert_store.mark_started(self.engine, event.event_id, event.claim_token, now=BASE)
        )
        late = self._ingest([_alert("a-1"), _alert("a-2")], now=BASE + timedelta(seconds=10))
        self.assertEqual(late.to_start, [])
        self.assertEqual(late.busy, [])
        self.assertEqual(late.events[0].status, "started")
        self.assertEqual(late.events[0].count, 2)
        self.assertEqual(self._counts(), (1, 2))

    def test_window_expiry_after_start_opens_new_event(self) -> None:
        first = self._ingest([_alert("a-1")])
        alert_store.mark_started(
            self.engine, first.to_start[0].event_id, first.to_start[0].claim_token, now=BASE
        )
        nxt = self._ingest([_alert("a-2")], now=BASE + timedelta(seconds=91))
        self.assertEqual(len(nxt.to_start), 1)
        self.assertNotEqual(nxt.to_start[0].event_id, first.to_start[0].event_id)
        self.assertEqual(self._counts(), (2, 2))

    def test_window_boundary_is_fixed_from_first_alert(self) -> None:
        first = self._ingest([_alert("a-1")])
        alert_store.mark_started(
            self.engine, first.to_start[0].event_id, first.to_start[0].claim_token, now=BASE
        )
        inside = self._ingest([_alert("a-2")], now=BASE + timedelta(seconds=89))
        self.assertEqual(inside.to_start, [])  # 89s：仍在窗口内，归并
        edge = self._ingest([_alert("a-3")], now=BASE + timedelta(seconds=90))
        self.assertEqual(len(edge.to_start), 1)  # 90s 整：窗口关闭，开新事件

    def test_failed_start_retry_same_event_and_wf_id(self) -> None:
        first = self._ingest([_alert("a-1"), _alert("a-2")])
        event = first.to_start[0]
        self.assertTrue(
            alert_store.mark_failed(
                self.engine, event.event_id, event.claim_token, "temporal down", now=BASE
            )
        )
        row = self._event_rows()[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["claim_token"], "")
        self.assertIsNotNone(row["retry_at"])
        self.assertIn("temporal down", row["error_summary"])
        # 重投（Alertmanager 整批重发）：成员不重复计数，重新认领同一事件/同一代表 id
        retry = self._ingest([_alert("a-1"), _alert("a-2")], now=BASE + timedelta(seconds=1))
        self.assertEqual(len(retry.to_start), 1)
        self.assertEqual(retry.to_start[0].event_id, event.event_id)
        self.assertEqual(retry.to_start[0].wf_id, event.wf_id)
        self.assertEqual(retry.to_start[0].count, 2)
        self.assertEqual(self._counts(), (1, 2))
        self.assertTrue(
            alert_store.mark_started(
                self.engine, event.event_id, retry.to_start[0].claim_token, now=BASE
            )
        )
        self.assertEqual(self._event_rows()[0]["attempts"], 2)

    def test_expired_lease_takeover_reuses_representative_id(self) -> None:
        first = self._ingest([_alert("a-1")])  # 模拟：认领后进程崩溃（不回报）
        event = first.to_start[0]
        with self.engine.begin() as conn:  # 强行让租约过期
            conn.execute(
                db.alert_events.update()
                .where(db.alert_events.c.event_id == event.event_id)
                .values(lease_expires_at=BASE - timedelta(seconds=1))
            )
        takeover = self._ingest([_alert("a-2")], now=BASE + timedelta(seconds=5))
        self.assertEqual(len(takeover.to_start), 1)
        self.assertEqual(takeover.to_start[0].event_id, event.event_id)
        self.assertEqual(takeover.to_start[0].wf_id, event.wf_id)  # 同一代表 id：不开第二条流程
        # 旧认领者迟到的回报被拒绝（token 已换）
        self.assertFalse(
            alert_store.mark_started(self.engine, event.event_id, event.claim_token, now=BASE)
        )

    def test_all_stale_resend_opens_no_event(self) -> None:
        first = self._ingest([_alert("a-1")])
        alert_store.mark_started(
            self.engine, first.to_start[0].event_id, first.to_start[0].claim_token, now=BASE
        )
        stale = self._ingest([_alert("a-1")], now=BASE + timedelta(seconds=600))
        self.assertEqual(stale.events, [])  # 历史周期重投：不给旧周期开第二条流程
        self.assertEqual(stale.to_start, [])
        self.assertEqual(self._counts(), (1, 1))

    def test_fault_domains_isolated_by_service(self) -> None:
        outcome = self._ingest([_alert("a-1", "nl2sql"), _alert("b-1", "order")])
        self.assertEqual(len(outcome.to_start), 2)
        self.assertEqual(self._counts(), (2, 2))
        self.assertEqual(len({view.domain_key for view in outcome.events}), 2)

    def test_due_events_honors_retry_backoff_and_claim_mutex(self) -> None:
        first = self._ingest([_alert("a-1")])
        event = first.to_start[0]
        alert_store.mark_failed(self.engine, event.event_id, event.claim_token, "boom", now=BASE)
        self.assertEqual(alert_store.due_events(self.engine, now=BASE), [])  # 退避未到
        due_at = BASE + timedelta(seconds=16)
        due = alert_store.due_events(self.engine, now=due_at)
        self.assertEqual([view.event_id for view in due], [event.event_id])
        token = alert_store.claim_event(self.engine, event.event_id, now=due_at)
        self.assertIsNotNone(token)
        # 租约生效：补偿实例互斥，第二个认领失败
        self.assertIsNone(
            alert_store.claim_event(self.engine, event.event_id, now=due_at + timedelta(seconds=1))
        )


if __name__ == "__main__":
    unittest.main()
