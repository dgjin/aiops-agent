"""需求基线域单测（bff/routes/requirements）：应用选择 / 令牌指引 / 拉取透传 / 接线冒烟。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
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

# 被监控系统「继续评估」后的最新条目（updatedAt 变化 + 新结论）
_ENTRY_SYNCED = dict(
    _ENTRY, updatedAt="2026-10-09T02:00:00Z", assessment="继续评估：补充验收口径"
)

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


_ANALYSIS_RESULT = {
    "understanding": "需要在首页顶部新增需求反馈入口。",
    "plan": ["更新首页模板", "补充跳转测试"],
    "suspect_files": ["demo-app/http_server.py"],
    "acceptance": ["首页可见入口"],
    "risk": "低",
    "complexity": "低",
    "confidence": 0.88,
    "degraded": False,
}


class AnalysisRoutesTest(unittest.TestCase):
    """智能分析闭环路由：创建 / 列表 / 详情 / 反馈 / 批准（状态校验与工作流衔接）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path_patcher = mock.patch.object(
            routes.requirement_analyses,
            "DATA_PATH",
            Path(self._tmp.name) / "requirement_analyses.json",
        )
        path_patcher.start()
        self.addCleanup(path_patcher.stop)

        apps_patcher = mock.patch.object(routes.store, "list_all", return_value=list(_APPS))
        apps_patcher.start()
        self.addCleanup(apps_patcher.stop)

        thread_patcher = mock.patch.object(routes.requirement_analyses, "start_analysis_thread")
        self.thread = thread_patcher.start()
        self.addCleanup(thread_patcher.stop)

        ref_patcher = mock.patch.object(
            routes.requirement_analyses, "_code_references", return_value=[]
        )
        ref_patcher.start()
        self.addCleanup(ref_patcher.stop)

        audit_patcher = mock.patch.object(routes.audit, "write_audit")
        self.audit = audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

    def _request(self, user: str = "admin1"):
        request = mock.Mock()
        request.state.identity.user = user
        return request

    def _create(self) -> dict:
        body = routes.AnalysisCreateBody(app_id="app-b", entry=dict(_ENTRY))
        return asyncio.run(routes.api_requirement_analysis_create(self._request(), body))

    def _finish(self, analysis_id: str) -> None:
        entry = routes.requirement_analyses.get(analysis_id)
        routes.requirement_analyses.complete_version(
            entry, 1, dict(_ANALYSIS_RESULT), {"degraded": False}
        )
        routes.requirement_analyses.persist_entry(entry)

    # ---- 创建 / 列表 / 详情 ----

    def test_create_starts_analysis_and_audits(self) -> None:
        data = self._create()
        self.assertTrue(data["started"])
        self.assertEqual(data["analysis"]["id"], "ra-app-b-7")
        self.assertEqual(data["analysis"]["status"], "analyzing")
        self.thread.assert_called_once_with("ra-app-b-7", 1)
        self.assertEqual(self.audit.call_args.kwargs["action"], "requirement:analyze")

    def test_create_unknown_app_404(self) -> None:
        body = routes.AnalysisCreateBody(app_id="nope", entry=dict(_ENTRY))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_create(self._request(), body))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_missing_title_400(self) -> None:
        body = routes.AnalysisCreateBody(app_id="app-b", entry={"id": 7})
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_create(self._request(), body))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_existing_analyzed_returns_without_run(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        data = self._create()
        self.assertFalse(data["started"])
        self.assertEqual(data["analysis"]["status"], "analyzed")

    def test_create_while_analyzing_409(self) -> None:
        self._create()
        body = routes.AnalysisCreateBody(app_id="app-b", entry=dict(_ENTRY))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_create(self._request(), body))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_list_and_detail(self) -> None:
        self._create()
        listed = asyncio.run(routes.api_requirement_analysis_list(app_id="app-b"))
        self.assertEqual(len(listed["analyses"]), 1)
        self.assertEqual(listed["analyses"][0]["entry_id"], "7")
        self.assertNotIn("versions", listed["analyses"][0])
        detail = asyncio.run(routes.api_requirement_analysis_detail("ra-app-b-7"))
        self.assertIn("versions", detail["analysis"])

    def test_detail_unknown_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_detail("ra-nope-1"))
        self.assertEqual(ctx.exception.status_code, 404)

    # ---- 反馈再分析 ----

    def test_feedback_appends_version_and_restarts(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        body = routes.FeedbackBody(feedback="必须保持接口兼容")
        data = asyncio.run(
            routes.api_requirement_analysis_feedback(self._request(), "ra-app-b-7", body)
        )
        self.assertEqual(data["analysis"]["current_version"], 2)
        self.assertEqual(data["analysis"]["versions"][1]["feedback"], "必须保持接口兼容")
        self.assertEqual(self.thread.call_args_list[-1].args, ("ra-app-b-7", 2))
        self.assertEqual(self.audit.call_args.kwargs["action"], "requirement:feedback")

    def test_feedback_while_analyzing_409(self) -> None:
        self._create()
        body = routes.FeedbackBody(feedback="x")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(
                routes.api_requirement_analysis_feedback(self._request(), "ra-app-b-7", body)
            )
        self.assertEqual(ctx.exception.status_code, 409)

    # ---- 同步被监控系统最新内容（继续评估后的结论） ----

    def test_sync_updates_snapshot_and_restarts(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        body = routes.SyncBody(entry=dict(_ENTRY_SYNCED))
        data = asyncio.run(
            routes.api_requirement_analysis_sync(self._request(), "ra-app-b-7", body)
        )
        self.assertTrue(data["started"])
        self.assertEqual(data["analysis"]["current_version"], 2)
        self.assertEqual(data["analysis"]["versions"][1]["trigger"], "refresh")
        self.assertEqual(
            data["analysis"]["entry_snapshot"]["assessment"], "继续评估：补充验收口径"
        )
        self.assertEqual(self.thread.call_args_list[-1].args, ("ra-app-b-7", 2))
        self.assertEqual(self.audit.call_args.kwargs["action"], "requirement:sync")
        self.assertTrue(self.audit.call_args.kwargs["params"]["restarted"])

    def test_sync_no_change_409(self) -> None:
        body0 = routes.AnalysisCreateBody(app_id="app-b", entry=dict(_ENTRY_SYNCED))
        asyncio.run(routes.api_requirement_analysis_create(self._request(), body0))
        self._finish("ra-app-b-7")
        body = routes.SyncBody(entry=dict(_ENTRY_SYNCED))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(
                routes.api_requirement_analysis_sync(self._request(), "ra-app-b-7", body)
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_sync_while_analyzing_409(self) -> None:
        self._create()
        body = routes.SyncBody(entry=dict(_ENTRY_SYNCED))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(
                routes.api_requirement_analysis_sync(self._request(), "ra-app-b-7", body)
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_sync_unknown_404(self) -> None:
        body = routes.SyncBody(entry=dict(_ENTRY_SYNCED))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_sync(self._request(), "ra-nope-1", body))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_sync_approved_only_refreshes_snapshot(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        with mock.patch.object(
            routes.gw,
            "start_requirement_flow",
            new_callable=mock.AsyncMock,
            return_value="aiops-req-ra-app-b-7-v1",
        ):
            asyncio.run(routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7"))
        body = routes.SyncBody(entry=dict(_ENTRY_SYNCED))
        data = asyncio.run(
            routes.api_requirement_analysis_sync(self._request(), "ra-app-b-7", body)
        )
        self.assertFalse(data["started"])
        self.assertEqual(data["analysis"]["status"], "approved")
        self.assertEqual(data["analysis"]["current_version"], 1)  # 终态不追加版本
        self.assertEqual(
            data["analysis"]["entry_snapshot"]["assessment"], "继续评估：补充验收口径"
        )

    # ---- 批准进入修复工作流 ----

    def test_approve_starts_requirement_workflow(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        started = "aiops-req-ra-app-b-7-v1"
        with mock.patch.object(
            routes.gw,
            "start_requirement_flow",
            new_callable=mock.AsyncMock,
            return_value=started,
        ) as start:
            data = asyncio.run(
                routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7")
            )
        self.assertEqual(data["wf_id"], started)
        self.assertEqual(data["analysis"]["status"], "approved")
        task, wf_id = start.call_args.args
        self.assertEqual(wf_id, started)
        self.assertEqual(task.analysis_id, "ra-app-b-7")
        self.assertEqual(task.version, 1)
        self.assertEqual(task.approved_by, "admin1")
        self.assertIn("更新首页模板", task.plan)
        self.assertEqual(task.acceptance, ["首页可见入口"])
        self.assertEqual(self.audit.call_args.kwargs["action"], "requirement:approve")

    def test_approve_while_analyzing_409(self) -> None:
        self._create()
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_approve_unknown_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(routes.api_requirement_analysis_approve(self._request(), "ra-nope-1"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_approve_workflow_conflict_409(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        with mock.patch.object(
            routes.gw,
            "start_requirement_flow",
            new_callable=mock.AsyncMock,
            side_effect=ValueError("流程已存在（幂等拒绝）：x"),
        ):
            with self.assertRaises(ApiError) as ctx:
                asyncio.run(
                    routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7")
                )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_approve_twice_409(self) -> None:
        self._create()
        self._finish("ra-app-b-7")
        with mock.patch.object(
            routes.gw,
            "start_requirement_flow",
            new_callable=mock.AsyncMock,
            return_value="aiops-req-ra-app-b-7-v1",
        ):
            asyncio.run(routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7"))
            with self.assertRaises(ApiError) as ctx:
                asyncio.run(
                    routes.api_requirement_analysis_approve(self._request(), "ra-app-b-7")
                )
        self.assertEqual(ctx.exception.status_code, 409)


class AnalysisWiringTest(unittest.TestCase):
    """接线冒烟：新端点挂载 + 写接口权限（admin）与读接口（viewer）。"""

    def test_analysis_routes_defined(self) -> None:
        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/api/requirements/analyses", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/feedback", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/sync", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/approve", paths)

    def test_registered_in_app(self) -> None:
        from bff import app as bff_app

        paths = set(bff_app.app.openapi().get("paths", {}))
        self.assertIn("/api/requirements/analyses", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/feedback", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/sync", paths)
        self.assertIn("/api/requirements/analyses/{analysis_id}/approve", paths)

    def test_role_rules(self) -> None:
        from bff.middleware import required_role

        self.assertEqual(required_role("POST", "/api/requirements/analyses"), "admin")
        self.assertEqual(
            required_role("POST", "/api/requirements/analyses/ra-x-1/feedback"), "admin"
        )
        self.assertEqual(
            required_role("POST", "/api/requirements/analyses/ra-x-1/sync"), "admin"
        )
        self.assertEqual(
            required_role("POST", "/api/requirements/analyses/ra-x-1/approve"), "admin"
        )
        self.assertEqual(required_role("GET", "/api/requirements/analyses"), "viewer")
        self.assertEqual(required_role("GET", "/api/requirements/analyses/ra-x-1"), "viewer")


if __name__ == "__main__":
    unittest.main()
