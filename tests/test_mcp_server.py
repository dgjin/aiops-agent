"""MCP server 单测（P4⑬）：BffClient 行为 / 工具函数拼装 / MCP 注册与错误呈现。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v

设计：
  - 假 BFF 用进程内 ThreadingHTTPServer（随机端口）——真实 urllib 链路（连接 / 头 /
    状态码 / JSON），零外部依赖；同时天然验证「绕过系统代理」（如有代理且未绕过，
    127.0.0.1 会得到 502 而非预期响应）；
  - mcp 包未安装时注册类用例自动跳过（工具函数用例仍全量执行：mcp_server.py 顶层
    不 import mcp，是刻意保持的隔离）。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import mcp_server
from mcp_server import (
    BffClient,
    BffError,
    build_parser,
    build_server,
    tool_approve_flow,
    tool_check_system_health,
    tool_get_flow,
    tool_list_escalations,
    tool_list_flows,
    tool_list_pending_approvals,
    tool_ops_overview,
    tool_query_audit,
    tool_second_approve_flow,
)

try:  # mcp 为可选组件（requirements-mcp.txt），未安装时相关用例跳过
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError

    HAS_MCP = True
except ImportError:  # pragma: no cover - 取决于本地是否安装 mcp
    _ToolError = None  # type: ignore[assignment]
    HAS_MCP = False


class _FakeBff:
    """进程内假 BFF：记录请求 + 可按路径编程响应（默认 200 {"ok": True}）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.routes: dict[str, tuple[int, dict]] = {}
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else None,
                    }
                )
                status, payload = outer.routes.get(self.path.split("?", 1)[0], (200, {"ok": True}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

            def log_message(self, *args) -> None:  # 静音测试输出
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def client(self, token: str = "tok-test") -> BffClient:
        return BffClient(base_url=self.base_url, token=token)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _query_of(path: str) -> dict:
    """提取请求路径的 query 为 dict（免 urlencode 顺序影响）。"""
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(path).query))


class BffClientTest(unittest.TestCase):
    """BffClient：鉴权头 / 信封剥离 / 错误转译 / 代理绕过 / 参数裁剪。"""

    def setUp(self) -> None:
        self.fake = _FakeBff()
        self.addCleanup(self.fake.close)

    def test_token_header_sent(self) -> None:
        self.fake.client("tok-abc").get("/api/health")
        self.assertEqual(self.fake.requests[0]["headers"]["authorization"], "Bearer tok-abc")

    def test_no_token_no_header(self) -> None:
        BffClient(base_url=self.fake.base_url, token="").get("/api/health")
        self.assertNotIn("authorization", self.fake.requests[0]["headers"])

    def test_server_time_stripped(self) -> None:
        self.fake.routes["/api/x"] = (200, {"server_time": "2026-10-12T00:00:00Z", "v": 1})
        self.assertEqual(self.fake.client().get("/api/x"), {"v": 1})

    def test_error_translated_with_reason(self) -> None:
        self.fake.routes["/api/x"] = (409, {"error": "流程已结束（COMPLETED），不能执行写操作"})
        with self.assertRaises(BffError) as ctx:
            self.fake.client().get("/api/x")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("流程已结束", ctx.exception.message)
        self.assertIn("BFF 409", str(ctx.exception))

    def test_401_without_detail_gives_token_hint(self) -> None:
        self.fake.routes["/api/x"] = (401, {"error": ""})
        with self.assertRaises(BffError) as ctx:
            self.fake.client().get("/api/x")
        self.assertIn("AIOPS_MCP_TOKEN", ctx.exception.message)

    def test_network_unreachable_status_zero(self) -> None:
        # 死端口拒绝连接：若未绕过系统代理，本机代理会返回 502 等误导性状态码
        with self.assertRaises(BffError) as ctx:
            BffClient(base_url="http://127.0.0.1:1", token="t").get("/api/health")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("无法连接控制台 BFF", ctx.exception.message)

    def test_empty_params_dropped(self) -> None:
        self.fake.client().get("/api/flows", status="", service="svc")
        self.assertEqual(self.fake.requests[0]["path"], "/api/flows?service=svc")

    def test_post_sends_json_body(self) -> None:
        self.fake.client().post("/api/flows/wf-1/approval", {"decision": "approve"})
        req = self.fake.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["body"], {"decision": "approve"})


