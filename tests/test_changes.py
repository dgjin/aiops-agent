"""变更关联单测（P1-1）：双通道变更源读取与窗口过滤，离线零外部依赖。

覆盖：
    - feed 通道：窗口/service 过滤、时间倒序、limit 截断、坏行容错、naive 时间补 UTC；
    - argocd 通道：manifest 解析（service/version/mtime）、无关服务与过期条目剔除；
    - 双通道合并排序、通道故障降级、AIOPS_CHANGE_LOOKBACK_MINUTES 覆盖。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiops_agent import changes

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


class ChangesTestCase(unittest.TestCase):
    """公共环境：feed 与 argocd 目录均隔离到临时目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.feed = self.dir / "changes.jsonl"
        self.argocd = self.dir / "argocd"
        for target, value in (
            ("DEFAULT_FEED", self.feed),
            ("ARGOCD_DIR", self.argocd),
        ):
            patcher = mock.patch.object(changes, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(
            os.environ, {"AIOPS_CHANGE_FEED": "", "AIOPS_CHANGE_LOOKBACK_MINUTES": ""}
        )
        env.start()
        self.addCleanup(env.stop)

    def _write_feed(self, lines: list[str]) -> None:
        self.feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _line(self, ts, service="order", version="v1.0.601", kind="config", summary="s") -> str:
        return json.dumps(
            {"ts": ts, "service": service, "version": version, "kind": kind, "summary": summary}
        )

    def _manifest(self, name: str, service: str, version: str) -> Path:
        self.argocd.mkdir(exist_ok=True)
        path = self.argocd / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "metadata": {
                        "name": name,
                        "labels": {"aiops.service": service, "aiops.patch-id": f"p-{name}"},
                        "annotations": {"aiops.version": version},
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def _touch_at(self, path: Path, when: datetime) -> None:
        epoch = when.timestamp()
        os.utime(path, (epoch, epoch))


class TestFeedChannel(ChangesTestCase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(changes.recent_changes("order", now=NOW), [])

    def test_window_and_service_filter_with_sorting(self) -> None:
        self._write_feed(
            [
                self._line("2026-10-06T11:50:00+00:00", summary="窗口内-近"),
                self._line("2026-10-06T09:00:00+00:00", summary="窗口外-太旧"),
                self._line("2026-10-06T11:00:00+00:00", service="nl2sql", summary="其他服务"),
                self._line("2026-10-06T11:10:00+00:00", summary="窗口内-较远"),
            ]
        )
        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual([r["summary"] for r in result], ["窗口内-近", "窗口内-较远"])
        self.assertEqual(result[0]["source"], "feed")

    def test_bad_lines_skipped(self) -> None:
        self._write_feed(
            [
                "not a json at all",
                json.dumps(["list", "not", "dict"]),
                json.dumps({"ts": "非法时间", "service": "order"}),
                json.dumps({"service": "order", "summary": "缺 ts"}),
                "",
                self._line("2026-10-06T11:30:00+00:00", summary="唯一合法行"),
            ]
        )
        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["summary"], "唯一合法行")

    def test_limit_truncates_to_five(self) -> None:
        self._write_feed(
            [self._line(f"2026-10-06T11:{minute:02d}:00+00:00") for minute in range(7)]
        )
        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual(len(result), changes.MAX_ITEMS)

    def test_naive_ts_treated_as_utc(self) -> None:
        self._write_feed([self._line("2026-10-06T11:30:00")])
        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual(len(result), 1)

    def test_future_ts_excluded(self) -> None:
        self._write_feed([self._line("2026-10-06T13:00:00+00:00")])
        self.assertEqual(changes.recent_changes("order", lookback_minutes=120, now=NOW), [])


class TestArgocdChannel(ChangesTestCase):
    def test_manifest_parsed_from_mtime(self) -> None:
        fresh = self._manifest("demo-app-p-x-r1", "order", "v1.0.514")
        self._touch_at(fresh, NOW - timedelta(minutes=5))
        stale = self._manifest("demo-app-p-old-r1", "order", "v1.0.500")
        self._touch_at(stale, NOW - timedelta(minutes=200))
        other = self._manifest("demo-app-p-nl2sql-r1", "nl2sql", "v1.0.501")
        self._touch_at(other, NOW - timedelta(minutes=5))

        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "argocd")
        self.assertEqual(result[0]["version"], "v1.0.514")
        self.assertIn("p-x-r1", result[0]["summary"])

    def test_bad_manifest_skipped(self) -> None:
        self.argocd.mkdir(exist_ok=True)
        (self.argocd / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(changes.recent_changes("order", now=NOW), [])


class TestMergeAndDegrade(ChangesTestCase):
    def test_two_channels_merged_and_sorted(self) -> None:
        self._write_feed([self._line("2026-10-06T11:00:00+00:00", summary="feed-旧")])
        manifest = self._manifest("demo-app-p-y-r1", "order", "v1.0.520")
        self._touch_at(manifest, NOW - timedelta(minutes=10))

        result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual([r["source"] for r in result], ["argocd", "feed"])

    def test_channel_exception_degrades(self) -> None:
        self._write_feed([self._line("2026-10-06T11:00:00+00:00", summary="feed 事件")])
        with mock.patch.object(changes, "_argocd_events", side_effect=RuntimeError("boom")):
            result = changes.recent_changes("order", lookback_minutes=120, now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["summary"], "feed 事件")

    def test_env_lookback_override(self) -> None:
        self._write_feed([self._line("2026-10-06T11:00:00+00:00")])
        with mock.patch.dict(os.environ, {"AIOPS_CHANGE_LOOKBACK_MINUTES": "30"}):
            self.assertEqual(changes.recent_changes("order", now=NOW), [])
        with mock.patch.dict(os.environ, {"AIOPS_CHANGE_LOOKBACK_MINUTES": "180"}):
            self.assertEqual(len(changes.recent_changes("order", now=NOW)), 1)
        with mock.patch.dict(os.environ, {"AIOPS_CHANGE_LOOKBACK_MINUTES": "bad"}):
            self.assertEqual(len(changes.recent_changes("order", now=NOW)), 1)  # 非法值回落 120


if __name__ == "__main__":
    unittest.main()
