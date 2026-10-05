"""控制台鉴权与角色权限（Bearer Token + 三角色，支持**自动轮换**）。

背景（评估报告 C1 / B2）：原 BFF 的写接口无鉴权、审计 actor 恒为写死的 ``web-console``。
本模块提供最小可用的身份与角色能力，读接口同样受保护。

角色（最小 RBAC，等级递进）：

======== ================== ============================================
角色      等级                可执行
======== ================== ============================================
viewer    1                  全部读接口
operator  2                  + 审批 / 发布指令 / 批量排队补丁
admin     3                  + 被监控应用 CRUD、令牌轮换（及后续配置项维护）
======== ================== ============================================

令牌存储（**自动轮换**，双后端）：
    - **demo**：运行时注册表放在 ``data/console_tokens.json``（权限 600，原子写），
      加载时**每次读盘**——与控制台「配置热生效」一致，轮换后无需重启；
    - **production**：注册表落 MySQL ``console_tokens`` 表（仅存 sha256 令牌指纹，无明文），
      元数据（轮换时间等）存 ``system_kv``；校验侧带短 TTL 缓存（默认 2s 可配），
      轮换/写入后立即失效缓存，兼顾性能与热生效；
    - 环境变量 ``AIOPS_CONSOLE_AUTH_TOKENS`` 仅作**首次播种**（已有数据时不覆盖）；
    - 每轮轮换：为每个身份生成新令牌（active），旧令牌转 ``previous`` 并保留**宽限期**，
      因此自动轮换不会把在用的运维"锁在门外"；超宽限期的 previous 会被清理。

安全默认（**fail-closed**）：注册表不可用（文件缺失且 env 未配置 / JSON 非法 /
生产数据库不可达）时，所有 ``/api`` 请求返回 **503** 并提示如何配置——绝不在"未配置"状态下放行。

配置::

    AIOPS_CONSOLE_AUTH_TOKENS='{"tok-ops":{"user":"zhang","role":"operator"}}'   # 首次播种
    AIOPS_CONSOLE_TOKEN_FILE=data/console_tokens.json                            # demo 注册表路径
    AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS=2592000                                  # 轮换间隔（默认 30 天；0=关闭）
    AIOPS_TOKEN_GRACE_SECONDS=604800                                             # 宽限期（默认 7 天）
    AIOPS_TOKEN_ALLOW_REVEAL=false                                               # 是否允许经接口回显令牌值（默认否）
    AIOPS_TOKEN_CACHE_SECONDS=2                                                  # production 校验缓存 TTL（秒；0=禁用）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Header, HTTPException
from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

ENV_TOKENS = "AIOPS_CONSOLE_AUTH_TOKENS"
ENV_FILE = "AIOPS_CONSOLE_TOKEN_FILE"
ENV_INTERVAL = "AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS"
ENV_GRACE = "AIOPS_TOKEN_GRACE_SECONDS"
ENV_REVEAL = "AIOPS_TOKEN_ALLOW_REVEAL"

DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "data" / "console_tokens.json"
DEFAULT_INTERVAL_SECONDS = 30 * 24 * 3600  # 30 天
DEFAULT_GRACE_SECONDS = 7 * 24 * 3600  # 7 天

# 角色等级（数值越大权限越高）
ROLE_ORDER: dict[str, int] = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass(frozen=True)
class Identity:
    """已认证身份（仅用于授权判定与审计留痕）。"""

    user: str
    role: str

    @property
    def level(self) -> int:
        return ROLE_ORDER.get(self.role, 0)


class AuthConfigError(RuntimeError):
    """令牌注册表缺失或非法（由守卫转 503，fail-closed）。"""


# ----------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------


def registry_path() -> Path:
    return Path(os.environ.get(ENV_FILE) or DEFAULT_REGISTRY)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise AuthConfigError(f"{name} 必须是整数秒：{raw!r}") from exc


def rotation_interval() -> int:
    """轮换间隔（秒）；0 表示关闭自动轮换。"""
    return _int_env(ENV_INTERVAL, DEFAULT_INTERVAL_SECONDS)


def grace_seconds() -> int:
    return _int_env(ENV_GRACE, DEFAULT_GRACE_SECONDS)


def allow_reveal() -> bool:
    """是否允许经接口回显令牌值（默认否：令牌只落在 600 权限的注册表文件里）。"""
    return (os.environ.get(ENV_REVEAL) or "").strip().lower() in ("1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _expiry(moment: datetime, interval: int, grace: int) -> str | None:
    """计划失效时间；关闭自动轮换（interval=0）时**不设**失效时间。

    轮换关闭时 active 令牌不会自动过期（``registry_tokens`` 对 active 不做时间判定），
    因此此处返回 None，避免界面显示出"7 天后失效"这类与实际不符的信息。
    """
    if interval <= 0:
        return None
    return _iso(moment + timedelta(seconds=interval + grace))


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 纯解析（供播种与单测）
# ----------------------------------------------------------------------


def load_tokens(raw: str | None = None) -> dict[str, Identity]:
    """解析 ``{token: {user, role}}`` 形式的注册表字符串（纯函数）。"""
    text = raw if raw is not None else os.environ.get(ENV_TOKENS)
    if not text or not text.strip():
        raise AuthConfigError(
            f"未配置 {ENV_TOKENS}（且注册表文件不存在），控制台处于 fail-closed 状态，拒绝所有 /api 请求。"
            f' 配置示例：{ENV_TOKENS}=\'{{"tok-ops":{{"user":"zhang","role":"operator"}}}}\''
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"{ENV_TOKENS} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not data:
        raise AuthConfigError(f"{ENV_TOKENS} 必须是非空 JSON 对象（token → {{user, role}}）")

    tokens: dict[str, Identity] = {}
    for token, spec in data.items():
        if not isinstance(spec, dict):
            raise AuthConfigError(f"token 条目必须是对象：{token!r}")
        user, role = spec.get("user"), str(spec.get("role", "")).lower()
        if not user:
            raise AuthConfigError(f"token 条目缺少 user：{token!r}")
        if role not in ROLE_ORDER:
            raise AuthConfigError(
                f"token 条目 role 非法：{token!r} → {role!r}（可选 {'/'.join(ROLE_ORDER)}）"
            )
        tokens[str(token)] = Identity(user=str(user), role=role)
    return tokens


# ----------------------------------------------------------------------
# 文件注册表
# ----------------------------------------------------------------------


def _seed_records(moment: datetime | None = None) -> list[dict]:
    """由环境变量播种令牌记录。"""
    moment = moment or _now()
    interval, grace = rotation_interval(), grace_seconds()
    records = []
    for token, identity in load_tokens().items():
        records.append(
            {
                "token": token,
                "user": identity.user,
                "role": identity.role,
                "state": "active",
                "created_at": _iso(moment),
                "expires_at": _expiry(moment, interval, grace),
                "rotated_from": "env",
            }
        )
    return records


def _read_registry(moment: datetime | None = None) -> dict:
    if mode.is_production():
        return _read_registry_db(moment or _now())
    path = registry_path()
    if not path.is_file():
        moment = moment or _now()
        registry = {
            "version": 1,
            # 播种即视为**首次发放**：否则 rotated_at 为空时 due 恒为 False，
            # 自动轮换永远不会启动（早期实现的缺陷）。
            "rotated_at": _iso(moment),
            "interval_seconds": rotation_interval(),
            "grace_seconds": grace_seconds(),
            "tokens": _seed_records(moment),
        }
        _write_registry(registry)
        return registry
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"令牌注册表不可读或非法（{path}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
        raise AuthConfigError(f"令牌注册表结构非法：{path}")
    return data


def _write_registry(registry: dict) -> None:
    """demo：原子写 + 0600 权限（注册表含明文令牌，必须仅本用户可读）；production 转发 DB。"""
    if mode.is_production():
        _write_registry_db(registry)
        return
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ----------------------------------------------------------------------
# DB 后端（production）：console_tokens 表 + system_kv 元数据
# ----------------------------------------------------------------------

_KV_META = "console_tokens:meta"
DEFAULT_CACHE_SECONDS = 2.0


def _token_key(token: str) -> str:
    """写库键：明文 → sha256 hex；已是 64 位 hex（读库回填值）→ 原样返回。"""
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    return hashlib.sha256(token.encode()).hexdigest()


def _iso_from_db(value: datetime | None) -> str | None:
    """库内 naive UTC datetime → auth 口径的秒级 ISO 字符串。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _record_from_row(row) -> dict:
    """DB 行 → 文件版同构的令牌记录（``token`` 为 sha256 指纹，库内无明文）。"""
    return {
        "token": row["token_hash"],
        "user": row["user_name"],
        "role": row["role"],
        "state": row["state"],
        "created_at": _iso_from_db(row["created_at"]),
        "expires_at": _iso_from_db(row["expires_at"]),
        "retired_at": _iso_from_db(row["retired_at"]),
        "rotated_from": row["rotated_from"],
    }