class ToolFunctionsTest(unittest.TestCase):
    """9 个工具函数的端点拼装 / 合并 / 降级（普通 BffClient，不依赖 mcp 包）。"""

    def setUp(self) -> None:
        self.fake = _FakeBff()
        self.addCleanup(self.fake.close)
        self.client = self.fake.client()

    def test_ops_overview_merges(self) -> None:
        self.fake.routes["/api/overview"] = (200, {"counts": {"running": 2}})
        self.fake.routes["/api/metrics/ops"] = (200, {"mttr_seconds": 42})
        result = tool_ops_overview(self.client, days=3)
        self.assertEqual(result["counts"], {"running": 2})
        self.assertEqual(result["ops_metrics"], {"mttr_seconds": 42})
        paths = [r["path"] for r in self.fake.requests]
        self.assertEqual(paths[0], "/api/overview")
        self.assertEqual(_query_of(paths[1]), {"days": "3"})

    def test_ops_overview_metrics_degraded(self) -> None:
        self.fake.routes["/api/overview"] = (200, {"counts": {"running": 1}})
        self.fake.routes["/api/metrics/ops"] = (500, {"error": "boom"})
        result = tool_ops_overview(self.client)
        self.assertEqual(result["counts"], {"running": 1})  # 总览不受指标故障影响
        self.assertIn("BFF 500", result["ops_metrics"]["error"])

    def test_list_flows_params(self) -> None:
        tool_list_flows(self.client, status="open", service="demo", stage="FIXING", limit=5)
        req = self.fake.requests[0]
        self.assertEqual(urllib.parse.urlparse(req["path"]).path, "/api/flows")
        self.assertEqual(
            _query_of(req["path"]), {"status": "open", "service": "demo", "stage": "FIXING", "limit": "5"}
        )

    def test_get_flow_quotes_special_chars(self) -> None:
        tool_get_flow(self.client, "a b/c")
        self.assertEqual(self.fake.requests[0]["path"], "/api/flows/a%20b%2Fc")

    def test_list_pending_approvals(self) -> None:
        tool_list_pending_approvals(self.client)
        self.assertEqual(self.fake.requests[0]["path"], "/api/approvals")

    def test_list_escalations_status(self) -> None:
        tool_list_escalations(self.client, status="open")
        self.assertEqual(self.fake.requests[0]["path"], "/api/escalations?status=open")

    def test_query_audit_params(self) -> None:
        tool_query_audit(self.client, q="", stage="DONE", days=7, limit=10)
        self.assertEqual(_query_of(self.fake.requests[0]["path"]), {"stage": "DONE", "days": "7", "limit": "10"})

    def test_check_system_health_merges(self) -> None:
        self.fake.routes["/api/health"] = (200, {"ok": True, "temporal": {"connected": True}})
        self.fake.routes["/api/subsystems"] = (200, {"subsystems": {"loki": {"ok": True}}, "degraded": []})
        result = tool_check_system_health(self.client)
        self.assertTrue(result["ok"])
        self.assertTrue(result["temporal"]["connected"])
        self.assertIn("loki", result["subsystems"])
        self.assertEqual(result["degraded"], [])

    def test_approve_flow_post(self) -> None:
        tool_approve_flow(self.client, "wf-1", "reject")
        req = self.fake.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/api/flows/wf-1/approval")
        self.assertEqual(req["body"], {"decision": "reject"})

    def test_second_approve_flow_post(self) -> None:
        tool_second_approve_flow(self.client, "wf-2", "approve")
        req = self.fake.requests[0]
        self.assertEqual(req["path"], "/api/flows/wf-2/second-approval")
        self.assertEqual(req["body"], {"decision": "approve"})


