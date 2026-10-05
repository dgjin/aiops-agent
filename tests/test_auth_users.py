"""用户与会话（登录 / 登出 / 本人改密 / 用户管理 / 会话吊销）单测。

覆盖层次：
- 密码哈希与输入校验（纯函数）；
- 用户/会话存储直测（demo 文件后端 + production SQLite 分支）；
- BFF 集成（TestClient 真实中间件链）：登录 → 访问 → 登出/吊销 → 401、
  角色矩阵（viewer/operator/admin）、静态令牌兼容、登录失败限速。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aiops_agent import db

from bff import audit, monitored_apps, ratelimit
from bff import auth as auth_store

ADMIN_PASSWORD = "admin12345"
TOKENS = {
    "tok-view": {"user": "viewer01", "role": "viewer"},
    "tok-adm": {"user": "li", "role": "admin"},
}


class PasswordHashTest(unittest.TestCase):
    """pbkdf2 哈希：往返 / 错误格式安全 / 盐随机。"""

    def test_roundtrip_and_mismatch(self) -> None:
        stored = auth_store.hash_password("correct-horse")
        self.assertTrue(auth_store.verify_password("correct-horse", stored))
        self.assertFalse(auth_store.verify_password("wrong-horse", stored))

    def test_bad_stored_format_returns_false(self) -> None:
        for bad in ("", "garbage", "pbkdf2_sha256$broken", "md5$1$aa$bb"):
            self.assertFalse(auth_store.verify_password("x", bad))

    def test_distinct_salts(self) -> None:
        self.assertNotEqual(
            auth_store.hash_password("same"), auth_store.hash_password("same")
        )

    def test_validate_username_password(self) -> None:
        self.assertEqual(auth_store.validate_username(" ops.1 "), "ops.1")
        for bad in ("a", "带中文", "has space", "x" * 33, ""):
            with self.assertRaises(auth_store.AuthError):
                auth_store.validate_username(bad)
        with self.assertRaises(auth_store.AuthError):
            auth_store.validate_password("short")


class UserFileStoreTest(unittest.TestCase):
    """demo 文件后端：播种 / 登录 / 会话生命周期 / 用户管理规则。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_USERS_FILE": str(Path(self._tmp.name) / "users.json"),
                "AIOPS_SESSIONS_FILE": str(Path(self._tmp.name) / "sessions.json"),
                "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
            },
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_seed_admin_and_login(self) -> None:
        users = auth_store.list_users()
        self.assertEqual([u["username"] for u in users], ["admin"])
        self.assertEqual(users[0]["role"], "admin")
        self.assertNotIn("password_hash", users[0])
        # 重新读取不重复播种（登录仍可用）
        session = auth_store.login("admin", ADMIN_PASSWORD)
        self.assertEqual(session["role"], "admin")
        self.assertGreater(len(session["token"]), 20)
        self.assertGreater(session["ttl_seconds"], 0)

    def test_login_failures(self) -> None:
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.login("admin", "wrong-password")
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.login("nobody", "whatever1")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_create_and_duplicate(self) -> None:
        user = auth_store.create_user("u_ops.1", "password123", "operator")
        self.assertEqual(user["role"], "operator")
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.create_user("u_ops.1", "password123", "viewer")
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(auth_store.AuthError):
            auth_store.create_user("u2", "password123", "root")

    def test_role_change_immediate_on_session(self) -> None:
        auth_store.create_user("u1", "password123", "viewer")
        session = auth_store.login("u1", "password123")
        identity, record = auth_store.resolve_session(f"Bearer {session['token']}")
        self.assertEqual(identity.role, "viewer")
        self.assertEqual(record["source"], "session")
        auth_store.update_user("u1", role="operator")
        identity, _ = auth_store.resolve_session(f"Bearer {session['token']}")
        self.assertEqual(identity.role, "operator")  # 角色以用户表为准，立即生效

    def test_disable_invalidates_session_and_login(self) -> None:
        auth_store.create_user("u1", "password123", "viewer")
        session = auth_store.login("u1", "password123")
        auth_store.update_user("u1", state="disabled")
        self.assertIsNone(auth_store.resolve_session(f"Bearer {session['token']}"))
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.login("u1", "password123")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_last_admin_protection(self) -> None:
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.update_user("admin", role="viewer")
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.delete_user("admin")
        self.assertEqual(ctx.exception.status_code, 409)
        # 存在第二个启用管理员后，降级第一个是允许的
        auth_store.create_user("backup", "password123", "admin")
        self.assertEqual(auth_store.update_user("admin", role="viewer")["role"], "viewer")

    def test_session_sliding_expiry(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session = auth_store.login("admin", ADMIN_PASSWORD, now=t0)
        # 不活跃超过 TTL：过期失效
        self.assertIsNone(
            auth_store.resolve_session(
                f"Bearer {session['token']}",
                now=t0 + timedelta(seconds=session["ttl_seconds"] + 1),
            )
        )
        # 滑动续期：窗口内活跃使用 → last_seen 与 expires 顺延
        active = auth_store.resolve_session(
            f"Bearer {session['token']}", now=t0 + timedelta(hours=1)
        )
        self.assertIsNotNone(active)
        record = active[1]
        self.assertEqual(auth_store._parse(record["last_seen_at"]), t0 + timedelta(hours=1))
        renewed = auth_store.resolve_session(
            f"Bearer {session['token']}",
            now=t0 + timedelta(seconds=session["ttl_seconds"] + 1),
        )
        self.assertIsNotNone(renewed)  # 原过期点已顺延，仍有效

    def test_logout_and_revoke(self) -> None:
        s1 = auth_store.login("admin", ADMIN_PASSWORD)
        s2 = auth_store.login("admin", ADMIN_PASSWORD)
        self.assertIsNotNone(auth_store.resolve_session(f"Bearer {s1['token']}"))
        self.assertTrue(auth_store.revoke_session(f"Bearer {s1['token']}"))
        self.assertFalse(auth_store.revoke_session(f"Bearer {s1['token']}"))  # 幂等
        self.assertIsNone(auth_store.resolve_session(f"Bearer {s1['token']}"))
        self.assertEqual(auth_store.revoke_user_sessions("admin"), 1)  # s2 被吊销
        self.assertIsNone(auth_store.resolve_session(f"Bearer {s2['token']}"))
        self.assertFalse(auth_store.revoke_session("Bearer tok-static-not-session"))

    def test_list_sessions_masked(self) -> None:
        session = auth_store.login("admin", ADMIN_PASSWORD)
        sessions = auth_store.list_sessions("admin")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]["id"]), 8)  # 仅指纹前 8 位
        self.assertNotIn("token", sessions[0])
        self.assertNotIn(session["token"], json.dumps(sessions))

    def test_change_own_password(self) -> None:
        auth_store.create_user("u1", "password123", "viewer")
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.change_own_password("u1", "wrong-old", "newpassword1")
        self.assertEqual(ctx.exception.status_code, 401)
        auth_store.change_own_password("u1", "password123", "newpassword1")
        self.assertEqual(auth_store.login("u1", "newpassword1")["user"], "u1")
        with self.assertRaises(auth_store.AuthError):
            auth_store.login("u1", "password123")

    def test_set_password_by_admin(self) -> None:
        auth_store.create_user("u1", "password123", "viewer")
        auth_store.set_password("u1", "resetpass1")
        self.assertEqual(auth_store.login("u1", "resetpass1")["user"], "u1")