def _stored_record(record: dict) -> dict:
    """明文记录 → 存储视图（token 替换为指纹，与读库结果一致）。"""
    return {**record, "token": _token_key(record["token"])}


def _replace_records_db(conn, records: list[dict]) -> None:
    """单事务全量替换令牌记录（注册表低容量）；写库前统一转 sha256 指纹。"""
    conn.execute(db_layer.console_tokens.delete())
    now = db_layer.now_dt()
    if not records:
        return
    conn.execute(
        db_layer.console_tokens.insert(),
        [
            {
                "token_hash": _token_key(record["token"]),
                "user_name": record["user"],
                "role": record["role"],
                "state": record.get("state") or "active",
                "created_at": db_layer.to_dt(record.get("created_at")) or now,
                "expires_at": db_layer.to_dt(record.get("expires_at")),
                "retired_at": db_layer.to_dt(record.get("retired_at")),
                "rotated_from": record.get("rotated_from"),
            }
            for record in records
        ],
    )


def _write_meta_db(conn, meta: dict) -> None:
    conn.execute(db_layer.system_kv.delete().where(db_layer.system_kv.c.kv_key == _KV_META))
    conn.execute(
        db_layer.system_kv.insert().values(
            kv_key=_KV_META,
            kv_value=json.dumps(meta, ensure_ascii=False),
            updated_at=db_layer.now_dt(),
        )
    )


