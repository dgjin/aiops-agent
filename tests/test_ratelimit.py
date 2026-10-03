"""限速与安全头单测（P2-01 / P2-05）：滑动窗口 / 双维度拦截 / 响应头。

BFF 集成经 TestClient（真实中间件链）；demo 模式默认关闭限速，
集成用例显式开启并压低配额以验证 429 行为。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aiops_agent import kill_switch

from bff import ratelimit

TOKENS = {
    "tok-view": {"user": "viewer01", "role": "viewer"},
    "tok-adm": {"user": "li", "role": "admin"},
}


class MemoryWindowTest(unittest.TestCase):
    """进程内滑动窗口语义。"""

    def setUp(self) -> None:
        ratelimit.reset()

    def test_allows_within_limit_then_blocks(self) -> None:
        for _ in range(3):
            allowed, retry = ratelimit.hit("k", limit=3)
            self.assertTrue(allowed)
            self.assertEqual(retry, 0.0)
        allowed, retry = ratelimit.hit("k", limit=3)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0.0)

    def test_window_rollover_releases_quota(self) -> None:
        self.assertTrue(ratelimit.hit("roll", limit=1, window=0.05)[0])
        self.assertFalse(ratelimit.hit("roll", limit=1, window=0.05)[0])
        time.sleep(0.06)
        self.assertTrue(ratelimit.hit("roll", limit=1, window=0.05)[0])

    def test_keys_are_isolated(self) -> None:
        ratelimit.hit("a", limit=1)
        self.assertTrue(ratelimit.hit("b", limit=1)[0])


class EnabledAndQuotaTest(unittest.TestCase):
    """开关默认值（production 开 / demo 关）、环境覆盖与配额解析。"""

    def test_defaults_follow_mode(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            os.environ.pop("AIOPS_RATE_LIMIT_ENABLED", None)
            self.assertFalse(ratelimit.enabled())
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            os.environ.pop("AIOPS_RATE_LIMIT_ENABLED", None)
            self.assertTrue(ratelimit.enabled())

    def test_explicit_env_overrides_mode(self) -> None:
        with mock.patch.dict(
            os.environ, {"AIOPS_MODE": "production", "AIOPS_RATE_LIMIT_ENABLED": "0"}
        ):
            self.assertFalse(ratelimit.enabled())
        with mock.patch.dict(
            os.environ, {"AIOPS_MODE": "demo", "AIOPS_RATE_LIMIT_ENABLED": "on"}
        ):
            self.assertTrue(ratelimit.enabled())

    def test_quota_env_parsing_with_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_RATE_LIMIT_WRITE_PER_MIN": "7"}):
            self.assertEqual(ratelimit.write_limit(), 7)
        with mock.patch.dict(os.environ, {"AIOPS_RATE_LIMIT_WRITE_PER_MIN": "bad"}):
            self.assertEqual(ratelimit.write_limit(), 20)
        with mock.patch.dict(os.environ, {"AIOPS_RATE_LIMIT_READ_PER_MIN": "0"}):
            self.assertEqual(ratelimit.read_limit(), 1)  # 配额下限 1


class RateLimitApiTest(unittest.TestCase):
    """BFF 集成：读配额 429 / 写配额更严 / 认证失败按 IP 计数 / 无凭证不计数。"""

    def setUp(self) -> None:
        ratelimit.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
                "AIOPS_CONSOLE_TOKEN_FILE": str(Path(self._tmp.name) / "tokens.json"),
                "AIOPS_RATE_LIMIT_ENABLED": "1",
                "AIOPS_RATE_LIMIT_READ_PER_MIN": "3",
                "AIOPS_RATE_LIMIT_WRITE_PER_MIN": "2",
                "AIOPS_RATE_LIMIT_AUTH_FAIL_PER_MIN": "2",
            },
        )
        self._env.start()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}
        self.bad = {"Authorization": "Bearer wrong-token"}

    def tearDown(self) -> None:
        ratelimit.reset()
        self._env.stop()
        self._tmp.cleanup()

    def test_read_quota_blocks_with_429_and_retry_after(self) -> None:
        for _ in range(3):
            self.assertEqual(self.client.get("/api/health", headers=self.admin).status_code, 200)
        blocked = self.client.get("/api/health", headers=self.admin)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertIn("过于频繁", blocked.json()["error"])

    def test_write_quota_is_stricter(self) -> None:
        with mock.patch.object(kill_switch, "DATA_PATH", Path(self._tmp.name) / "ks.json"):
            for _ in range(2):
                response = self.client.post(
                    "/api/system/kill-switch", json={"active": False}, headers=self.admin
                )
                self.assertEqual(response.status_code, 200)
            blocked = self.client.post(
                "/api/system/kill-switch", json={"active": False}, headers=self.admin
            )
        self.assertEqual(blocked.status_code, 429)

    def test_auth_failures_counted_per_ip(self) -> None:
        # 坏凭证连续请求：配额内 401，超限转为 429（防令牌暴力破解）
        self.assertEqual(self.client.get("/api/health", headers=self.bad).status_code, 401)
        self.assertEqual(self.client.get("/api/health", headers=self.bad).status_code, 401)
        self.assertEqual(self.client.get("/api/health", headers=self.bad).status_code, 429)

    def test_missing_credentials_not_rate_counted(self) -> None:
        # 无凭证的 401 不计数（前端首次打开页面的引导路径不被限速破坏）
        for _ in range(5):
            self.assertEqual(self.client.get("/api/health").status_code, 401)

    def test_explicit_disable_bypasses_limits(self) -> None:
        with mock.patch.dict(os.environ, {"AIOPS_RATE_LIMIT_ENABLED": "0"}):
            ratelimit.reset()
            for _ in range(6):
                self.assertEqual(
                    self.client.get("/api/health", headers=self.admin).status_code, 200
                )


class SecurityHeadersTest(unittest.TestCase):
    """P2-05：安全响应头（含鉴权早退响应）与 HSTS 条件下发。"""

    def setUp(self) -> None:
        ratelimit.reset()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
            },
        )
        self._env.start()
        from bff.app import app

        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._env.stop()

    def test_headers_on_api_response(self) -> None:
        response = self.client.get(
            "/api/health", headers={"Authorization": "Bearer tok-adm"}
        )
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_headers_on_auth_early_rejection(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_hsts_only_behind_tls(self) -> None:
        plain = self.client.get("/api/health")
        self.assertNotIn("Strict-Transport-Security", plain.headers)
        tls = self.client.get(
            "/api/health",
            headers={
                "X-Forwarded-Proto": "https",
                "Authorization": "Bearer tok-adm",
            },
        )
        self.assertEqual(
            tls.headers["Strict-Transport-Security"], "max-age=31536000; includeSubDomains"
        )


if __name__ == "__main__":
    unittest.main()
