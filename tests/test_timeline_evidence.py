"""M4 统一时间线与审计证据包：来源合并 / 排序裁剪 / manifest 哈希 / 导出审计。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from unittest import mock

from bff import evidence_package, timeline

NOW = "2026-11-08T12:00:00+00:00"
DETAIL = {
    "wf_id": "wf-1",
    "run_id": "run-1",
    "exec_status": "COMPLETED",
    "start_time": "2026-11-08T11:50:00+00:00",
    "close_time": NOW,
    "workflow_type": "FixWorkflow",
    "task_queue": "aiops",
    "status": {},
    "result": {
        "stage": "DONE",
        "alert_id": "a-1",
        "service": "order",
        "patch_id": "p-1",
        "confidence": 0.9,
        "requested_by": "alice",
        "case_id": "case-1",
        "approvals": [
            {
                "gate": "approval",
                "decision": "approve",
                "actor": "bob",
                "channel": "console",
                "comment": "同意",
                "command_id": "cmd-1",
                "received_at": "2026-11-08T11:55:00+00:00",
            }
        ],
        "gate_events": ["approval:received:approval:approve:by=bob"],
        "command_ids": ["cmd-1"],
        "deploy_result": {"version": "v2", "rolled_back": False},
        "test_report": {"passed": True, "unit_tests": "10/10"},
        "root_cause": {"error_type": "TIMEOUT", "confidence": 0.9, "summary": "超时"},
        "patch": {"patch_id": "p-1", "files": ["a.py"], "risk": "low"},
    },
}

HISTORY_ITEMS = [
    {
        "event_id": 1,
        "event_type": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
        "event_time": "2026-11-08T11:51:00+00:00",
        "detail": {
            "activity_task_scheduled_event_attributes": {
                "activity_type": {"name": "triage"},
                "attempt": 1,
            }
        },
    },
    {
        "event_id": 2,
        "event_type": "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
        "event_time": NOW,
        "detail": {"workflow_execution_completed_event_attributes": {"result": {"stage": "DONE"}}},
    },
]


def _history(**overrides) -> dict:
    payload = {
        "run_id": "run-1",
        "items": HISTORY_ITEMS,
        "has_more": False,
        "next_after": None,
    }
    payload.update(overrides)
    return payload


class TimelineBuildTest(unittest.TestCase):
    def _build(self, **kwargs) -> dict:
        with (
            mock.patch.object(timeline.history_projector, "read_events", return_value=_history()),
            mock.patch.object(
                timeline.history_projector,
                "sync_status",
                return_value={
                    "run_id": "run-1",
                    "event_count": 2,
                    "last_event_id": 2,
                    "complete": True,
                    "gap": False,
                },
            ),
            mock.patch.object(
                timeline.audit,
                "rows_for_workflow",
                return_value=[
                    {
                        "ts": "2026-11-08T11:56:00+00:00",
                        "actor": "alice",
                        "action": "approval:approve",
                        "result": "ok",
                        "command_id": "cmd-1",
                        "wf_id": "wf-1",
                        "target": None,
                        "params": {},
                    }
                ],
            ),
            mock.patch.object(
                timeline.commands,
                "list_commands",
                return_value=[
                    {
                        "command_id": "cmd-1",
                        "command_type": "approval",
                        "actor": "alice",
                        "channel": "console",
                        "status": "ACCEPTED",
                        "audit_action": "approval:approve",
                        "created_at": "2026-11-08T11:54:00+00:00",
                        "submitted_at": "2026-11-08T11:54:01+00:00",
                        "accepted_at": "2026-11-08T11:54:02+00:00",
                        "updated_at": "2026-11-08T11:54:02+00:00",
                    }
                ],
            ),
            mock.patch.object(timeline, "_notify_records", return_value=[]),
            mock.patch.object(
                timeline.escalations,
                "list_all",
                return_value=[
                    {
                        "id": "esc-1",
                        "wf_id": "wf-1",
                        "status": "retrying",
                        "escalated_at": "2026-11-08T11:57:00+00:00",
                        "auto_reason": "人工重试",
                        "assignee": "carol",
                        "history": [
                            {
                                "action": "retry",
                                "actor": "carol",
                                "ts": "2026-11-08T11:58:00+00:00",
                                "detail": "已触发",
                            }
                        ],
                    }
                ],
            ),
        ):
            return timeline.build(DETAIL, **kwargs)

    def test_merges_all_sources_in_time_order(self) -> None:
        payload = self._build()
        sources = {item["source"] for item in payload["items"]}
        self.assertTrue({"history", "audit", "command", "escalation"} <= sources)
        self.assertIn("status", sources)
        counts = payload["counts"]
        self.assertEqual(sum(counts.values()), len(payload["items"]))
        for source, count in counts.items():
            self.assertEqual(
                count, sum(1 for item in payload["items"] if item["source"] == source)
            )

        times = [item.get("time") or "" for item in payload["items"]]
        self.assertEqual(times, sorted(times))
        history = next(item for item in payload["items"] if item["source"] == "history")
        self.assertEqual(history["ref"]["activity_type"], "triage")
        estimated = [
            item for item in payload["items"] if item.get("time_estimated") is True
        ]
        self.assertTrue(estimated, "无真实时间的快照节点必须标记 time_estimated")

    def test_limit_keeps_latest_and_marks_has_more(self) -> None:
        full = self._build()
        limited = self._build(limit=2)
        self.assertTrue(limited["has_more"])
        self.assertEqual(len(limited["items"]), 2)
        self.assertEqual(
            [item["id"] for item in limited["items"]],
            [item["id"] for item in full["items"][-2:]],
        )

    def test_running_flow_has_status_snapshot_even_without_history(self) -> None:
        detail = {
            **DETAIL,
            "exec_status": "RUNNING",
            "close_time": None,
            "result": {},
            "status": {
                "stage": "WAIT_APPROVAL",
                "alert": {"alert_id": "a-1", "service": "order"},
                "approvals": [],
                "gate_events": [],
                "deadline": {"kind": "approval", "at": "2026-11-08T13:00:00+00:00"},
            },
        }
        with (
            mock.patch.object(
                timeline.history_projector,
                "read_events",
                return_value={"run_id": "run-1", "items": [], "has_more": False, "next_after": None},
            ),
            mock.patch.object(timeline, "_notify_records", return_value=[]),
            mock.patch.object(timeline.escalations, "list_all", return_value=[]),
            mock.patch.object(timeline.audit, "rows_for_workflow", return_value=[]),
            mock.patch.object(timeline.commands, "list_commands", return_value=[]),
        ):
            payload = timeline.build(detail)
        self.assertEqual(payload["exec_status"], "RUNNING")
        self.assertTrue(any(item["source"] == "status" for item in payload["items"]))
        self.assertTrue(payload["history"]["complete"] is False)


class EvidencePackageTest(unittest.TestCase):
    def _patches(self):
        return (
            mock.patch.object(
                evidence_package.history_projector,
                "sync_workflow",
                new=mock.AsyncMock(return_value={"error": "", "stored": 0}),
            ),
            mock.patch.object(
                evidence_package.history_projector, "read_events", return_value=_history()
            ),
            mock.patch.object(
                evidence_package.history_projector,
                "sync_status",
                return_value={"complete": True, "event_count": 2},
            ),
            mock.patch.object(evidence_package.audit, "rows_for_workflow", return_value=[]),
            mock.patch.object(evidence_package.commands, "list_commands", return_value=[]),
            mock.patch.object(evidence_package.timeline, "notification_records", return_value=[]),
            mock.patch.object(evidence_package.timeline, "escalation_records", return_value=[]),
            mock.patch.object(
                evidence_package.evidence_store, "get", return_value={"lines": ["boom"]}
            ),
        )

    def test_manifest_hashes_are_reproducible(self) -> None:
        patches = self._patches()
        for patch in patches:
            patch.start()
        try:
            package = asyncio.run(
                evidence_package.build("wf-1", detail=DETAIL, actor="alice")
            )
        finally:
            for patch in patches:
                patch.stop()

        self.assertEqual(package["schema"], "aiops-evidence-package/v1")
        for section in (
            "workflow",
            "gates",
            "history",
            "audit",
            "commands",
            "notifications",
            "escalations",
            "manual_actions",
            "log_evidence",
            "test_report",
            "release",
            "retry_chain",
        ):
            self.assertIn(section, package)

        manifest = package["manifest"]
        self.assertEqual(manifest["algorithm"], "sha256(canonical-json)")
        for entry in manifest["sections"]:
            body = json.dumps(
                package[entry["name"]],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            self.assertEqual(
                hashlib.sha256(body.encode("utf-8")).hexdigest(), entry["sha256"]
            )
            self.assertEqual(len(body.encode("utf-8")), entry["bytes"])
        root = hashlib.sha256(
            "\n".join(f"{item['name']}:{item['sha256']}" for item in manifest["sections"]).encode(
                "utf-8"
            )
        ).hexdigest()
        self.assertEqual(manifest["root_sha256"], root)

        self.assertEqual(package["history"]["complete"], True)
        self.assertEqual(package["workflow"]["requested_by"], "alice")
        self.assertEqual(package["workflow"]["workflow"]["run_id"], "run-1")
        self.assertEqual(package["retry_chain"][0]["wf_id"], "wf-1")
        self.assertEqual(package["retry_chain"][0]["relation"], "self")

    def test_export_writes_audit_fail_closed(self) -> None:
        patches = self._patches()
        write_patch = mock.patch.object(
            evidence_package.audit,
            "write_audit",
            return_value={"event_id": "evt-1", "entry_hash": "hash-1"},
        )
        for patch in patches:
            patch.start()
        write = write_patch.start()
        try:
            package, entry = asyncio.run(
                evidence_package.build_and_audit("wf-1", actor="alice", detail=DETAIL)
            )
        finally:
            write_patch.stop()
            for patch in patches:
                patch.stop()

        self.assertEqual(entry["event_id"], "evt-1")
        self.assertEqual(package["export"]["audit_event_id"], "evt-1")
        self.assertEqual(package["export"]["entry_hash"], "hash-1")
        self.assertEqual(package["export"]["exported_by"], "alice")
        write.assert_called_once()
        kwargs = write.call_args.kwargs
        self.assertEqual(kwargs["action"], "evidence:export")
        self.assertEqual(kwargs["params"]["manifest_sha256"], package["manifest"]["root_sha256"])


if __name__ == "__main__":
    unittest.main()
