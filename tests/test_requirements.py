"""需求基线条目客户端与主动分析单测（requirements_client / analyze_requirements）。

覆盖：
    - export_url：查询参数拼装与空格 / 冒号百分号编码；
    - fetch_requirements：正常拉取、401、非 JSON、缺 entries、连接不可达——绝不抛异常；
    - token：参数优先 / 环境变量 NL2SQL_OPS_TOKEN 回退 / Authorization 头注入；
    - summarize / render_markdown：分布汇总、高优排序、报告渲染与转义；
    - run_analyze：--json 模式与报告落盘（本地假服务，端到端）；
    - app_manifest：_STR_FIELDS 收录 requirements_path 且类型受校验。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import analyze_requirements
from aiops_agent import app_manifest, requirements_client

GOOD_ENTRY = {
    "id": 2,
    "kind": "SUGGESTION",
    "title": "看板支持定时自动刷新",
    "content": "大屏投放场景希望看板可以每 5 分钟自动刷新数据。",
    "status": "BASELINED",
    "priority": "P1",
    "baselineVersion": "v0.9.99",
    "assessment": "价值高、成本可控，纳入 v0.9.99 需求基线计划。",
    "submitter": "analyst01",
    "department": "运营部",
    "reviewer": "admin",
    "reviewedAt": "2026-10-08T11:00:00.000Z",
    "createdAt": "2026-10-08T09:00:00.000Z",
    "updatedAt": "2026-10-08T11:00:00.000Z",
}

GOOD = {
    "spec_version": "1.0",
    "service": "nl2sql",
    "system": "智能问数据分析系统",
    "exportedAt": "2026-10-08T12:00:00.000Z",
    "filter": {"status": "BASELINED", "kind": None, "since": None},
    "returned": 1,
    "entries": [GOOD_ENTRY],
}


class ExportUrlTest(unittest.TestCase):
    def test_default_params(self) -> None:
        url = requirements_client.export_url("http://localhost:3000")
        self.assertEqual(
            url, "http://localhost:3000/api/requirements/export?status=BASELINED&limit=200"
        )

    def test_trailing_slash_and_param_normalization(self) -> None:
        url = requirements_client.export_url(
            "http://localhost:3000/",
            status="baselined",
            kind="bug",
            since="2026-10-08 12:00",
            limit=50,
        )
        self.assertIn("status=BASELINED", url)
        self.assertIn("kind=BUG", url)
        self.assertIn("since=2026-10-08%2012%3A00", url)
        self.assertIn("limit=50", url)

    def test_invalid_limit_omitted(self) -> None:
        url = requirements_client.export_url("http://x", limit="abc")  # type: ignore[arg-type]
        self.assertNotIn("limit=", url)


class _Handler(BaseHTTPRequestHandler):
    """本地回显服务：类属性控制响应内容与状态码（测试内改写）。"""

    payload: object = GOOD
    status: int = 200
    last_path: str = ""
    last_auth: str = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        _Handler.last_path = self.path
        _Handler.last_auth = self.headers.get("Authorization", "")
        raw = self.payload if isinstance(self.payload, str) else json.dumps(
            self.payload, ensure_ascii=False
        )
        body = raw.encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 测试静默
        pass


class _LocalServerCase(unittest.TestCase):
    """起一个本地假服务；子类按需改写 _Handler.payload / status。"""

    def setUp(self) -> None:
        _Handler.payload = GOOD
        _Handler.status = 200
        _Handler.last_path = ""
        _Handler.last_auth = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"


class FetchTest(_LocalServerCase):
    def test_fetch_ok(self) -> None:
        result = requirements_client.fetch_requirements(self.base, token="tok-1")
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["spec_version"], "1.0")
        self.assertEqual(result["service"], "nl2sql")
        self.assertEqual(result["system"], "智能问数据分析系统")
        self.assertEqual(result["exported_at"], "2026-10-08T12:00:00.000Z")
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["entries"][0]["id"], 2)
        self.assertEqual(result["filter"]["status"], "BASELINED")
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["url"].startswith(self.base + "/api/requirements/export?"))
        self.assertEqual(_Handler.last_auth, "Bearer tok-1")

    def test_fetch_query_params_encoded(self) -> None:
        requirements_client.fetch_requirements(
            self.base,
            status="ALL",
            kind="requirement",
            since="2026-10-08 12:00",
            limit=50,
            token="t",
        )
        self.assertIn("status=ALL", _Handler.last_path)
        self.assertIn("kind=REQUIREMENT", _Handler.last_path)
        self.assertIn("since=2026-10-08%2012%3A00", _Handler.last_path)
        self.assertIn("limit=50", _Handler.last_path)

    def test_fetch_token_from_env(self) -> None:
        with mock.patch.dict(os.environ, {requirements_client.TOKEN_ENV: "tok-env"}):
            result = requirements_client.fetch_requirements(self.base)
        self.assertTrue(result["ok"])
        self.assertEqual(_Handler.last_auth, "Bearer tok-env")

    def test_fetch_explicit_token_wins_over_env(self) -> None:
        with mock.patch.dict(os.environ, {requirements_client.TOKEN_ENV: "tok-env"}):
            requirements_client.fetch_requirements(self.base, token="tok-param")
        self.assertEqual(_Handler.last_auth, "Bearer tok-param")

    def test_fetch_no_token_no_auth_header(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(requirements_client.TOKEN_ENV, None)
            result = requirements_client.fetch_requirements(self.base)
        self.assertTrue(result["ok"])
        self.assertEqual(_Handler.last_auth, "")

    def test_fetch_401_is_graceful(self) -> None:
        _Handler.status = 401
        _Handler.payload = {"error": "未提供有效凭据"}
        result = requirements_client.fetch_requirements(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertIn("OPS_API_TOKEN", result["error"])
        self.assertEqual(result["entries"], [])

    def test_fetch_400_reports_server_detail(self) -> None:
        _Handler.status = 400
        _Handler.payload = {"error": "since 格式无效，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM"}
        result = requirements_client.fetch_requirements(self.base, since="abc")
        self.assertFalse(result["ok"])
        self.assertIn("400", result["error"])
        self.assertIn("since 格式无效", result["error"])

    def test_fetch_invalid_json_is_graceful(self) -> None:
        _Handler.payload = "<!doctype html><html>不是 JSON</html>"
        result = requirements_client.fetch_requirements(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])

    def test_fetch_missing_entries_is_graceful(self) -> None:
        _Handler.payload = {"spec_version": "1.0"}
        result = requirements_client.fetch_requirements(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("entries", result["error"])

    def test_fetch_missing_spec_version_is_graceful(self) -> None:
        _Handler.payload = {"entries": []}
        result = requirements_client.fetch_requirements(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("spec_version", result["error"])

    def test_fetch_other_major_version_warns_not_errors(self) -> None:
        _Handler.payload = {**GOOD, "spec_version": "2.0"}
        result = requirements_client.fetch_requirements(self.base)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_fetch_connection_refused_is_graceful(self) -> None:
        result = requirements_client.fetch_requirements("http://127.0.0.1:1", timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertIn("无法访问", result["error"])


class SummarizeTest(unittest.TestCase):
    def test_summary_counts_and_cursor(self) -> None:
        entries = [
            GOOD_ENTRY,
            {
                **GOOD_ENTRY,
                "id": 3,
                "kind": "REQUIREMENT",
                "priority": "P0",
                "baselineVersion": "v1.0.0",
                "updatedAt": "2026-10-09T08:00:00.000Z",
            },
        ]
        summary = analyze_requirements.summarize(entries)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["byKind"], {"REQUIREMENT": 1, "SUGGESTION": 1})
        self.assertEqual(summary["byPriority"], {"P0": 1, "P1": 1})
        self.assertEqual(summary["byBaselineVersion"], {"v0.9.99": 1, "v1.0.0": 1})
        self.assertEqual([item["id"] for item in summary["highPriority"]], [3, 2])  # P0 在前
        self.assertEqual(summary["latestUpdatedAt"], "2026-10-09T08:00:00.000Z")

    def test_summary_without_priority_not_high(self) -> None:
        summary = analyze_requirements.summarize(
            [{**GOOD_ENTRY, "priority": "", "baselineVersion": ""}]
        )
        self.assertEqual(summary["highPriority"], [])
        self.assertEqual(summary["byPriority"], {"未定级": 1})
        self.assertEqual(summary["byBaselineVersion"], {"未指定": 1})

    def test_summary_empty(self) -> None:
        summary = analyze_requirements.summarize([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["highPriority"], [])
        self.assertEqual(summary["latestUpdatedAt"], "")


class RenderMarkdownTest(unittest.TestCase):
    def _result(self, entries: list[dict]) -> dict:
        return {
            "url": "http://x/api/requirements/export?status=BASELINED",
            "service": "nl2sql",
            "system": "智能问数据分析系统",
            "exported_at": "2026-10-08T12:00:00.000Z",
            "returned": len(entries),
            "entries": entries,
        }

    def test_render_contains_key_sections(self) -> None:
        report = analyze_requirements.render_markdown(
            self._result([GOOD_ENTRY]), analyze_requirements.summarize([GOOD_ENTRY])
        )
        self.assertIn("# 需求基线主动分析报告", report)
        self.assertIn("## 一、分布汇总", report)
        self.assertIn("## 二、高优先级条目（P0/P1）", report)
        self.assertIn("## 三、需求条目清单", report)
        self.assertIn("看板支持定时自动刷新", report)
        self.assertIn("v0.9.99", report)
        self.assertIn("exportedAt", report)

    def test_render_empty_entries(self) -> None:
        report = analyze_requirements.render_markdown(self._result([]), analyze_requirements.summarize([]))
        self.assertIn("（无基线条目）", report)
        self.assertIn("- （无）", report)

    def test_render_escapes_pipe_and_newline(self) -> None:
        entry = {**GOOD_ENTRY, "title": "含|竖线\n换行"}
        report = analyze_requirements.render_markdown(
            self._result([entry]), analyze_requirements.summarize([entry])
        )
        self.assertIn("含\\|竖线 换行", report)


class RunAnalyzeTest(_LocalServerCase):
    def _run_json(self, **overrides) -> tuple[int, str]:
        kwargs = dict(
            token="tok-1",
            status="BASELINED",
            kind="",
            since="",
            limit=200,
            out="",
            as_json=True,
            timeout=2.0,
        )
        kwargs.update(overrides)
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = analyze_requirements.run_analyze(self.base, **kwargs)
        return code, buffer.getvalue()

    def test_json_mode_success(self) -> None:
        code, output = self._run_json()
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["entries"][0]["id"], 2)
        self.assertEqual(payload["exportedAt"], "2026-10-08T12:00:00.000Z")

    def test_json_mode_failure(self) -> None:
        _Handler.status = 401
        code, output = self._run_json()
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertIn("401", payload["error"])

    def test_report_written_and_summary_printed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "report.md")
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = analyze_requirements.run_analyze(
                    self.base,
                    token="tok-1",
                    status="BASELINED",
                    kind="",
                    since="",
                    limit=200,
                    out=out,
                    as_json=False,
                    timeout=2.0,
                )
            self.assertEqual(code, 0)
            report = Path(out).read_text(encoding="utf-8")
            self.assertIn("# 需求基线主动分析报告", report)
            self.assertIn("看板支持定时自动刷新", report)
            output = buffer.getvalue()
            self.assertIn("报告已写入", output)
            self.assertIn("高优提示（P0/P1）：#2 [P1] 看板支持定时自动刷新", output)

    def test_failure_writes_no_report(self) -> None:
        _Handler.status = 401
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "report.md")
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = analyze_requirements.run_analyze(
                    self.base,
                    token="",
                    status="BASELINED",
                    kind="",
                    since="",
                    limit=200,
                    out=out,
                    as_json=False,
                    timeout=2.0,
                )
            self.assertEqual(code, 1)
            self.assertFalse(Path(out).exists())
            self.assertIn("拉取失败", buffer.getvalue())


class ManifestFieldTest(unittest.TestCase):
    def test_requirements_path_in_str_fields(self) -> None:
        self.assertIn("requirements_path", app_manifest._STR_FIELDS)

    def test_requirements_path_type_checked(self) -> None:
        errors, _ = app_manifest.validate_manifest(
            {"spec_version": "1.0", "service": "nl2sql", "requirements_path": 123}
        )
        self.assertEqual(errors, ["requirements_path 必须是字符串"])

    def test_requirements_path_valid_string(self) -> None:
        errors, warnings = app_manifest.validate_manifest(
            {
                "spec_version": "1.0",
                "service": "nl2sql",
                "requirements_path": "/api/requirements/export",
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
