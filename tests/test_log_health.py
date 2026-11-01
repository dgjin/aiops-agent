"""日志采集健康心跳（M1）：写入 / 新鲜度 / 连续失败 / 警告与损坏文件容错。

心跳用于区分「进程活着」与「日志供给正常」：watchdog status 与 /metrics 读取同一份
``log_ship_health.json``，任一服务超龄、连续失败或带警告即判不健康。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiops_agent import log_health

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class HealthPathTest(unittest.TestCase):
    def test_env_override(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            old = os.environ.get(log_health.ENV_PATH)
            os.environ[log_health.ENV_PATH] = str(path)
            try:
                self.assertEqual(log_health.health_path(), path)
            finally:
                if old is None:
                    os.environ.pop(log_health.ENV_PATH, None)
                else:
                    os.environ[log_health.ENV_PATH] = old


class RecordAndSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "log_ship_health.json"

    def test_success_is_healthy(self) -> None:
        log_health.record(
            "nl2sql", success=True, pushed=12, read_lines=12, path=self.path, now=NOW
        )
        items = log_health.snapshot(self.path, now=NOW, max_age_seconds=300)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["service"], "nl2sql")
        self.assertTrue(item["healthy"])
        self.assertEqual(item["consecutive_failures"], 0)
        self.assertEqual(item["last_pushed_lines"], 12)
        self.assertIn("心跳新鲜", item["detail"])

    def test_failure_counts_consecutive_and_clears_on_success(self) -> None:
        log_health.record("nl2sql", success=False, error="HTTP 502", path=self.path, now=NOW)
        log_health.record(
            "nl2sql", success=False, error="HTTP 502", path=self.path, now=NOW + timedelta(seconds=10)
        )
        item = log_health.snapshot(self.path, now=NOW + timedelta(seconds=20))[0]
        self.assertFalse(item["healthy"])
        self.assertEqual(item["consecutive_failures"], 2)
        self.assertIn("连续失败 2 次", item["detail"])
        self.assertIn("HTTP 502", item["detail"])

        log_health.record("nl2sql", success=True, pushed=1, path=self.path, now=NOW + timedelta(seconds=30))
        healed = log_health.snapshot(self.path, now=NOW + timedelta(seconds=40))[0]
        self.assertTrue(healed["healthy"])
        self.assertEqual(healed["consecutive_failures"], 0)
        self.assertEqual(healed["last_error"], "")

    def test_stale_heartbeat_is_unhealthy(self) -> None:
        log_health.record("nl2sql", success=True, path=self.path, now=NOW - timedelta(minutes=30))
        item = log_health.snapshot(self.path, now=NOW, max_age_seconds=300)[0]
        self.assertFalse(item["healthy"])
        self.assertIn("停摆", item["detail"])
        self.assertEqual(item["age_seconds"], 1800)

    def test_warning_marks_unhealthy(self) -> None:
        log_health.record(
            "nl2sql",
            success=True,
            warning="日志文件不存在：/tmp/none.log",
            path=self.path,
            now=NOW,
        )
        item = log_health.snapshot(self.path, now=NOW)[0]
        self.assertFalse(item["healthy"])
        self.assertIn("日志文件不存在", item["detail"])

    def test_never_succeeded_is_unhealthy(self) -> None:
        log_health.record("nl2sql", success=False, error="boom", path=self.path, now=NOW)
        item = log_health.snapshot(self.path, now=NOW)[0]
        self.assertFalse(item["healthy"])
        self.assertIn("从未成功采集", item["detail"])

    def test_multiple_services_sorted(self) -> None:
        log_health.record("svc-b", success=True, path=self.path, now=NOW)
        log_health.record("svc-a", success=True, path=self.path, now=NOW)
        items = log_health.snapshot(self.path, now=NOW)
        self.assertEqual([item["service"] for item in items], ["svc-a", "svc-b"])

    def test_missing_and_corrupt_file_tolerated(self) -> None:
        self.assertEqual(log_health.read(self.path), {"services": {}})
        self.assertEqual(log_health.snapshot(self.path, now=NOW), [])

        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(log_health.read(self.path), {"services": {}})

    def test_record_replaces_atomically_and_keeps_history(self) -> None:
        log_health.record("nl2sql", success=False, error="first", path=self.path, now=NOW)
        log_health.record(
            "nl2sql", success=False, error="second", path=self.path, now=NOW + timedelta(seconds=5)
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        entry = payload["services"]["nl2sql"]
        self.assertEqual(entry["last_error"], "second")
        self.assertEqual(entry["consecutive_failures"], 2)
        self.assertIn("updated_at", payload)

    def test_snapshot_default_path_uses_data_root(self) -> None:
        # 未显式指定 path：走 AIOPS_DATA_ROOT 下的 log_ship_health.json（测试隔离目录）
        log_health.record("nl2sql", success=True, now=NOW)
        self.assertEqual(log_health.health_path().name, "log_ship_health.json")
        log_health.health_path().unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
