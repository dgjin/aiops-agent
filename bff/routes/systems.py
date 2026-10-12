"""系统视角聚合域：「系统总览」与「系统工作台时间线」的服务端聚合（UI 重构 Phase 2，2026-10-12）。

职责（《控制台 UI 重构设计方案》Phase 2 三项中的两项 + 时间线聚合）：
- ``/api/systems/overview``：把总览页原「overview + monitored-apps 两路轮询 + 前端 join」
  收敛为一次请求，并附带**未纳管服务检测**（产生过流程但不在清单中的 service）；
- ``/api/systems/{app_id}/timeline``：把工作台「多流程活动时间线」聚合为一次请求
  （gate_events 翻译 + 状态快照推导），替代前端逐流程拉详情（渐进请求）。

两端点均为只读聚合，数据全部来自现有源：
- 流程计数口径复用 ``flows.overview_payload``（与 /api/overview 同源，防止规则漂移）；
- 清单读盘热生效（monitored_apps store）；流程数据复用 gw.list_flows 的 3s 缓存。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Query

from .. import monitored_apps as store
from ..deps import ApiError, gw, ok
from .flows import overview_payload
from .monitored_apps import apps_with_probe

router = APIRouter()


# ----------------------------------------------------------------------
# 未纳管服务检测（清单外 service）
# ----------------------------------------------------------------------


def _unmanaged_services(items: list[dict], apps: list[dict]) -> list[dict]:
    """清单外服务检测：触发过流程但不在被监控清单（按 service join）的服务。

    停用（enabled=false）的系统仍视为「已纳管」（配置仍在清单中）；仅统计
    ``alert.service`` 非空的流程，避免把无服务归属的流程误报为未纳管。
    排序：运行中数降序 → 最近出现时间降序。
    """
    managed = {str(app.get("service") or "") for app in apps}
    stats: dict[str, dict] = {}
    for item in items:
        service = str((item.get("alert") or {}).get("service") or "")
        if not service or service in managed:
            continue
        entry = stats.setdefault(
            service,
            {"service": service, "flows": 0, "running": 0, "last_start": None, "last_wf_id": None},
        )
        entry["flows"] += 1
        if item.get("exec_status") == "RUNNING":
            entry["running"] += 1
        started = item.get("start_time")
        if started and (entry["last_start"] is None or started > entry["last_start"]):
            entry["last_start"] = started
            entry["last_wf_id"] = item.get("wf_id")
    out = sorted(stats.values(), key=lambda e: e["last_start"] or "", reverse=True)
    out.sort(key=lambda e: e["running"], reverse=True)  # 稳定排序：运行中优先
    return out


# ----------------------------------------------------------------------
# 事件流：gate_events 翻译 / 状态快照推导
# ----------------------------------------------------------------------


def _attempt_label(event: str) -> str:
    """attempt 为 0 基索引（初次=0，见 workflows.py 回炉循环），展示统一转 1 基。"""
    match = re.search(r"attempt=(\d+)", event)
    return f"（第 {int(match.group(1)) + 1} 轮）" if match else ""


# 精确匹配的 gate_events（格式固定，来自 workflows.py 的全部事件生成点）
_GATE_EXACT = {
    "gate2:approved": ("闸门2（人工审批）：已批准", "ok"),
    "gate2:reject": ("闸门2（人工审批）：已拒绝", "danger"),
    "gate2:timeout->reject": ("闸门2（人工审批）：超时自动拒绝", "danger"),
    "gate2b:approved:protected-dir": ("闸门2b（二级审批 · 受保护目录）：已批准", "ok"),
    "gate2b:reject": ("闸门2b（二级审批 · 受保护目录）：已拒绝", "danger"),
    "gate2b:timeout->reject": ("闸门2b（二级审批 · 受保护目录）：超时自动拒绝", "danger"),
    "gate3:cancelled": ("发布窗口已取消（用户指令）", "idle"),
    "shadow:suggestion-only": ("影子模式：仅出建议，不执行变更", "idle"),
    "canary:degraded->rollback": ("金丝雀劣化 → 已自动回滚，转人工", "danger"),
    "canary:healthy->full-rollout": ("金丝雀健康 → 全量发布完成", "ok"),
}


def _translate_gate_event(event: str) -> dict:
    """gate_events 机器串 → {text, tone}（中文文案；未知格式降级为原文，不丢信息）。"""
    if event in _GATE_EXACT:
        text, tone = _GATE_EXACT[event]
        return {"text": text, "tone": tone}
    if event.startswith("gate1:blocked"):
        detail = event[len("gate1:blocked") :].lstrip(":")
        return {"text": f"闸门1（置信度把关）拦截：{detail or '低于阈值'}", "tone": "danger"}
    if event.startswith("tests:passed"):
        return {"text": f"沙箱测试通过{_attempt_label(event)}", "tone": "ok"}
    if event.startswith("tests:failed"):
        parts = event.split(":", 3)
        reason = parts[3] if len(parts) > 3 else ""
        return {"text": f"沙箱测试失败{_attempt_label(event)}：{reason[:60]}", "tone": "danger"}
    if event.startswith("mr:created:"):
        return {"text": f"变更单已创建：{event[len('mr:created:'):]}", "tone": "info"}
    if event.startswith("gate3:announced:"):
        return {
            "text": f"闸门3（用户公告）：已公告 {event[len('gate3:announced:'):]}，倒计时后自动发布",
            "tone": "warn",
        }
    if event.startswith("gate3:command:"):
        command = event[len("gate3:command:") :]
        label = {"deploy_now": "立即发布", "cancel": "取消发布"}.get(command, command)
        return {"text": f"发布指令：{label}", "tone": "info" if command == "deploy_now" else "warn"}
    if event.startswith("gate3:patch-queued:"):
        return {"text": f"窗口内新补丁入队：{event[len('gate3:patch-queued:'):]}", "tone": "info"}
    if event.startswith("requirement:approved:"):
        # 需求流程（AIOpsRequirementWorkflow）：requirement:approved:{analysis_id}:v{n}:by={approver}
        parts = event.split(":", 4)
        analysis_id = parts[2] if len(parts) > 2 else ""
        version = (parts[3] if len(parts) > 3 else "").lstrip("v")
        approver = parts[4] if len(parts) > 4 else ""
        approver = approver[3:] if approver.startswith("by=") else approver
        detail = " · ".join(
            x for x in (f"v{version}" if version else "", f"审批人 {approver}" if approver else "") if x
        )
        text = f"需求批准启动修复（{detail}）：{analysis_id}" if detail else f"需求批准启动修复：{analysis_id}"
        return {"text": text, "tone": "ok"}
    return {"text": event, "tone": "muted"}


def _alert_event(item: dict) -> dict | None:
    alert = item.get("alert") or {}
    if not alert.get("alert_id"):
        return None
    text = f"告警接收：{alert['alert_id']}"
    description = str(alert.get("description") or "").strip()
    if description:
        text += f" · {description[:48]}"
    return {"text": text, "tone": "info", "at": item.get("start_time")}


def _derived_events(item: dict) -> list[dict]:
    """从状态快照派生关键事件（无 gate_events 的运行中流程 / 老数据流程兜底路径）。"""
    events: list[dict] = []
    root_cause = item.get("root_cause")
    if isinstance(root_cause, dict) and root_cause.get("summary"):
        confidence = root_cause.get("confidence")
        suffix = f"（置信度 {confidence:.2f}）" if isinstance(confidence, (int, float)) else ""
        events.append({"text": f"根因分析{suffix}：{str(root_cause['summary'])[:60]}", "tone": "info"})
    patch = item.get("patch")
    if isinstance(patch, dict):
        degraded = bool(patch.get("degraded"))
        files = patch.get("files") or []
        prefix = "补丁（降级兜底产物）已生成" if degraded else "补丁已生成"
        events.append(
            {"text": f"{prefix}：{len(files)} 个文件", "tone": "warn" if degraded else "info"}
        )
    report = item.get("test_report")
    if isinstance(report, dict):
        passed = bool(report.get("passed"))
        events.append(
            {"text": f"沙箱测试{'通过' if passed else '失败'}", "tone": "ok" if passed else "danger"}
        )
    approval = item.get("approval")
    if approval:
        events.append(
            {
                "text": f"一级审批：{'已批准' if approval == 'approve' else '已拒绝'}",
                "tone": "ok" if approval == "approve" else "danger",
            }
        )
    second = item.get("second_approval")
    if item.get("needs_second") and second:
        events.append(
            {
                "text": f"二级审批（受保护目录）：{'已批准' if second == 'approve' else '已拒绝'}",
                "tone": "ok" if second == "approve" else "danger",
            }
        )
    command = item.get("deploy_command")
    if command in ("deploy_now", "cancel"):
        events.append(
            {
                "text": f"发布指令：{'立即发布' if command == 'deploy_now' else '取消发布'}",
                "tone": "info" if command == "deploy_now" else "warn",
            }
        )
    return events


def _running_meta_event(item: dict) -> dict | None:
    """运行中流程的「当前进展」尾事件（等待审批 / 公告倒计时）。"""
    if item.get("exec_status") != "RUNNING":
        return None
    stage = item.get("stage")
    if stage == "WAIT_APPROVAL" and not item.get("approval"):
        return {"text": "当前：等待人工审批（倒计时结束将自动拒绝并转人工）", "tone": "warn", "at": None}
    if stage == "NOTIFYING":
        return {"text": "当前：用户公告倒计时中，到期按策略金丝雀发布", "tone": "warn", "at": None}
    return None


_TERMINAL_META = {
    "DONE": ("已完成（金丝雀健康 → 全量发布成功）", "ok"),
    "ESCALATED": ("已转人工（等待运维处置后可重试修复）", "danger"),
    "FAILED": ("硬失败（执行异常或超时，无终态审计）", "danger"),
    "CANCELLED": ("已取消", "idle"),
    "SHADOWED": ("影子档结束（仅建议，未执行变更）", "idle"),
}


def _terminal_event(item: dict) -> dict | None:
    if item.get("exec_status") == "RUNNING":
        return None
    stage = item.get("stage")
    if not stage:
        return None
    text, tone = _TERMINAL_META.get(stage, (f"流程终态：{stage}", "muted"))
    if stage in _TERMINAL_META:
        text = f"流程终态：{text}"
    return {"text": text, "tone": tone, "at": item.get("close_time")}


def _timeline_events(item: dict) -> list[dict]:
    """单流程事件流：告警接收 →（gate_events 翻译 | 快照推导）→ 当前进展 → 终态。

    时间戳：仅「告警接收」（=流程开始）与「终态」（=流程关闭）有精确时间；
    中间事件按阶段顺序排列（工作流内部阶段时间未暴露给 BFF，不臆造时间）。
    """
    events: list[dict] = []
    alert_event = _alert_event(item)
    if alert_event:
        events.append(alert_event)
    raw_events = item.get("gate_events") or []
    if raw_events:
        for raw in raw_events:
            events.append({**_translate_gate_event(str(raw)), "at": None})
    else:
        events.extend({**event, "at": None} for event in _derived_events(item))
    meta = _running_meta_event(item)
    if meta:
        events.append(meta)
    stage_error = item.get("stage_error")
    if stage_error:
        events.append({"text": f"数据读取异常：{stage_error}", "tone": "danger", "at": None})
    terminal = _terminal_event(item)
    if terminal:
        events.append(terminal)
    return [{"seq": index + 1, **event} for index, event in enumerate(events)]


def build_timeline_groups(items: list[dict], *, max_flows: int = 5) -> list[dict]:
    """多流程时间线分组：运行中优先（按开始时间倒序）、终态按关闭时间倒序，取前 max_flows 个。"""
    running = sorted(
        (i for i in items if i.get("exec_status") == "RUNNING"),
        key=lambda i: i.get("start_time") or "",
        reverse=True,
    )
    closed = sorted(
        (i for i in items if i.get("exec_status") != "RUNNING"),
        key=lambda i: i.get("close_time") or i.get("start_time") or "",
        reverse=True,
    )
    groups: list[dict] = []
    for item in (running + closed)[:max_flows]:
        groups.append(
            {
                "wf_id": item.get("wf_id"),
                "stage": item.get("stage"),
                "exec_status": item.get("exec_status"),
                "current": item.get("exec_status") == "RUNNING",
                "start_time": item.get("start_time"),
                "close_time": item.get("close_time"),
                "duration_seconds": item.get("duration_seconds"),
                "events": _timeline_events(item),
            }
        )
    return groups


# ----------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------


@router.get("/api/systems/overview")
async def api_systems_overview() -> dict:
    """系统总览聚合：清单+实时探测 + 流程计数 + 未纳管检测，一次返回。

    替代总览页原「/api/overview + /api/monitored-apps」两路轮询与前端 join；
    流程计数与 /api/overview 共用 overview_payload（同一口径）。
    注意：apps_with_probe 每次请求触发一轮实时探测（启用项并发 HTTP），前端轮询
    间隔应 >=10s；后续系统规模增大时可对探测加 TTL 缓存（当前演示规模无必要）。
    """
    items = await gw.list_flows(limit=200)
    payload = overview_payload(items)
    apps = await apps_with_probe()
    payload["apps"] = apps
    payload["unmanaged"] = _unmanaged_services(items, apps)
    return ok(payload)


@router.get("/api/systems/{app_id}/timeline")
async def api_system_timeline(app_id: str, flows: int = Query(5, ge=1, le=20)) -> dict:
    """单系统多流程活动时间线（服务端聚合；前端一次请求替代逐流程拉详情）。"""
    app = store.get(app_id)
    if app is None:
        raise ApiError(404, f"未找到被监控应用：{app_id}")
    service = str(app.get("service") or "")
    items = await gw.list_flows(limit=200)
    mine = [i for i in items if str((i.get("alert") or {}).get("service") or "") == service]
    return ok(
        {"app_id": app_id, "service": service, "groups": build_timeline_groups(mine, max_flows=flows)}
    )
