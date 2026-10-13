"""BFF 层单元测试：前置校验 / 项归一化 / 聚合 / 操作审计（不依赖运行中的 Temporal）。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.responses import JSONResponse

from bff import aggregator, app, audit, config_items, middleware, temporal_gateway
from bff.deps import ApiError
from bff.routes import chatops as chatops_routes
from bff.routes import flows as flows_routes
from bff.routes import systems as systems_routes
from bff.routes.monitored_apps import _find_rollback_source
from bff.temporal_gateway import _item_from_result, _item_from_status, check_stage_allowed


class StageGuardTest(unittest.TestCase):
    def test_allowed(self):
        self.assertIsNone(check_stage_allowed("WAIT_APPROVAL", {"WAIT_APPROVAL"}))

    def test_blocked(self):
        message = check_stage_allowed("NOTIFYING", {"WAIT_APPROVAL"})
        self.assertIsNotNone(message)
        self.assertIn("NOTIFYING", message)
        self.assertIn("WAIT_APPROVAL", message)

    def test_unknown_stage(self):
        message = check_stage_allowed(None, {"NOTIFYING"})
        self.assertIn("未知", message)


class ItemNormalizeTest(unittest.TestCase):
    def test_from_status(self):
        base: dict = {}
        _item_from_status(
            base,
            {
                "stage": "WAIT_APPROVAL",
                "alert": {"alert_id": "a-1", "service": "order"},
                "deadline": {"kind": "approval", "at": "2026-10-01T14:00:00+00:00"},
                "needs_second": True,
                "approval": None,
                "second_approval": None,
                "deploy_command": None,
                "queued_patches": [{"workflow_id": "w-2", "alert_id": "a-2"}],
            },
        )
        self.assertEqual(base["stage"], "WAIT_APPROVAL")
        self.assertTrue(base["needs_second"])
        self.assertEqual(base["deadline"]["kind"], "approval")
        self.assertEqual(base["queued_patches"][0]["alert_id"], "a-2")

    def test_from_result(self):
        base: dict = {}
        _item_from_result(
            base,
            {
                "stage": "DONE",
                "alert_id": "a-1",
                "service": "order",
                "duration_seconds": 12.5,
                "confidence": 0.9,
                "model_version": "qwen3:8b",
                "patch_id": "p-1",
                "queued_patches": ["a-9"],
            },
        )
        self.assertEqual(
            base["alert"],
            {
                "alert_id": "a-1",
                "service": "order",
                "description": "",
                "severity": "critical",
            },
        )
        self.assertEqual(base["gate_events"], [])
        self.assertEqual(base["queued_patches"], [{"workflow_id": None, "alert_id": "a-9"}])
        self.assertEqual(base["confidence"], 0.9)
        self.assertEqual(base["model_version"], "qwen3:8b")

    def test_from_result_carries_alert_meta_and_gate_events(self):
        """P1-3：重试构造原告警所需的 description/severity 与升级原因所需的 gate_events。"""
        base: dict = {}
        _item_from_result(
            base,
            {
                "stage": "ESCALATED",
                "alert_id": "a-2",
                "service": "order",
                "alert": {"description": "订单错误率激增", "severity": "warning"},
                "gate_events": ["gate1:blocked:confidence-low", "gate2:timeout"],
                "root_cause": "下游超时",
            },
        )
        self.assertEqual(base["alert"]["description"], "订单错误率激增")
        self.assertEqual(base["alert"]["severity"], "warning")
        self.assertEqual(base["gate_events"][-1], "gate2:timeout")
        self.assertEqual(base["root_cause"], "下游超时")


class AuditTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(
                    actor="tester", action="approval:approve", wf_id="aiops-fix-order-a-1",
                    result="signaled",
                )
                audit.write_audit(
                    actor="tester", action="deploy-command:cancel", wf_id="aiops-fix-order-a-1",
                    result="signaled",
                )
                rows = audit.read_audit()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "deploy-command:cancel")  # 最近的在最前
        self.assertEqual(rows[0]["actor"], "tester")
        self.assertEqual(rows[0]["result"], "signaled")

    def test_read_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                self.assertEqual(audit.read_audit(), [])

    def test_write_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="tester", action="x", wf_id="w")
                self.assertTrue((Path(tmp) / "web-audit").is_dir())


class AggregatorTest(unittest.TestCase):
    def test_index_stats_shape(self):
        stats = aggregator.index_stats()
        if stats is None:
            self.skipTest("data/code_index.json 不存在")
        self.assertEqual(stats["dim"], 1024)
        self.assertGreater(stats["chunks"], 0)
        self.assertIn("backend", stats)

    def test_artifacts_for_patch_structure(self):
        result = aggregator.artifacts_for_patch("p-nonexistent-0")
        self.assertEqual(result["notify"], [])
        self.assertEqual(result["argocd"], [])
        self.assertIsNone(result["sandbox_dir"])

    def test_recent_releases(self):
        releases = aggregator.recent_releases(3)
        if not releases:
            self.skipTest("无 argocd 留痕")
        self.assertLessEqual(len(releases), 3)
        self.assertIn("version", releases[0])

    def test_probe_stable_down(self):
        result = aggregator.probe_stable("1", timeout=0.3)  # 端口 1 无服务
        self.assertFalse(result["running"])
        self.assertIn("error", result)


class StageFromExecStatusTest(unittest.TestCase):
    """B3：硬失败/取消的流程读不到 result，需按执行状态派生终态，避免显示为「未知」。"""

    def test_failed_and_timeout_map_to_failed(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("FAILED"), "FAILED")
        self.assertEqual(temporal_gateway.stage_from_exec_status("TIMED_OUT"), "FAILED")

    def test_cancelled_and_terminated_map_to_cancelled(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("CANCELED"), "CANCELLED")
        self.assertEqual(temporal_gateway.stage_from_exec_status("TERMINATED"), "CANCELLED")

    def test_completed_and_unknown_return_none(self):
        self.assertIsNone(temporal_gateway.stage_from_exec_status("COMPLETED"))
        self.assertIsNone(temporal_gateway.stage_from_exec_status("RUNNING"))
        self.assertIsNone(temporal_gateway.stage_from_exec_status(None))
        self.assertIsNone(temporal_gateway.stage_from_exec_status(""))

    def test_case_insensitive(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("failed"), "FAILED")


class ClassifyReleaseStatusTest(unittest.TestCase):
    """需求状态控制：流程终态 stage → 发布状态分类（已发布成功 = released）。"""

    def test_done_maps_to_released(self):
        self.assertEqual(temporal_gateway.classify_release_status("DONE"), "released")

    def test_escalated_and_cancelled(self):
        self.assertEqual(temporal_gateway.classify_release_status("ESCALATED"), "escalated")
        self.assertEqual(temporal_gateway.classify_release_status("CANCELLED"), "cancelled")

    def test_failed_maps_to_failed(self):
        self.assertEqual(temporal_gateway.classify_release_status("FAILED"), "failed")

    def test_missing_or_unknown_stays_unknown(self):
        self.assertEqual(temporal_gateway.classify_release_status(None), "unknown")
        self.assertEqual(temporal_gateway.classify_release_status(""), "unknown")
        self.assertEqual(temporal_gateway.classify_release_status("NOTIFYING"), "unknown")


class RequiredRoleTest(unittest.TestCase):
    """集中式角色策略：读接口 viewer，配置写入 admin，流程写入 operator。"""

    def test_get_endpoints_need_viewer(self):
        for path in ("/api/overview", "/api/flows", "/api/system", "/api/audit", "/api/health"):
            self.assertEqual(app.required_role("GET", path), "viewer", path)

    def test_monitored_apps_writes_need_admin(self):
        self.assertEqual(app.required_role("POST", "/api/monitored-apps"), "admin")
        self.assertEqual(app.required_role("PUT", "/api/monitored-apps/app-1"), "admin")
        self.assertEqual(app.required_role("DELETE", "/api/monitored-apps/app-1"), "admin")
        self.assertEqual(app.required_role("POST", "/api/monitored-apps/app-1/toggle"), "admin")

    def test_flow_writes_need_operator(self):
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/approval"), "operator")
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/deploy-command"), "operator")
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/queue-patch"), "operator")

    def test_unknown_write_defaults_to_admin(self):
        """未知写接口按最高要求（安全默认），避免新增端点漏配。"""
        self.assertEqual(app.required_role("POST", "/api/something-new"), "admin")

    def test_im_command_requires_viewer(self):
        """IM 通道（ChatOps）：通道级 viewer，命令级权限（发送者角色）在路由内判定。"""
        self.assertEqual(app.required_role("POST", "/api/im/command"), "viewer")


class ServiceTokenTest(unittest.TestCase):
    """服务令牌旁路：仅精确路径 + GET + 令牌匹配才生效（被监控系统只读集成通道）。"""

    _PATH = "/api/requirements/track"
    _ENV = "AIOPS_TRACK_SERVICE_TOKEN"

    def _request(self, path: str | None = None, method: str = "GET", token: str | None = None):
        from starlette.requests import Request

        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("utf-8")))
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path or self._PATH,
                "headers": headers,
                "query_string": b"",
            }
        )

    def test_disabled_when_env_missing(self):
        with mock.patch.dict(os.environ, {self._ENV: ""}):
            self.assertIsNone(middleware._service_identity(self._request(token="tok-svc")))

    def test_matches_only_exact_path_get_and_token(self):
        with mock.patch.dict(os.environ, {self._ENV: "tok-svc"}):
            identity = middleware._service_identity(self._request(token="tok-svc"))
            self.assertIsNotNone(identity)
            self.assertEqual(identity.role, "viewer")
            self.assertEqual(identity.level, 1)  # ROLE_ORDER["viewer"]
            # 错令牌 / 非 GET / 其他路径一律回落到常规鉴权
            self.assertIsNone(middleware._service_identity(self._request(token="bad")))
            self.assertIsNone(
                middleware._service_identity(self._request(method="POST", token="tok-svc"))
            )
            self.assertIsNone(
                middleware._service_identity(
                    self._request(path="/api/requirements", token="tok-svc")
                )
            )


class ImChannelTest(unittest.TestCase):
    """IM 通道专用认证（ChatOps）：启动前任何失败都不回落常规鉴权。"""

    _PATH = "/api/im/command"
    _ENV = "AIOPS_IM_TOKEN"

    def _request(self, path: str | None = None, method: str = "POST", token: str | None = None):
        from starlette.requests import Request

        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("utf-8")))
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path or self._PATH,
                "headers": headers,
                "query_string": b"",
            }
        )

    def test_non_im_path_returns_none(self):
        with mock.patch.dict(os.environ, {self._ENV: "tok-im", "AIOPS_MODE": "demo"}):
            self.assertIsNone(
                middleware._im_channel(self._request(path="/api/im/other", token="tok-im"), None)
            )

    def test_closed_when_env_missing(self):
        with mock.patch.dict(os.environ, {self._ENV: "", "AIOPS_MODE": "demo"}):
            with self.assertRaises(middleware.ImChannelError) as ctx:
                middleware._im_channel(self._request(token="tok-im"), None)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_closed_in_production(self):
        # 未接入可信网桥（签名/时效/防重放）前，production 通道整体关闭
        with mock.patch.dict(os.environ, {self._ENV: "tok-im", "AIOPS_MODE": "production"}):
            with self.assertRaises(middleware.ImChannelError) as ctx:
                middleware._im_channel(self._request(token="tok-im"), None)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_wrong_or_missing_token_is_auth_failure(self):
        with mock.patch.dict(os.environ, {self._ENV: "tok-im", "AIOPS_MODE": "demo"}):
            for request in (
                self._request(token="bad"),
                self._request(token=None),
                self._request(method="GET", token="tok-im"),
            ):
                with self.assertRaises(middleware.ImChannelError) as ctx:
                    middleware._im_channel(request, None)
                self.assertIn(ctx.exception.status_code, (401, 405))

    def test_token_reuse_rejected(self):
        identity = middleware.auth_store.Identity(user="alice", role="operator")
        with mock.patch.dict(os.environ, {self._ENV: "tok-im", "AIOPS_MODE": "demo"}):
            with self.assertRaises(middleware.ImChannelError) as ctx:
                middleware._im_channel(self._request(token="tok-im"), {"tok-im": (identity, {})})
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("控制台令牌复用", ctx.exception.message)
            # production 注册表存 sha256 指纹键，同样拦截
            fingerprint = hashlib.sha256(b"tok-im").hexdigest()
            with self.assertRaises(middleware.ImChannelError):
                middleware._im_channel(self._request(token="tok-im"), {fingerprint: (identity, {})})
        with mock.patch.dict(
            os.environ,
            {self._ENV: "tok-shared", "AIOPS_MODE": "demo", "AIOPS_TRACK_SERVICE_TOKEN": "tok-shared"},
        ):
            with self.assertRaises(middleware.ImChannelError) as ctx:
                middleware._im_channel(self._request(token="tok-shared"), None)
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("服务令牌复用", ctx.exception.message)

    def test_positive_demo_identity(self):
        with mock.patch.dict(os.environ, {self._ENV: "tok-im", "AIOPS_MODE": "demo"}):
            identity = middleware._im_channel(self._request(token="tok-im"), None)
        self.assertIsNotNone(identity)
        self.assertEqual(identity.user, "im:gateway")
        self.assertEqual(identity.role, "viewer")


class ClientIpTrustTest(unittest.TestCase):
    """限速/审计归因：仅受信代理的 X-Forwarded-For 生效，且取末段（最近一跳真实来源）。"""

    def _request(self, client, forwarded: str | None = None):
        from starlette.requests import Request

        headers = []
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode("utf-8")))
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/x",
                "headers": headers,
                "query_string": b"",
                "client": client,
            }
        )

    def test_untrusted_peer_ignores_forwarded(self):
        request = self._request(("192.168.1.9", 1234), forwarded="6.6.6.6")
        self.assertEqual(middleware._client_ip(request), "192.168.1.9")

    def test_trusted_proxy_uses_last_hop(self):
        # 客户端伪造值在前、Caddy 写入的真实来源在末段
        request = self._request(("127.0.0.1", 1234), forwarded="6.6.6.6, 10.0.0.8")
        self.assertEqual(middleware._client_ip(request), "10.0.0.8")
        single = self._request(("127.0.0.1", 1234), forwarded="10.0.0.8")
        self.assertEqual(middleware._client_ip(single), "10.0.0.8")

    def test_missing_client_returns_unknown(self):
        request = self._request(None)
        self.assertEqual(middleware._client_ip(request), "unknown")


class ImChannelGuardTest(unittest.IsolatedAsyncioTestCase):
    """真实 _auth_guard 链路：普通控制台凭证不能进入 IM 通道，也不会触发后续路由。"""

    _IM_TOKEN = "tok-im"

    def _request(self, token: str | None = None, path: str = "/api/im/command"):
        from starlette.requests import Request

        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("utf-8")))
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "headers": headers,
                "query_string": b"",
                "client": ("127.0.0.1", 40000),
                "server": ("127.0.0.1", 8600),
            }
        )

    @contextlib.contextmanager
    def _guard_mocks(self, entries=()):
        identity = middleware.auth_store.Identity(user="alice", role="operator")
        with mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_IM_TOKEN": self._IM_TOKEN,
                "AIOPS_TRACK_SERVICE_TOKEN": "",
            },
        ), mock.patch.object(
            middleware.auth_store, "registry_tokens",
            return_value={key: (identity, {}) for key in entries},
        ), mock.patch.object(middleware.auth_store, "resolve_session", return_value=None):
            yield

    async def test_viewer_static_token_rejected_before_route(self):
        seen = []

        async def call_next(request):
            seen.append(request.url.path)
            return JSONResponse(status_code=200, content={})

        request = self._request(token="viewer-tok")
        with self._guard_mocks(["viewer-tok"]):
            response = await middleware._auth_guard(request, call_next)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(seen, [])

    async def test_unconfigured_channel_closed_without_fallback(self):
        seen = []

        async def call_next(request):
            seen.append(True)
            return JSONResponse(status_code=200, content={})

        request = self._request(token="viewer-tok")
        with mock.patch.dict(
            os.environ, {"AIOPS_MODE": "demo", "AIOPS_IM_TOKEN": "", "AIOPS_TRACK_SERVICE_TOKEN": ""}
        ), mock.patch.object(middleware.auth_store, "registry_tokens", return_value={}), \
                mock.patch.object(middleware.auth_store, "resolve_session", return_value=None):
            response = await middleware._auth_guard(request, call_next)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(seen, [])

    async def test_production_channel_closed(self):
        async def call_next(request):
            raise AssertionError("路由不应被调用")

        request = self._request(token=self._IM_TOKEN)
        with mock.patch.dict(
            os.environ,
            {"AIOPS_MODE": "production", "AIOPS_IM_TOKEN": self._IM_TOKEN, "AIOPS_TRACK_SERVICE_TOKEN": ""},
        ), mock.patch.object(middleware.auth_store, "registry_tokens", return_value={}), \
                mock.patch.object(middleware.auth_store, "resolve_session", return_value=None):
            response = await middleware._auth_guard(request, call_next)
        self.assertEqual(response.status_code, 503)

    async def test_im_token_passes_with_im_source(self):
        captured = {}

        async def call_next(request):
            captured["record"] = dict(request.state.token_record)
            captured["identity"] = request.state.identity
            return JSONResponse(status_code=200, content={"ok": True})

        request = self._request(token=self._IM_TOKEN)
        with self._guard_mocks():
            response = await middleware._auth_guard(request, call_next)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["record"], {"source": "im"})
        self.assertEqual(captured["identity"].user, "im:gateway")

    async def test_guard_does_not_touch_other_endpoints(self):
        # 非 IM 路径：普通静态令牌正常通过（不因无 IM 配置受影响）
        captured = {}

        async def call_next(request):
            captured["record"] = dict(request.state.token_record)
            return JSONResponse(status_code=200, content={})

        request = self._request(token="viewer-tok", path="/api/auth/logout")
        with self._guard_mocks(["viewer-tok"]):
            response = await middleware._auth_guard(request, call_next)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["record"].get("source"), "static")


class AuditRecordTest(unittest.TestCase):
    """WP1：配置类审计写 target、wf_id 置空，result 不再一律 signaled。"""

    def _write(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                return audit.write_audit(**kwargs), audit.read_audit()

    def test_config_action_uses_target_and_empty_wf_id(self):
        record, rows = self._write(actor="li", action="monitored-app:update", target="app-1")
        self.assertEqual(record["target"], "app-1")
        self.assertEqual(record["wf_id"], "")
        self.assertEqual(record["result"], "ok")  # 配置操作不再是 signaled
        self.assertEqual(rows[0]["target"], "app-1")

    def test_workflow_action_keeps_wf_id_and_signaled(self):
        record, _ = self._write(
            actor="zhang", action="approval:approve", wf_id="aiops-fix-order-a1", result="signaled"
        )
        self.assertEqual(record["wf_id"], "aiops-fix-order-a1")
        self.assertIsNone(record["target"])
        self.assertEqual(record["result"], "signaled")


class ConfigItemsTest(unittest.TestCase):
    """WP4：配置项元数据——来源 / 生效方式 / 对运行中流程是否生效；敏感项脱敏。"""

    def _collect(self, **overrides):
        env = {
            "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps({"t1": {"user": "u", "role": "viewer"}}),
            **overrides,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return config_items.collect(
                policy_source="demo-policy.yaml", policy_summary="threshold=0.8", monitored_app_count=2
            )

    def test_item_shape(self):
        for item in self._collect():
            for key in (
                "key", "label", "value", "source", "effect", "owner", "applies_to_running", "note",
            ):
                self.assertIn(key, item)
            self.assertIn(item["effect"], {"hot", "restart", "snapshot"})

    def test_policy_is_snapshot_and_not_applied_to_running(self):
        item = next(i for i in self._collect() if i["key"] == "release-gate-policy.yaml")
        self.assertEqual(item["effect"], config_items.SNAPSHOT)
        self.assertFalse(item["applies_to_running"])
        self.assertIn("demo-policy.yaml", item["source"])

    def test_monitored_apps_is_hot(self):
        item = next(i for i in self._collect() if i["key"] == "data/monitored_apps.json")
        self.assertEqual(item["effect"], config_items.HOT)
        self.assertTrue(item["applies_to_running"])
        self.assertEqual(item["value"], "2 个应用")

    def test_secret_value_not_leaked(self):
        item = next(i for i in self._collect() if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS")
        self.assertEqual(item["effect"], config_items.HOT)
        self.assertIn("1 个令牌", item["value"])
        self.assertNotIn("t1", item["value"])  # 令牌值绝不回显

    def test_unset_secret_reports_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_CONSOLE_AUTH_TOKENS", None)
            item = next(
                i for i in config_items.collect() if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS"
            )
        self.assertIn("fail-closed", item["value"])

    def test_invalid_secret_json_reports_fail_closed(self):
        item = next(i for i in self._collect(AIOPS_CONSOLE_AUTH_TOKENS="{ not json") if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS")
        self.assertIn("fail-closed", item["value"])

    def test_worker_configs_require_restart(self):
        for key in ("AIOPS_FIX_PROVIDER", "AIOPS_QODER_MODEL", "AIOPS_SANDBOX_IMAGE"):
            item = next(i for i in self._collect() if i["key"] == key)
            self.assertEqual(item["effect"], config_items.RESTART, key)
            self.assertFalse(item["applies_to_running"], key)


class DependencyProbeTest(unittest.TestCase):
    """WP9：修复链路依赖探测（降级横幅数据源）。"""

    def test_http_reachable_ok(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch("bff.aggregator.urllib.request.urlopen", return_value=resp):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertTrue(ok)
        self.assertIn("200", detail)

    def test_http_error_still_counts_as_reachable(self):
        import urllib.error

        with mock.patch(
            "bff.aggregator.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://x/", 404, "nf", None, None),
        ):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertTrue(ok)
        self.assertIn("404", detail)

    def test_http_unreachable(self):
        with mock.patch(
            "bff.aggregator.urllib.request.urlopen", side_effect=ConnectionRefusedError("no")
        ):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertFalse(ok)
        self.assertIn("ConnectionRefusedError", detail)

    def test_probe_docker_ok(self):
        proc = mock.MagicMock(returncode=0, stdout="29.2.1\n", stderr="")
        with mock.patch("bff.aggregator.subprocess.run", return_value=proc):
            ok, detail = aggregator.probe_docker()
        self.assertTrue(ok)
        self.assertEqual(detail, "29.2.1")

    def test_probe_docker_missing_cli(self):
        with mock.patch("bff.aggregator.subprocess.run", side_effect=FileNotFoundError):
            ok, detail = aggregator.probe_docker()
        self.assertFalse(ok)
        self.assertIn("docker", detail)

    def test_probe_docker_daemon_down(self):
        proc = mock.MagicMock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon\n")
        with mock.patch("bff.aggregator.subprocess.run", return_value=proc):
            ok, detail = aggregator.probe_docker()
        self.assertFalse(ok)
        self.assertIn("daemon", detail)

    def test_probe_dependencies_shape(self):
        with mock.patch.object(aggregator, "_http_reachable", return_value=(True, "HTTP 200")), mock.patch.object(
            aggregator, "probe_docker", return_value=(True, "29.2.1")
        ):
            deps = aggregator.probe_dependencies()
        self.assertEqual(sorted(deps), ["docker", "loki", "ollama"])
        self.assertTrue(all(info["ok"] for info in deps.values()))


class RollbackSourceTest(unittest.TestCase):
    """WP6：回滚依据来自审计记录（target 匹配 + update/toggle 动作）。"""

    def test_finds_latest_update_for_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(
                    actor="li", action="monitored-app:create", target="app-1", params={"name": "A"}
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1",
                    params={"changed": {"url": {"from": "http://old/", "to": "http://new/"}}},
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-2",
                    params={"changed": {"url": {"from": "x", "to": "y"}}},
                )
                record = _find_rollback_source("app-1", None)
        self.assertIsNotNone(record)
        self.assertEqual(record["action"], "monitored-app:update")
        self.assertEqual(record["params"]["changed"]["url"]["from"], "http://old/")

    def test_returns_none_when_no_change_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="li", action="monitored-app:create", target="app-1")
                self.assertIsNone(_find_rollback_source("app-1", None))

    def test_exact_ts_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                first = audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1", params={"changed": {}}
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1", params={"changed": {}}
                )
                record = _find_rollback_source("app-1", first["ts"])
        self.assertEqual(record["ts"], first["ts"])


class MonitoredAppProbeTest(unittest.TestCase):
    """被监控应用探测（AIOPS_MONITOR_URL，默认 http://localhost:3000/）。"""

    def test_default_url(self):
        self.assertEqual(aggregator.DEFAULT_MONITOR_URL, "http://localhost:3000/")

    def test_env_overrides_default(self):
        with mock.patch.dict("os.environ", {"AIOPS_MONITOR_URL": "http://example.test:8080/"}), \
             mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<html><body><div id="root"></div></body></html>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app()
        self.assertEqual(result["target"], "http://example.test:8080/")
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("latency_ms", result)

    def test_explicit_url_wins_over_env(self):
        with mock.patch.dict("os.environ", {"AIOPS_MONITOR_URL": "http://env.test/"}), \
             mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 204
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://explicit.test/")
        self.assertEqual(result["target"], "http://explicit.test/")
        self.assertEqual(result["status_code"], 204)

    def test_http_error_still_means_online(self):
        """根路径返回 4xx/5xx 说明端口有服务在响应 → 视为在线（不解析 JSON）。"""
        import urllib.error

        with mock.patch("bff.aggregator.urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            url="http://x/", code=404, msg="Not Found", hdrs=None, fp=None)):
            result = aggregator.probe_monitored_app("http://x/")
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 404)
        self.assertIn("note", result)

    def test_keyword_match_means_online(self):
        """配置页面关键字且响应内容包含 → 在线（正常页面）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<div id="root"></div>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", keyword='<div id="root"')
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 200)

    def test_keyword_missing_marks_down(self):
        """HTTP 200 但内容缺少关键字（白屏类故障）→ 判失败，交由巡检计数/告警。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<div id="rroot"></div>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", keyword='<div id="root"')
        self.assertFalse(result["running"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("关键字", result["error"])

    def test_no_keyword_blank_page_marks_down(self):
        """未配置关键字：HTTP 200 但页面空壳（默认通用判据）→ 判失败（白屏）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b"<html><body></body></html>"
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/")
        self.assertFalse(result["running"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("白屏", result["error"])

    def test_no_keyword_normal_page_online(self):
        """未配置关键字：完整 HTML 页面（含可渲染容器）→ 在线。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<html><body><div id="root"></div></body></html>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/")
        self.assertTrue(result["running"])

    def test_no_keyword_json_health_online(self):
        """未配置关键字：JSON 健康端点（非 HTML）→ 非空即在线。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"status": "ok"}'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", health_path="/health")
        self.assertTrue(result["running"])

    def test_no_keyword_script_only_page_marks_down(self):
        """未配置关键字：body 只剩入口脚本（挂载点缺失/被删）→ 判失败。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = (
                b'<html><body><script type="module" src="/src/main.tsx"></script></body></html>'
            )
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/")
        self.assertFalse(result["running"])
        self.assertIn("挂载点缺失", result["error"])

    def test_health_path_appended_to_target(self):
        """Manifest 预填的健康路径拼接到探测地址（关键字在健康页上校验）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"status": "ok"}'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app(
                "http://x:8000/", keyword='"status": "ok"', health_path="/health"
            )
        self.assertTrue(result["running"])
        self.assertEqual(result["target"], "http://x:8000/health")
        self.assertEqual(urlopen.call_args.args[0], "http://x:8000/health")

    def test_health_path_root_keeps_original_target(self):
        """health_path 为空或 "/" 时探测根地址（旧语义不变）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<html><body><div id="root"></div></body></html>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", health_path="/")
        self.assertEqual(result["target"], "http://x/")

    def test_unreachable_reports_error(self):
        with mock.patch("bff.aggregator.urllib.request.urlopen",
                        side_effect=ConnectionRefusedError("connection refused")):
            result = aggregator.probe_monitored_app("http://down.test/", timeout=0.2)
        self.assertFalse(result["running"])
        self.assertIn("ConnectionRefusedError", result["error"])

    def test_default_scheme_when_unset(self):
        """未设 AIOPS_MONITOR_URL 时回落默认地址。"""
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AIOPS_MONITOR_URL", None)
            with mock.patch("bff.aggregator.urllib.request.urlopen",
                            side_effect=ConnectionRefusedError("nope")):
                result = aggregator.probe_monitored_app(timeout=0.2)
        self.assertEqual(result["target"], aggregator.DEFAULT_MONITOR_URL)


# ----------------------------------------------------------------------
# P3（UI 重构 Phase 2）：系统聚合端点 / 未纳管检测 / 时间线聚合
# ----------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


def _iso_at(minutes: float) -> str:
    return (_NOW + timedelta(minutes=minutes)).isoformat()


# 运行中（待审批）：无 gate_events，事件由状态快照推导，尾部为「当前进展」
_RUNNING_ITEM: dict = {
    "wf_id": "aiops-fix-order-a-101",
    "run_id": "run-101",
    "exec_status": "RUNNING",
    "start_time": _iso_at(-10),
    "close_time": None,
    "stage": "WAIT_APPROVAL",
    "alert": {
        "alert_id": "alert-101",
        "service": "order",
        "description": "订单错误率激增",
        "severity": "critical",
    },
    "needs_second": False,
    "patch_id": "patch-101",
    "approval": None,
    "second_approval": None,
    "deploy_command": None,
    "queued_patches": [],
    "duration_seconds": None,
    "root_cause": {
        "error_type": "timeout",
        "suspect_files": ["order_service.py"],
        "confidence": 0.9,
        "summary": "下游优惠券服务超时导致订单积压",
    },
    "patch": {
        "patch_id": "patch-101",
        "files": ["order_service.py"],
        "diff": "--- a/order_service.py\n+++ b/order_service.py\n",
        "description": "增加超时重试",
        "risk": "low",
        "model_version": "qwen3:8b",
        "confidence": 0.9,
    },
    "test_report": {
        "patch_id": "patch-101",
        "passed": True,
        "unit_tests": "OK",
        "regression_tests": "OK",
        "sast": "OK",
        "details": "",
    },
    "gate_events": [],
    "stage_error": None,
}

# 终态（已完成）：gate_events 链（5 条）为权威事件源，翻译后尾部补终态事件
_TERMINAL_ITEM: dict = {
    "wf_id": "aiops-fix-order-a-102",
    "run_id": "run-102",
    "exec_status": "COMPLETED",
    "start_time": _iso_at(-30),
    "close_time": _iso_at(-5),
    "stage": "DONE",
    "alert": {
        "alert_id": "alert-102",
        "service": "order",
        "description": "",
        "severity": "critical",
    },
    "needs_second": False,
    "patch_id": "patch-102",
    "approval": "approve",
    "second_approval": None,
    "deploy_command": "deploy_now",
    "queued_patches": [],
    "duration_seconds": 1500.0,
    "confidence": 0.92,
    "model_version": "qwen3:8b",
    "gate_events": [
        "tests:passed:attempt=0",
        "mr:created:mr-102",
        "gate2:approved",
        "gate3:announced:v1.2.0",
        "gate3:command:deploy_now",
    ],
    "stage_error": None,
}

# 硬失败：读不到 result（无 gate_events / close_time），stage_error 透出读取异常
_HARD_FAILED_ITEM: dict = {
    "wf_id": "aiops-fix-ghost-a-103",
    "run_id": "run-103",
    "exec_status": "FAILED",
    "start_time": _iso_at(-60),
    "close_time": None,
    "stage": "FAILED",
    "alert": {
        "alert_id": "alert-103",
        "service": "ghost-svc",
        "description": "探活连续失败",
        "severity": "critical",
    },
    "needs_second": False,
    "patch_id": None,
    "approval": None,
    "second_approval": None,
    "deploy_command": None,
    "queued_patches": [],
    "duration_seconds": None,
    "gate_events": [],
    "stage_error": "无法读取工作流结果：RPC timeout",
}


class GateEventTranslateTest(unittest.TestCase):
    """P3⑨：gate_events 机器串 → 时间线中文文案（未知格式降级原文，不丢信息）。"""

    def test_exact_formats(self):
        cases = {
            "gate2:approved": ("闸门2（人工审批）：已批准", "ok"),
            "gate2:reject": ("闸门2（人工审批）：已拒绝", "danger"),
            "gate2:timeout->reject": ("闸门2（人工审批）：超时自动拒绝", "danger"),
            "gate2b:approved:protected-dir": ("闸门2b（二级审批 · 受保护目录）：已批准", "ok"),
            "gate2b:reject": ("闸门2b（二级审批 · 受保护目录）：已拒绝", "danger"),
            "gate2b:timeout->reject": ("闸门2b（二级审批 · 受保护目录）：超时自动拒绝", "danger"),
            "gate3:cancelled": ("发布窗口已取消（用户指令）", "idle"),
            "shadow:suggestion-only": ("影子模式：仅出建议，不执行变更", "idle"),
            "canary:degraded->rollback": ("金丝雀劣化 → 已自动回滚，转人工", "danger"),
            "canary:healthy->full-rollout": ("金丝雀健康 → 全量发布完成", "ok"),
        }
        for event, expected in cases.items():
            got = systems_routes._translate_gate_event(event)
            self.assertEqual((got["text"], got["tone"]), expected, event)

    def test_gate1_blocked_keeps_detail(self):
        got = systems_routes._translate_gate_event("gate1:blocked:confidence=0.42<0.8")
        self.assertEqual(got["tone"], "danger")
        self.assertIn("闸门1", got["text"])
        self.assertIn("confidence=0.42<0.8", got["text"])
        bare = systems_routes._translate_gate_event("gate1:blocked")
        self.assertIn("低于阈值", bare["text"])

    def test_tests_rounds_are_one_based(self):
        """workflows.py 的 attempt 为 0 基（初次=0），时间线展示统一 1 基，不出现「第 0 轮」。"""
        self.assertEqual(
            systems_routes._translate_gate_event("tests:passed:attempt=0")["text"],
            "沙箱测试通过（第 1 轮）",
        )
        passed = systems_routes._translate_gate_event("tests:passed:attempt=2")
        self.assertEqual(passed["tone"], "ok")
        self.assertIn("第 3 轮", passed["text"])
        failed = systems_routes._translate_gate_event("tests:failed:attempt=1:unit test error: boom")
        self.assertEqual(failed["tone"], "danger")
        self.assertIn("第 2 轮", failed["text"])
        self.assertIn("boom", failed["text"])
        no_attempt = systems_routes._translate_gate_event("tests:passed")
        self.assertEqual(no_attempt["text"], "沙箱测试通过")  # 无轮次信息不臆造

    def test_requirement_approved_event(self):
        got = systems_routes._translate_gate_event(
            "requirement:approved:ra-app-d42214-26:v1:by=admin"
        )
        self.assertEqual(got["tone"], "ok")
        self.assertIn("v1", got["text"])
        self.assertIn("审批人 admin", got["text"])
        self.assertIn("ra-app-d42214-26", got["text"])
        bare = systems_routes._translate_gate_event("requirement:approved:ra-1")
        self.assertEqual(bare["text"], "需求批准启动修复：ra-1")

    def test_mr_and_gate3_dynamic_segments(self):
        self.assertEqual(
            systems_routes._translate_gate_event("mr:created:mr-102")["text"], "变更单已创建：mr-102"
        )
        announced = systems_routes._translate_gate_event("gate3:announced:v1.2.0")
        self.assertIn("已公告 v1.2.0", announced["text"])
        self.assertEqual(announced["tone"], "warn")
        self.assertEqual(
            systems_routes._translate_gate_event("gate3:command:deploy_now")["text"],
            "发布指令：立即发布",
        )
        self.assertEqual(systems_routes._translate_gate_event("gate3:command:cancel")["tone"], "warn")
        self.assertEqual(
            systems_routes._translate_gate_event("gate3:patch-queued:alert-9")["text"],
            "窗口内新补丁入队：alert-9",
        )

    def test_unknown_format_falls_back_to_raw(self):
        got = systems_routes._translate_gate_event("future:unknown-thing")
        self.assertEqual(got["text"], "future:unknown-thing")
        self.assertEqual(got["tone"], "muted")


class UnmanagedServicesTest(unittest.TestCase):
    """P3⑧：清单外服务检测（按 service join；停用/空值语义）。"""

    def test_detects_unlisted_running_first(self):
        apps = [
            {"service": "order", "enabled": True},
            {"service": "legacy-off", "enabled": False},  # 停用仍在清单 → 视为已纳管
        ]
        items = [
            {"alert": {"service": "order"}, "exec_status": "RUNNING", "start_time": "2026-10-12T10:00:00+00:00", "wf_id": "wf-o1"},
            {"alert": {"service": "legacy-off"}, "exec_status": "RUNNING", "start_time": "2026-10-12T10:00:00+00:00", "wf_id": "wf-o2"},
            {"alert": {"service": ""}, "exec_status": "RUNNING", "start_time": "2026-10-12T10:00:00+00:00", "wf_id": "wf-o3"},
            {"alert": None, "exec_status": "RUNNING", "start_time": "2026-10-12T10:00:00+00:00", "wf_id": "wf-o4"},
            {"alert": {"service": "ghost-svc"}, "exec_status": "RUNNING", "start_time": "2026-10-12T10:00:00+00:00", "wf_id": "wf-g1"},
            {"alert": {"service": "ghost-svc"}, "exec_status": "COMPLETED", "start_time": "2026-10-12T09:00:00+00:00", "wf_id": "wf-g0"},
            {"alert": {"service": "ghost-2"}, "exec_status": "COMPLETED", "start_time": "2026-10-12T11:00:00+00:00", "wf_id": "wf-g2"},
        ]
        out = systems_routes._unmanaged_services(items, apps)
        self.assertEqual([e["service"] for e in out], ["ghost-svc", "ghost-2"])  # 运行中优先
        ghost = out[0]
        self.assertEqual(ghost["flows"], 2)
        self.assertEqual(ghost["running"], 1)
        self.assertEqual(ghost["last_start"], "2026-10-12T10:00:00+00:00")
        self.assertEqual(ghost["last_wf_id"], "wf-g1")
        self.assertEqual(out[1]["running"], 0)

    def test_empty_inputs(self):
        self.assertEqual(systems_routes._unmanaged_services([], []), [])


class TimelineAggregationTest(unittest.TestCase):
    """P3⑨：单流程事件流与多流程分组（运行中优先 / 时间倒序 / 上限截断）。"""

    def test_running_item_derives_events_from_snapshot(self):
        events = systems_routes._timeline_events(_RUNNING_ITEM)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4, 5])
        self.assertIn("告警接收：alert-101", events[0]["text"])
        self.assertEqual(events[0]["at"], _RUNNING_ITEM["start_time"])
        self.assertIn("根因分析（置信度 0.90）", events[1]["text"])
        self.assertEqual(events[2]["text"], "补丁已生成：1 个文件")
        self.assertEqual(events[3]["text"], "沙箱测试通过")
        self.assertIn("等待人工审批", events[4]["text"])  # 尾部当前进展
        self.assertEqual(events[4]["tone"], "warn")
        self.assertNotIn("流程终态", events[-1]["text"])  # 运行中无终态

    def test_terminal_item_translates_gate_events(self):
        events = systems_routes._timeline_events(_TERMINAL_ITEM)
        self.assertEqual(len(events), 7)  # 告警 + 5 条翻译 + 终态
        self.assertEqual([e["seq"] for e in events], list(range(1, 8)))
        texts = [e["text"] for e in events]
        self.assertIn("沙箱测试通过（第 1 轮）", texts)
        self.assertIn("变更单已创建：mr-102", texts)
        self.assertIn("闸门2（人工审批）：已批准", texts)
        self.assertIn("发布指令：立即发布", texts)
        self.assertEqual(texts[-1], "流程终态：已完成（金丝雀健康 → 全量发布成功）")
        self.assertEqual(events[-1]["tone"], "ok")
        self.assertEqual(events[-1]["at"], _TERMINAL_ITEM["close_time"])

    def test_hard_failure_uses_stage_error_and_terminal(self):
        events = systems_routes._timeline_events(_HARD_FAILED_ITEM)
        self.assertEqual(len(events), 3)
        self.assertIn("告警接收：alert-103", events[0]["text"])
        self.assertEqual(events[1]["text"], "数据读取异常：无法读取工作流结果：RPC timeout")
        self.assertEqual(events[1]["tone"], "danger")
        self.assertEqual(events[2]["text"], "流程终态：硬失败（执行异常或超时，无终态审计）")
        self.assertEqual(events[2]["tone"], "danger")
        self.assertIsNone(events[2]["at"])  # close_time 缺失：不臆造时间

    def test_escalated_with_gate1_blocked(self):
        item = {
            "exec_status": "COMPLETED",
            "stage": "ESCALATED",
            "start_time": "2026-10-12T08:00:00+00:00",
            "close_time": "2026-10-12T08:01:00+00:00",
            "alert": {"alert_id": "alert-9", "service": "order", "description": ""},
            "gate_events": ["gate1:blocked:confidence=0.42<0.8"],
            "stage_error": None,
        }
        events = systems_routes._timeline_events(item)
        self.assertEqual(len(events), 3)
        self.assertIn("confidence=0.42<0.8", events[1]["text"])
        self.assertEqual(events[2]["text"], "流程终态：已转人工（等待运维处置后可重试修复）")

    def test_wait_approval_with_decision_uses_derived_approval(self):
        item = {
            "exec_status": "RUNNING",
            "stage": "WAIT_APPROVAL",
            "alert": None,
            "approval": "approve",
            "needs_second": True,
            "second_approval": "reject",
            "gate_events": [],
        }
        events = systems_routes._timeline_events(item)
        texts = [e["text"] for e in events]
        self.assertIn("一级审批：已批准", texts)
        self.assertIn("二级审批（受保护目录）：已拒绝", texts)
        # 已有审批结论 → 不再输出「当前：等待人工审批」尾事件
        self.assertNotIn("当前：等待人工审批（倒计时结束将自动拒绝并转人工）", texts)

    def test_notifying_meta_event(self):
        item = {"exec_status": "RUNNING", "stage": "NOTIFYING", "alert": None, "gate_events": []}
        events = systems_routes._timeline_events(item)
        self.assertEqual(len(events), 1)
        self.assertIn("公告倒计时", events[0]["text"])

    def test_degraded_patch_marked_warn(self):
        item = {
            "exec_status": "RUNNING",
            "stage": "FIXING",
            "alert": None,
            "patch": {"degraded": True, "files": ["a.py", "b.py"]},
            "gate_events": [],
        }
        events = systems_routes._timeline_events(item)
        self.assertEqual(events[0]["text"], "补丁（降级兜底产物）已生成：2 个文件")
        self.assertEqual(events[0]["tone"], "warn")

    def test_groups_running_first_then_closed_by_time(self):
        def mk(wf_id: str, status: str, start: str, close: str | None = None) -> dict:
            return {
                "wf_id": wf_id,
                "exec_status": status,
                "start_time": start,
                "close_time": close,
                "stage": "FIXING" if status == "RUNNING" else "DONE",
                "alert": None,
                "gate_events": ["gate2:approved"],
            }

        items = [
            mk("r1", "RUNNING", "2026-10-12T10:00:00+00:00"),
            mk("r2", "RUNNING", "2026-10-12T12:00:00+00:00"),
            mk("r3", "RUNNING", "2026-10-12T11:00:00+00:00"),
            mk("c1", "COMPLETED", "2026-10-12T07:00:00+00:00", "2026-10-12T13:00:00+00:00"),
            mk("c2", "COMPLETED", "2026-10-12T06:00:00+00:00", "2026-10-12T14:00:00+00:00"),
        ]
        groups = systems_routes.build_timeline_groups(items, max_flows=4)
        self.assertEqual([g["wf_id"] for g in groups], ["r2", "r3", "r1", "c2"])
        self.assertEqual([g["current"] for g in groups], [True, True, True, False])

    def test_groups_empty_input(self):
        self.assertEqual(systems_routes.build_timeline_groups([]), [])


class SystemsEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """P3⑦⑧：聚合端点装配（Temporal/探测/escalations 打桩，不依赖运行中服务与写盘）。"""

    async def test_systems_overview_merges_payload_apps_and_unmanaged(self):
        apps = [{"id": "app-1", "name": "订单服务", "service": "order", "enabled": True}]
        items = [dict(_RUNNING_ITEM), dict(_TERMINAL_ITEM), dict(_HARD_FAILED_ITEM)]
        with mock.patch.object(systems_routes.gw, "list_flows", mock.AsyncMock(return_value=items)), \
             mock.patch.object(systems_routes, "apps_with_probe", mock.AsyncMock(return_value=apps)), \
             mock.patch.object(flows_routes, "_open_escalations", return_value=2):
            payload = await systems_routes.api_systems_overview()
        self.assertIn("server_time", payload)
        self.assertEqual(payload["counts"]["running"], 1)
        self.assertEqual(payload["counts"]["wait_approval"], 1)
        self.assertEqual(payload["counts"]["escalations_open"], 2)  # 打桩口径透传
        self.assertEqual(payload["terminal_dist_7d"]["DONE"], 1)
        self.assertEqual(payload["apps"], apps)
        self.assertEqual(
            payload["unmanaged"],
            [
                {
                    "service": "ghost-svc",
                    "flows": 1,
                    "running": 0,
                    "last_start": _HARD_FAILED_ITEM["start_time"],
                    "last_wf_id": _HARD_FAILED_ITEM["wf_id"],
                }
            ],
        )

    async def test_systems_timeline_filters_by_service_and_orders_groups(self):
        app = {"id": "app-1", "name": "订单服务", "service": "order", "enabled": True}
        other = {
            "wf_id": "aiops-fix-coupon-a-200",
            "exec_status": "COMPLETED",
            "stage": "DONE",
            "start_time": "2026-10-12T07:00:00+00:00",
            "close_time": "2026-10-12T07:10:00+00:00",
            "alert": {"alert_id": "alert-200", "service": "coupon", "description": ""},
            "gate_events": [],
            "stage_error": None,
        }
        items = [dict(_RUNNING_ITEM), dict(other), dict(_TERMINAL_ITEM)]
        with mock.patch.object(systems_routes.store, "get", return_value=app), \
             mock.patch.object(systems_routes.gw, "list_flows", mock.AsyncMock(return_value=items)):
            payload = await systems_routes.api_system_timeline("app-1", flows=5)
        self.assertEqual(payload["app_id"], "app-1")
        self.assertEqual(payload["service"], "order")
        groups = payload["groups"]
        self.assertEqual([g["wf_id"] for g in groups], [_RUNNING_ITEM["wf_id"], _TERMINAL_ITEM["wf_id"]])
        self.assertTrue(groups[0]["current"])
        self.assertFalse(groups[1]["current"])

    async def test_systems_timeline_respects_flows_cap(self):
        app = {"id": "app-1", "service": "order"}
        items = [dict(_RUNNING_ITEM), dict(_TERMINAL_ITEM)]
        with mock.patch.object(systems_routes.store, "get", return_value=app), \
             mock.patch.object(systems_routes.gw, "list_flows", mock.AsyncMock(return_value=items)):
            payload = await systems_routes.api_system_timeline("app-1", flows=1)
        self.assertEqual(len(payload["groups"]), 1)

    async def test_systems_timeline_404_for_unknown_app(self):
        with mock.patch.object(systems_routes.store, "get", return_value=None):
            with self.assertRaises(ApiError) as ctx:
                await systems_routes.api_system_timeline("missing-app", flows=5)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("missing-app", ctx.exception.message)