class UserDbStoreTest(unittest.TestCase):
    """production DB 后端（SQLite 模拟）：播种 / 登录 / 会话吊销。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "production",
                "AIOPS_DATABASE_URL": f"sqlite:///{Path(self._tmp.name) / 'users.db'}",
                "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
            },
        )
        self._env.start()
        db.reset_engine()

    def tearDown(self) -> None:
        db.reset_engine()
        self._env.stop()
        self._tmp.cleanup()

    def test_seed_login_resolve_revoke(self) -> None:
        self.assertEqual([u["username"] for u in auth_store.list_users()], ["admin"])
        session = auth_store.login("admin", ADMIN_PASSWORD)
        resolved = auth_store.resolve_session(f"Bearer {session['token']}")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[0].role, "admin")
        self.assertEqual(auth_store.revoke_user_sessions("admin"), 1)
        self.assertIsNone(auth_store.resolve_session(f"Bearer {session['token']}"))
        # 会话表跨调用持久（读库路径）
        self.assertEqual(auth_store.list_sessions("admin"), [])

    def test_user_crud_db(self) -> None:
        auth_store.create_user("u1", "password123", "operator")
        self.assertEqual(auth_store.update_user("u1", role="admin")["role"], "admin")
        auth_store.set_password("u1", "changed123")
        self.assertEqual(auth_store.login("u1", "changed123")["role"], "admin")
        auth_store.delete_user("u1")
        with self.assertRaises(auth_store.AuthError) as ctx:
            auth_store.login("u1", "changed123")
        self.assertEqual(ctx.exception.status_code, 401)


class AuthApiTest(unittest.TestCase):
    """BFF 集成（TestClient 真实中间件链）：登录/登出/角色矩阵/静态令牌兼容。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AIOPS_MODE": "demo",
                "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps(TOKENS),
                "AIOPS_CONSOLE_TOKEN_FILE": str(Path(self._tmp.name) / "tokens.json"),
                "AIOPS_USERS_FILE": str(Path(self._tmp.name) / "users.json"),
                "AIOPS_SESSIONS_FILE": str(Path(self._tmp.name) / "sessions.json"),
                "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
                "AIOPS_RATE_LIMIT_ENABLED": "0",
            },
        )
        self._env.start()
        self._patches = [
            mock.patch.object(audit, "DATA_DIR", Path(self._tmp.name)),
            mock.patch.object(monitored_apps, "DATA_PATH", Path(self._tmp.name) / "apps.json"),
        ]
        for patcher in self._patches:
            patcher.start()
        ratelimit.reset()
        from bff.app import app

        self.client = TestClient(app)
        self.admin = {"Authorization": "Bearer tok-adm"}
        self.viewer = {"Authorization": "Bearer tok-view"}

    def tearDown(self) -> None:
        ratelimit.reset()
        for patcher in self._patches:
            patcher.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _login(self, username: str, password: str, expect: int = 200) -> dict:
        response = self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, expect)
        return response.json()

    def _session_headers(self, username: str, password: str) -> dict:
        data = self._login(username, password)
        return {"Authorization": f"Bearer {data['token']}"}

    def test_login_flow_and_self_status(self) -> None:
        headers = self._session_headers("admin", ADMIN_PASSWORD)
        status = self.client.get("/api/auth/status", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["self"]["user"], "admin")
        self.assertEqual(status.json()["self"]["source"], "session")
        # 会话（admin）可访问用户清单
        self.assertEqual(self.client.get("/api/users", headers=headers).status_code, 200)
        # 首启播种落盘
        self.assertTrue((Path(self._tmp.name) / "users.json").is_file())

    def test_login_bad_password_401_and_audited(self) -> None:
        self._login("admin", "wrong-password", expect=401)
        rows = audit.read_audit()
        self.assertEqual(rows[0]["action"], "auth:login")
        self.assertEqual(rows[0]["result"], "denied")

    def test_protected_requires_credential(self) -> None:
        self.assertEqual(self.client.get("/api/auth/status").status_code, 401)
        self.assertEqual(self.client.get("/api/users").status_code, 401)

    def test_logout_revokes_session(self) -> None:
        headers = self._session_headers("admin", ADMIN_PASSWORD)
        logout = self.client.post("/api/auth/logout", headers=headers)
        self.assertEqual(logout.status_code, 200)
        self.assertTrue(logout.json()["revoked"])
        self.assertEqual(self.client.get("/api/auth/status", headers=headers).status_code, 401)

    def test_static_token_compatible(self) -> None:
        status = self.client.get("/api/auth/status", headers=self.admin)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["self"]["source"], "static")
        self.assertEqual(status.json()["self_token"]["user"], "li")
        # 静态令牌写接口角色规则不变：admin 可写、viewer 403
        created = self.client.post(
            "/api/monitored-apps",
            json={"name": "auth-探针", "url": "http://localhost:19999/"},
            headers=self.admin,
        )
        self.assertEqual(created.status_code, 200)
        forbidden = self.client.post(
            "/api/monitored-apps",
            json={"name": "auth-探针2", "url": "http://localhost:19999/"},
            headers=self.viewer,
        )
        self.assertEqual(forbidden.status_code, 403)
        # 静态令牌登出：无会话可吊销，返回提示
        logout = self.client.post("/api/auth/logout", headers=self.admin)
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(logout.json()["revoked"])

    def test_viewer_role_matrix(self) -> None:
        admin_headers = self._session_headers("admin", ADMIN_PASSWORD)
        created = self.client.post(
            "/api/users",
            json={"username": "viewer_u", "password": "password123", "role": "viewer"},
            headers=admin_headers,
        )
        self.assertEqual(created.status_code, 200)
        viewer_headers = self._session_headers("viewer_u", "password123")
        # 读放行、用户管理 403、配置写 403
        self.assertEqual(self.client.get("/api/auth/status", headers=viewer_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/users", headers=viewer_headers).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/users",
                json={"username": "x", "password": "password123", "role": "viewer"},
                headers=viewer_headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/monitored-apps",
                json={"name": "v-探针", "url": "http://localhost:19999/"},
                headers=viewer_headers,
            ).status_code,
            403,
        )

    def test_change_own_password_forces_relogin(self) -> None:
        admin_headers = self._session_headers("admin", ADMIN_PASSWORD)
        self.client.post(
            "/api/users",
            json={"username": "u1", "password": "password123", "role": "viewer"},
            headers=admin_headers,
        )
        headers = self._session_headers("u1", "password123")
        wrong = self.client.post(
            "/api/auth/password",
            json={"old_password": "nope-nope1", "new_password": "newpassword1"},
            headers=headers,
        )
        self.assertEqual(wrong.status_code, 401)
        changed = self.client.post(
            "/api/auth/password",
            json={"old_password": "password123", "new_password": "newpassword1"},
            headers=headers,
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["relogin_required"])
        # 全部会话被吊销（含当前）
        self.assertEqual(self.client.get("/api/auth/status", headers=headers).status_code, 401)
        # 新密码可登录
        self._session_headers("u1", "newpassword1")

    def test_admin_reset_password_forces_offline(self) -> None:
        admin_headers = self._session_headers("admin", ADMIN_PASSWORD)
        self.client.post(
            "/api/users",
            json={"username": "u1", "password": "password123", "role": "operator"},
            headers=admin_headers,
        )
        old_headers = self._session_headers("u1", "password123")
        reset = self.client.post(
            "/api/users/u1/password",
            json={"password": "resetpass1"},
            headers=admin_headers,
        )
        self.assertEqual(reset.status_code, 200)
        self.assertGreaterEqual(reset.json()["revoked_sessions"], 1)
        self.assertEqual(self.client.get("/api/auth/status", headers=old_headers).status_code, 401)
        self._session_headers("u1", "resetpass1")

    def test_self_protection(self) -> None:
        headers = self._session_headers("admin", ADMIN_PASSWORD)
        down = self.client.put("/api/users/admin", json={"role": "viewer"}, headers=headers)
        self.assertEqual(down.status_code, 409)
        disable = self.client.put("/api/users/admin", json={"state": "disabled"}, headers=headers)
        self.assertEqual(disable.status_code, 409)
        delete = self.client.delete("/api/users/admin", headers=headers)
        self.assertEqual(delete.status_code, 409)

    def test_sessions_list_and_revoke_flow(self) -> None:
        admin_headers = self._session_headers("admin", ADMIN_PASSWORD)
        self.client.post(
            "/api/users",
            json={"username": "u1", "password": "password123", "role": "viewer"},
            headers=admin_headers,
        )
        h1 = self._session_headers("u1", "password123")
        h2 = self._session_headers("u1", "password123")
        sessions = self.client.get("/api/users/u1/sessions", headers=admin_headers)
        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(len(sessions.json()["sessions"]), 2)
        revoked = self.client.delete("/api/users/u1/sessions", headers=admin_headers)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["revoked_sessions"], 2)
        self.assertEqual(self.client.get("/api/auth/status", headers=h1).status_code, 401)
        self.assertEqual(self.client.get("/api/auth/status", headers=h2).status_code, 401)

    def test_delete_user_revokes_and_409_self(self) -> None:
        headers = self._session_headers("admin", ADMIN_PASSWORD)
        self.client.post(
            "/api/users",
            json={"username": "u1", "password": "password123", "role": "viewer"},
            headers=headers,
        )
        user_headers = self._session_headers("u1", "password123")
        deleted = self.client.delete("/api/users/u1", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/status", headers=user_headers).status_code, 401)

    def test_login_rate_limited(self) -> None:
        ratelimit.reset()
        with mock.patch.dict(
            os.environ,
            {"AIOPS_RATE_LIMIT_ENABLED": "1", "AIOPS_RATE_LIMIT_AUTH_FAIL_PER_MIN": "3"},
        ):
            for _ in range(3):
                self._login("admin", "wrong-password", expect=401)
            blocked = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("Retry-After", blocked.headers)
        ratelimit.reset()

    def test_unknown_api_returns_401_not_503(self) -> None:
        # 静态注册表存在但未认证 → 统一 401（登录引导），不再 fail-closed 503
        self.assertEqual(self.client.get("/api/nonexistent").status_code, 401)


if __name__ == "__main__":
    unittest.main()