def _read_meta_db(conn) -> dict:
    raw = conn.execute(
        select(db_layer.system_kv.c.kv_value).where(db_layer.system_kv.c.kv_key == _KV_META)
    ).scalar()
    meta = {
        "version": 1,
        "rotated_at": None,
        "interval_seconds": rotation_interval(),
        "grace_seconds": grace_seconds(),
    }
    if raw:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            meta.update(loaded)
    return meta


def _read_registry_db(moment: datetime) -> dict:
    """DB 注册表读取（结构对齐文件版）；空表时用环境变量播种。"""
    try:
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            rows = (
                conn.execute(select(db_layer.console_tokens).order_by(db_layer.console_tokens.c.id))
                .mappings()
                .all()
            )
            if not rows:
                seeded = _seed_records(moment)
                _replace_records_db(conn, seeded)
                meta = {
                    "version": 1,
                    # 播种即视为首次发放（与文件版一致的既有修复：否则自动轮换永不启动）
                    "rotated_at": _iso(moment),
                    "interval_seconds": rotation_interval(),
                    "grace_seconds": grace_seconds(),
                }
                _write_meta_db(conn, meta)
                return {**meta, "tokens": [_stored_record(record) for record in seeded]}
            meta = _read_meta_db(conn)
    except Exception as exc:  # noqa: BLE001 - DB 不可用一律 fail-closed（守卫转 503）
        raise AuthConfigError(f"令牌注册表（数据库）不可用：{exc}") from exc
    return {**meta, "tokens": [_record_from_row(row) for row in rows]}


def _write_registry_db(registry: dict) -> None:
    try:
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            _replace_records_db(conn, registry["tokens"])
            _write_meta_db(conn, {key: value for key, value in registry.items() if key != "tokens"})
    except Exception as exc:  # noqa: BLE001
        raise AuthConfigError(f"令牌注册表（数据库）写入失败：{exc}") from exc
    invalidate_registry_cache()


# ----------------------------------------------------------------------
# 校验缓存（仅 production：避免每请求一次读库；轮换/写入后立即失效）
# ----------------------------------------------------------------------

_registry_cache: tuple[float, dict[str, tuple[Identity, dict]]] | None = None


def cache_seconds() -> float:
    raw = os.environ.get("AIOPS_TOKEN_CACHE_SECONDS")
    if not raw or not str(raw).strip():
        return DEFAULT_CACHE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CACHE_SECONDS


def invalidate_registry_cache() -> None:
    """清空校验缓存（轮换 / 写入后调用，避免新令牌在 TTL 内不可用）。"""
    global _registry_cache
    _registry_cache = None


def registry_tokens(now: datetime | None = None) -> dict[str, tuple[Identity, dict]]:
    """运行时可用令牌：active 全部 + previous 且仍在宽限期内。

    demo 每次读盘 → 轮换后立即生效，无需重启；
    production 读库并带短 TTL 缓存（默认 2s，``AIOPS_TOKEN_CACHE_SECONDS`` 可调；0=禁用），
    写入 / 轮换后缓存立即失效。
    """
    global _registry_cache
    moment = now or _now()
    if mode.is_production():
        cached = _registry_cache
        if cache_seconds() > 0 and cached is not None and (time.monotonic() - cached[0]) < cache_seconds():
            return cached[1]
    registry = _read_registry(moment)
    grace = int(registry.get("grace_seconds") or grace_seconds())
    out: dict[str, tuple[Identity, dict]] = {}
    for record in registry["tokens"]:
        state = record.get("state")
        if state == "active":
            out[record["token"]] = (Identity(record["user"], record["role"]), record)
            continue
        if state != "previous":
            continue
        retired = _parse(record.get("retired_at"))
        if retired and (moment - retired).total_seconds() <= grace:
            out[record["token"]] = (Identity(record["user"], record["role"]), record)
    if mode.is_production():
        _registry_cache = (time.monotonic(), out)
    return out


