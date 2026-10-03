"""控制台操作审计：demo = JSONL 文件 / production = MySQL ``audit_events`` 表。

每个写操作（审批 / 窗口指令 / 排队 / 配置变更）成功后被记录，前端不可绕过。

双后端说明：
    - **demo**（AIOPS_MODE=demo）：落 ``data/web-audit/audit-YYYYMMDD.jsonl``，
      保持既有形态与热行为（测试经 ``mock.patch.object(audit, "DATA_DIR", ...)`` 隔离）；
    - **production**：落 MySQL ``audit_events`` 表（索引 ts / workflow_id / actor），
      支持跨实例聚合与报表导出；两后端返回的 record 结构完全一致。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from aiops_agent import db as db_layer
from aiops_agent import mode

DATA_DIR = mode.data_root()  # 演示为 <repo>/data（既有数据零迁移）；生产走 DB，仅作兜底


def _audit_dir() -> Path:
    return DATA_DIR / "web-audit"


def _to_record(row) -> dict:
    """DB 行 → 与文件后端一致的 record 结构（``detail`` 即 ``params``）。"""
    return {
        "ts": db_layer.to_iso(row["ts"]),
        "actor": row["actor"],
        "action": row["action"],
        "wf_id": row["workflow_id"],
        "target": row["target"],
        "params": row["detail"] or {},
        "result": row["result"],
    }


def write_audit(
    *,
    actor: str,
    action: str,
    wf_id: str = "",
    params: dict | None = None,
    result: str = "ok",
    target: str | None = None,
) -> dict:
    """写入一条操作审计。

    ``wf_id`` 仅用于**工作流类**操作；**配置类**操作（如 monitored-app:*）应留空 ``wf_id``
    并把被操作对象写入 ``target``，避免前端把它误当作工作流 ID 生成链接（历史缺陷 B1）。
    ``result`` 默认 ``ok``；工作流写操作可显式传 ``signaled``。
    """
    record = {
        "ts": db_layer.now_iso(),
        "actor": actor,
        "action": action,
        "wf_id": wf_id,
        "target": target,
        "params": params or {},
        "result": result,
    }
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.begin() as conn:
            conn.execute(
                db_layer.audit_events.insert().values(
                    ts=db_layer.to_dt(record["ts"]),
                    actor=actor,
                    action=action,
                    workflow_id=wf_id or "",
                    target=target,
                    detail=record["params"],
                    result=result,
                    mode="production",
                )
            )
        return record

    directory = _audit_dir()
    directory.mkdir(parents=True, exist_ok=True)  # 先建目录再写盘，避免静默失败
    path = directory / f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_audit(limit: int = 100) -> list[dict]:
    """读取最近操作审计（最新在前）。

    demo：按文件倒序 + 行倒序，坏行跳过；
    production：``ORDER BY id DESC LIMIT n``（BIGINT 主键即写入顺序）。
    """
    if mode.is_production():
        engine = db_layer.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(db_layer.audit_events)
                .order_by(db_layer.audit_events.c.id.desc())
                .limit(limit)
            ).mappings()
            return [_to_record(row) for row in rows]

    directory = _audit_dir()
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("audit-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(rows) >= limit:
                return rows
    return rows


def _top(counter: Counter, top: int) -> list[dict]:
    """Counter → 〔key,count〕降序 Top N。"""
    return [{"key": key, "count": count} for key, count in counter.most_common(top)]


def _parse_ts(value: str) -> datetime | None:
    """ISO 字符串 → aware UTC datetime（坏值返回 None）。"""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aggregate_audit(*, days: int = 30, top: int = 10, sample_limit: int = 5000) -> dict:
    """审计报表聚合（P3-07）：按操作者 / 动作 / 日期 / 结果聚合计数。

    - production：SQL ``GROUP BY`` 全量聚合（无采样截断）；
    - demo：读最近 ``sample_limit`` 条后内存聚合（``truncated=true`` 时报表为
      采样口径，仅作趋势参考）。

    返回 ``{total, days, truncated, by_actor, by_action, by_day, by_result, latest_ts}``；
    各分组为 ``[{key, count}]``，除 ``by_day`` 按日期升序（趋势）外均降序 Top N。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if mode.is_production():
        table = db_layer.audit_events
        where = table.c.ts >= db_layer.to_dt(cutoff)
        engine = db_layer.get_engine()

        def _group(column):  # noqa: ANN001, ANN202 - 内部辅助
            return [
                {"key": str(key or "unknown"), "count": int(count)}
                for key, count in conn.execute(
                    select(column, func.count())
                    .where(where)
                    .group_by(column)
                    .order_by(func.count().desc())
                    .limit(top)
                ).all()
            ]

        with engine.connect() as conn:
            total = int(conn.execute(select(func.count()).select_from(table).where(where)).scalar() or 0)
            latest = conn.execute(select(func.max(table.c.ts)).where(where)).scalar()
            by_actor = _group(table.c.actor)
            by_action = _group(table.c.action)
            by_result = _group(table.c.result)
            by_day = [
                {"key": str(day), "count": int(count)}
                for day, count in conn.execute(
                    select(func.date(table.c.ts), func.count())
                    .where(where)
                    .group_by(func.date(table.c.ts))
                    .order_by(func.date(table.c.ts))
                ).all()
            ]
        return {
            "total": total,
            "days": days,
            "truncated": False,
            "by_actor": by_actor,
            "by_action": by_action,
            "by_day": by_day,
            "by_result": by_result,
            "latest_ts": db_layer.to_iso(latest),
        }

    rows = read_audit(limit=sample_limit)
    actors: Counter = Counter()
    actions: Counter = Counter()
    results: Counter = Counter()
    days_counter: Counter = Counter()
    latest_ts: str | None = None
    total = 0
    for row in rows:  # 最新在前
        parsed = _parse_ts(row.get("ts") or "")
        if parsed is None or parsed < cutoff:
            continue
        total += 1
        if latest_ts is None:
            latest_ts = row.get("ts")
        actors[str(row.get("actor") or "unknown")] += 1
        actions[str(row.get("action") or "unknown")] += 1
        results[str(row.get("result") or "unknown")] += 1
        days_counter[parsed.strftime("%Y-%m-%d")] += 1
    return {
        "total": total,
        "days": days,
        "truncated": len(rows) >= sample_limit,
        "by_actor": _top(actors, top),
        "by_action": _top(actions, top),
        "by_day": [{"key": day, "count": count} for day, count in sorted(days_counter.items())],
        "by_result": _top(results, top),
        "latest_ts": latest_ts,
    }
