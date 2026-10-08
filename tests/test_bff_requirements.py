"""需求基线域单测（bff/routes/requirements）：应用选择 / 令牌指引 / 拉取透传 / 接线冒烟。"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from aiops_agent import requirements_client

from bff.deps import ApiError
from bff.routes import requirements as routes

_APPS = [
    {"id": "app-a", "name": "应用A", "url": "http://a.test/", "service": "nl2sql", "enabled": False},
    {"id": "app-b", "name": "应用B", "url": "http://b.test/", "service": "svc-b", "enabled": True},
]

_ENTRY = {
    "id": 7,
    "kind": "REQUIREMENT",
    "title": "希望在首页顶端增加需求反馈入口链接",
    "status": "BASELINED",
    "priority": "P1",
    "baselineVersion": "v1.2",
}

_OK_RESULT = {
    "url": "http://b.test/api/requirements/export?status=BASELINED&limit=200",
    "ok": True,
    "error": "",
    "warnings": [],
    "spec_version": "1.0",
    "service": "svc-b",
    "system": "被监控系统",
    "exported_at": "2026-10-09T00:00:00+00:00",
    "filter": {"status": "BASELINED"},
    "returned": 1,
    "entries": [_ENTRY],
}


class RequirementsRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        apps_patcher = mock.patch.object(routes.store, "list_all", return_value=list(_APPS))
        self.list_all = apps_patcher.start()
        self.addCleanup(apps_patcher.stop)

        fetch_patcher = mock.patch.object(
            requirements_client, "fetch_requirements", return_value=dict(_OK_RESULT)
        )
        self.fetch = fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

        env_patcher = mock.patch.dict(os.environ, {requirements_client.TOKEN_ENV: "tok-x"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _call(self, **overrides):
        params = dict(app_id="", status="BASELINED", kind="", since="", limit=200)
        params.update(overrides)
        return asyncio.run(routes.api_requirements(**params))

    def test_default_picks_first_enabled_app(self) -> None:
        data = self._call()
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"]["id"], "app-b")
        self.fetch.assert_called_once()
        args, kwargs = self.fetch.call_args
        self.assertEqual(args[0], "http://b.test/")  # base_url 来自选中应用
        self.assertEqual(kwargs["token"], "tok-x")
        self.assertEqual(data["entries"], [_ENTRY])
        self.assertEqual(data["system"], "被监控系统")
        self.assertEqual(data["exported_at"], "2026-10-09T00:00:00+00:00")

    def test_explicit_app_id_respected_even_if_disabled(self) -> None:
        data = self._call(app_id="app-a")
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"]["id"], "app-a")
        self.assertEqual(self.fetch.call_args[0][0], "http://a.test/")

    def test_unknown_app_id_raises_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._call(app_id="nope")
        self.assertEqual(ctx.exception.status_code, 404)
        self.fetch.assert_not_called()

    def test_empty_manifest_guides_configuration(self) -> None:
        self.list_all.return_value = []
        data = self._call()
        self.assertFalse(data["ok"])
        self.assertIn("被监控应用", data["error"])
        self.assertEqual(data["apps"], [])
        self.fetch.assert_not_called()

    def test_missing_token_guides_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(requirements_client.TOKEN_ENV, None)
            data = self._call()
        self.assertFalse(data["ok"])
        self.assertIn(requirements_client.TOKEN_ENV, data["error"])
        self.assertEqual(data["app"]["id"], "app-b")  # 仍回带选中应用，前端可展示目标
        self.fetch.assert_not_called()

    def test_fetch_failure_passthrough(self) -> None:
        self.fetch.return_value = {
            "url": "http://b.test/api/requirements/export",
            "ok": False,
            "error": "无法访问需求基线条目接口：connection refused",
            "warnings": [],
            "spec_version": "",
            "service": "",
            "system": "",
            "exported_at": "",
            "filter": {},
            "returned": 0,
            "entries": [],
        }
        data = self._call()
        self.assertFalse(data["ok"])
        self.assertIn("无法访问", data["error"])
        self.assertEqual(data["entries"], [])

    def test_params_passed_through(self) -> None:
        self._call(status="ALL", kind="BUG", since="2026-10-01", limit=50)
        kwargs = self.fetch.call_args.kwargs
        self.assertEqual(kwargs["status"], "ALL")
        self.assertEqual(kwargs["kind"], "BUG")
        self.assertEqual(kwargs["since"], "2026-10-01")
        self.assertEqual(kwargs["limit"], 50)


class WiringTest(unittest.TestCase):
    """接线冒烟：路由挂载（防遗漏注册导致 404）。"""

    def test_route_registered(self) -> None:
        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/requirements", paths)

    def test_registered_in_app(self) -> None:
        from bff import app as bff_app

        # 注：FastAPI 0.142 起 app.routes 为惰性 _IncludedRouter 包装（无 path 属性），
        # 以 openapi schema 路径表为准——与客户端真实可见的路由一致。
        paths = set(bff_app.app.openapi().get("paths", {}))
        self.assertIn("/api/requirements", paths)


if __name__ == "__main__":
    unittest.main()
