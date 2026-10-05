"""被监控应用域：/api/monitored-apps*（清单 CRUD / 导入导出 / 批量 / 回滚 / 标准接口探测）。

清单**每次读盘**（monitored_apps.list_all），因此控制台的增删改无需重启 BFF 即生效；
写盘即热生效，全部写操作落操作审计（配置类：wf_id 留空 + target 记录对象）。
每条附「接入就绪度」（仓库 / 索引 / 日志三态）与「标准接口自动探测」（“从标准接口自动探测”），
支撑「前台添加后配置 → 各链路自动适配」。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aiops_agent import app_manifest, code_rag

from .. import aggregator, audit
from .. import monitored_apps as store
from ..deps import ApiError, ok

router = APIRouter()


class MonitoredAppBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=512)
    service: str = Field(default=store.DEFAULT_SERVICE, max_length=64)
    # 应用落盘日志路径（供 ship_app_logs.py 按清单采集；支持通配，如 app_server*.log）
    log_path: str = Field(default="", max_length=512)
    # 页面健康关键字（可选；空=仅连接级探测）：响应内容须包含该关键字才算在线
    probe_keyword: str = Field(default="", max_length=256)
    # 健康检查路径（可选；空=根路径"/"）：Manifest 自动预填，探测/巡检按该路径校验关键字
    health_path: str = Field(default="", max_length=512)
    # 修复目标仓库路径（AIOps 修复引擎据此定位并生成补丁）；None=请求未携带该字段时保留原值，防漏字段清空
    repo: str | None = Field(default=None, max_length=1024)
    enabled: bool = True
    note: str = Field(default="", max_length=200)


def _monitor_error_code(message: str) -> int:
    return 404 if message.startswith("未找到") else 400


_GLOB_CHARS = re.compile(r"[*?\[]")


def _log_files_state(log_path: str) -> int | None:
    """日志路径就绪度：None=未配置；否则为当前可采集文件数（与采集器同口径：expanduser + 通配）。"""
    raw = (log_path or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not _GLOB_CHARS.search(path.name):
        return 1 if path.is_file() else 0
    return sum(1 for item in path.parent.glob(path.name) if item.is_file())


def _readiness(app: dict) -> dict:
    """应用接入就绪度三态：None=未配置 / True=就绪 / False=未就绪。

    - ``repo_ok``：配置了修复仓库且目录存在；
    - ``index_ok``：专属索引已存在（仓库变更时首次检索会自动重建，此处仅展示现状）；
      未配置仓库 → None（不使用应用专属索引，走默认索引）；
    - ``log_files``：可采集日志文件数（未配置 → None；0 = 路径未命中须排查）。
    """
    repo_raw = str(app.get("repo") or "").strip()
    repo_ok = Path(repo_raw).expanduser().is_dir() if repo_raw else None
    index_ok = (
        code_rag.app_index_path(str(app.get("service") or "")).is_file() if repo_ok else None
    )
    return {
        "repo_ok": repo_ok,
        "index_ok": index_ok,
        "log_files": _log_files_state(str(app.get("log_path") or "")),
    }


async def apps_with_probe() -> list[dict]:
    """被监控应用清单 + 实时探测 + 主动巡检状态。

    仅探测启用项（并发），停用项直接标记为未探测；system 域与本域共用。
    ``watcher`` 来自 app_prober.py 落盘的状态（未运行巡检时为 None），
    用于在状态列展示「连续失败 N 次 / 已自动触发修复流程」。
    """
    apps = store.list_all()
    enabled = [app for app in apps if app.get("enabled")]
    probes = (
        await asyncio.gather(
            *[
                asyncio.to_thread(
                    aggregator.probe_monitored_app,
                    app.get("url"),
                    keyword=app.get("probe_keyword") or "",
                    health_path=app.get("health_path") or "",
                )
                for app in enabled
            ]
        )
        if enabled
        else []
    )
    probed = {app["id"]: probe for app, probe in zip(enabled, probes)}
    watchers = store.read_probe_status().get("apps") or {}
    return [
        {
            **app,
            "probe": probed.get(app["id"])
            or {"running": False, "target": app.get("url"), "error": "已停用（未探测）"},
            "watcher": watchers.get(app["id"]),
            "readiness": _readiness(app),
        }
        for app in apps
    ]


@router.get("/api/monitored-apps")
async def api_monitored_apps() -> dict:
    """清单 + 实时探测 + 接入就绪度（控制台管理页数据源）。"""
    return ok({"items": await apps_with_probe()})


class DiscoverBody(BaseModel):
    url: str = Field(min_length=1, max_length=512)


@router.post("/api/monitored-apps/discover")
async def api_monitored_apps_discover(body: DiscoverBody) -> dict:
    """标准接口自动探测：GET ``<url>/.well-known/aiops.json``（AIOps Manifest v1.0）。

    成功时返回 ``manifest`` 原文与 ``suggested`` 预填值（控制台一键填表，
    免人工翻代码找日志路径 / 健康关键字）；失败是常态（未接入标准接口的应用），
    返回 ``ok=False`` 与原因。只读探测，不写审计。
    """
    try:
        url = store._normalize_url(body.url)
    except store.MonitorStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    result = await asyncio.to_thread(app_manifest.fetch_manifest, url)
    manifest = result.get("manifest") or {}
    if result.get("ok"):
        result["suggested"] = {
            "name": (manifest.get("name") or "").strip() or None,
            "service": (manifest.get("service") or "").strip() or None,
            "probe_keyword": manifest.get("probe_keyword") or "",
            "health_path": manifest.get("health_path") or "",
            "log_path": manifest.get("log_path") or "",
        }
    return ok(result)


@router.post("/api/monitored-apps")
async def api_monitored_app_create(request: Request, body: MonitoredAppBody) -> dict:
    try:
        created = store.add(**body.model_dump())
    except store.MonitorStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:create",
        target=created["id"],
        params={"name": created["name"], "url": created["url"], "service": created["service"]},
    )
    return ok({"ok": True, "app": created})


@router.put("/api/monitored-apps/{app_id}")
async def api_monitored_app_update(request: Request, app_id: str, body: MonitoredAppBody) -> dict:
    try:
        before, updated = store.update(app_id, **body.model_dump())
    except store.MonitorStoreError as exc:
        raise ApiError(_monitor_error_code(str(exc)), str(exc)) from exc
    changed = store.diff_fields(before, updated)
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:update",
        target=app_id,
        params={"name": updated["name"], "changed": changed},
    )
    return ok({"ok": True, "app": updated, "changed": changed})


@router.post("/api/monitored-apps/{app_id}/toggle")
async def api_monitored_app_toggle(request: Request, app_id: str) -> dict:
    """启停切换（ENABLED 是高频操作，单独提供避免前端读改写竞态）。"""
    current = store.get(app_id)
    if current is None:
        raise ApiError(404, f"未找到被监控应用：{app_id}")
    before, updated = store.update(app_id, enabled=not current.get("enabled"))
    changed = store.diff_fields(before, updated)
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:toggle",
        target=app_id,
        params={"name": updated["name"], "changed": changed},
    )
    return ok({"ok": True, "app": updated, "changed": changed})


@router.delete("/api/monitored-apps/{app_id}")
async def api_monitored_app_delete(request: Request, app_id: str) -> dict:
    removed = store.remove(app_id)
    if removed is None:
        raise ApiError(404, f"未找到被监控应用：{app_id}")
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:delete",
        target=app_id,
        params={
            "removed": {
                k: removed.get(k)
                for k in ("name", "url", "service", "log_path", "probe_keyword", "health_path", "repo", "enabled")
            }
        },
    )
    return ok({"ok": True, "id": app_id})


class ImportBody(BaseModel):
    items: list[dict] = Field(min_length=1)
    mode: str = Field(default="merge", pattern="^(merge|replace)$")


class BatchBody(BaseModel):
    ids: list[str] = Field(min_length=1)
    action: str = Field(pattern="^(enable|disable|delete)$")


class DuplicateBody(BaseModel):
    name: str | None = None


@router.get("/api/monitored-apps/export")
async def api_monitored_apps_export() -> dict:
    """导出清单（不含探测结果，便于跨环境搬运）。"""
    items = [
        {
            key: app.get(key)
            for key in ("name", "url", "service", "log_path", "probe_keyword", "health_path", "repo", "enabled", "note")
        }
        for app in store.list_all()
    ]
    return ok({"version": 1, "items": items})


@router.post("/api/monitored-apps/import")
async def api_monitored_apps_import(request: Request, body: ImportBody) -> dict:
    try:
        summary = store.import_many(body.items, mode=body.mode)
    except store.MonitorStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:import",
        target=f"{body.mode}:{summary['total']}条",
        params=summary,
    )
    return ok({"ok": True, **summary})


@router.post("/api/monitored-apps/batch")
async def api_monitored_apps_batch(request: Request, body: BatchBody) -> dict:
    try:
        summary = store.batch(body.ids, body.action)
    except store.MonitorStoreError as exc:
        raise ApiError(400, str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action=f"monitored-app:batch-{body.action}",
        target=",".join(body.ids[:5]) + ("…" if len(body.ids) > 5 else ""),
        params=summary,
    )
    return ok({"ok": True, **summary})


@router.post("/api/monitored-apps/{app_id}/duplicate")
async def api_monitored_app_duplicate(
    request: Request, app_id: str, body: DuplicateBody | None = None
) -> dict:
    try:
        created = store.duplicate(app_id, new_name=(body.name if body else None))
    except store.MonitorStoreError as exc:
        raise ApiError(_monitor_error_code(str(exc)), str(exc)) from exc
    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:duplicate",
        target=created["id"],
        params={"from": app_id, "name": created["name"]},
    )
    return ok({"ok": True, "app": created})


class RollbackBody(BaseModel):
    ts: str | None = Field(default=None, description="回滚到该审计记录之前的状态；缺省取最近一次变更")


# 可回滚（可编辑）字段白名单
_ROLLBACK_FIELDS = {"name", "url", "service", "log_path", "probe_keyword", "repo", "enabled", "note"}


def _find_rollback_source(app_id: str, ts: str | None) -> dict | None:
    """在操作审计中定位该应用的可回滚变更记录（默认最近一条 update/toggle）。

    审计按时间倒序返回，因此取到的第一条即"最近一次变更"，其 ``changed[*].from``
    即上一版取值。
    """
    for row in audit.read_audit(limit=1000):
        if row.get("target") != app_id:
            continue
        if row.get("action") not in ("monitored-app:update", "monitored-app:toggle"):
            continue
        if ts is None or row.get("ts") == ts:
            return row
    return None


@router.post("/api/monitored-apps/{app_id}/rollback")
async def api_monitored_app_rollback(
    request: Request, app_id: str, body: RollbackBody | None = None
) -> dict:
    """按审计记录回滚配置：取该记录 ``changed[*].from`` 恢复上一版（评估报告 A3）。"""
    record = _find_rollback_source(app_id, body.ts if body else None)
    if record is None:
        raise ApiError(404, f"未找到该应用的可回滚变更记录：{app_id}")

    changed = ((record.get("params") or {}).get("changed") or {}) if isinstance(record.get("params"), dict) else {}
    fields = {
        key: spec.get("from")
        for key, spec in changed.items()
        if key in _ROLLBACK_FIELDS and isinstance(spec, dict) and "from" in spec
    }
    if not fields:
        raise ApiError(400, "该变更记录没有可回滚的字段")

    try:
        before, rolled = store.update(app_id, **fields)
    except store.MonitorStoreError as exc:
        raise ApiError(_monitor_error_code(str(exc)), str(exc)) from exc

    audit.write_audit(
        actor=request.state.identity.user,
        action="monitored-app:rollback",
        target=app_id,
        params={"from_ts": record.get("ts"), "restored": fields},
    )
    return ok(
        {
            "ok": True,
            "app": rolled,
            "rolled_back_from": record.get("ts"),
            "restored": fields,
            "changed": store.diff_fields(before, rolled),
        }
    )
