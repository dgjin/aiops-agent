"""可用性巡检单测（app_prober）：状态机防抖 / 告警载荷 / 单轮编排 / 状态落盘与读取容错。

核心不变式：
- 同一次故障周期（fail_since 不变）只注入一次告警（Alertmanager 侧再按 fingerprint 兜底去重）；
- 恢复后复位并注入 resolved（同 labelset + endsAt 置过去，立即关闭未决告警）；
- 清单移除/停用条目同样收尾 resolved，避免 Alertmanager 挂着过期告警；
- 状态文件读写绝不阻断巡检与控制台展示（读侧容错返回空对象）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_prober as prober
from bff import monitored_apps as store


def _app(app_id: str = "app-1", **overrides) -> dict:
    app = {
        "id": app_id,
        "name": "演示应用",
        "url": f"http://{app_id}.test:3000/",
        "service": "nl2sql",
        "enabled": True,
    }
    app.update(overrides)
    return app


def _fail(error: str = "connection refused") -> dict:
    return {"running": False, "target": "http://x.test/", "error": error}


def _ok(status_code: int = 200, latency_ms: float = 8.0) -> dict:
    return {"running": True, "target": "http://x.test/", "status_code": status_code, "latency_ms": latency_ms}


class AdvanceTest(unittest.TestCase):
    """状态机：阈值累计 / 同故障周期只告警一次 / 恢复复位 / 再故障新周期新 alert_id。"""

    def setUp(self) -> None:
        self.record = {"id": "app-1"}
        self.t0 = 1_000_000.0

    def test_failures_accumulate_and_alert_at_threshold(self) -> None:
        self.assertIsNone(prober.advance(self.record, False, ts=self.t0, threshold=3))
        self.assertIsNone(prober.advance(self.record, False, ts=self.t0 + 15, threshold=3))
        self.assertEqual(prober.advance(self.record, False, ts=self.t0 + 30, threshold=3), "alert")
        self.assertEqual(self.record["failures"], 3)
        self.assertTrue(self.record["alerted"])
        self.assertEqual(self.record["alert_id"], prober.alert_id_for("app-1", self.t0))
        self.assertEqual(self.record["fail_since"], self.t0)  # 首败时间跨轮保持

    def test_no_duplicate_alert_within_same_failure_cycle(self) -> None:
        for i in range(3):
            prober.advance(self.record, False, ts=self.t0 + i, threshold=3)
        alert_id = self.record["alert_id"]
        self.assertIsNone(prober.advance(self.record, False, ts=self.t0 + 30, threshold=3))
        self.assertEqual(self.record["failures"], 4)
        self.assertEqual(self.record["alert_id"], alert_id)  # 同周期幂等键不变

    def test_recovery_resolves_once_and_resets(self) -> None:
        for i in range(3):
            prober.advance(self.record, False, ts=self.t0 + i, threshold=3)
        self.assertEqual(prober.advance(self.record, True, ts=self.t0 + 10, threshold=3), "resolve")
        self.assertFalse(self.record["alerted"])
        self.assertEqual(self.record["failures"], 0)
        self.assertIsNone(self.record["fail_since"])
        self.assertIn("last_ok_at", self.record)
        # 已恢复后继续在线：不再返回 resolve（防重复收尾）
        self.assertIsNone(prober.advance(self.record, True, ts=self.t0 + 25, threshold=3))

    def test_recovery_without_alert_returns_none(self) -> None:
        prober.advance(self.record, False, ts=self.t0, threshold=3)  # 只失败 1 次（未达阈值）
        self.assertIsNone(prober.advance(self.record, True, ts=self.t0 + 5, threshold=3))
        self.assertEqual(self.record["failures"], 0)

    def test_new_failure_cycle_gets_new_alert_id(self) -> None:
        for i in range(3):
            prober.advance(self.record, False, ts=self.t0 + i, threshold=3)
        prober.advance(self.record, True, ts=self.t0 + 10, threshold=3)
        first_id = self.record["alert_id"]
        for i in range(3):
            prober.advance(self.record, False, ts=self.t0 + 100 + i, threshold=3)
        self.assertNotEqual(self.record["alert_id"], first_id)
        self.assertEqual(self.record["alert_id"], prober.alert_id_for("app-1", self.t0 + 100))


class BuildAlertTest(unittest.TestCase):
    """告警载荷：aiops_demo 路由标签 / 幂等键 / resolved 同 labelset。"""

    def test_unreachable_alert_labels_and_annotations(self) -> None:
        app = _app("app-9", service="svc-y")
        payload = prober.build_unreachable_alert(app, failures=3, error="connection refused", fail_since=123.9)
        self.assertEqual(len(payload), 1)
        labels = payload[0]["labels"]
        self.assertEqual(labels["alertname"], "AppUnreachable")
        self.assertEqual(labels["severity"], "critical")
        self.assertEqual(labels["service"], "svc-y")
        self.assertEqual(labels["aiops_demo"], "true")  # 路由到 AIOps 接入服务的关键标签
        self.assertEqual(labels["alert_id"], "probe-app-9-123")
        self.assertIn("演示应用", payload[0]["annotations"]["summary"])
        self.assertIn("connection refused", payload[0]["annotations"]["description"])

    def test_service_falls_back_to_default(self) -> None:
        payload = prober.build_unreachable_alert(_app(service=None), failures=1, error="x", fail_since=1.0)
        self.assertEqual(payload[0]["labels"]["service"], store.DEFAULT_SERVICE)

    def test_resolved_alert_same_labelset_with_ends_at(self) -> None:
        labels = prober.build_unreachable_alert(_app(), failures=3, error="x", fail_since=5.0)[0]["labels"]
        resolved = prober.build_resolved_alert(labels)
        self.assertEqual(resolved[0]["labels"], labels)  # 同 fingerprint → 立即置 resolved
        self.assertEqual(resolved[0]["annotations"], {})
        self.assertIn("endsAt", resolved[0])


class RunRoundTest(unittest.TestCase):
    """单轮编排：mock 清单与探测函数，验证注入时序、参数透传与状态复位。"""

    def setUp(self) -> None:
        self.state: dict = {}
        self.injected: list[list[dict]] = []
        patcher = mock.patch.object(
            prober,
            "inject_alerts",
            side_effect=lambda payload, alertmanager: self.injected.append(payload),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _fake_probe(results: list[dict]):
        """按顺序出队探测结果（末项复用），并记录调用参数供断言。"""
        queue = list(results)
        calls: list[tuple] = []

        def probe(url, *, timeout=2.0, keyword="", health_path=""):
            calls.append((url, timeout, keyword, health_path))
            return queue.pop(0) if len(queue) > 1 else queue[0]

        probe.calls = calls
        return probe

    def test_threshold_alert_injected_once_then_resolved(self) -> None:
        app = _app("app-1")
        probe = self._fake_probe([_fail()] * 4 + [_ok()])
        with mock.patch.object(prober.store, "list_all", return_value=[app]):
            for i in range(4):  # 连续 4 轮不可达：仅第 3 轮（达阈值）注入 1 次
                prober.run_round(self.state, probe_fn=probe, threshold=3, timeout=1.5, ts=1000.0 + i * 15)
            prober.run_round(self.state, probe_fn=probe, threshold=3, timeout=1.5, ts=2000.0)  # 恢复轮

        self.assertEqual(len(self.injected), 2)
        alert_payload, resolved_payload = self.injected
        self.assertEqual(alert_payload[0]["labels"]["alertname"], "AppUnreachable")
        self.assertEqual(alert_payload[0]["labels"]["alert_id"], "probe-app-1-1000")  # fail_since 首败时刻
        self.assertEqual(resolved_payload[0]["labels"], alert_payload[0]["labels"])
        self.assertIn("endsAt", resolved_payload[0])
        self.assertTrue(all(c == (app["url"], 1.5, "", "") for c in probe.calls))  # 探测超时/关键字/健康路径透传

        record = self.state["app-1"]
        self.assertTrue(record["ok"])
        self.assertEqual(record["failures"], 0)
        self.assertFalse(record["alerted"])

    def test_inject_failure_retries_next_round(self) -> None:
        app = _app("app-1")
        probe = self._fake_probe([_fail()])
        with mock.patch.object(prober.store, "list_all", return_value=[app]):
            with mock.patch.object(prober, "inject_alerts", side_effect=RuntimeError("boom")):
                for i in range(3):
                    prober.run_round(self.state, probe_fn=probe, threshold=3, ts=1000.0 + i)
        self.assertFalse(self.state["app-1"]["alerted"])  # 注入失败已复位，下轮可重试

    def test_removed_app_resolves_and_state_pruned(self) -> None:
        app = _app("app-1")
        probe = self._fake_probe([_fail()])
        with mock.patch.object(prober.store, "list_all", return_value=[app]):
            for i in range(3):
                prober.run_round(self.state, probe_fn=probe, threshold=3, ts=1000.0 + i)
        self.assertEqual(len(self.injected), 1)
        # 条目从清单移除 → 下一轮注入 resolved 并清理状态
        with mock.patch.object(prober.store, "list_all", return_value=[]):
            prober.run_round(self.state, probe_fn=probe, threshold=3, ts=2000.0)
        self.assertEqual(len(self.injected), 2)
        self.assertEqual(self.injected[1][0]["labels"], self.injected[0][0]["labels"])
        self.assertNotIn("app-1", self.state)

    def test_probe_keyword_passed_through(self) -> None:
        """条目的页面关键字透传给探测函数（未配置为空串 → 纯连接级探测）。"""
        app = _app("app-1", probe_keyword='<div id="root"')
        probe = self._fake_probe([_ok()])
        with mock.patch.object(prober.store, "list_all", return_value=[app]):
            prober.run_round(self.state, probe_fn=probe, threshold=3, ts=1000.0)
        self.assertEqual(probe.calls, [(app["url"], 2.0, '<div id="root"', "")])

    def test_health_path_passed_through(self) -> None:
        """条目的健康路径透传给探测函数（Manifest 预填；未配置为空串 → 根路径）。"""
        app = _app("app-1", health_path="/health")
        probe = self._fake_probe([_ok()])
        with mock.patch.object(prober.store, "list_all", return_value=[app]):
            prober.run_round(self.state, probe_fn=probe, threshold=3, ts=1000.0)
        self.assertEqual(probe.calls, [(app["url"], 2.0, "", "/health")])


class DaemonLoopResilienceTest(unittest.TestCase):
    """守护循环：单轮异常（如存储层瞬断）不杀死进程，等待下一轮重试；--once 保留异常传播。"""

    def test_transient_error_does_not_kill_daemon_loop(self) -> None:
        """对齐 ship/surge 的守护语义：单轮故障 warning 留痕后继续下一轮。"""
        with (
            mock.patch.object(
                prober, "run_round", side_effect=[RuntimeError("db down"), KeyboardInterrupt()]
            ) as run_round,
            mock.patch.object(prober, "write_status"),
            mock.patch.object(prober.time, "sleep"),
            mock.patch.object(prober.logger, "warning") as warn,
            mock.patch.object(sys, "argv", ["app_prober.py"]),
        ):
            prober.main()  # 第 1 轮异常被兜底、第 2 轮 KeyboardInterrupt 正常收尾 → 不向外抛出
        self.assertEqual(run_round.call_count, 2)
        warn.assert_called_once()
        self.assertIn("db down", str(warn.call_args))

    def test_once_mode_propagates_error(self) -> None:
        """--once 调试模式保留异常传播（非零退出码可被脚本感知）。"""
        with (
            mock.patch.object(prober, "run_round", side_effect=RuntimeError("db down")),
            mock.patch.object(prober, "write_status"),
            mock.patch.object(prober.logger, "warning"),
            mock.patch.object(sys, "argv", ["app_prober.py", "--once"]),
        ):
            with self.assertRaises(RuntimeError):
                prober.main()


class WriteStatusAndReadTest(unittest.TestCase):
    """状态落盘（原子写）与 BFF 读取容错。"""

    def test_write_status_creates_dirs_and_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "probe_status.json"
            state = {"app-1": {"id": "app-1", "ok": False, "failures": 2}}
            prober.write_status(path, state)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("updated_at", data)
            self.assertEqual(data["apps"], state)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])  # 原子替换无残留

    def test_read_probe_status_tolerates_missing_and_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe_status.json"
            with mock.patch.object(store, "PROBE_STATUS_PATH", path):
                self.assertEqual(store.read_probe_status(), {})  # 文件缺失
                path.write_text("{bad json", encoding="utf-8")
                self.assertEqual(store.read_probe_status(), {})  # 损坏 JSON
                path.write_text("[1, 2]", encoding="utf-8")
                self.assertEqual(store.read_probe_status(), {})  # 非 dict
                path.write_text(json.dumps({"apps": {"app-1": {"ok": True}}}), encoding="utf-8")
                self.assertTrue(store.read_probe_status()["apps"]["app-1"]["ok"])


if __name__ == "__main__":
    unittest.main()
