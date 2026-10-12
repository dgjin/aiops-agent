"""服务依赖拓扑单测（aiops_agent/topology，P2-2 浅拓扑）。

覆盖：依赖/被依赖解析、探测健康 join、无关系返回 None、异常容错（可选信息不阻断 triage）。
数据源 mock：清单来自 app_registry.load_apps（demo 文件 / production 表同一函数），
探针状态来自 AIOPS_DATA_ROOT/probe_status.json（临时目录注入）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import app_registry, topology


def _app(app_id: str, service: str, depends_on: list[str] | None = None) -> dict:
    entry = {"id": app_id, "service": service}
    if depends_on is not None:
        entry["depends_on"] = depends_on
    return entry


class DependencyContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {"AIOPS_DATA_ROOT": str(self.data_root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_probe(self, apps: dict) -> None:
        (self.data_root / "probe_status.json").write_text(
            json.dumps({"apps": apps}), encoding="utf-8"
        )

    def _patch_apps(self, apps: list[dict]):
        patcher = mock.patch.object(app_registry, "load_apps", return_value=apps)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_depends_on_with_health_join(self) -> None:
        self._patch_apps(
            [
                _app("order-app", "order", ["coupon", "payment"]),
                _app("coupon-app", "coupon"),
                _app("payment-app", "payment"),
            ]
        )
        self._write_probe(
            {
                "coupon-app": {"ok": False, "failures": 62, "last_error": "connect timeout"},
                "payment-app": {"ok": True, "failures": 0},
            }
        )
        ctx = topology.dependency_context("order")
        self.assertEqual(ctx["service"], "order")
        self.assertEqual([d["service"] for d in ctx["depends_on"]], ["coupon", "payment"])
        coupon = ctx["depends_on"][0]
        self.assertEqual(coupon["probe"]["ok"], False)
        self.assertEqual(coupon["probe"]["failures"], 62)
        self.assertEqual(coupon["probe"]["last_error"], "connect timeout")
        self.assertEqual(ctx["depends_on"][1]["probe"]["ok"], True)
        self.assertEqual(ctx["unhealthy"], ["coupon"])  # 不健康依赖高亮（根因候选）

    def test_depended_by_reverse_lookup(self) -> None:
        self._patch_apps(
            [
                _app("coupon-app", "coupon"),
                _app("order-app", "order", ["coupon"]),
                _app("gateway-app", "gateway", ["coupon"]),
            ]
        )
        ctx = topology.dependency_context("coupon")
        self.assertEqual(ctx["depends_on"], [])
        self.assertEqual([d["service"] for d in ctx["depended_by"]], ["order", "gateway"])

    def test_no_relations_returns_none(self) -> None:
        self._patch_apps([_app("solo-app", "solo")])
        self.assertIsNone(topology.dependency_context("solo"))
        self.assertIsNone(topology.dependency_context("not-in-index"))

    def test_blank_service_returns_none(self) -> None:
        self.assertIsNone(topology.dependency_context("  "))
        self.assertIsNone(topology.dependency_context(""))

    def test_probe_file_missing_health_is_none(self) -> None:
        self._patch_apps(
            [
                _app("order-app", "order", ["coupon"]),
                _app("coupon-app", "coupon"),
            ]
        )
        ctx = topology.dependency_context("order")
        self.assertIsNone(ctx["depends_on"][0]["probe"])  # 无探针数据 → 省略健康信息
        self.assertEqual(ctx["unhealthy"], [])

    def test_corrupt_probe_file_tolerated(self) -> None:
        (self.data_root / "probe_status.json").write_text("{ not json", encoding="utf-8")
        self._patch_apps(
            [
                _app("order-app", "order", ["coupon"]),
                _app("coupon-app", "coupon"),
            ]
        )
        ctx = topology.dependency_context("order")
        self.assertIsNone(ctx["depends_on"][0]["probe"])

    def test_dependency_not_managed_has_no_probe(self) -> None:
        """依赖名未纳管（清单无此 service）→ 仍展示，但无健康信息。"""
        self._patch_apps([_app("order-app", "order", ["external-svc"])])
        ctx = topology.dependency_context("order")
        self.assertEqual(ctx["depends_on"], [{"service": "external-svc", "probe": None}])

    def test_registry_failure_returns_none(self) -> None:
        with mock.patch.object(app_registry, "load_apps", side_effect=RuntimeError("db down")):
            self.assertIsNone(topology.dependency_context("order"))

    def test_depends_on_entries_cleaned(self) -> None:
        """脏数据容错：非字符串/空名条目被忽略，service 名 trim。"""
        self._patch_apps(
            [
                {"id": "order-app", "service": "order", "depends_on": [" coupon ", "", 42, None]},
                _app("coupon-app", "coupon"),
            ]
        )
        ctx = topology.dependency_context("order")
        self.assertEqual([d["service"] for d in ctx["depends_on"]], ["coupon"])

    def test_depends_on_not_a_list_tolerated(self) -> None:
        self._patch_apps(
            [
                {"id": "order-app", "service": "order", "depends_on": "coupon"},
                _app("coupon-app", "coupon"),
            ]
        )
        # 非列表 → 视为无依赖；但 order 声明过什么不影响 coupon 的反查逻辑本身
        self.assertIsNone(topology.dependency_context("order"))


if __name__ == "__main__":
    unittest.main()
