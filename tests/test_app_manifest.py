"""AIOps Manifest 标准接口单测（app_manifest）：校验规则 + 本地 HTTP 探测。

覆盖：
    - validate_manifest：必填字段 / 类型校验 / 主版本告警；
    - fetch_manifest：正常抓取、404、非法 JSON、字段缺失、连接不可达——绝不抛异常。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiops_agent import app_manifest

GOOD = {
    "spec_version": "1.0",
    "service": "svc-manifest",
    "name": "标准接口测试应用",
    "probe_keyword": '"status": "ok"',
    "log_path": "/tmp/app*.log",
}


class ManifestValidationTest(unittest.TestCase):
    def test_valid_manifest_has_no_errors(self) -> None:
        errors, warnings = app_manifest.validate_manifest(GOOD)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_non_dict_rejected(self) -> None:
        errors, _ = app_manifest.validate_manifest(["not", "a", "dict"])
        self.assertTrue(errors)

    def test_missing_required_fields(self) -> None:
        errors, _ = app_manifest.validate_manifest({"name": "x"})
        self.assertEqual(len(errors), 2)  # spec_version + service

    def test_type_mismatch_rejected(self) -> None:
        payload = {**GOOD, "probe_keyword": 123, "protected_paths": "auth/**"}
        errors, _ = app_manifest.validate_manifest(payload)
        self.assertEqual(len(errors), 2)

    def test_other_major_version_warns_not_errors(self) -> None:
        errors, warnings = app_manifest.validate_manifest({**GOOD, "spec_version": "2.1"})
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)


class _Handler(BaseHTTPRequestHandler):
    """本地回显服务：类属性控制响应内容与状态码（测试内改写）。"""

    payload: object = GOOD
    status: int = 200

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
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


class ManifestFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.payload = GOOD
        _Handler.status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_fetch_ok(self) -> None:
        result = app_manifest.fetch_manifest(self.base)
        self.assertTrue(result["ok"])
        self.assertEqual(result["manifest"]["service"], "svc-manifest")
        self.assertEqual(result["url"], f"{self.base}/.well-known/aiops.json")
        self.assertEqual(result["errors"], [])

    def test_fetch_trailing_slash_base(self) -> None:
        result = app_manifest.fetch_manifest(self.base + "/")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], f"{self.base}/.well-known/aiops.json")

    def test_fetch_404_is_graceful(self) -> None:
        _Handler.status = 404
        result = app_manifest.fetch_manifest(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("404", result["error"])

    def test_fetch_invalid_json_is_graceful(self) -> None:
        _Handler.payload = "<!doctype html><html>不是 JSON</html>"
        result = app_manifest.fetch_manifest(self.base)
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])

    def test_fetch_missing_required_field_reports_error(self) -> None:
        _Handler.payload = {"name": "no service"}
        result = app_manifest.fetch_manifest(self.base)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_fetch_connection_refused_is_graceful(self) -> None:
        result = app_manifest.fetch_manifest("http://127.0.0.1:1", timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertIn("无法访问", result["error"])


if __name__ == "__main__":
    unittest.main()