@unittest.skipUnless(HAS_MCP, "未安装 mcp 包（requirements-mcp.txt）")
class RegistrationTest(unittest.TestCase):
    """MCP 注册层：工具清单 / schema / 错误呈现（本地 call_tool 直调）。"""

    def setUp(self) -> None:
        self.fake = _FakeBff()
        self.addCleanup(self.fake.close)
        self.server = build_server(self.fake.client("tok-special"))

    def test_tool_names_complete_with_description(self) -> None:
        tools = asyncio.run(self.server.list_tools())
        self.assertEqual(sorted(t.name for t in tools), sorted(mcp_server.TOOL_NAMES))
        for tool in tools:
            self.assertTrue(tool.description, f"{tool.name} 缺 description")
            self.assertIsNotNone(tool.input_schema)

    def test_approve_schema_enum(self) -> None:
        tools = asyncio.run(self.server.list_tools())
        approve = next(t for t in tools if t.name == "approve_flow")
        schema = approve.input_schema
        self.assertEqual(schema["properties"]["decision"]["enum"], ["approve", "reject"])
        self.assertEqual(schema["required"], ["wf_id"])

    def test_call_tool_success_and_token_passthrough(self) -> None:
        self.fake.routes["/api/approvals"] = (200, {"items": [{"wf_id": "wf-x", "pending": "approval"}]})
        result = asyncio.run(self.server.call_tool("list_pending_approvals", {}))
        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["items"][0]["wf_id"], "wf-x")
        # 显式传入的 client 配置（token）被复制到注册层客户端
        self.assertEqual(self.fake.requests[0]["headers"]["authorization"], "Bearer tok-special")

    def test_call_tool_error_carries_reason(self) -> None:
        self.fake.routes["/api/approvals"] = (503, {"error": "kill switch 已激活"})
        with self.assertRaises(_ToolError) as ctx:
            asyncio.run(self.server.call_tool("list_pending_approvals", {}))
        self.assertIn("kill switch 已激活", str(ctx.exception))

    def test_ops_metrics_degrade_inside_tool_error_client(self) -> None:
        # 注册层客户端把 BffError 升级为双身份异常：工具内的降级捕获必须仍然生效
        self.fake.routes["/api/overview"] = (200, {"counts": {"running": 0}})
        self.fake.routes["/api/metrics/ops"] = (500, {"error": "boom"})
        result = asyncio.run(self.server.call_tool("ops_overview", {}))
        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["counts"], {"running": 0})
        self.assertIn("BFF 500", payload["ops_metrics"]["error"])


class ParserTest(unittest.TestCase):
    """CLI 参数：默认 stdio / --http 覆盖。"""

    def test_defaults_stdio(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.http)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, mcp_server.DEFAULT_HTTP_PORT)
        self.assertEqual(args.base_url, "")

    def test_http_overrides(self) -> None:
        # 解析层仅记录参数；是否允许监听由启动门禁 _enforce_transport_policy 判定
        args = build_parser().parse_args(["--http", "--host", "0.0.0.0", "--port", "9999", "--base-url", "http://x:1"])
        self.assertTrue(args.http)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9999)
        self.assertEqual(args.base_url, "http://x:1")


class TransportPolicyTest(unittest.TestCase):
    """启动门禁：production 仅 stdio；demo HTTP 仅 loopback（在监听前失败）。"""

    def test_stdio_allowed_in_production(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            mcp_server._enforce_transport_policy(build_parser().parse_args([]))

    def test_production_http_rejected(self) -> None:
        args = build_parser().parse_args(["--http", "--host", "127.0.0.1", "--port", "9971"])
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            with self.assertRaises(SystemExit) as ctx:
                mcp_server._enforce_transport_policy(args)
        self.assertIn("production", str(ctx.exception))

    def test_main_rejects_production_http_before_start(self) -> None:
        # main 在构建/启动前即失败：误开 HTTP 不会进入监听状态
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            with self.assertRaises(SystemExit):
                mcp_server.main(["--http", "--host", "127.0.0.1", "--port", "9971"])

    def test_demo_http_loopback_allowed(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            mcp_server._enforce_transport_policy(
                build_parser().parse_args(["--http", "--host", "127.0.0.1", "--port", "9971"])
            )
            mcp_server._enforce_transport_policy(
                build_parser().parse_args(["--http", "--host", "::1", "--port", "9971"])
            )

    def test_demo_http_non_loopback_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            for host in ("0.0.0.0", "192.168.1.9", "::", ""):
                args = build_parser().parse_args(["--http", "--host", host, "--port", "9971"])
                with self.assertRaises(SystemExit):
                    mcp_server._enforce_transport_policy(args)


if __name__ == "__main__":
    unittest.main()