def rotate(now: datetime | None = None, *, reason: str = "manual") -> dict:
    """执行一次轮换：生成新令牌，旧令牌转 previous（保留宽限期），清理超期项。"""
    moment = now or _now()
    interval, grace = rotation_interval(), grace_seconds()
    registry = _read_registry(moment)
    records: list[dict] = registry["tokens"]

    identities = {(r["user"], r["role"]) for r in records if r.get("state") == "active"}
    if not identities:
        # 没有 active（异常状态）：从全部历史身份兜底，避免轮换后无可用令牌
        identities = {(r["user"], r["role"]) for r in records} or {("admin", "admin")}

    for record in records:
        if record.get("state") == "active":
            record["state"] = "previous"
            record["retired_at"] = _iso(moment)

    created: list[dict] = []
    for user, role in sorted(identities):
        new_record = {
            "token": secrets.token_urlsafe(32),
            "user": user,
            "role": role,
            "state": "active",
            "created_at": _iso(moment),
            "expires_at": _expiry(moment, interval, grace),
            "rotated_from": reason,
        }
        records.append(new_record)
        created.append(new_record)

    # 清理：active 全留；previous 超过宽限期则移除
    kept = [
        record
        for record in records
        if record.get("state") == "active"
        or (
            (retired := _parse(record.get("retired_at"))) is not None
            and (moment - retired).total_seconds() <= grace
        )
    ]

    registry.update(
        {
            "version": 1,
            "rotated_at": _iso(moment),
            "interval_seconds": interval,
            "grace_seconds": grace,
            "rotation_reason": reason,
            "tokens": kept,
        }
    )
    _write_registry(registry)

    return {
        "rotated_at": _iso(moment),
        "reason": reason,
        "grace_seconds": grace,
        "interval_seconds": interval,
        "next_rotation_at": _iso(moment + timedelta(seconds=interval)) if interval else None,
        "identities": [{"user": user, "role": role} for user, role in sorted(identities)],
        "new_tokens": [
            {"token": r["token"], "user": r["user"], "role": r["role"]} for r in created
        ],
        "revoked": len(kept) - len(created),  # 本次转 previous 的数量（含被清理的）
    }


def rotation_status(now: datetime | None = None) -> dict:
    """轮换状态：上次/下次时间、是否到期、宽限期与令牌清单（**不含令牌值**）。"""
    moment = now or _now()
    registry = _read_registry(moment)
    interval = int(registry.get("interval_seconds") or rotation_interval())
    grace = int(registry.get("grace_seconds") or grace_seconds())
    last = _parse(registry.get("rotated_at"))
    due_at = (last + timedelta(seconds=interval)) if (last and interval) else None

    tokens = []
    for record in registry["tokens"]:
        tokens.append(
            {
                # 指纹：sha256 前 8 位，仅供区分不同令牌，**不可反推**（不回显任何令牌片段）
                "id": hashlib.sha256(record["token"].encode()).hexdigest()[:8],
                "user": record["user"],
                "role": record["role"],
                "state": record.get("state"),
                "created_at": record.get("created_at"),
                "expires_at": record.get("expires_at"),
                "retired_at": record.get("retired_at"),
            }
        )
    return {
        "auto_rotation_enabled": bool(interval),
        "interval_seconds": interval,
        "grace_seconds": grace,
        "last_rotated_at": registry.get("rotated_at"),
        "next_rotation_at": _iso(due_at) if due_at else None,
        "due": bool(due_at and moment >= due_at),
        "registry_path": str(registry_path()) if mode.is_demo() else "mysql://console_tokens",
        "allow_reveal": allow_reveal(),
        "tokens": tokens,
    }


def resolve_token(
    authorization: str | None, *, tokens: dict[str, tuple[Identity, dict]]
) -> tuple[Identity, dict]:
    """从 Bearer 头解析身份，并返回命中的令牌记录（用于告知调用方自身令牌状态）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="缺少 Bearer 凭证（Authorization: Bearer <token>）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    matched = tokens.get(token)
    if matched is None:
        # production 注册表仅存 sha256 指纹：按指纹再匹配一次（demo 为明文键，不受影响）
        matched = tokens.get(_token_key(token))
    if matched is None:
        raise HTTPException(
            status_code=401, detail="凭证无效或已过期", headers={"WWW-Authenticate": "Bearer"}
        )
    return matched


def resolve_identity(authorization: str | None, *, tokens: dict[str, Identity]) -> Identity:
    """从 ``Authorization: Bearer`` 头解析身份；缺失或非法抛 401（纯函数版，供单测）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="缺少 Bearer 凭证（Authorization: Bearer <token>）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    identity = tokens.get(token)
    if identity is None:
        raise HTTPException(
            status_code=401, detail="凭证无效", headers={"WWW-Authenticate": "Bearer"}
        )
    return identity


