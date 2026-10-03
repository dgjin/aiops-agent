"""令牌服务单测（受保护目录 auth/** 的回归用例）。"""

from __future__ import annotations

import unittest

from auth.token_service import TokenService


class TestTokenService(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TokenService(secret="demo-secret")

    def test_verify_valid_token(self) -> None:
        token = self.service.issue(expiry=2000)
        self.assertTrue(self.service.verify(token, now=1000))

    def test_verify_expired_token(self) -> None:
        token = self.service.issue(expiry=1000)
        self.assertFalse(self.service.verify(token, now=1000))

    def test_verify_bad_signature(self) -> None:
        self.assertFalse(self.service.verify("2000.deadbeef", now=1000))


if __name__ == "__main__":
    unittest.main()
