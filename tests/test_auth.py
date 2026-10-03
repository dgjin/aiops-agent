"""控制台鉴权与角色单测（bff/auth）：令牌解析 / 身份解析 / 角色判定 / fail-closed。

对应评估报告 C1（无鉴权）与 B2（无操作身份）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from bff import auth

TOKENS = {
    "tok-view": {"user": "viewer01", "role": "viewer"},
    "tok-ops": {"user": "zhang", "role": "operator"},
    "tok-adm": {"user": "li", "role": "admin"},
}


class LoadTokensTest(unittest.TestCase):
    def test_valid_registry(self) -> None:
        tokens = auth.load_tokens(json.dumps(TOKENS))
        self.assertEqual(tokens["tok-ops"], auth.Identity(user="zhang", role="operator"))
        self.assertEqual(tokens["tok-adm"].level, 3)

    def test_missing_env_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError) as ctx:
            auth.load_tokens("")
        self.assertIn("fail-closed", str(ctx.exception))

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError):
            auth.load_tokens("{ not json")

    def test_empty_object_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError):
            auth.load_tokens("{}")

    def test_invalid_role_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError) as ctx:
            auth.load_tokens(json.dumps({"t": {"user": "u", "role": "root"}}))
        self.assertIn("role 非法", str(ctx.exception))

    def test_missing_user_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError):
            auth.load_tokens(json.dumps({"t": {"role": "viewer"}}))

    def test_entry_not_object_raises(self) -> None:
        with self.assertRaises(auth.AuthConfigError):
            auth.load_tokens(json.dumps({"t": "viewer"}))


class ResolveIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = auth.load_tokens(json.dumps(TOKENS))

    def test_bearer_ok(self) -> None:
        identity = auth.resolve_identity("Bearer tok-ops", tokens=self.tokens)
        self.assertEqual((identity.user, identity.role), ("zhang", "operator"))

    def test_bearer_case_insensitive(self) -> None:
        identity = auth.resolve_identity("bearer tok-adm", tokens=self.tokens)
        self.assertEqual(identity.role, "admin")

    def test_missing_header_401(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            auth.resolve_identity(None, tokens=self.tokens)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_non_bearer_401(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            auth.resolve_identity("Basic abc", tokens=self.tokens)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_unknown_token_401(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            auth.resolve_identity("Bearer nope", tokens=self.tokens)
        self.assertEqual(ctx.exception.status_code, 401)


class RegistryTestBase(unittest.TestCase):
    """把注册表指向临时文件：**绝不触碰真实的 data/console_tokens.json**。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registry = Path(self._tmp.name) / "console_tokens.json"
        patcher = mock.patch.dict(
            os.environ,
            {
                "AIOPS_CONSOLE_TOKEN_FILE": str(self.registry),
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class RequireRoleTest(RegistryTestBase):
    def _guard(self, role: str, header: str | None):
        return auth.require_role(role)(header)

    def test_viewer_allowed_for_viewer_endpoint(self) -> None:
        identity = self._guard("viewer", "Bearer tok-view")
        self.assertEqual(identity.user, "viewer01")

    def test_operator_rejected_on_admin_endpoint(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._guard("admin", "Bearer tok-ops")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_allowed_on_admin_endpoint(self) -> None:
        identity = self._guard("admin", "Bearer tok-adm")
        self.assertEqual(identity.role, "admin")

    def test_fail_closed_when_unconfigured(self) -> None:
        """注册表缺失且 env 未配置 → 503（绝不放行）。"""
        os.environ.pop("AIOPS_CONSOLE_AUTH_TOKENS", None)
        os.environ["AIOPS_CONSOLE_TOKEN_FILE"] = str(Path(self._tmp.name) / "absent.json")
        with self.assertRaises(HTTPException) as ctx:
            self._guard("viewer", "Bearer tok-view")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_unknown_min_role_rejected_at_build(self) -> None:
        with self.assertRaises(ValueError):
            auth.require_role("superuser")


class RegistrySeedTest(RegistryTestBase):
    def test_seeds_from_env_and_sets_600(self) -> None:
        tokens = auth.registry_tokens()
        self.assertEqual(set(tokens), set(TOKENS))  # 播种即 env 里的三个令牌
        self.assertTrue(self.registry.is_file())
        self.assertEqual(oct(self.registry.stat().st_mode)[-3:], "600")

    def test_env_not_reapplied_once_file_exists(self) -> None:
        """文件已存在时不再用 env 覆盖（env 仅首次播种）。"""
        auth.registry_tokens()
        os.environ["AIOPS_CONSOLE_AUTH_TOKENS"] = json.dumps(
            {"tok-extra": {"user": "extra", "role": "admin"}}
        )
        self.assertNotIn("tok-extra", auth.registry_tokens())

    def test_seeding_counts_as_first_issue_so_rotation_can_start(self) -> None:
        """播种即视为首次发放：否则 rotated_at 为空 → due 恒 False → 自动轮换永不启动。

        显式固定轮换间隔，避免受工程 `.env`（可能设 ROTATE_INTERVAL=0 关闭轮换）影响。
        """
        with mock.patch.dict(
            os.environ, {"AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS": "2592000"}, clear=False
        ):
            auth.registry_tokens()
            status = auth.rotation_status()
            self.assertIsNotNone(status["last_rotated_at"])
            self.assertIsNotNone(status["next_rotation_at"])

    def test_seeding_with_rotation_disabled_has_no_expiry(self) -> None:
        """关闭自动轮换（interval=0）：无下次轮换时间，且令牌不设失效时间。"""
        with mock.patch.dict(
            os.environ, {"AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS": "0"}, clear=False
        ):
            auth.registry_tokens()
            status = auth.rotation_status()
            self.assertFalse(status["auto_rotation_enabled"])
            self.assertIsNone(status["next_rotation_at"])
            self.assertFalse(status["due"])
            self.assertTrue(all(t["expires_at"] is None for t in status["tokens"]))

    def test_seeded_registry_becomes_due_after_interval(self) -> None:
        """播种后经过一个轮换间隔即到期 —— 保证自动轮换**能启动**（早期缺陷：永不启动）。"""
        with mock.patch.dict(
            os.environ, {"AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS": "3600"}, clear=False
        ):
            t0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
            auth.registry_tokens(now=t0)  # 播种（rotated_at = t0）
            self.assertFalse(auth.rotation_status(now=t0 + timedelta(seconds=60))["due"])
            self.assertTrue(auth.rotation_status(now=t0 + timedelta(seconds=3601))["due"])

    def test_invalid_registry_raises(self) -> None:
        self.registry.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(auth.AuthConfigError):
            auth.registry_tokens()


class RotationTest(RegistryTestBase):
    def setUp(self) -> None:
        super().setUp()
        # 缩短间隔/宽限，便于断言（不改生产默认值）
        os.environ["AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS"] = "3600"
        os.environ["AIOPS_TOKEN_GRACE_SECONDS"] = "60"
        self.t0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)

    def _seed(self) -> None:
        auth.registry_tokens(now=self.t0)

    def test_rotate_creates_new_active_and_retires_old(self) -> None:
        self._seed()
        result = auth.rotate(now=self.t0, reason="manual")
        self.assertEqual(result["reason"], "manual")
        self.assertEqual(len(result["new_tokens"]), 3)  # 三个身份各生成一枚

        status = auth.rotation_status(now=self.t0)
        states = [item["state"] for item in status["tokens"]]
        self.assertEqual(states.count("active"), 3)
        self.assertEqual(states.count("previous"), 3)
        self.assertEqual(status["last_rotated_at"], "2026-10-03T12:00:00+00:00")

    def test_old_token_usable_within_grace_then_rejected(self) -> None:
        self._seed()
        auth.rotate(now=self.t0, reason="auto")
        within = auth.registry_tokens(now=self.t0 + timedelta(seconds=30))
        self.assertIn("tok-adm", within)  # 宽限期内旧令牌仍可用（不会把运维锁在门外）

        after = auth.registry_tokens(now=self.t0 + timedelta(seconds=61))
        self.assertNotIn("tok-adm", after)  # 超宽限期失效
        self.assertTrue(any(rec["role"] == "admin" for _i, rec in after.values()))  # 新令牌在

    def test_previous_pruned_after_grace(self) -> None:
        self._seed()
        auth.rotate(now=self.t0, reason="auto")
        auth.rotate(now=self.t0 + timedelta(seconds=120), reason="auto")  # 第二次轮换会清理超期项
        status = auth.rotation_status(now=self.t0 + timedelta(seconds=120))
        self.assertEqual([item["state"] for item in status["tokens"]].count("previous"), 3)

    def test_rotation_status_excludes_any_token_value(self) -> None:
        self._seed()
        status = auth.rotation_status(now=self.t0)
        blob = json.dumps(status, ensure_ascii=False)
        for token in TOKENS:
            self.assertNotIn(token, blob)  # 清单接口绝不回显令牌值或其片段
            # 令牌标识必须是不可反推的哈希指纹（早期实现用了前 6 位 → 短令牌近乎泄露）
            self.assertNotIn(token[:4], blob)
        for item in status["tokens"]:
            self.assertRegex(item["id"], r"^[0-9a-f]{8}$")

    def test_due_flag_and_next_rotation(self) -> None:
        self._seed()
        auth.rotate(now=self.t0, reason="auto")
        not_due = auth.rotation_status(now=self.t0 + timedelta(seconds=10))
        self.assertFalse(not_due["due"])
        self.assertEqual(not_due["next_rotation_at"], "2026-10-03T13:00:00+00:00")

        due = auth.rotation_status(now=self.t0 + timedelta(seconds=3601))
        self.assertTrue(due["due"])

    def test_interval_zero_disables_auto_rotation(self) -> None:
        os.environ["AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS"] = "0"
        self._seed()
        status = auth.rotation_status(now=self.t0)
        self.assertFalse(status["auto_rotation_enabled"])
        self.assertIsNone(status["next_rotation_at"])
        self.assertFalse(status["due"])

    def test_allow_reveal_defaults_false_and_parses_true(self) -> None:
        os.environ.pop("AIOPS_TOKEN_ALLOW_REVEAL", None)
        self.assertFalse(auth.allow_reveal())
        os.environ["AIOPS_TOKEN_ALLOW_REVEAL"] = "true"
        self.assertTrue(auth.allow_reveal())


class RoleLevelTest(unittest.TestCase):
    def test_ordering(self) -> None:
        self.assertLess(auth.ROLE_ORDER["viewer"], auth.ROLE_ORDER["operator"])
        self.assertLess(auth.ROLE_ORDER["operator"], auth.ROLE_ORDER["admin"])

    def test_unknown_role_has_zero_level(self) -> None:
        self.assertEqual(auth.Identity(user="x", role="nope").level, 0)


if __name__ == "__main__":
    unittest.main()