def require_role(min_role: str):
    """FastAPI 依赖工厂：校验凭证（读注册表）并断言角色不低于 ``min_role``。"""
    if min_role not in ROLE_ORDER:
        raise ValueError(f"未知角色等级：{min_role}")

    def _guard(authorization: str | None = Header(default=None)) -> Identity:
        try:
            tokens = registry_tokens()
        except AuthConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        identity, _record = resolve_token(authorization, tokens=tokens)
        if identity.level < ROLE_ORDER[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"角色 {identity.role} 无权执行该操作（要求 {min_role}）",
            )
        return identity

    return _guard


# ======================================================================
# 用户与会话（用户名+密码登录；与静态令牌**并存**，校验顺序：会话 → 静态令牌）
# ======================================================================
#
# 设计说明：
# - 用户存储双后端：demo=``data/console_users.json``（600，原子写，不存在时播种 admin）；
#   production=MySQL ``console_users`` 表（空表播种）。
# - 会话令牌与静态令牌同走 ``Authorization: Bearer``：会话命中则身份以**用户表**为准
#   （改角色/禁用即时生效）；未命中回落静态令牌（自动化/应急通道）。
# - 会话仅存 sha256 指纹；滑动续期按节流写盘，避免每次轮询落盘。
# - 密码：pbkdf2_sha256（stdlib，零新依赖），错误信息不区分"用户不存在/密码错误"。
#
# 配置：：
#
#     AIOPS_USERS_FILE=data/console_users.json              # demo 用户注册表路径
#     AIOPS_SESSIONS_FILE=data/console_sessions.json        # demo 会话注册表路径
#     AIOPS_SESSION_TTL_SECONDS=43200                       # 会话有效期（默认 12 小时）
#     AIOPS_SESSION_TOUCH_SECONDS=60                        # 滑动续期写盘节流（默认 60s）
#     AIOPS_BOOTSTRAP_ADMIN_PASSWORD=admin123               # 首启管理员密码（生产必须修改）

ENV_USERS_FILE = "AIOPS_USERS_FILE"
ENV_SESSIONS_FILE = "AIOPS_SESSIONS_FILE"
ENV_SESSION_TTL = "AIOPS_SESSION_TTL_SECONDS"
ENV_SESSION_TOUCH = "AIOPS_SESSION_TOUCH_SECONDS"
ENV_BOOTSTRAP_PASSWORD = "AIOPS_BOOTSTRAP_ADMIN_PASSWORD"

DEFAULT_USERS_PATH = Path(__file__).resolve().parent.parent / "data" / "console_users.json"
DEFAULT_SESSIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "console_sessions.json"
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600
DEFAULT_SESSION_TOUCH_SECONDS = 60.0
BOOTSTRAP_ADMIN_USER = "admin"
DEFAULT_BOOTSTRAP_PASSWORD = "admin123"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 210_000


