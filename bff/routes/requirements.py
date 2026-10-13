"""需求基线域：/api/requirements（被监控系统「需求收集与反馈」→ AIOps 主动分析）。

用户在**被监控系统**提交的需求 / 建议 / 缺陷，经其管理员评估、纳入「需求基线」后，
由标准导出接口 ``GET /api/requirements/export`` 开放给 AIOps（契约 v1.0，客户端实现见
:mod:`aiops_agent.requirements_client`）。本域把该接口包装成控制台读接口：

- 数据源（被监控应用）沿用「被监控应用」清单：``app_id`` 显式指定优先，缺省取第一个
  启用项；响应同时回带清单摘要（``apps``），供前端下拉切换；
- 令牌取环境变量 ``NL2SQL_OPS_TOKEN``（值 = 被监控系统的 ``OPS_API_TOKEN``）；
  未配置时**不发请求**、直接返回中文配置指引；
- 拉取失败是常态（未部署 / 未配置令牌 / 网络不可达），一律 ``ok=False + error``
  透传（HTTP 仍为 200），由前端展示原因。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aiops_agent import requirements_client

from .. import audit, requirement_analyses
from .. import monitored_apps as store
from ..deps import ApiError, gw, ok

router = APIRouter()

# 回带前端的应用清单字段（其余字段属维护面，不需要）
_APP_FIELDS = ("id", "name", "url", "service", "enabled")


def _app_summary(app: dict) -> dict:
    return {key: app.get(key) for key in _APP_FIELDS}


def _empty(error: str, *, apps: list[dict], app: dict | None) -> dict:
    """失败/空态响应骨架：ok=False + 中文原因（前端直接展示）。"""
    return ok(
        {
            "ok": False,
            "error": error,
            "warnings": [],
            "apps": apps,
            "app": app,
            "url": "",
            "service": "",
            "system": "",
            "exported_at": "",
            "filter": {},
            "returned": 0,
            "entries": [],
        }
    )


@router.get("/api/requirements")
async def api_requirements(
    app_id: str = Query("", max_length=64, description="被监控应用 ID；缺省取清单中第一个启用项"),
    status: str = Query("BASELINED", pattern="^(BASELINED|PENDING|REJECTED|ALL)$"),
    kind: str = Query("", max_length=32),
    since: str = Query("", max_length=40, description="增量游标（上次导出的 exportedAt，>= 语义）"),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    """拉取需求基线条目（读接口：viewer 起可访问）。"""
    apps = [_app_summary(app) for app in store.list_all()]
    if app_id:
        target = next((app for app in apps if app.get("id") == app_id), None)
        if target is None:
            raise ApiError(404, f"未找到被监控应用：{app_id}")
    else:
        target = next((app for app in apps if app.get("enabled")), apps[0] if apps else None)
    if target is None:
        return _empty(
            "被监控应用清单为空：请先在「被监控应用」页添加应用（该应用需部署需求收集与反馈能力）",
            apps=[],
            app=None,
        )

    token = (os.environ.get(requirements_client.TOKEN_ENV) or "").strip()
    if not token:
        return _empty(
            f"未配置令牌：请设置环境变量 {requirements_client.TOKEN_ENV}"
            "（值 = 被监控系统的 OPS_API_TOKEN）并重启 BFF",
            apps=apps,
            app=target,
        )

    result = await asyncio.to_thread(
        requirements_client.fetch_requirements,
        str(target.get("url") or ""),
        status=status,
        kind=kind or None,
        since=since or None,
        limit=limit,
        token=token,
    )
    return ok(
        {
            "ok": result["ok"],
            "error": result["error"],
            "warnings": result["warnings"],
            "apps": apps,
            "app": target,
            "url": result["url"],
            "service": result["service"],
            "system": result["system"],
            "exported_at": result["exported_at"],
            "filter": result["filter"],
            "returned": result["returned"],
            "entries": result["entries"],
        }
    )


# ----------------------------------------------------------------------
# 处理进度跟踪（被监控系统「需求反馈 → 我的反馈」展示用）
#
# 被监控系统的最终用户在提交需求后，只能看到「待评估 / 已纳入基线」；需求在
# AIOps 侧的分析 / 批准 / 修复发布进度对它不可见。本端点把分析会话视图翻译成
# 面向提交者的 stage + timeline，由被监控系统经**服务令牌通道**（环境变量
# AIOPS_TRACK_SERVICE_TOKEN，见 middleware）拉取并展示。
# ----------------------------------------------------------------------

_MAX_TRACK_IDS = 100


@router.get("/api/requirements/track")
async def api_requirements_track(
    app_id: str = Query("", max_length=64, description="被监控应用 ID；缺省取清单中第一个启用项"),
    entry_ids: str = Query(
        "", max_length=1024, description="需求条目 ID 列表（逗号分隔，最多 100 个）"
    ),
) -> dict:
    """需求处理进度跟踪（服务令牌通道可读；无会话的条目返回 NOT_ANALYZED）。"""
    ids: list[str] = []
    for raw in entry_ids.split(","):
        value = raw.strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise ApiError(400, "缺少 entry_ids（逗号分隔的需求条目 ID 列表）")
    if len(ids) > _MAX_TRACK_IDS:
        raise ApiError(400, f"entry_ids 最多 {_MAX_TRACK_IDS} 个")

    apps = [_app_summary(app) for app in store.list_all()]
    if app_id:
        target = next((app for app in apps if app.get("id") == app_id), None)
        if target is None:
            raise ApiError(404, f"未找到被监控应用：{app_id}")
    else:
        target = next((app for app in apps if app.get("enabled")), apps[0] if apps else None)
    if target is None:
        return ok({"ok": False, "error": "被监控应用清单为空", "apps": [], "app": None, "entries": []})

    tracked: list[dict] = []
    for entry_id in ids:
        entry = requirement_analyses.find(str(target.get("id")), entry_id)
        release_status = None
        if entry is not None and entry.get("status") == "approved":
            release_status = await _resolve_release_status(entry)
        tracked.append(requirement_analyses.track_view(entry, release_status, entry_id))
    return ok({"ok": True, "apps": apps, "app": target, "entries": tracked})


# ----------------------------------------------------------------------
# 智能分析闭环：分析结果查看 → 管理员反馈 → 再次分析 → 批准进入修复工作流
#
# 写操作（POST）均为 admin：批准即启动「需求驱动修复工作流」（沙箱验证 / 人工审批 /
# 公告倒计时 / 金丝雀发布在其内部完整保留，见 AIOpsRequirementWorkflow）。
# ----------------------------------------------------------------------

# 需求条目快照保留字段（发起分析时由前端携带，避免二次拉取被监控系统）
_ENTRY_KEEP = (
    "id",
    "kind",
    "title",
    "content",
    "status",
    "priority",
    "baselineVersion",
    "assessment",
    "submitter",
    "department",
    "updatedAt",
)


class AnalysisCreateBody(BaseModel):
    app_id: str = Field(min_length=1, max_length=64)
    entry: dict = Field(description="需求条目快照（前端从当前列表携带）")


class FeedbackBody(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)


class SyncBody(BaseModel):
    entry: dict = Field(description="被监控系统当前条目快照（含最新评估结论与 updatedAt）")


def _entry_snapshot(entry: dict) -> dict:
    """裁剪并校验需求条目快照（id / title 必备）。"""
    snapshot = {key: entry.get(key) for key in _ENTRY_KEEP if entry.get(key) is not None}
    if not str(snapshot.get("id") or "").strip():
        raise ApiError(400, "需求条目缺少 id，无法发起分析")
    if not str(snapshot.get("title") or "").strip():
        raise ApiError(400, "需求条目缺少标题，无法发起分析")
    return snapshot


def _find_analysis(analysis_id: str) -> dict:
    entry = requirement_analyses.get(analysis_id)
    if entry is None:
        raise ApiError(404, f"未找到分析会话：{analysis_id}")
    return entry


# ----------------------------------------------------------------------
# 状态控制：修复流程发布状态（已发布成功的需求为终态，不可再次分析）
#
# 分析会话批准（approved）后，修复工作流走沙箱 / 审批 / 公告 / 金丝雀链路，
# 其结果决定需求是否真正落地：
#   released（stage=DONE）——已发布成功，需求终态：一切「再次分析」入口一律 409；
#   其余终态（escalated / cancelled / failed）——展示真实结果，控制仍按
#   「已批准」拦截（可到流程详情人工跟进）。
# 首次观测到终态即冻结回会话（approved.result）：Temporal 工作流记录过期
# 被清理后（start-dev 默认保留约 24h），「已发布成功」不丢；查询带进程内缓存：
# 终态长 TTL、运行中短 TTL（轮询不打爆 Temporal）；Temporal 不可达时降级
# unknown——「已批准不可重分析」的兜底拦截仍然有效。
# ----------------------------------------------------------------------

_RELEASE_TTL_LIVE = 20.0  # running / unknown：工作流可能随时推进或恢复
_RELEASE_TTL_FINAL = 600.0  # 终态：仅防重复查询（冻结回会话后不再查询）
_RELEASE_FINAL = ("released", "escalated", "cancelled", "failed")
_RELEASE_CACHE: dict[str, tuple[float, str, float]] = {}
_FREEZE_LOCK = asyncio.Lock()


async def _release_status_cached(wf_id: str | None) -> str | None:
    """查询修复流程发布状态（带缓存；查询失败降级 unknown，不影响主流程）。"""
    if not wf_id:
        return None
    now = time.monotonic()
    cached = _RELEASE_CACHE.get(wf_id)
    if cached and now - cached[0] < cached[2]:
        return cached[1]
    try:
        status = await gw.release_status(wf_id)
    except Exception:  # noqa: BLE001 - Temporal 不可达：状态未知，列表照常返回
        status = "unknown"
    ttl = _RELEASE_TTL_LIVE if status in ("running", "unknown") else _RELEASE_TTL_FINAL
    _RELEASE_CACHE[wf_id] = (now, status, ttl)
    return status


def _frozen_release(entry: dict) -> str | None:
    """会话上已冻结的发布终态（approved.result.status；无则 None）。"""
    result = (entry.get("approved") or {}).get("result")
    status = result.get("status") if isinstance(result, dict) else None
    return status if status in _RELEASE_FINAL else None


async def _resolve_release_status(entry: dict) -> str | None:
    """会话发布状态：已冻结终态优先；否则查询，首次观测到终态即写回会话（冻结）。

    冻结的必要性：Temporal start-dev 工作流记录默认仅保留约 24h，记录被清理后
    「已发布成功」将无法再查询——冻结保证需求终态（能否再次分析）不随记录丢失。
    """
    approved = entry.get("approved") or {}
    wf_id = str(approved.get("wf_id") or "")
    if not wf_id:
        return None
    frozen = _frozen_release(entry)
    if frozen:
        return frozen
    status = await _release_status_cached(wf_id)
    if status in _RELEASE_FINAL:
        async with _FREEZE_LOCK:
            frozen = _frozen_release(entry)  # 并发下可能已被另一端冻结：双检跳过
            if not frozen:
                updated = dict(entry.get("approved") or {})
                updated["result"] = {
                    "status": status,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                entry["approved"] = updated
                requirement_analyses.persist_entry(entry)
    return status


async def _with_release_status(entry: dict) -> dict:
    """会话视图 + release_status（浅拷贝；先解析（可能冻结回写）再拷贝）。"""
    status = await _resolve_release_status(entry)
    view = dict(entry)
    if status is not None:
        view["release_status"] = status
    return view


async def _reject_if_released(entry: dict | None) -> None:
    """状态控制：已发布成功的需求为终态——任何「再次分析」入口统一 409 拒绝。"""
    if not entry or entry.get("status") != "approved":
        return
    if await _resolve_release_status(entry) == "released":
        raise ApiError(409, "该需求已发布成功（终态），不能再次进行智能分析")


@router.get("/api/requirements/analyses")
async def api_requirement_analysis_list(app_id: str = Query("", max_length=64)) -> dict:
    """分析会话列表（viewer 起可读；摘要字段，不含版本明细）。

    approved 会话附带 ``release_status``（修复流程发布结果；released=已发布成功，
    需求终态不可再次分析）；查询失败降级未知，不影响列表。
    """
    entries = requirement_analyses.list_all(app_id or None)
    statuses = await asyncio.gather(*(_resolve_release_status(entry) for entry in entries))
    items = [requirement_analyses.summary(entry) for entry in entries]
    for item, status in zip(items, statuses):
        if status is not None:
            item["release_status"] = status
    return ok({"analyses": items})


@router.get("/api/requirements/analyses/{analysis_id}")
async def api_requirement_analysis_detail(analysis_id: str) -> dict:
    """分析会话详情（含全部版本、批准信息与发布状态；viewer 起可读）。"""
    entry = _find_analysis(analysis_id)
    return ok({"analysis": await _with_release_status(entry)})


@router.post("/api/requirements/analyses")
async def api_requirement_analysis_create(request: Request, body: AnalysisCreateBody) -> dict:
    """发起（或失败重试）智能分析；已完成的分析直接返回既有结果。

    状态控制：已发布成功的需求为终态，不允许再次发起（409，先于全部状态机分支）。
    """
    target = next((app for app in store.list_all() if app.get("id") == body.app_id), None)
    if target is None:
        raise ApiError(404, f"未找到被监控应用：{body.app_id}")
    snapshot = _entry_snapshot(body.entry)
    actor = request.state.identity.user
    existing = requirement_analyses.find(str(target.get("id")), str(snapshot.get("id", "")))
    await _reject_if_released(existing)
    try:
        entry, need_run = requirement_analyses.create_or_touch(
            str(target.get("id")), str(target.get("service") or ""), snapshot, actor
        )
    except requirement_analyses.RequirementAnalysisError as exc:
        raise ApiError(409, str(exc)) from exc
    if need_run:
        requirement_analyses.start_analysis_thread(
            entry["id"], int(entry.get("current_version") or 1)
        )
        trigger = (entry.get("versions") or [{}])[-1].get("trigger")
        audit.write_audit(
            actor=actor,
            action="requirement:analyze",
            target=entry["id"],
            params={"entry_id": entry.get("entry_id"), "trigger": trigger},
        )
    return ok({"analysis": entry, "started": need_run})


@router.post("/api/requirements/analyses/{analysis_id}/feedback")
async def api_requirement_analysis_feedback(
    request: Request, analysis_id: str, body: FeedbackBody
) -> dict:
    """管理员反馈（优化建议 / 具体要求）→ 追加版本并提交系统再次分析。

    状态控制：已发布成功的需求为终态，不允许通过反馈再次分析（409）。
    """
    entry = _find_analysis(analysis_id)
    actor = request.state.identity.user
    await _reject_if_released(entry)
    try:
        requirement_analyses.apply_feedback(entry, body.feedback, actor)
        requirement_analyses.persist_entry(entry)
    except requirement_analyses.RequirementAnalysisError as exc:
        raise ApiError(409, str(exc)) from exc
    requirement_analyses.start_analysis_thread(analysis_id, int(entry.get("current_version") or 1))
    audit.write_audit(
        actor=actor,
        action="requirement:feedback",
        target=analysis_id,
        params={
            "version": entry.get("current_version"),
            "feedback": body.feedback.strip()[:200],
        },
    )
    return ok({"analysis": entry})


@router.post("/api/requirements/analyses/{analysis_id}/sync")
async def api_requirement_analysis_sync(
    request: Request, analysis_id: str, body: SyncBody
) -> dict:
    """同步被监控系统最新条目内容（「继续评估」后的结论 / 优先级变化）→ 按需重分析。

    - 快照无变化 → 409；分析进行中 → 409；
    - analyzed / failed → 刷新快照并追加 refresh 版本重分析（started=True）；
    - approved（终态）→ 仅刷新快照留档（started=False）。
    """
    entry = _find_analysis(analysis_id)
    snapshot = _entry_snapshot(body.entry)
    actor = request.state.identity.user
    try:
        need_run = requirement_analyses.apply_refresh(entry, snapshot, actor)
        requirement_analyses.persist_entry(entry)
    except requirement_analyses.RequirementAnalysisError as exc:
        raise ApiError(409, str(exc)) from exc
    if need_run:
        requirement_analyses.start_analysis_thread(
            analysis_id, int(entry.get("current_version") or 1)
        )
    audit.write_audit(
        actor=actor,
        action="requirement:sync",
        target=analysis_id,
        params={
            "entry_id": entry.get("entry_id"),
            "updated_at": snapshot.get("updatedAt"),
            "restarted": need_run,
        },
    )
    return ok({"analysis": entry, "started": need_run})


@router.post("/api/requirements/analyses/{analysis_id}/approve")
async def api_requirement_analysis_approve(request: Request, analysis_id: str) -> dict:
    """管理员同意 → 启动需求驱动修复工作流（内部仍保留沙箱验证 / 审批 / 公告链路）。"""
    entry = _find_analysis(analysis_id)
    if entry.get("status") == "approved":
        raise ApiError(409, "该需求已批准，请勿重复操作")
    if entry.get("status") != "analyzed":
        raise ApiError(
            409, f"当前状态 {entry.get('status') or '未知'} 不允许批准（要求分析完成）"
        )
    analysis = requirement_analyses.latest_analysis(entry) or {}
    if analysis.get("degraded"):
        raise ApiError(409, "最新分析未成功完成（无有效结果），请提交反馈或重试后再批准")
    actor = request.state.identity.user
    # 任务构造与「转人工待办→重试修复」共用（bff.requirement_analyses.build_requirement_task）
    task, version = requirement_analyses.build_requirement_task(entry, actor)
    wf_id = f"aiops-req-{analysis_id}-v{version}"
    try:
        started = await gw.start_requirement_flow(task, wf_id)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from exc
    try:
        requirement_analyses.apply_approved(entry, version, started, actor)
        requirement_analyses.persist_entry(entry)
    except requirement_analyses.RequirementAnalysisError as exc:
        # 工作流已启动：状态回写失败不静默——提示运行中流程与人工核对路径
        raise ApiError(500, f"修复流程已启动（{started}），但会话状态回写失败：{exc}") from exc
    audit.write_audit(
        actor=actor,
        action="requirement:approve",
        wf_id=started,
        target=analysis_id,
        params={"version": version, "entry_id": entry.get("entry_id")},
    )
    return ok({"analysis": entry, "wf_id": started})


@router.post("/api/requirements/analyses/{analysis_id}/resume-shadow")
async def api_requirement_analysis_resume_shadow(
    request: Request, analysis_id: str
) -> dict:
    """影子建议续接：复用原流程已生成补丁，续接执行完整链路（MR → 审批 → 公告 → 发布）。

    影子档（shadow_mode）下流程以 SHADOWED 终态结束（只建议不执行）。归档评估后
    决定采纳时走本端点：不重新分析、不重新生成补丁，直接以原补丁进入完整沙箱验证
    与后续闸门。续接流程 id 固定 ``aiops-req-{analysis_id}-v{version}-resume1``
    （幂等防重），并将会话 approved.wf_id 指向续接流程（发布状态控制随之跟踪）。
    """
    entry = _find_analysis(analysis_id)
    if entry.get("status") != "approved":
        raise ApiError(409, f"当前状态 {entry.get('status') or '未知'} 不允许续接（要求已批准）")
    if await _resolve_release_status(entry) == "released":
        raise ApiError(409, "该需求已发布成功（终态），不能再次续接")
    approved = entry.get("approved") or {}
    original_wf_id = str(approved.get("wf_id") or "")
    if not original_wf_id:
        raise ApiError(409, "会话缺少已批准流程记录，无法续接影子建议")
    try:
        detail = await gw.flow_detail(original_wf_id)
    except Exception as exc:  # noqa: BLE001 - Temporal 不可达/记录过期：无法核对原流程
        raise ApiError(409, "原流程记录不可读（Temporal 记录可能已过期），无法续接影子建议") from exc
    result = detail.get("result")
    if not isinstance(result, dict):
        if detail.get("exec_status") == "RUNNING":
            raise ApiError(409, "该需求的修复流程正在运行，无需续接")
        raise ApiError(409, "原流程结果不可读（Temporal 记录可能已过期），无法续接影子建议")
    if result.get("stage") != "SHADOWED":
        raise ApiError(
            409,
            f"原流程阶段 {result.get('stage') or '未知'} 不是影子拦截（SHADOWED），无需续接",
        )
    patch = result.get("patch")
    if not isinstance(patch, dict) or not patch.get("patch_id"):
        raise ApiError(409, "原流程结果缺少补丁数据，无法续接影子建议")
    actor = request.state.identity.user
    task, version = requirement_analyses.build_requirement_task(entry, actor)
    wf_id = f"aiops-req-{analysis_id}-v{version}-resume1"
    try:
        started = await gw.start_requirement_flow(task, wf_id, resume_patch=patch)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from exc
    try:
        requirement_analyses.apply_resumed(entry, started, original_wf_id, actor)
        requirement_analyses.persist_entry(entry)
    except requirement_analyses.RequirementAnalysisError as exc:
        # 工作流已启动：状态回写失败不静默——提示运行中流程与人工核对路径
        raise ApiError(500, f"续接流程已启动（{started}），但会话状态回写失败：{exc}") from exc
    audit.write_audit(
        actor=actor,
        action="requirement:resume-shadow",
        wf_id=started,
        target=analysis_id,
        params={
            "version": version,
            "patch_id": patch.get("patch_id"),
            "resumed_from": original_wf_id,
        },
    )
    return ok({"analysis": entry, "wf_id": started})
