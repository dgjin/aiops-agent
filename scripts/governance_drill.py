"""治理闭环本机演练（M5）：把「故障注入 → 系统必须发现」固化为可重复执行的脚本。

覆盖六类场景（全部在本机临时目录完成，不连接 Temporal/MySQL/外部通知）：

1. ``audit-tamper``：修改链中记录 → 完整性校验必须失败并定位到被改行；
2. ``audit-archive``：归档并 purge 后全链仍可校验，新写入接续归档链尾；
3. ``command-replay``：同 command_id 同内容幂等、不同内容可检出冲突；
4. ``notify-sla``：recorded/failed 通知计入「未送达」，live 不计入；
5. ``history-gap``：事件断档（id 不连续）可被统计发现；
6. ``history-pagination``：250 条事件全量投影后游标分页不截断。

边界：本脚本证明「本机实现可发现这些异常」，不代表外部 WORM / 真实通知通道 /
真实 OTel Collector / 多主机并发已验收；这些仍须在目标环境单独演练。

用法：

    .venv/bin/python scripts/governance_drill.py
    .venv/bin/python scripts/governance_drill.py --scenario audit-tamper --keep

退出码：0 = 全部通过；1 = 存在失败；2 = 环境不具备（导入/初始化失败）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCENARIOS = (
    "audit-tamper",
    "audit-archive",
    "command-replay",
    "notify-sla",
    "history-gap",
    "history-pagination",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(actor: str = "drill", action: str = "approval:approve", wf_id: str = "wf-drill") -> dict:
    return {
        "ts": _now(),
        "actor": actor,
        "action": action,
        "wf_id": wf_id,
        "target": None,
        "params": {"drill": True},
        "result": "ok",
    }


def _event(event_id: int, event_type: str = "ACTIVITY_TASK_SCHEDULED") -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "event_time": _now(),
        "detail": {"event_id": event_id, "drill": True},
    }


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------


def audit_tamper(root: Path) -> dict:
    """修改链中记录：校验必须失败，并指出 broken_at。"""
    from aiops_agent import audit_chain

    directory = root / "audit-tamper"
    with audit_chain.file_lock(directory):
        entries = [
            audit_chain.append_demo_locked(directory, _record(actor=f"u{index}"), day="20990101")
            for index in range(3)
        ]
    healthy = audit_chain.verify_demo(directory)
    if not healthy["ok"] or healthy["checked"] != 3:
        return {"ok": False, "detail": f"初始链校验未通过：{healthy}"}

    (path,) = audit_chain.audit_files(directory)
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["actor"] = "tampered"
    lines[1] = json.dumps(payload, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    broken = audit_chain.verify_demo(directory)
    ok = (
        not broken["ok"]
        and "entry_hash" in str(broken.get("error") or "")
        and (broken.get("broken_at") or {}).get("event_id") == entries[1]["event_id"]
    )
    return {
        "ok": ok,
        "detail": {
            "detected": not broken["ok"],
            "error": broken.get("error"),
            "broken_at": broken.get("broken_at"),
            "expected_event_id": entries[1]["event_id"],
        },
    }


def audit_archive(root: Path) -> dict:
    """归档 purge 后：全链仍可校验，新写入接续归档链尾。"""
    from aiops_agent import audit_chain

    directory = root / "audit-archive"
    with audit_chain.file_lock(directory):
        entries = [
            audit_chain.append_demo_locked(
                directory, _record(actor="legacy"), day="20200101"
            ),
            audit_chain.append_demo_locked(
                directory, _record(actor="legacy"), day="20200101"
            ),
        ]
    archived = audit_chain.archive_demo(directory, "29991231", purge=True)
    if archived.get("error") or archived["purged"] != 1:
        return {"ok": False, "detail": f"归档未按预期执行：{archived}"}

    verified = audit_chain.verify_demo(directory)
    if not verified["ok"] or verified["checked"] != 2:
        return {"ok": False, "detail": f"purge 后链校验失败：{verified}"}

    with audit_chain.file_lock(directory):
        appended = audit_chain.append_demo_locked(directory, _record(actor="new"), day="20990101")
    # purge 删光活动文件后，新记录必须接续归档段最后一条的 entry_hash
    chained = appended["prev_hash"] == entries[-1]["entry_hash"]
    final = audit_chain.verify_demo(directory)
    return {
        "ok": bool(chained and final["ok"] and final["checked"] == 3),
        "detail": {
            "purged": archived["purged"],
            "archived": archived["archived"],
            "prev_hash_linked": chained,
            "checked": final["checked"],
        },
    }


def command_replay(root: Path) -> dict:
    """同 command_id：同内容视为重投（同摘要），不同内容可检出冲突（异摘要）。"""
    from aiops_agent import command_contract

    base = dict(
        command_type="deploy-command",
        command_id="cmd-drill-1",
        actor="drill",
        channel="console",
        submitted_at=_now(),
    )
    first = command_contract.build_envelope(
        **base, payload=command_contract.deploy_payload("deploy_now")
    )
    replay = command_contract.build_envelope(
        **base, payload=command_contract.deploy_payload("deploy_now")
    )
    conflict = command_contract.build_envelope(
        **base, payload=command_contract.deploy_payload("cancel")
    )
    idempotent = command_contract.envelope_digest(first) == command_contract.envelope_digest(replay)
    detectable = command_contract.envelope_digest(first) != command_contract.envelope_digest(conflict)
    structure = command_contract.envelope_problem(first, "deploy-command") is None
    return {
        "ok": bool(idempotent and detectable and structure),
        "detail": {
            "same_digest": idempotent,
            "conflict_digest_differs": detectable,
            "structure_valid": structure,
        },
    }


def notify_sla(root: Path) -> dict:
    """通知送达统计：recorded/failed 计入未送达，live 不计入。"""
    from bff import escalations

    entry = escalations.new_entry(
        {
            "wf_id": "wf-drill",
            "run_id": "run-drill",
            "alert": {"alert_id": "a-drill", "service": "demo-app"},
            "gate_events": ["gate2:timeout->reject"],
        }
    )
    escalations.persist_entry(entry)
    before = escalations.stats()

    recorded = escalations.apply_notify_result(
        dict(entry), {"mode": "recorded", "error": "webhook 未配置"}, 30
    )
    escalations.persist_entry(recorded, expected_version=entry["version"])
    after_recorded = escalations.stats()

    live = escalations.apply_notify_result(dict(recorded), {"mode": "live"}, 30)
    escalations.persist_entry(live, expected_version=recorded["version"])
    after_live = escalations.stats()

    return {
        "ok": bool(
            before.get("notify_pending") == 0
            and after_recorded.get("notify_pending") == 1
            and after_live.get("notify_pending") == 0
        ),
        "detail": {
            "before": before.get("notify_pending"),
            "after_recorded": after_recorded.get("notify_pending"),
            "after_live": after_live.get("notify_pending"),
        },
    }


def history_gap(root: Path) -> dict:
    """事件 id 不连续时，统计必须报 gap=1。"""
    from bff import history_projector

    history_projector._append_demo(
        "wf-gap",
        "run-gap",
        [_event(1), _event(2), _event(4)],
        complete=True,
        exec_status="COMPLETED",
        start_time=_now(),
        close_time=_now(),
    )
    snapshot = history_projector.refresh_metrics()
    stats = history_projector.stats()
    return {
        "ok": bool(stats.get("gaps") == 1 and snapshot.get("gaps") == 1),
        "detail": {"gaps": stats.get("gaps"), "runs": stats.get("runs")},
    }


def history_pagination(root: Path) -> dict:
    """250 条事件：首屏 200 + 游标翻页取完，无截断无重复。"""
    from bff import history_projector

    events = [_event(index) for index in range(1, 251)]
    history_projector._append_demo(
        "wf-page",
        "run-page",
        events,
        complete=True,
        exec_status="COMPLETED",
        start_time=_now(),
        close_time=_now(),
    )
    first = history_projector.read_events("wf-page", "run-page", limit=200)
    if not first["has_more"] or first["next_after"] != 200:
        return {"ok": False, "detail": {"first_page": first["has_more"], "cursor": first["next_after"]}}
    second = history_projector.read_events(
        "wf-page", "run-page", after_id=first["next_after"], limit=200
    )
    ids = [item["event_id"] for item in first["items"]] + [
        item["event_id"] for item in second["items"]
    ]
    return {
        "ok": bool(len(ids) == 250 and len(set(ids)) == 250 and not second["has_more"]),
        "detail": {
            "first": len(first["items"]),
            "second": len(second["items"]),
            "unique": len(set(ids)),
            "has_more_second": second["has_more"],
        },
    }


RUNNERS = {
    "audit-tamper": audit_tamper,
    "audit-archive": audit_archive,
    "command-replay": command_replay,
    "notify-sla": notify_sla,
    "history-gap": history_gap,
    "history-pagination": history_pagination,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="治理闭环本机演练（只写临时目录）")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        help="只跑指定场景（可重复）；缺省跑全部",
    )
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args(argv)

    selected = args.scenario or list(SCENARIOS)
    workdir = Path(tempfile.mkdtemp(prefix="aiops-governance-drill-"))
    os.environ["AIOPS_MODE"] = "demo"
    os.environ["AIOPS_DATA_ROOT"] = str(workdir)

    results: list[dict] = []
    try:
        for name in selected:
            try:
                outcome = RUNNERS[name](workdir)
            except Exception:  # noqa: BLE001 - 演练失败要保留堆栈摘要
                outcome = {"ok": False, "detail": traceback.format_exc(limit=3)}
            results.append({"scenario": name, **outcome})
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    passed = all(item["ok"] for item in results)
    print(
        json.dumps(
            {
                "ok": passed,
                "mode": "local-drill",
                "boundary": "本机实现演练；外部 WORM/通知通道/Collector/多主机未实测",
                "kept": str(workdir) if args.keep else "",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
