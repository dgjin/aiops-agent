"""应用日志采集器单测（ship_app_logs）：解析 / 分级 / 分组推送 / 增量位点 / 半行与轮转。

离线零外部依赖：Loki 推送一律 mock。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ship_app_logs


class ParseLineTest(unittest.TestCase):
    JSON_LINE = (
        '{"ts":"2026-10-03T01:23:12.166Z","level":"info",'
        '"msg":"[HTTP] GET /api/system/engine -> 304 4ms","module":"HTTP",'
        '"requestId":"ffd13e4bbc16","userId":1}'
    )

    def test_json_line_extracts_level_and_trace_id(self):
        level, line = ship_app_logs.parse_line(self.JSON_LINE)
        self.assertEqual(level, "info")
        self.assertTrue(line.startswith("trace_id=ffd13e4bbc16 "), line)
        self.assertIn("[HTTP]", line)
        self.assertIn("GET /api/system/engine", line)

    def test_json_error_level_normalized(self):
        raw = json.dumps({"level": "ERROR", "msg": "boom", "requestId": "abc"})
        level, line = ship_app_logs.parse_line(raw)
        self.assertEqual(level, "error")
        self.assertTrue(line.startswith("trace_id=abc "))

    def test_json_warning_maps_to_warn(self):
        level, _ = ship_app_logs.parse_line(json.dumps({"level": "warning", "msg": "x"}))
        self.assertEqual(level, "warn")

    def test_non_json_line_keeps_text_and_detects_level(self):
        level, line = ship_app_logs.parse_line("11:35:05 PM [vite] (client) page reload server/routes/report.ts")
        self.assertEqual(level, "info")
        self.assertIn("[vite]", line)

    def test_non_json_error_detected(self):
        level, line = ship_app_logs.parse_line("2026-10-03 ERROR [cc60f393ba1e] GET /api/query -> 500")
        self.assertEqual(level, "error")
        self.assertIn("GET /api/query", line)

    def test_blank_line_ignored(self):
        self.assertEqual(ship_app_logs.parse_line(""), ("", ""))
        self.assertEqual(ship_app_logs.parse_line("   "), ("", ""))

    def test_missing_request_id_has_no_trace_prefix(self):
        level, line = ship_app_logs.parse_line(json.dumps({"level": "info", "msg": "no id"}))
        self.assertEqual(level, "info")
        self.assertFalse(line.startswith("trace_id="))
        self.assertEqual(line, "no id")

    def test_normalize_level_fallback(self):
        self.assertEqual(ship_app_logs.normalize_level("NOTICE"), "info")
        self.assertEqual(ship_app_logs.normalize_level(None), "info")
        self.assertEqual(ship_app_logs.normalize_level("debug"), "info")
        self.assertEqual(ship_app_logs.normalize_level("fatal"), "error")


class ShipOnceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log = self.root / "app_server.log"
        self.positions = self.root / "positions.json"

    def _write(self, text: str) -> None:
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def test_groups_by_level_and_injects_trace_id(self):
        self._write(
            json.dumps({"level": "info", "msg": "ok", "requestId": "r1"}) + "\n"
            + json.dumps({"level": "error", "msg": "boom", "requestId": "r2"}) + "\n"
            + json.dumps({"level": "error", "msg": "boom2", "requestId": "r3"}) + "\n"
        )
        with mock.patch(
            "ship_app_logs.logs.push_lines", side_effect=lambda service, level, lines, **kw: len(lines)
        ) as push:
            stats = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)

        self.assertEqual(stats["read"], 3)
        self.assertEqual(stats["by_level"], {"info": 1, "error": 2})
        self.assertEqual(stats["pushed"], 3)

        calls = {call.args[1]: call.args[2] for call in push.call_args_list}
        self.assertEqual(len(calls["info"]), 1)
        self.assertEqual(len(calls["error"]), 2)
        self.assertTrue(all(c.args[0] == "nl2sql" for c in push.call_args_list))
        self.assertTrue(calls["error"][0].startswith("trace_id=r2 "))

    def test_incremental_offset_only_ships_new_lines(self):
        self._write(json.dumps({"level": "info", "msg": "first"}) + "\n")
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0):
            first = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(first["read"], 1)

        # 无新增 → 不重复推送
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0) as push:
            second = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(second["read"], 0)
        push.assert_not_called()

        # 追加 → 只推送新增
        self._write(json.dumps({"level": "warn", "msg": "second"}) + "\n")
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0) as push:
            third = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(third["read"], 1)
        self.assertEqual(third["by_level"], {"warn": 1})
        self.assertIn("second", push.call_args_list[0].args[2][0])

    def test_partial_line_not_pushed_until_completed(self):
        """半个 JSON 行不得被当成坏行推送；补全后再推。"""
        self._write(json.dumps({"level": "error", "msg": "half", "requestId": "r9"}))  # 无换行
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0) as push:
            first = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(first["read"], 0)
        self.assertTrue(first["pending_partial"])
        push.assert_not_called()

        self._write("\n")  # 补上换行，行完成
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0) as push:
            second = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(second["read"], 1)
        self.assertEqual(second["by_level"], {"error": 1})
        self.assertTrue(push.call_args_list[0].args[2][0].startswith("trace_id=r9 "))

    def test_from_start_reads_whole_file_ignoring_positions(self):
        self._write(json.dumps({"level": "info", "msg": "a"}) + "\n")
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0):
            ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0):
            again = ship_app_logs.ship_once(self.log, "nl2sql", self.positions, from_start=True)
        self.assertEqual(again["read"], 1)

    def test_truncated_file_restarts_from_zero(self):
        self._write(json.dumps({"level": "info", "msg": "long line " * 5}) + "\n")
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0):
            ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.log.write_text(json.dumps({"level": "info", "msg": "after rotate"}) + "\n", encoding="utf-8")
        with mock.patch("ship_app_logs.logs.push_lines", return_value=0) as push:
            after = ship_app_logs.ship_once(self.log, "nl2sql", self.positions)
        self.assertEqual(after["read"], 1)
        self.assertIn("after rotate", push.call_args_list[0].args[2][0])

    def test_missing_file_is_noop(self):
        stats = ship_app_logs.ship_once(self.root / "nope.log", "nl2sql", self.positions)
        self.assertEqual(stats["read"], 0)
        self.assertEqual(stats["pushed"], 0)


class TestResolveLogFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_plain_path_returns_single(self):
        self.assertEqual(ship_app_logs.resolve_log_files("/tmp/x/a.log"), [Path("/tmp/x/a.log")])

    def test_glob_expands_matching_files_sorted(self):
        (self.root / "app_server.log").write_text("a\n", encoding="utf-8")
        (self.root / "app_server-2026.log").write_text("b\n", encoding="utf-8")
        (self.root / "other.log").write_text("c\n", encoding="utf-8")
        files = ship_app_logs.resolve_log_files(str(self.root / "app_server*.log"))
        self.assertEqual([p.name for p in files], ["app_server-2026.log", "app_server.log"])

    def test_glob_without_match_returns_empty(self):
        self.assertEqual(ship_app_logs.resolve_log_files(str(self.root / "none*.log")), [])


class TestShipList(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.positions = self.root / "positions.json"
        self.log = self.root / "app.log"
        self.log.write_text(json.dumps({"level": "error", "msg": "boom", "requestId": "r1"}) + "\n", encoding="utf-8")

    def test_skips_disabled_and_entries_without_log_path(self):
        apps = [
            {"id": "a", "name": "A", "service": "svc-a", "enabled": True, "log_path": str(self.log)},
            {"id": "b", "name": "B", "service": "svc-b", "enabled": False, "log_path": str(self.log)},
            {"id": "c", "name": "C", "service": "svc-c", "enabled": True, "log_path": "  "},
        ]
        targets = [a for a in apps if a["enabled"] and a["log_path"].strip()]
        with mock.patch(
            "ship_app_logs.logs.push_lines", side_effect=lambda s, l, lines, **kw: len(lines)
        ) as push:
            stats = ship_app_logs.ship_list(targets, self.positions)
        self.assertEqual(stats["apps"], 1)
        self.assertEqual({call.args[0] for call in push.call_args_list}, {"svc-a"})

    def test_per_app_service_label(self):
        log_b = self.root / "app_b.log"
        log_b.write_text(json.dumps({"level": "info", "msg": "ok"}) + "\n", encoding="utf-8")
        apps = [
            {"id": "a", "name": "A", "service": "svc-a", "log_path": str(self.log)},
            {"id": "b", "name": "B", "service": "svc-b", "log_path": str(log_b)},
        ]
        with mock.patch(
            "ship_app_logs.logs.push_lines", side_effect=lambda s, l, lines, **kw: len(lines)
        ) as push:
            stats = ship_app_logs.ship_list(apps, self.positions)
        self.assertEqual(stats["pushed"], 2)
        self.assertEqual({call.args[0] for call in push.call_args_list}, {"svc-a", "svc-b"})

    def test_glob_entry_ships_all_rotated_files_with_same_service(self):
        (self.root / "app_server.log").write_text(json.dumps({"level": "info", "msg": "x"}) + "\n", encoding="utf-8")
        (self.root / "app_server-old.log").write_text(json.dumps({"level": "info", "msg": "y"}) + "\n", encoding="utf-8")
        apps = [
            {
                "id": "a",
                "name": "A",
                "service": "nl2sql",
                "log_path": str(self.root / "app_server*.log"),
            }
        ]
        with mock.patch(
            "ship_app_logs.logs.push_lines", side_effect=lambda s, l, lines, **kw: len(lines)
        ):
            stats = ship_app_logs.ship_list(apps, self.positions)
        self.assertEqual(stats["results"][0]["files"], 2)
        self.assertEqual(stats["pushed"], 2)

    def test_offsets_isolated_per_file(self):
        """多个应用共用一份位点文件，互不干扰。"""
        log_b = self.root / "b.log"
        log_b.write_text(json.dumps({"level": "info", "msg": "b"}) + "\n", encoding="utf-8")
        apps = [
            {"id": "a", "name": "A", "service": "a", "log_path": str(self.log)},
            {"id": "b", "name": "B", "service": "b", "log_path": str(log_b)},
        ]
        with mock.patch("ship_app_logs.logs.push_lines", side_effect=lambda s, l, n, **k: len(n)):
            ship_app_logs.ship_list(apps, self.positions)
            again = ship_app_logs.ship_list(apps, self.positions)
        self.assertEqual(again["read"], 0)


class TestLoadAppsHotRead(unittest.TestCase):
    """清单热读取：bff 清单变化后，load_apps() 下一次调用即生效（采集器无需重启）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "monitored_apps.json"
        patcher = mock.patch("bff.monitored_apps.DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reflects_store_changes_immediately(self):
        from bff import monitored_apps as store

        store.add(name="A", url="http://a.test/", service="svc-a", log_path="/tmp/a.log")
        self.assertEqual([a["service"] for a in ship_app_logs.load_apps()], ["svc-a"])

        store.add(name="B", url="http://b.test/", service="svc-b", log_path="/tmp/b.log")
        self.assertEqual({a["service"] for a in ship_app_logs.load_apps()}, {"svc-a", "svc-b"})

        # 停用 A → 立即从采集目标消失
        target = next(a for a in store.list_all() if a["name"] == "A")
        store.update(target["id"], enabled=False)
        self.assertEqual([a["service"] for a in ship_app_logs.load_apps()], ["svc-b"])

        # 清除日志路径 → 也不再采集（仍可被探测）
        target_b = next(a for a in store.list_all() if a["name"] == "B")
        store.update(target_b["id"], log_path="")
        self.assertEqual(ship_app_logs.load_apps(), [])

    def test_load_apps_survives_broken_store(self):
        """清单不可读时返回空列表，不抛异常（采集循环不中断）。"""
        with mock.patch("bff.monitored_apps.log_targets", side_effect=RuntimeError("boom")):
            self.assertEqual(ship_app_logs.load_apps(), [])


if __name__ == "__main__":
    unittest.main()
