"""被监控应用清单单测（bff/monitored_apps）：播种 / 校验 / CRUD / 持久化 / 热生效。

「热生效」的判据：清单不在进程内缓存——**外部改动磁盘后，下一次 list_all() 即反映新值**。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bff import monitored_apps as store


class MonitorStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "monitored_apps.json"
        patcher = mock.patch.object(store, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------- 播种 ----------

    def test_seeds_from_env_on_first_read(self) -> None:
        with mock.patch.dict(
            os.environ, {"AIOPS_MONITOR_URL": "http://seed.test:9000/", "AIOPS_MONITOR_SERVICE": "svc-x"}
        ):
            apps = store.list_all()
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["url"], "http://seed.test:9000/")
        self.assertEqual(apps[0]["service"], "svc-x")
        self.assertEqual(apps[0]["id"], "default")
        self.assertTrue(self.path.is_file())

    def test_seeds_default_when_env_absent(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_MONITOR_URL", None)
            apps = store.list_all()
        self.assertEqual(apps[0]["url"], store.DEFAULT_URL)

    # ---------- 校验 ----------

    def test_duplicate_name_rejected(self) -> None:
        store.add(name="应用A", url="http://a.test/")
        with self.assertRaises(store.MonitorStoreError) as ctx:
            store.add(name="应用A", url="http://b.test/")
        self.assertIn("名称已存在", str(ctx.exception))

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(store.MonitorStoreError):
            store.add(name="   ", url="http://a.test/")

    def test_url_requires_scheme(self) -> None:
        with self.assertRaises(store.MonitorStoreError) as ctx:
            store.add(name="A", url="localhost:3000")
        self.assertIn("http://", str(ctx.exception))

    def test_blank_url_rejected(self) -> None:
        with self.assertRaises(store.MonitorStoreError):
            store.add(name="A", url="  ")

    # ---------- CRUD ----------

    def test_add_persists_and_lists(self) -> None:
        app = store.add(name="智能问数据分析系统", url="http://localhost:3000/", service="nl2sql", note="测试")
        self.assertTrue(app["id"])
        self.assertEqual(app["name"], "智能问数据分析系统")
        self.assertEqual(app["service"], "nl2sql")
        self.assertTrue(app["enabled"])
        apps = store.list_all()
        self.assertIn(app["id"], {a["id"] for a in apps})

    def test_id_is_slug_of_ascii_name(self) -> None:
        app = store.add(name="Query API", url="http://a.test/")
        self.assertEqual(app["id"], "query-api")

    def test_unique_id_when_slug_collides(self) -> None:
        first = store.add(name="App", url="http://a.test/")
        second = store.add(name="app", url="http://b.test/")  # slug 同为 app
        self.assertNotEqual(first["id"], second["id"])

    def test_update_changes_fields_and_timestamp(self) -> None:
        app = store.add(name="A", url="http://a.test/")
        before, updated = store.update(app["id"], url="https://new.test/x", enabled=False, note="改了")
        self.assertEqual(before["url"], "http://a.test/")  # 返回变更前快照（供审计/回滚）
        self.assertTrue(before["enabled"])
        self.assertEqual(updated["url"], "https://new.test/x")
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["note"], "改了")
        self.assertGreaterEqual(updated["updated_at"], app["updated_at"])

    def test_update_missing_raises(self) -> None:
        with self.assertRaises(store.MonitorStoreError) as ctx:
            store.update("nope", url="http://x.test/")
        self.assertIn("未找到", str(ctx.exception))

    def test_update_duplicate_name_rejected(self) -> None:
        a = store.add(name="A", url="http://a.test/")
        store.add(name="B", url="http://b.test/")
        with self.assertRaises(store.MonitorStoreError):
            store.update(a["id"], name="B")

    def test_remove_returns_removed_entry(self) -> None:
        app = store.add(name="A", url="http://a.test/")
        removed = store.remove(app["id"])
        self.assertIsNotNone(removed)
        self.assertEqual(removed["name"], "A")  # 返回被删条目，供审计留痕
        self.assertIsNone(store.remove(app["id"]))  # 再次删除 → None
        self.assertNotIn(app["id"], {a["id"] for a in store.list_all()})

    def test_get(self) -> None:
        app = store.add(name="A", url="http://a.test/")
        self.assertEqual(store.get(app["id"])["name"], "A")
        self.assertIsNone(store.get("nope"))

    # ---------- log_path 与采集目标 ----------

    def test_log_path_roundtrip(self) -> None:
        app = store.add(name="A", url="http://a.test/", service="svc-a", log_path="/tmp/app_server*.log")
        self.assertEqual(app["log_path"], "/tmp/app_server*.log")
        _before, updated = store.update(app["id"], log_path="/tmp/new.log")
        self.assertEqual(updated["log_path"], "/tmp/new.log")

    def test_log_path_defaults_empty(self) -> None:
        self.assertEqual(store.add(name="A", url="http://a.test/")["log_path"], "")

    # ---------- probe_keyword（页面健康关键字） ----------

    def test_probe_keyword_roundtrip_and_default(self) -> None:
        self.assertEqual(store.add(name="A", url="http://a.test/")["probe_keyword"], "")
        app = store.add(name="B", url="http://b.test/", probe_keyword='  <div id="root"  ')
        self.assertEqual(app["probe_keyword"], '<div id="root"')  # 写入即 trim
        _before, updated = store.update(app["id"], probe_keyword="")
        self.assertEqual(updated["probe_keyword"], "")

    def test_duplicate_copies_probe_keyword(self) -> None:
        src = store.add(name="A", url="http://a.test/", probe_keyword='<div id="root"')
        self.assertEqual(store.duplicate(src["id"])["probe_keyword"], '<div id="root"')

    def test_log_targets_filters_enabled_and_path(self) -> None:
        store.add(name="A", url="http://a.test/", service="svc-a", log_path="/tmp/a.log")
        second = store.add(name="B", url="http://b.test/", service="svc-b", log_path="/tmp/b.log")
        store.add(name="C", url="http://c.test/", service="svc-c")  # 未配置 log_path
        store.update(second["id"], enabled=False)  # 停用

        targets = store.log_targets()
        self.assertEqual([t["service"] for t in targets], ["svc-a"])
        self.assertEqual(targets[0]["name"], "A")
        self.assertEqual(targets[0]["log_path"], "/tmp/a.log")

    def test_log_targets_seeded_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MONITOR_LOG_PATH": "/var/log/app.log"}):
            targets = store.log_targets()
        self.assertEqual(targets[0]["log_path"], "/var/log/app.log")

    # ---------- 热生效 / 健壮性 ----------

    def test_external_file_change_is_visible_immediately(self) -> None:
        """核心：清单不做进程内缓存——外部改写磁盘后下一次读取即生效。"""
        store.list_all()  # 触发播种
        rewritten = [
            {
                "id": "ext",
                "name": "外部写入",
                "url": "http://external.test/",
                "service": "ext",
                "enabled": True,
                "note": "",
                "created_at": "2026-10-03T00:00:00+00:00",
                "updated_at": "2026-10-03T00:00:00+00:00",
            }
        ]
        self.path.write_text(json.dumps(rewritten, ensure_ascii=False), encoding="utf-8")
        apps = store.list_all()
        self.assertEqual([a["id"] for a in apps], ["ext"])

    def test_write_is_atomic_no_tmp_left(self) -> None:
        store.add(name="A", url="http://a.test/")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_corrupt_file_falls_back_to_seed_without_crash(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        apps = store.list_all()
        self.assertTrue(apps)
        self.assertIn("url", apps[0])


class ImportBatchDuplicateTest(unittest.TestCase):
    """WP7：导入 / 批量 / 复制。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "monitored_apps.json"
        patcher = mock.patch.object(store, "DATA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------- 导入 ----------

    def test_merge_adds_and_updates(self) -> None:
        store.add(name="A", url="http://a.test/", enabled=True)
        result = store.import_many(
            [
                {"name": "A", "url": "http://a-new.test/", "enabled": False},
                {"name": "B", "url": "http://b.test/", "service": "svc-b"},
            ],
            mode="merge",
        )
        self.assertEqual((result["added"], result["updated"]), (1, 1))
        self.assertEqual(result["errors"], [])
        apps = {a["name"]: a for a in store.list_all()}
        self.assertEqual(apps["A"]["url"], "http://a-new.test/")
        self.assertFalse(apps["A"]["enabled"])
        self.assertEqual(apps["B"]["service"], "svc-b")

    def test_merge_collects_errors_without_aborting(self) -> None:
        result = store.import_many(
            [
                {"name": "", "url": "http://x.test/"},          # 缺名称
                {"name": "C", "url": "localhost:3000"},          # 非法地址
                {"name": "D", "url": "http://d.test/"},           # 正常
            ],
            mode="merge",
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("D", {a["name"] for a in store.list_all()})

    def test_replace_clears_first(self) -> None:
        store.add(name="OLD", url="http://old.test/")
        result = store.import_many([{"name": "NEW", "url": "http://new.test/"}], mode="replace")
        self.assertEqual(result["added"], 1)
        self.assertEqual([a["name"] for a in store.list_all()], ["NEW"])

    def test_invalid_mode_and_empty_items_rejected(self) -> None:
        with self.assertRaises(store.MonitorStoreError):
            store.import_many([{"name": "A", "url": "http://a.test/"}], mode="upsert")
        with self.assertRaises(store.MonitorStoreError):
            store.import_many([], mode="merge")

    # ---------- 批量 ----------

    def test_batch_enable_disable_delete(self) -> None:
        a = store.add(name="A", url="http://a.test/", enabled=True)
        b = store.add(name="B", url="http://b.test/", enabled=True)

        self.assertEqual(store.batch([a["id"], b["id"]], "disable")["affected"], 2)
        self.assertFalse(store.get(a["id"])["enabled"])

        self.assertEqual(store.batch([a["id"]], "enable")["affected"], 1)
        self.assertTrue(store.get(a["id"])["enabled"])

        result = store.batch([a["id"], b["id"]], "delete")
        self.assertEqual(result["affected"], 2)
        remaining = {item["name"] for item in store.list_all()}
        self.assertNotIn("A", remaining)  # A/B 已删除
        self.assertNotIn("B", remaining)

    def test_batch_reports_missing(self) -> None:
        store.add(name="A", url="http://a.test/")
        result = store.batch(["nope"], "disable")
        self.assertEqual(result["affected"], 0)
        self.assertEqual(result["missing"], ["nope"])

    def test_batch_rejects_bad_action_and_empty_ids(self) -> None:
        with self.assertRaises(store.MonitorStoreError):
            store.batch(["a"], "purge")
        with self.assertRaises(store.MonitorStoreError):
            store.batch([], "disable")

    # ---------- 复制 ----------

    def test_duplicate_creates_new_name(self) -> None:
        src = store.add(name="A", url="http://a.test/", service="svc-a", log_path="/tmp/a.log")
        copy = store.duplicate(src["id"])
        self.assertNotEqual(copy["id"], src["id"])
        self.assertEqual(copy["name"], "A 副本")
        self.assertEqual(copy["url"], src["url"])
        self.assertEqual(copy["log_path"], src["log_path"])

    def test_duplicate_dedupes_names(self) -> None:
        src = store.add(name="A", url="http://a.test/")
        first = store.duplicate(src["id"])
        second = store.duplicate(src["id"])
        self.assertEqual(first["name"], "A 副本")
        self.assertEqual(second["name"], "A 副本 2")

    def test_duplicate_missing_raises(self) -> None:
        with self.assertRaises(store.MonitorStoreError):
            store.duplicate("nope")


class DiffFieldsTest(unittest.TestCase):
    """WP6：字段级差异（审计留痕与回滚依据）。"""

    def test_reports_only_changed_fields(self) -> None:
        before = {"id": "a", "name": "A", "url": "http://a/", "enabled": True, "updated_at": "t1"}
        after = {"id": "a", "name": "A", "url": "http://b/", "enabled": False, "updated_at": "t2"}
        diff = store.diff_fields(before, after)
        self.assertEqual(
            diff,
            {"url": {"from": "http://a/", "to": "http://b/"}, "enabled": {"from": True, "to": False}},
        )

    def test_ignores_updated_at(self) -> None:
        self.assertEqual(store.diff_fields({"updated_at": "t1"}, {"updated_at": "t2"}), {})

    def test_no_change_is_empty(self) -> None:
        self.assertEqual(store.diff_fields({"url": "x"}, {"url": "x"}), {})

    def test_new_field_shows_none_from(self) -> None:
        self.assertEqual(store.diff_fields({}, {"note": "hi"}), {"note": {"from": None, "to": "hi"}})


if __name__ == "__main__":
    unittest.main()
