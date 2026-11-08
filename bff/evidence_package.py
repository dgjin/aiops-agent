"""审计证据包导出（M4）：自包含 JSON + manifest/SHA-256。

导出内容（全部内联，离线可复核）：

- 流程身份与状态（case_id / 证据绑定 / 发起人 / 父子重试链）；
- 闸门与审批（gate_events / approvals / 拒绝与错误）；
- Temporal History 全量事件（导出前做一次最终同步，失败不阻断但会标记）；
- 操作审计、写命令 outbox、通知留痕、人工待办；
- 日志证据、测试报告、发布 / 回滚结果。

manifest 对每个 section 计算 SHA-256（canonical JSON），再对 section 摘要计算
``root_sha256``；导出动作本身写入审计链，返回 ``X-Audit-Event-Id``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from aiops_agent import evidence_store

from . import audit, commands, history_projector, timeline
from .deps import gw, log

SCHEMA = "aiops-evidence-package/v1"
_IDENTITY_FIELDS = (
    "case_id",
    "approval_contract",
    "evidence_binding",
    "requested_by",
    "parent_run_id",
    "parent_wf_id",
    "retry_wf_id",
    "run_id",
)
_GATE_FIELDS = (
    "gate_events",
    "approvals",
    "approval_error",
    "approval_rejections",
    "command_rejections",
    "command_error",
    "deploy_command",
    "command_ids",
)


def _canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha256(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _payload(detail: dict) -> dict:
    result = detail.get("result") or {}
    status = detail.get("status") or {}
    return {**status, **{key: value for key, value in result.items() if value is not None}}


def _linked_summary(detail: dict) -> dict:
    payload = _payload(detail)
    alert = payload.get("alert") or detail.get("alert") or {}
    return {
        "wf_id": detail.get("wf_id"),
        "run_id": detail.get("run_id"),
        "exec_status": detail.get("exec_status"),
        "stage": payload.get("stage"),
        "requested_by": payload.get("requested_by") or "",
        "parent_wf_id": payload.get("parent_wf_id") or "",
        "retry_wf_id": payload.get("retry_wf_id") or "",
        "patch_id": payload.get("patch_id") or "",
        "alert_id": alert.get("alert_id") or payload.get("alert_id") or "",
        "service": alert.get("service") or payload.get("service") or "",
        "start_time": detail.get("start_time"),
        "close_time": detail.get("close_time"),
    }


async def _fetch_link(wf_id: str, relation: str) -> dict:
    try:
        detail = await asyncio.wait_for(gw.flow_detail(wf_id), timeout=5)
        return {**_linked_summary(detail), "relation": relation}
    except Exception as exc:  # noqa: BLE001 - 关联流程查询失败只降级标记
        return {"wf_id": wf_id, "relation": relation, "error": f"{type(exc).__name__}: {exc}"}


async def _retry_chain(detail: dict, escalations: list[dict]) -> list[dict]:
    """向上追父流程、向下追重试流程（最多各 5 跳），并合并待办中的重试关联。"""
    payload = _payload(detail)
    self_id = str(detail.get("wf_id") or "")
    chain: list[dict] = [{**_linked_summary(detail), "relation": "self"}]
    seen = {self_id}

    parents: list[dict] = []
    parent = str(payload.get("parent_wf_id") or "")
    hops = 0
    while parent and parent not in seen and hops < 5:
        seen.add(parent)
        link = await _fetch_link(parent, "parent")
        parents.append(link)
        parent = str(link.get("parent_wf_id") or "")
        hops += 1

    children: list[dict] = []
    child = str(payload.get("retry_wf_id") or "")
    for entry in escalations:
        candidate = str(entry.get("retry_wf_id") or "")
        if candidate and str(entry.get("wf_id") or "") == self_id and candidate not in seen:
            child = child or candidate
    hops = 0
    while child and child not in seen and hops < 5:
        seen.add(child)
        link = await _fetch_link(child, "retry")
        children.append(link)
        child = str(link.get("retry_wf_id") or "")
        hops += 1

    return list(reversed(parents)) + chain + children


async def build(
    wf_id: str,
    *,
    detail: dict | None = None,
    actor: str = "",
    run_id: str | None = None,
    sync_history: bool = True,
) -> dict:
    """构造证据包（不含落审计）；调用方负责写导出审计。"""
    if detail is None:
        detail = await gw.flow_detail(wf_id)
    if run_id:
        detail = {**detail, "run_id": run_id}
    payload = _payload(detail)
    alert = payload.get("alert") or detail.get("alert") or {}
    alert_id = str(alert.get("alert_id") or payload.get("alert_id") or "")
    patch_id = str(payload.get("patch_id") or "")

    sync: dict = {
        "attempted": bool(sync_history),
        "ok": False,
        "error": "",
        "result": None,
    }
    if sync_history:
        try:
            sync["result"] = await asyncio.wait_for(
                history_projector.sync_workflow(wf_id, detail.get("run_id")), timeout=15
            )
            sync["ok"] = not sync["result"].get("error")
            sync["error"] = sync["result"].get("error") or ""
        except Exception as exc:  # noqa: BLE001 - 最终同步失败仍导出已投影部分并显式标记
            sync["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("[evidence] %s 导出前 History 最终同步失败：%s", wf_id, sync["error"])

    history = await asyncio.to_thread(
        history_projector.read_events,
        wf_id,
        detail.get("run_id"),
        after_id=0,
        limit=100000,
        event_type="",
        include_detail=True,
    )
    audit_rows = await asyncio.to_thread(audit.rows_for_workflow, wf_id, 2000)
    command_rows = await asyncio.to_thread(
        commands.list_commands, wf_id=wf_id, limit=500, oldest_first=True
    )
    notifications = await asyncio.to_thread(
        timeline.notification_records, wf_id, alert_id, patch_id
    )
    escalation_rows = await asyncio.to_thread(timeline.escalation_records, wf_id)
    log_evidence = await asyncio.to_thread(evidence_store.get, wf_id)
    retry_chain = await _retry_chain(detail, escalation_rows)

    identity = {
        "workflow": {
            "wf_id": detail.get("wf_id"),
            "run_id": detail.get("run_id"),
            "workflow_type": detail.get("workflow_type"),
            "task_queue": detail.get("task_queue"),
            "exec_status": detail.get("exec_status"),
            "start_time": detail.get("start_time"),
            "close_time": detail.get("close_time"),
        },
        "alert": alert,
        "patch_id": patch_id,
        "service": alert.get("service") or payload.get("service") or "",
        "alert_id": alert_id,
        **{key: payload.get(key) for key in _IDENTITY_FIELDS if key != "run_id"},
    }
    gates = {key: payload.get(key) for key in _GATE_FIELDS}
    gates["stage"] = payload.get("stage")
    release = {
        "stage": payload.get("stage"),
        "deploy_result": payload.get("deploy_result"),
        "patch": payload.get("patch"),
        "test_report": payload.get("test_report"),
        "root_cause": payload.get("root_cause"),
    }
    sections = {
        "workflow": identity,
        "gates": gates,
        "history": {
            "run_id": history.get("run_id"),
            "complete": history_projector.sync_status(wf_id, detail.get("run_id")).get("complete"),
            "sync": sync,
            "events": history.get("items") or [],
            "has_more": history.get("has_more"),
        },
        "audit": audit_rows,
        "commands": command_rows,
        "notifications": notifications,
        "escalations": escalation_rows,
        "manual_actions": {
            "escalations": escalation_rows,
            "retry_chain": retry_chain,
        },
        "log_evidence": log_evidence,
        "test_report": payload.get("test_report"),
        "release": release,
        "retry_chain": retry_chain,
    }
    manifest_sections = [
        {
            "name": name,
            "sha256": _sha256(value),
            "bytes": len(_canonical(value).encode("utf-8")),
        }
        for name, value in sections.items()
    ]
    root = hashlib.sha256(
        "\n".join(f"{item['name']}:{item['sha256']}" for item in manifest_sections).encode(
            "utf-8"
        )
    ).hexdigest()
    package = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": actor,
        **sections,
        "manifest": {
            "algorithm": "sha256(canonical-json)",
            "root_sha256": root,
            "sections": manifest_sections,
            "history_note": (
                "History 在导出前做一次最终同步；未终止流程可能仍有后续事件，"
                "complete=false 时不得视为最终历史"
            ),
        },
    }
    return package


async def build_and_audit(
    wf_id: str,
    *,
    actor: str,
    detail: dict | None = None,
    run_id: str | None = None,
) -> tuple[dict, dict]:
    """构造证据包并写入导出审计（fail-closed：审计写失败则不返回证据包）。"""
    package = await build(wf_id, detail=detail, actor=actor, run_id=run_id)
    manifest = package["manifest"]
    entry = await asyncio.to_thread(
        audit.write_audit,
        actor=actor,
        action="evidence:export",
        wf_id=wf_id,
        params={
            "run_id": (package["workflow"].get("workflow") or {}).get("run_id"),
            "manifest_sha256": manifest["root_sha256"],
            "sections": len(manifest["sections"]),
            "history_complete": bool(package["history"]["complete"]),
        },
        result="ok",
    )
    package["export"] = {
        "audit_event_id": entry.get("event_id") or "",
        "entry_hash": entry.get("entry_hash") or "",
        "exported_at": package["generated_at"],
        "exported_by": actor,
    }
    return package, entry
