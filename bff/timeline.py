"""统一时间线（M4）：Temporal History / 审批闸门 / 操作审计 / 命令 / 通知 / 待办合并。

读取口径：

- **History**：读投影存储（``workflow_events`` / demo JSONL），不在请求内全量拉取
  Temporal；运行中的最新阶段仍由 ``status`` 快照补充，避免等待投影周期；
- **审批 / 闸门**：取 ``status`` 或终态 ``result`` 中的 ``gate_events`` / ``approvals`` /
  ``approval_rejections``；审批记录带 ``received_at`` 时使用真实时间，缺失时标
  ``time_estimated``（旧流程没有时间字段）；
- **操作审计**：``bff.audit.rows_for_workflow``（哈希链记录）；
- **命令**：outbox 的登记 / 送达 / 回执节点，直接反映投递状态；
- **通知**：``data/notify/*.json`` 留痕，按卡片内 ``wf_id`` 或文件名前缀匹配；
- **人工待办**：``bff.escalations`` 条目与处置历史。

排序按时间升序；无时间来源的闸门节点使用流程快照时间并显式标记 ``time_estimated``。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from aiops_agent import notify

from . import audit, commands, escalations, history_projector
from .deps import log

_SKIP_HISTORY_PREFIXES = (
    "EVENT_TYPE_WORKFLOW_TASK_",
    "EVENT_TYPE_ACTIVITY_TASK_STARTED",
    "EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED",
)

_HISTORY_TITLES = {
    "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED": "流程启动",
    "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED": "流程完成",
    "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED": "流程失败",
    "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED": "流程取消",
    "EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED": "流程终止",
    "EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT": "流程超时",
    "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW": "流程续跑",
    "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED": "活动调度",
    "EVENT_TYPE_ACTIVITY_TASK_COMPLETED": "活动完成",
    "EVENT_TYPE_ACTIVITY_TASK_FAILED": "活动失败",
    "EVENT_TYPE_ACTIVITY_TASK_CANCELED": "活动取消",
    "EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION": "收到外部信号",
    "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED": "收到信号",
    "EVENT_TYPE_TIMER_FIRED": "定时器触发",
    "EVENT_TYPE_MARKER_RECORDED": "标记记录",
    "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATED": "流程更新",
}

_NOTIFY_PREFIX_KEYS = {
    "approval": "patch_id",
    "broadcast": "patch_id",
    "shadow": "patch_id",
    "intake": "alert_id",
    "escalation": "alert_id",
    "fix-start": "alert_id",
    "req-intake": "alert_id",
}

_notify_cache: tuple[str, float, list[dict]] | None = None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sort_key(item: dict) -> tuple[float, str]:
    parsed = _parse(item.get("time"))
    return (parsed.timestamp() if parsed else 0.0, str(item.get("id") or ""))


def _attributes(detail: dict) -> dict:
    if not isinstance(detail, dict):
        return {}
    for key, value in detail.items():
        if key.endswith("_event_attributes") and isinstance(value, dict):
            return value
    return {}


def _name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _history_item(event: dict) -> dict:
    event_type = str(event.get("event_type") or "")
    detail = event.get("detail") or {}
    attrs = _attributes(detail)
    activity = _name(attrs.get("activity_type"))
    signal = str(attrs.get("signal_name") or "")
    timer = str(attrs.get("timer_id") or "")
    workflow_type = _name(attrs.get("workflow_type"))
    title = _HISTORY_TITLES.get(event_type, event_type.replace("EVENT_TYPE_", "").lower())
    if activity:
        title = f"{title} · {activity}"
    elif signal:
        title = f"{title} · {signal}"
    elif timer:
        title = f"{title} · {timer}"
    elif workflow_type:
        title = f"{title} · {workflow_type}"

    summary_parts: list[str] = []
    if attrs.get("attempt"):
        summary_parts.append(f"attempt={attrs['attempt']}")
    if attrs.get("identity"):
        summary_parts.append(f"identity={attrs['identity']}")
    failure = attrs.get("failure") or attrs.get("last_failure") or {}
    if isinstance(failure, dict) and failure.get("message"):
        summary_parts.append(str(failure["message"])[:200])
    result = attrs.get("result")
    if isinstance(result, dict) and result.get("failed"):
        summary_parts.append("result=failed")
    return {
        "id": f"history:{event.get('event_id')}",
        "source": "history",
        "kind": event_type,
        "title": title,
        "summary": " · ".join(summary_parts),
        "time": event.get("event_time"),
        "time_estimated": not bool(event.get("event_time")),
        "ref": {
            "event_id": event.get("event_id"),
            "activity_type": activity,
            "signal_name": signal,
            "timer_id": timer,
        },
        "detail": event.get("detail"),
    }


def _snapshot_payload(detail: dict) -> tuple[dict, str | None]:
    result = detail.get("result") or {}
    status = detail.get("status") or {}
    payload = {**status, **{key: value for key, value in result.items() if value is not None}}
    snapshot_time = (
        detail.get("close_time") or detail.get("start_time") or datetime.now(timezone.utc).isoformat()
    )
    return payload, snapshot_time


def _snapshot_items(detail: dict) -> list[dict]:
    payload, stamp = _snapshot_payload(detail)
    items: list[dict] = []

    for index, raw in enumerate(payload.get("gate_events") or []):
        items.append(
            {
                "id": f"gate:{index}",
                "source": "status",
                "kind": "gate",
                "title": str(raw),
                "summary": "闸门事件（时间取自流程快照）",
                "time": stamp,
                "time_estimated": True,
                "ref": {"gate_event": str(raw)},
            }
        )

    for index, record in enumerate(payload.get("approvals") or []):
        if not isinstance(record, dict):
            continue
        gate = str(record.get("gate") or "approval")
        decision = str(record.get("decision") or "")
        at = record.get("received_at") or record.get("submitted_at") or record.get("at")
        items.append(
            {
                "id": f"approval:{gate}:{index}",
                "source": "status",
                "kind": f"approval:{gate}",
                "title": f"审批 {gate}：{decision or '未记录'}",
                "summary": " · ".join(
                    part
                    for part in (
                        str(record.get("actor") or ""),
                        str(record.get("channel") or ""),
                        str(record.get("comment") or "")[:120],
                    )
                    if part
                ),
                "time": at or stamp,
                "time_estimated": not bool(at),
                "ref": {
                    "gate": gate,
                    "decision": decision,
                    "actor": record.get("actor") or "",
                    "command_id": record.get("command_id") or "",
                },
            }
        )

    for index, record in enumerate(payload.get("approval_rejections") or []):
        if not isinstance(record, dict):
            continue
        items.append(
            {
                "id": f"approval-rejected:{index}",
                "source": "status",
                "kind": "approval:rejected",
                "title": f"审批信号被拒：{record.get('gate') or ''}",
                "summary": f"{record.get('code') or ''} {record.get('reason') or ''}".strip(),
                "time": record.get("at") or stamp,
                "time_estimated": not bool(record.get("at")),
                "ref": dict(record),
            }
        )

    error = payload.get("approval_error")
    if isinstance(error, dict) and error:
        items.append(
            {
                "id": "approval-error",
                "source": "status",
                "kind": "approval:error",
                "title": "审批错误",
                "summary": str(error.get("reason") or error.get("code") or ""),
                "time": error.get("at") or stamp,
                "time_estimated": not bool(error.get("at")),
                "ref": dict(error),
            }
        )

    for index, record in enumerate(payload.get("command_rejections") or []):
        if not isinstance(record, dict):
            continue
        items.append(
            {
                "id": f"command-rejected:{index}",
                "source": "status",
                "kind": "command:rejected",
                "title": f"命令被拒：{record.get('command_type') or ''}",
                "summary": str(record.get("reason") or record.get("code") or ""),
                "time": record.get("at") or stamp,
                "time_estimated": not bool(record.get("at")),
                "ref": dict(record),
            }
        )

    deploy = payload.get("deploy_result")
    if deploy:
        items.append(
            {
                "id": "release:deploy",
                "source": "release",
                "kind": "release:deploy",
                "title": "发布结果",
                "summary": json.dumps(deploy, ensure_ascii=False)[:300] if not isinstance(deploy, str) else deploy[:300],
                "time": detail.get("close_time") or stamp,
                "time_estimated": True,
                "ref": {"deploy_result": deploy},
            }
        )

    stage = payload.get("stage")
    if stage:
        items.append(
            {
                "id": "workflow:stage",
                "source": "status",
                "kind": "stage",
                "title": f"流程阶段：{stage}",
                "summary": " · ".join(
                    part
                    for part in (
                        f"patch={payload.get('patch_id')}" if payload.get("patch_id") else "",
                        f"confidence={payload.get('confidence')}"
                        if payload.get("confidence") is not None
                        else "",
                    )
                    if part
                ),
                "time": detail.get("close_time") or stamp,
                "time_estimated": not bool(detail.get("close_time")),
                "ref": {"stage": stage},
            }
        )
    return items


def _command_items(wf_id: str) -> list[dict]:
    try:
        rows = commands.list_commands(wf_id=wf_id, limit=200, oldest_first=True)
    except Exception as exc:  # noqa: BLE001 - 时间线不因命令存储故障不可用
        log.warning("[timeline] 命令读取失败：%s", exc)
        return []
    items: list[dict] = []
    for record in rows:
        command_id = str(record.get("command_id") or "")
        command_type = str(record.get("command_type") or "")
        actor = str(record.get("actor") or "")
        base = {
            "source": "command",
            "ref": {
                "command_id": command_id,
                "command_type": command_type,
                "status": record.get("status"),
                "actor": actor,
                "channel": record.get("channel"),
            },
        }
        items.append(
            {
                **base,
                "id": f"command:{command_id}:created",
                "kind": f"command:{command_type}",
                "title": f"命令登记：{record.get('audit_action') or command_type}",
                "summary": f"{actor} · {record.get('channel') or ''}".strip(" ·"),
                "time": record.get("created_at"),
            }
        )
        if record.get("submitted_at"):
            items.append(
                {
                    **base,
                    "id": f"command:{command_id}:submitted",
                    "kind": f"command:{command_type}:submitted",
                    "title": "命令已送达 Temporal",
                    "summary": f"attempts={record.get('attempts') or 0}",
                    "time": record.get("submitted_at"),
                }
            )
        if record.get("completed_at"):
            items.append(
                {
                    **base,
                    "id": f"command:{command_id}:completed",
                    "kind": f"command:{command_type}:completed",
                    "title": f"命令回执：{record.get('status')}",
                    "summary": str(record.get("last_error") or ""),
                    "time": record.get("completed_at"),
                }
            )
    return items


def _collect_wf_ids(value, found: set[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("wf_id", "workflow_id") and isinstance(item, str) and item:
                found.add(item)
            else:
                _collect_wf_ids(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_wf_ids(item, found, depth + 1)


def _notify_records() -> list[dict]:
    """扫描通知留痕（5s 缓存，避免时间线轮询反复读盘）。"""
    global _notify_cache
    directory = Path(notify.NOTIFY_DIR)
    key = str(directory)
    now = datetime.now(timezone.utc).timestamp()
    if _notify_cache and _notify_cache[0] == key and now - _notify_cache[1] < 5:
        return _notify_cache[2]
    rows: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            rows.append(
                {
                    "msg_id": path.stem,
                    "path": str(path),
                    "mtime": datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "record": value,
                }
            )
    if len(rows) > 5000:
        rows = rows[-5000:]
    _notify_cache = (key, now, rows)
    return rows


def _notification_matches(record: dict, wf_id: str, alert_id: str, patch_id: str) -> bool:
    found: set[str] = set()
    _collect_wf_ids(record.get("record"), found)
    if wf_id and wf_id in found:
        return True
    msg_id = str(record.get("msg_id") or "")
    prefix, _, suffix = msg_id.partition("-")
    target = _NOTIFY_PREFIX_KEYS.get(prefix)
    if target == "patch_id" and patch_id and suffix == patch_id:
        return True
    if target == "alert_id" and alert_id and suffix == alert_id:
        return True
    return False


def _notification_items(wf_id: str, alert_id: str, patch_id: str) -> list[dict]:
    items: list[dict] = []
    for record in _notify_records():
        if not _notification_matches(record, wf_id, alert_id, patch_id):
            continue
        payload = record.get("record") or {}
        card = payload.get("card") or {}
        title = ""
        try:
            title = str(
                ((card.get("header") or {}).get("title") or {}).get("content") or ""
            )
        except (AttributeError, TypeError):
            title = ""
        msg_id = str(record.get("msg_id") or "")
        items.append(
            {
                "id": f"notify:{msg_id}",
                "source": "notification",
                "kind": f"notify:{msg_id.partition('-')[0]}",
                "title": title or f"通知：{msg_id}",
                "summary": msg_id,
                "time": record.get("mtime"),
                "ref": {"msg_id": msg_id, "path": record.get("path")},
            }
        )
    return items


def _escalation_entries(wf_id: str) -> list[dict]:
    try:
        rows = escalations.list_all()
    except Exception as exc:  # noqa: BLE001 - 待办存储故障不阻塞时间线
        log.warning("[timeline] 待办读取失败：%s", exc)
        return []
    return [
        entry
        for entry in rows
        if str(entry.get("wf_id") or "") == wf_id
        or str(entry.get("retry_wf_id") or "") == wf_id
    ]


def _escalation_items(wf_id: str) -> list[dict]:
    items: list[dict] = []
    for entry in _escalation_entries(wf_id):
        entry_id = str(entry.get("id") or "")
        items.append(
            {
                "id": f"escalation:{entry_id}:open",
                "source": "escalation",
                "kind": "escalation:open",
                "title": "转人工待办登记",
                "summary": str(entry.get("auto_reason") or "")[:200],
                "time": entry.get("escalated_at"),
                "ref": {
                    "id": entry_id,
                    "status": entry.get("status"),
                    "assignee": entry.get("assignee"),
                    "retry_wf_id": entry.get("retry_wf_id"),
                },
            }
        )
        for index, history in enumerate(entry.get("history") or []):
            if not isinstance(history, dict):
                continue
            items.append(
                {
                    "id": f"escalation:{entry_id}:{index}",
                    "source": "escalation",
                    "kind": f"escalation:{history.get('action') or 'action'}",
                    "title": f"待办处置：{history.get('action') or ''}",
                    "summary": " · ".join(
                        part
                        for part in (
                            str(history.get("actor") or ""),
                            str(history.get("detail") or "")[:160],
                        )
                        if part
                    ),
                    "time": history.get("ts"),
                    "ref": {"id": entry_id},
                }
            )
    return items


def _audit_items(wf_id: str) -> list[dict]:
    try:
        rows = audit.rows_for_workflow(wf_id)
    except Exception as exc:  # noqa: BLE001 - 审计读取失败不阻塞其他来源
        log.warning("[timeline] 操作审计读取失败：%s", exc)
        return []
    items: list[dict] = []
    for row in rows:
        event_id = str(row.get("event_id") or row.get("id") or "")
        items.append(
            {
                "id": f"audit:{event_id}",
                "source": "audit",
                "kind": str(row.get("action") or "audit"),
                "title": str(row.get("action") or "操作审计"),
                "summary": f"{row.get('result') or ''} · {row.get('actor') or ''}".strip(" ·"),
                "time": row.get("ts"),
                "ref": {
                    "actor": row.get("actor"),
                    "result": row.get("result"),
                    "target": row.get("target"),
                    "command_id": row.get("command_id") or "",
                    "entry_hash": row.get("entry_hash") or "",
                },
                "detail": row.get("params"),
            }
        )
    return items


def build(
    detail: dict,
    *,
    include_history_detail: bool = False,
    include_all_history: bool = False,
    history_limit: int = 5000,
    limit: int = 0,
) -> dict:
    """合并全部来源为统一时间线（同步函数；路由侧放线程池执行）。"""
    wf_id = str(detail.get("wf_id") or "")
    run_id = str(detail.get("run_id") or "")
    payload, _ = _snapshot_payload(detail)
    alert = payload.get("alert") or detail.get("alert") or {}
    alert_id = str(alert.get("alert_id") or "")
    patch_id = str(payload.get("patch_id") or (detail.get("result") or {}).get("patch_id") or "")

    history = history_projector.read_events(
        wf_id,
        run_id or None,
        limit=history_limit,
        include_detail=include_history_detail,
    )
    items: list[dict] = []
    for event in history["items"]:
        if not include_all_history and str(event.get("event_type") or "").startswith(
            _SKIP_HISTORY_PREFIXES
        ):
            continue
        items.append(_history_item(event))

    items.extend(_snapshot_items(detail))
    items.extend(_audit_items(wf_id))
    items.extend(_command_items(wf_id))
    items.extend(_notification_items(wf_id, alert_id, patch_id))
    items.extend(_escalation_items(wf_id))

    items.sort(key=_sort_key)
    has_more = False
    if limit and len(items) > limit:
        items = items[-limit:]
        has_more = True
    counts: dict[str, int] = {}
    for item in items:
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1

    return {
        "wf_id": wf_id,
        "run_id": run_id,
        "exec_status": detail.get("exec_status"),
        "stage": payload.get("stage"),
        "patch_id": patch_id,
        "alert": alert,
        "history": history_projector.sync_status(wf_id, run_id or None),
        "history_page": {
            "returned": len(history["items"]),
            "has_more": history["has_more"],
            "next_after": history["next_after"],
        },
        "items": items,
        "counts": counts,
        "has_more": has_more,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def notification_records(wf_id: str, alert_id: str = "", patch_id: str = "") -> list[dict]:
    """证据包用：返回匹配的完整通知留痕（不裁剪卡片内容）。"""
    return [
        {
            "msg_id": record["msg_id"],
            "path": record["path"],
            "mtime": record["mtime"],
            "record": record["record"],
        }
        for record in _notify_records()
        if _notification_matches(record, wf_id, alert_id, patch_id)
    ]


def escalation_records(wf_id: str) -> list[dict]:
    """证据包用：返回匹配的待办完整条目（含处置历史）。"""
    return _escalation_entries(wf_id)


def reset_cache_for_tests() -> None:
    global _notify_cache
    _notify_cache = None