class AuthError(Exception):
    """用户/会话业务错误（路由层转 HTTP 状态码）。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def users_path() -> Path:
    return Path(os.environ.get(ENV_USERS_FILE) or DEFAULT_USERS_PATH)


def sessions_path() -> Path:
    return Path(os.environ.get(ENV_SESSIONS_FILE) or DEFAULT_SESSIONS_PATH)


def session_ttl() -> int:
    """会话有效期（秒）；0/未配置回退默认。"""
    return _int_env(ENV_SESSION_TTL, DEFAULT_SESSION_TTL_SECONDS) or DEFAULT_SESSION_TTL_SECONDS


def session_touch_seconds() -> float:
    """滑动续期写盘节流（秒）。"""
    raw = os.environ.get(ENV_SESSION_TOUCH)
    if not raw or not str(raw).strip():
        return DEFAULT_SESSION_TOUCH_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_SESSION_TOUCH_SECONDS


# ----------------------------------------------------------------------
# 密码哈希（pbkdf2_sha256）与输入校验
# ----------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；存储格式非法一律 False（绝不抛异常）。"""
    try:
        algo, iterations, salt, expected = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def validate_username(username: str) -> str:
    value = (username or "").strip()
    if not USERNAME_PATTERN.match(value):
        raise AuthError("用户名须为 2-32 位字母/数字/._- 组合", 400)
    return value


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < PASSWORD_MIN_LENGTH:
        raise AuthError(f"密码长度不能少于 {PASSWORD_MIN_LENGTH} 位", 400)
    return value


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """时序均衡：用户不存在时也执行一次等价哈希，避免用户名枚举。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")
    return _DUMMY_HASH


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization.split(" ", 1)[1].strip()


# ----------------------------------------------------------------------
# 用户注册表（双后端）
# ----------------------------------------------------------------------


def _seed_admin_records(moment: datetime) -> list[dict]:
    """首启播种管理员（密码取 AIOPS_BOOTSTRAP_ADMIN_PASSWORD，默认 admin123）。"""
    password = os.environ.get(ENV_BOOTSTRAP_PASSWORD) or DEFAULT_BOOTSTRAP_PASSWORD
    now_iso = _iso(moment)
    return [
        {
            "username": BOOTSTRAP_ADMIN_USER,
            "password_hash": hash_password(password),
            "role": "admin",
            "state": "active",
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    ]


def _user_from_row(row) -> dict:
    """DB 行 → 文件版同构的用户记录。"""
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "state": row["state"],
        "created_at": _iso_from_db(row["created_at"]),
        "updated_at": _iso_from_db(row["updated_at"]),
    }


def _replace_users_db(conn, records: list[dict]) -> None:
    conn.execute(db_layer.console_users.delete())
    if not records:
        return
    now = db_layer.now_dt()
    conn.execute(
        db_layer.console_users.insert(),
        [
            {
                "username": record["username"],
                "password_hash": record["password_hash"],
                "role": record["role"],
                "state": record.get("state") or "active",
                "created_at": db_layer.to_dt(record.get("created_at")) or now,
                "updated_at": db_layer.to_dt(record.get("updated_at")) or now,
            }
            for record in records
        ],
    )


def _read_users_db(moment: datetime) -> dict[str, dict]:
    """DB 用户读取；空表时播种管理员。"""
    try:
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            rows = conn.execute(select(db_layer.console_users)).mappings().all()
            if not rows:
                seeded = _seed_admin_records(moment)
                _replace_users_db(conn, seeded)
                return {record["username"]: record for record in seeded}
    except Exception as exc:  # noqa: BLE001 - 不可用一律 fail-closed
        raise AuthConfigError(f"用户注册表（数据库）不可用：{exc}") from exc
    return {row["username"]: _user_from_row(row) for row in rows}


def _read_users(moment: datetime | None = None) -> dict[str, dict]:
    """用户注册表：demo 读文件（不存在则播种 admin）；production 读 DB（空表播种）。"""
    if mode.is_production():
        return _read_users_db(moment or _now())
    path = users_path()
    if not path.is_file():
        records = _seed_admin_records(moment or _now())
        _write_users({record["username"]: record for record in records})
        return {record["username"]: record for record in records}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"用户注册表不可读或非法（{path}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise AuthConfigError(f"用户注册表结构非法：{path}")
    return {
        str(record["username"]): record
        for record in data["users"]
        if isinstance(record, dict) and record.get("username")
    }


def _write_users(users: dict[str, dict]) -> None:
    """demo：原子写 + 0600（含密码哈希）；production 转发 DB。"""
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            _replace_users_db(conn, list(users.values()))
        return
    path = users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "users": list(users.values())}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ----------------------------------------------------------------------
# 会话注册表（双后端；仅存 sha256 指纹）
# ----------------------------------------------------------------------


def _session_from_row(row) -> dict:
    return {
        "token_hash": row["token_hash"],
        "user": row["user_name"],
        "role": row["role"],
        "created_at": _iso_from_db(row["created_at"]),
        "expires_at": _iso_from_db(row["expires_at"]),
        "last_seen_at": _iso_from_db(row["last_seen_at"]),
        "revoked": bool(row["revoked"]),
        "revoked_at": _iso_from_db(row["revoked_at"]),
    }


def _replace_sessions_db(conn, sessions: list[dict]) -> None:
    conn.execute(db_layer.console_sessions.delete())
    if not sessions:
        return
    now = db_layer.now_dt()
    conn.execute(
        db_layer.console_sessions.insert(),
        [
            {
                "token_hash": session["token_hash"],
                "user_name": session["user"],
                "role": session["role"],
                "created_at": db_layer.to_dt(session.get("created_at")) or now,
                "expires_at": db_layer.to_dt(session.get("expires_at")) or now,
                "last_seen_at": db_layer.to_dt(session.get("last_seen_at")),
                "revoked": bool(session.get("revoked")),
                "revoked_at": db_layer.to_dt(session.get("revoked_at")),
            }
            for session in sessions
        ],
    )


def _read_sessions_db() -> list[dict]:
    try:
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(select(db_layer.console_sessions)).mappings().all()
    except Exception as exc:  # noqa: BLE001
        raise AuthConfigError(f"会话注册表（数据库）不可用：{exc}") from exc
    return [_session_from_row(row) for row in rows]


def _read_sessions() -> list[dict]:
    """会话注册表：demo 读文件（不存在=空，不播种）；production 读 DB。"""
    if mode.is_production():
        return _read_sessions_db()
    path = sessions_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"会话注册表不可读或非法（{path}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise AuthConfigError(f"会话注册表结构非法：{path}")
    return [session for session in data["sessions"] if isinstance(session, dict)]


def _write_sessions(sessions: list[dict]) -> None:
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            _replace_sessions_db(conn, sessions)
        return
    path = sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "sessions": sessions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _prune_sessions(sessions: list[dict], moment: datetime) -> list[dict]:
    """剔除已过期会话（被吊销但未过期的保留，供界面查看后自然过期）。"""
    kept: list[dict] = []
    for session in sessions:
        expires = _parse(session.get("expires_at"))
        if expires is None or expires > moment:
            kept.append(session)
    return kept


# ----------------------------------------------------------------------
# 登录 / 登出 / 会话校验
# ----------------------------------------------------------------------


def create_session(username: str, role: str, *, now: datetime | None = None) -> dict:
    """签发会话令牌（明文仅返回一次；注册表只存 sha256 指纹）。"""
    moment = now or _now()
    token = secrets.token_urlsafe(32)
    ttl = session_ttl()
    record = {
        "token_hash": _token_key(token),
        "user": username,
        "role": role,
        "created_at": _iso(moment),
        "expires_at": _iso(moment + timedelta(seconds=ttl)),
        "last_seen_at": _iso(moment),
        "revoked": False,
    }
    sessions = _prune_sessions(_read_sessions(), moment)
    sessions.append(record)
    _write_sessions(sessions)
    return {
        "token": token,
        "user": username,
        "role": role,
        "expires_at": record["expires_at"],
        "ttl_seconds": ttl,
    }


def login(username: str, password: str, *, now: datetime | None = None) -> dict:
    """用户名+密码校验并签发会话；失败抛 AuthError（401/403）。"""
    moment = now or _now()
    users = _read_users(moment)
    record = users.get(username)
    if record is None:
        verify_password(password, _dummy_hash())  # 时序均衡，防用户名枚举
        raise AuthError("用户名或密码错误", 401)
    if record.get("state") != "active":
        raise AuthError("账户已被禁用，请联系管理员", 403)
    if not verify_password(password, record.get("password_hash", "")):
        raise AuthError("用户名或密码错误", 401)
    return create_session(str(record["username"]), str(record["role"]), now=moment)


def resolve_session(
    authorization: str | None, *, now: datetime | None = None
) -> tuple[Identity, dict] | None:
    """解析会话令牌：命中且有效返回 ``(Identity, 记录)``；未命中/过期/吊销返回 None。

    - 角色以**用户表**为准：管理员改角色/禁用后立即生效，无需等会话过期；
    - 滑动续期：last_seen 更新与 expires 顺延按 ``AIOPS_SESSION_TOUCH_SECONDS``
      节流写盘，避免每次轮询都落盘。
    """
    token = _bearer_token(authorization)
    if not token:
        return None
    moment = now or _now()
    fingerprint = _token_key(token)
    sessions = _read_sessions()
    hit = next((item for item in sessions if item.get("token_hash") == fingerprint), None)
    if hit is None or hit.get("revoked"):
        return None
    expires = _parse(hit.get("expires_at"))
    if expires is None or moment >= expires:
        return None
    users = _read_users(moment)
    user = users.get(str(hit.get("user")))
    if user is None or user.get("state") != "active":
        return None  # 用户被删除/禁用：会话即时失效
    last_seen = _parse(hit.get("last_seen_at"))
    if last_seen is None or (moment - last_seen).total_seconds() >= session_touch_seconds():
        hit["last_seen_at"] = _iso(moment)
        hit["expires_at"] = _iso(moment + timedelta(seconds=session_ttl()))
        _write_sessions(sessions)
    return Identity(user=str(user["username"]), role=str(user["role"])), {
        **hit,
        "state": "active",
        "source": "session",
    }


def revoke_session(authorization: str | None, *, now: datetime | None = None) -> bool:
    """吊销 Bearer 对应的会话；返回是否命中会话（静态令牌返回 False）。"""
    token = _bearer_token(authorization)
    if not token:
        return False
    moment = now or _now()
    fingerprint = _token_key(token)
    sessions = _read_sessions()
    changed = False
    for session in sessions:
        if session.get("token_hash") == fingerprint and not session.get("revoked"):
            session["revoked"] = True
            session["revoked_at"] = _iso(moment)
            changed = True
    if changed:
        _write_sessions(sessions)
    return changed


def revoke_user_sessions(username: str, *, now: datetime | None = None) -> int:
    """吊销某用户全部有效会话（强制下线）；返回吊销数量。"""
    moment = now or _now()
    sessions = _read_sessions()
    count = 0
    for session in sessions:
        if session.get("user") != username or session.get("revoked"):
            continue
        expires = _parse(session.get("expires_at"))
        if expires is None or expires > moment:
            session["revoked"] = True
            session["revoked_at"] = _iso(moment)
            count += 1
    if count:
        _write_sessions(sessions)
    return count


def list_sessions(username: str | None = None, *, now: datetime | None = None) -> list[dict]:
    """在线会话清单（脱敏：仅指纹前 8 位）；可按用户过滤。"""
    moment = now or _now()
    sessions = _prune_sessions(_read_sessions(), moment)
    out: list[dict] = []
    for session in sessions:
        if username and session.get("user") != username:
            continue
        if session.get("revoked"):
            continue
        out.append(
            {
                "id": str(session.get("token_hash"))[:8],
                "user": session.get("user"),
                "role": session.get("role"),
                "created_at": session.get("created_at"),
                "expires_at": session.get("expires_at"),
                "last_seen_at": session.get("last_seen_at"),
            }
        )
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out


# ----------------------------------------------------------------------
# 用户管理（管理员操作；规则：不能移除最后一个启用管理员）
# ----------------------------------------------------------------------


def _public_user(record: dict) -> dict:
    """对外视图：绝不包含 password_hash。"""
    return {
        "username": record.get("username"),
        "role": record.get("role"),
        "state": record.get("state"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _assert_admin_survives(users: dict[str, dict]) -> None:
    if not any(
        user.get("role") == "admin" and user.get("state") == "active"
        for user in users.values()
    ):
        raise AuthError("操作被拒绝：系统至少需要保留一个启用状态的管理员", 409)


def list_users() -> list[dict]:
    users = _read_users()
    return [
        _public_user(record)
        for record in sorted(users.values(), key=lambda item: str(item.get("username")))
    ]


def create_user(
    username: str, password: str, role: str, *, now: datetime | None = None
) -> dict:
    moment = now or _now()
    clean_user = validate_username(username)
    validate_password(password)
    if role not in ROLE_ORDER:
        raise AuthError(f"角色非法：{role!r}（可选 {'/'.join(ROLE_ORDER)}）", 400)
    users = _read_users(moment)
    if clean_user in users:
        raise AuthError(f"用户已存在：{clean_user}", 409)
    record = {
        "username": clean_user,
        "password_hash": hash_password(password),
        "role": role,
        "state": "active",
        "created_at": _iso(moment),
        "updated_at": _iso(moment),
    }
    users[clean_user] = record
    _write_users(users)
    return _public_user(record)


def update_user(
    username: str,
    *,
    role: str | None = None,
    state: str | None = None,
    now: datetime | None = None,
) -> dict:
    moment = now or _now()
    users = _read_users(moment)
    record = users.get(username)
    if record is None:
        raise AuthError(f"用户不存在：{username}", 404)
    if role is not None:
        if role not in ROLE_ORDER:
            raise AuthError(f"角色非法：{role!r}（可选 {'/'.join(ROLE_ORDER)}）", 400)
        record["role"] = role
    if state is not None:
        if state not in ("active", "disabled"):
            raise AuthError(f"状态非法：{state!r}（可选 active/disabled）", 400)
        record["state"] = state
    record["updated_at"] = _iso(moment)
    _assert_admin_survives(users)
    _write_users(users)
    return _public_user(record)


def delete_user(username: str, *, now: datetime | None = None) -> None:
    users = _read_users(now or _now())
    if username not in users:
        raise AuthError(f"用户不存在：{username}", 404)
    del users[username]
    _assert_admin_survives(users)
    _write_users(users)


def set_password(username: str, password: str, *, now: datetime | None = None) -> None:
    """管理员重置密码（无需原密码）。"""
    moment = now or _now()
    validate_password(password)
    users = _read_users(moment)
    record = users.get(username)
    if record is None:
        raise AuthError(f"用户不存在：{username}", 404)
    record["password_hash"] = hash_password(password)
    record["updated_at"] = _iso(moment)
    _write_users(users)


def change_own_password(
    username: str, old_password: str, new_password: str, *, now: datetime | None = None
) -> None:
    """本人修改密码（必须提供原密码）。"""
    moment = now or _now()
    users = _read_users(moment)
    record = users.get(username)
    if record is None:
        raise AuthError(f"用户不存在：{username}", 404)
    if not verify_password(old_password, record.get("password_hash", "")):
        raise AuthError("原密码不正确", 401)
    validate_password(new_password)
    record["password_hash"] = hash_password(new_password)
    record["updated_at"] = _iso(moment)
    _write_users(users)
