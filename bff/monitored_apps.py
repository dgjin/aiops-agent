"""被监控应用清单（控制台可维护；**热生效**）。

双后端（AIOPS_MODE 决定）：
    - **demo**：存储为 ``data/monitored_apps.json``，**每次读取都从磁盘重新解析**——
      控制台增删改后无需重启 BFF 立即生效（「热生效」：不做进程级缓存）；
    - **production**：存储为 MySQL ``monitored_apps`` 表，每次查询即读库（同样热生效），
      支持与生产审计/流程数据同库聚合；采集器（ship_app_logs）读同一份清单。

通用设计要点：
    - 首次访问且无数据时，用 ``AIOPS_MONITOR_URL`` / ``AIOPS_MONITOR_SERVICE`` 播种一条默认项
      （文件版：文件不存在；DB 版：``system_kv`` 播种标记 + 空表）；
    - 文件写盘使用「临时文件 + 原子替换」；DB 写入在单事务内全量替换（清单低容量、低频写）；
    - 校验：名称非空且唯一、地址必须带 http(s) scheme。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from aiops_agent import db as db_layer
from aiops_agent import mode

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（既有数据零迁移）
DATA_PATH = DATA_DIR / "monitored_apps.json"
PROBE_STATUS_PATH = DATA_DIR / "probe_status.json"  # app_prober.py 巡检状态（BFF 只读展示）
_KV_SEEDED = "monitored_apps:seeded"  # DB 后端播种标记（区分「未初始化」与「全删空」）

DEFAULT_URL = "http://localhost:3000/"
DEFAULT_SERVICE = "nl2sql"

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class MonitorStoreError(ValueError):
    """清单校验/操作错误（由 BFF 统一转 400/404）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return base or f"app-{uuid.uuid4().hex[:6]}"


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise MonitorStoreError("地址不能为空")
    if not _SCHEME_RE.match(value):
        raise MonitorStoreError("地址必须以 http:// 或 https:// 开头")
    return value


def _seed() -> list[dict]:
    """首次播种：沿用既有环境变量约定，向后兼容单一默认项。"""
    url = os.environ.get("AIOPS_MONITOR_URL") or DEFAULT_URL
    service = os.environ.get("AIOPS_MONITOR_SERVICE") or DEFAULT_SERVICE
    now = _now()
    return [
        {
            "id": "default",
            "name": "默认被监控应用",
            "url": url,
            "service": service,
            "log_path": os.environ.get("AIOPS_MONITOR_LOG_PATH", ""),
            "enabled": True,
            "note": "由 AIOPS_MONITOR_URL 初始化，可在控制台维护",
            "created_at": now,
            "updated_at": now,
        }
    ]


# ----------------------------------------------------------------------
# DB 后端（production）
# ----------------------------------------------------------------------