# ----------------------------------------------------------------------
# P4⑫（ChatOps）：IM 指令端点 / 审批附加校验双通道共用
# ----------------------------------------------------------------------


class ApprovalExtraTest(unittest.TestCase):
    """P4⑫：审批附加校验提取为模块级函数（Web 控制台与 ChatOps 双通道共用防漂移）。"""

    def test_first_approval_extra(self):
        self.assertIsNone(flows_routes._first_approval_extra({"approval": None}))
        message = flows_routes._first_approval_extra({"approval": "approve"})
        self.assertIn("不能重复提交", message)

    def test_second_approval_extra(self):
        self.assertIn("无需二级审批", flows_routes._second_approval_extra({"needs_second": False}))
        self.assertIn(
            "一级审批尚未批准",
            flows_routes._second_approval_extra({"needs_second": True, "approval": None}),
        )
        self.assertIn(
            "不能重复提交",
            flows_routes._second_approval_extra(
                {"needs_second": True, "approval": "approve", "second_approval": "reject"}
            ),
        )
        self.assertIsNone(
            flows_routes._second_approval_extra(
                {"needs_second": True, "approval": "approve", "second_approval": None}
            )
        )


class ImCommandEndpointTest(unittest.IsolatedAsyncioTestCase):
    """P4⑫ ChatOps：IM 指令端点（Temporal / kill switch / 审计落盘均打桩或隔离）。

    请求均携带 middleware 注入的 IM 通道来源（``token_record.source == "im"``）；
    来源缺失或来自普通凭证时路由直接 403（中间件级拒绝由 ImChannelGuardTest 覆盖）。
    """

    _WF_ID = "aiops-fix-order-a-201"

    def _im_request(self, source: str | None = "im"):
        """构造路由级 Request：默认模拟真 IM 通道注入（或指定/缺失来源用于负例）。"""
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/im/command",
                "headers": [],
                "query_string": b"",
            }
        )
        if source is not None:
            request.state.token_record = {"source": source}
        return request
    _WAIT_ITEM: dict = {
        "wf_id": _WF_ID,
        "exec_status": "RUNNING",
        "stage": "WAIT_APPROVAL",
        "alert": {"service": "order", "alert_id": "a-201"},
        "patch_id": "patch-201",
        "needs_second": False,
        "approval": None,
        "second_approval": None,
        "root_cause": None,
        "deadline": {"kind": "approval", "at": "2026-10-12T10:10:00+00:00"},
        "start_time": "2026-10-12T10:00:00+00:00",
    }

    def _status(self, **overrides) -> dict:
        base = {
            "stage": "WAIT_APPROVAL",
            "approval": None,
            "needs_second": False,
            "second_approval": None,
        }
        base.update(overrides)
        return base

    def _detail(self, **overrides) -> dict:
        base = {
            "wf_id": self._WF_ID,
            "exec_status": "RUNNING",
            "status": self._status(),
            "errors": [],
        }
        base.update(overrides)
        return base

    async def test_help_reply(self):
        payload = await chatops_routes.api_im_command(self._im_request(), {"sender": "alice", "text": "帮助"})
        self.assertTrue(payload["ok"])
        self.assertIn("可用命令", payload["reply"])
        self.assertIn("server_time", payload)

    async def test_pending_lists_only_wait_approval(self):
        items = [
            dict(self._WAIT_ITEM),
            {"wf_id": "wf-done", "exec_status": "COMPLETED", "stage": "DONE"},
        ]
        with mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=items)):
            payload = await chatops_routes.api_im_command(self._im_request(), {"sender": "alice", "text": "待办"})
        self.assertTrue(payload["ok"])
        self.assertIn(self._WF_ID, payload["reply"])
        self.assertNotIn("wf-done", payload["reply"])

    async def test_status_unique_prefix(self):
        items = [dict(self._WAIT_ITEM)]
        with mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=items)):
            payload = await chatops_routes.api_im_command(self._im_request(),
                {"sender": "alice", "text": "状态 aiops-fix-order"}
            )
        self.assertTrue(payload["ok"])
        self.assertIn("等待人工审批", payload["reply"])

    async def test_status_not_found_and_ambiguous(self):
        with mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=[])):
            payload = await chatops_routes.api_im_command(self._im_request(), {"sender": "alice", "text": "状态 nope"})
        self.assertFalse(payload["ok"])
        self.assertIn("未找到流程", payload["reply"])
        items = [dict(self._WAIT_ITEM), {**self._WAIT_ITEM, "wf_id": "aiops-fix-order-a-202"}]
        with mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=items)):
            payload = await chatops_routes.api_im_command(self._im_request(),
                {"sender": "alice", "text": "状态 aiops-fix-order"}
            )
        self.assertFalse(payload["ok"])
        self.assertIn("不唯一", payload["reply"])

    async def test_unmapped_sender_cannot_approve(self):
        with mock.patch.dict(os.environ, {"AIOPS_IM_USERS": ""}):
            payload = await chatops_routes.api_im_command(self._im_request(),
                {"sender": "alice", "text": f"批准 {self._WF_ID}"}
            )
        self.assertFalse(payload["ok"])
        self.assertIn("权限不足", payload["reply"])

    async def test_approve_happy_path(self):
        roles = json.dumps({"alice": "operator"})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AIOPS_IM_USERS": roles}), \
                 mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=[dict(self._WAIT_ITEM)])), \
                 mock.patch.object(chatops_routes.gw, "flow_detail", mock.AsyncMock(return_value=self._detail())), \
                 mock.patch.object(chatops_routes.gw, "signal_approval", mock.AsyncMock()) as signal, \
                 mock.patch.object(chatops_routes.gw, "invalidate_cache") as invalidate, \
                 mock.patch.object(chatops_routes.kill_switch, "get_state", return_value={"active": False}), \
                 mock.patch.object(chatops_routes.audit, "DATA_DIR", Path(tmp)):
                payload = await chatops_routes.api_im_command(self._im_request(),
                    {"sender": "alice", "text": f"批准 {self._WF_ID}"}
                )
                rows = audit.read_audit()
        self.assertTrue(payload["ok"])
        self.assertIn("一级审批已批准", payload["reply"])
        signal.assert_awaited_once_with(self._WF_ID, "approve")
        invalidate.assert_called_once()
        self.assertEqual(rows[0]["actor"], "im:alice")
        self.assertEqual(rows[0]["action"], "chatops:approval:approve")
        self.assertEqual(rows[0]["result"], "signaled")
        self.assertEqual(rows[0]["params"]["channel"], "im")

    async def test_approve_guard_rejections(self):
        """前置校验与 Web 控制台同款：阶段不符 / 已结束 / 已决策均被拒绝（ok:false）。"""
        roles = json.dumps({"alice": "operator"})
        cases = [
            (self._detail(status=self._status(stage="FIXING")), "不允许该操作"),
            (self._detail(exec_status="COMPLETED"), "流程已结束"),
            (self._detail(status=self._status(approval="approve")), "不能重复提交"),
        ]
        for detail, expected in cases:
            with mock.patch.dict(os.environ, {"AIOPS_IM_USERS": roles}), \
                 mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=[dict(self._WAIT_ITEM)])), \
                 mock.patch.object(chatops_routes.gw, "flow_detail", mock.AsyncMock(return_value=detail)), \
                 mock.patch.object(chatops_routes.kill_switch, "get_state", return_value={"active": False}):
                payload = await chatops_routes.api_im_command(self._im_request(),
                    {"sender": "alice", "text": f"批准 {self._WF_ID}"}
                )
            self.assertFalse(payload["ok"], expected)
            self.assertIn("未执行", payload["reply"])
            self.assertIn(expected, payload["reply"])

    async def test_second_approve_requires_protected_dir(self):
        roles = json.dumps({"alice": "operator"})
        with mock.patch.dict(os.environ, {"AIOPS_IM_USERS": roles}), \
             mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=[dict(self._WAIT_ITEM)])), \
             mock.patch.object(chatops_routes.gw, "flow_detail", mock.AsyncMock(return_value=self._detail())), \
             mock.patch.object(chatops_routes.kill_switch, "get_state", return_value={"active": False}):
            payload = await chatops_routes.api_im_command(self._im_request(),
                {"sender": "alice", "text": f"二级批准 {self._WF_ID}"}
            )
        self.assertFalse(payload["ok"])
        self.assertIn("无需二级审批", payload["reply"])

    async def test_kill_switch_blocks_decision_but_not_query(self):
        roles = json.dumps({"alice": "operator"})
        active = {"active": True, "reason": "演练", "actor": "admin"}
        with mock.patch.dict(os.environ, {"AIOPS_IM_USERS": roles}), \
             mock.patch.object(chatops_routes.kill_switch, "get_state", return_value=active), \
             mock.patch.object(chatops_routes.gw, "list_flows", mock.AsyncMock(return_value=[dict(self._WAIT_ITEM)])):
            with self.assertRaises(ApiError) as ctx:
                await chatops_routes.api_im_command(self._im_request(),
                    {"sender": "alice", "text": f"批准 {self._WF_ID}"}
                )
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("kill switch", ctx.exception.message)
            payload = await chatops_routes.api_im_command(self._im_request(), {"sender": "alice", "text": "待办"})
        self.assertTrue(payload["ok"])  # 查询命令保留可用（与控制台 GET 行为对齐）

    async def test_unknown_command_and_bad_payload(self):
        payload = await chatops_routes.api_im_command(self._im_request(), {"sender": "alice", "text": "部署 abc"})
        self.assertFalse(payload["ok"])
        self.assertIn("未识别命令", payload["reply"])
        with self.assertRaises(ApiError) as ctx:
            await chatops_routes.api_im_command(self._im_request(), {"foo": "bar"})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_route_rejects_non_im_source(self):
        """认证来源只认 middleware 注入；普通凭证/无来源/请求体伪造字段一律 403。"""
        for source, payload in (
            ("static", {"sender": "alice", "text": "帮助"}),
            (None, {"sender": "alice", "text": "帮助"}),
            ("service", {"sender": "alice", "text": "帮助"}),
            ("static", {"sender": "alice", "text": "帮助", "auth_source": "im"}),
        ):
            with self.assertRaises(ApiError) as ctx:
                await chatops_routes.api_im_command(self._im_request(source=source), payload)
            self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