def _dt_str(value: datetime | None) -> str:
    """naive UTC datetime（入库格式）→ 秒级 ISO 字符串（与文件后端 ``_now()`` 同格式）。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _row_to_app(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "service": row["service"],
        "log_path": row["log_path"],
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "created_at": _dt_str(row["created_at"]),
        "updated_at": _dt_str(row["updated_at"]),
    }


def _insert_apps(conn, apps: list[dict]) -> None:
    if not apps:
        return
    fallback = db_layer.now_dt()
    conn.execute(
        db_layer.monitored_apps.insert(),
        [
            {
                "id": app.get("id"),
                "name": app.get("name"),
                "url": app.get("url"),
                "service": app.get("service") or "",
                "log_path": app.get("log_path") or "",
                "enabled": bool(app.get("enabled", True)),
                "note": app.get("note") or "",
                "created_at": db_layer.to_dt(app.get("created_at")) or fallback,
                "updated_at": db_layer.to_dt(app.get("updated_at")) or fallback,
            }
            for app in apps
        ],
    )


def _read_db() -> list[dict]:
    """读全表；首次（播种标记缺失且空表）自动播种默认项。"""
    engine = db_layer.get_engine()
    with engine.begin() as conn:
        seeded = conn.execute(
            select(db_layer.system_kv.c.kv_value).where(db_layer.system_kv.c.kv_key == _KV_SEEDED)
        ).scalar()
        apps = [
            _row_to_app(row)
            for row in conn.execute(
                select(db_layer.monitored_apps).order_by(db_layer.monitored_apps.c.id)
            ).mappings()
        ]
        if seeded is None and not apps:
            apps = _seed()
            _insert_apps(conn, apps)
            conn.execute(
                db_layer.system_kv.insert().values(
                    kv_key=_KV_SEEDED, kv_value="1", updated_at=db_layer.now_dt()
                )
            )
        return apps


def _write_db(apps: list[dict]) -> None:
    """单事务全量替换（清单低容量、低频写；BFF 单副本，无跨实例写竞争）。"""
    engine = db_layer.get_engine()
    with engine.begin() as conn:
        conn.execute(db_layer.monitored_apps.delete())
        _insert_apps(conn, apps)


# ----------------------------------------------------------------------
# 读取 / 写入入口（按模式分发）
# ----------------------------------------------------------------------


def _read_raw() -> list[dict]:
    if mode.is_production():
        return _read_db()
    if not DATA_PATH.is_file():
        apps = _seed()
        _write_raw(apps)
        return apps
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _seed()  # 文件损坏：返回播种值但不覆盖，避免静默销毁现场
    return data if isinstance(data, list) else []


def _write_raw(apps: list[dict]) -> None:
    if mode.is_production():
        _write_db(apps)
        return
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + ".tmp")
    tmp.write_text(json.dumps(apps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def list_all() -> list[dict]:
    """全量清单（每次读盘 → 热生效）。"""
    return _read_raw()


def read_probe_status() -> dict:
    """可用性巡检状态（app_prober.py 落盘；文件缺失/损坏时返回空对象，绝不阻断清单展示）。"""
    try:
        data = json.loads(PROBE_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get(app_id: str) -> dict | None:
    return next((app for app in _read_raw() if app.get("id") == app_id), None)


def log_targets() -> list[dict]:
    """日志采集目标：**启用且配置了 log_path** 的条目。

    每次读盘（热生效）——控制台新增/停用/改路径后，采集器下一轮即按新清单工作。
    """
    return [
        {
            "id": app.get("id"),
            "name": app.get("name"),
            "service": app.get("service") or DEFAULT_SERVICE,
            "log_path": str(app.get("log_path") or "").strip(),
        }
        for app in _read_raw()
        if app.get("enabled") and str(app.get("log_path") or "").strip()
    ]


def _unique_id(apps: list[dict], name: str) -> str:
    base = _slug(name)
    existing = {app.get("id") for app in apps}
    return base if base not in existing else f"{base}-{uuid.uuid4().hex[:4]}"


def add(
    *,
    name: str,
    url: str,
    service: str = DEFAULT_SERVICE,
    log_path: str = "",
    enabled: bool = True,
    note: str = "",
) -> dict:
    apps = _read_raw()
    clean_name = (name or "").strip()
    if not clean_name:
        raise MonitorStoreError("名称不能为空")
    if any(app.get("name") == clean_name for app in apps):
        raise MonitorStoreError(f"名称已存在：{clean_name}")
    now = _now()
    app = {
        "id": _unique_id(apps, clean_name),
        "name": clean_name,
        "url": _normalize_url(url),
        "service": (service or DEFAULT_SERVICE).strip(),
        "log_path": (log_path or "").strip(),
        "enabled": bool(enabled),
        "note": (note or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    apps.append(app)
    _write_raw(apps)
    return app


def update(
    app_id: str,
    *,
    name: str | None = None,
    url: str | None = None,
    service: str | None = None,
    log_path: str | None = None,
    enabled: bool | None = None,
    note: str | None = None,
) -> tuple[dict, dict]:
    """更新并返回 ``(before, after)`` 两个快照，供审计留痕与回滚使用。"""
    apps = _read_raw()
    target = next((app for app in apps if app.get("id") == app_id), None)
    if target is None:
        raise MonitorStoreError(f"未找到被监控应用：{app_id}")
    before = dict(target)
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise MonitorStoreError("名称不能为空")
        if any(app.get("id") != app_id and app.get("name") == clean_name for app in apps):
            raise MonitorStoreError(f"名称已存在：{clean_name}")
        target["name"] = clean_name
    if url is not None:
        target["url"] = _normalize_url(url)
    if service is not None:
        target["service"] = service.strip()
    if log_path is not None:
        target["log_path"] = log_path.strip()
    if enabled is not None:
        target["enabled"] = bool(enabled)
    if note is not None:
        target["note"] = note.strip()
    target["updated_at"] = _now()
    _write_raw(apps)
    return before, dict(target)


def remove(app_id: str) -> dict | None:
    """删除并返回被删除的条目（None 表示不存在，供审计与回滚留痕）。"""
    apps = _read_raw()
    target = next((app for app in apps if app.get("id") == app_id), None)
    if target is None:
        return None
    _write_raw([app for app in apps if app.get("id") != app_id])
    return dict(target)


_IMPORTABLE = ("service", "log_path", "enabled", "note")


def import_many(items: list[dict], *, mode: str = "merge") -> dict:
    """批量导入清单。

    ``mode="merge"``：按名称匹配，存在则更新、不存在则新增；
    ``mode="replace"``：先清空再导入（危险操作，调用方需显式指定）。
    单条失败不中断整批，收集到 ``errors``（评估报告 A4：清单此前只能逐条维护）。
    """
    if mode not in ("merge", "replace"):
        raise MonitorStoreError("mode 仅支持 merge / replace")
    if not isinstance(items, list) or not items:
        raise MonitorStoreError("导入内容为空：items 必须是非空数组")

    if mode == "replace":
        _write_raw([])

    added = updated = 0
    errors: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            errors.append({"name": None, "error": "条目必须是对象"})
            continue
        name = str(raw.get("name") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not name or not url:
            errors.append({"name": name or None, "error": "缺少 name 或 url"})
            continue
        fields = {key: raw[key] for key in _IMPORTABLE if key in raw}
        existing = next((app for app in _read_raw() if app.get("name") == name), None)
        try:
            if existing:
                update(existing["id"], url=url, **fields)
                updated += 1
            else:
                add(name=name, url=url, **fields)
                added += 1
        except MonitorStoreError as exc:
            errors.append({"name": name, "error": str(exc)})
    return {"mode": mode, "total": len(items), "added": added, "updated": updated, "errors": errors}


def batch(ids: list[str], action: str) -> dict:
    """批量启停 / 删除。返回 ``{action, affected, missing}``。"""
    if action not in ("enable", "disable", "delete"):
        raise MonitorStoreError("action 仅支持 enable / disable / delete")
    wanted = [str(item) for item in (ids or [])]
    if not wanted:
        raise MonitorStoreError("未选择任何条目")

    affected = 0
    missing: list[str] = []
    for app_id in wanted:
        if action == "delete":
            if remove(app_id) is not None:
                affected += 1
            else:
                missing.append(app_id)
            continue
        if get(app_id) is None:
            missing.append(app_id)
            continue
        update(app_id, enabled=(action == "enable"))
        affected += 1
    return {"action": action, "affected": affected, "missing": missing}


def duplicate(app_id: str, *, new_name: str | None = None) -> dict:
    """复制条目（名称自动去重）。"""
    source = get(app_id)
    if source is None:
        raise MonitorStoreError(f"未找到被监控应用：{app_id}")
    base = (new_name or f"{source['name']} 副本").strip()
    name = base
    suffix = 2
    while any(app.get("name") == name for app in _read_raw()):
        name = f"{base} {suffix}"
        suffix += 1
    return add(
        name=name,
        url=source["url"],
        service=source["service"],
        log_path=source["log_path"],
        enabled=source["enabled"],
        note=source["note"],
    )


# 变更差异中忽略的字段（时间戳每次都会变，不算"配置变更"）
_DIFF_IGNORED = {"updated_at"}


def diff_fields(before: dict, after: dict) -> dict:
    """字段级差异：``{field: {"from": x, "to": y}}``（忽略 updated_at）。

    用于审计留痕（评估报告 A3）：让「谁把哪个字段从什么改成什么」可追溯，
    并可作为回滚依据（取 ``from`` 值即恢复上一版）。
    """
    keys = (set(before) | set(after)) - _DIFF_IGNORED
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(keys)
        if before.get(key) != after.get(key)
    }
