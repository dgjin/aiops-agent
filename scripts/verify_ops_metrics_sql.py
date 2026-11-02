"""M5 运营指标数据面核对：接口聚合值 vs 独立 SQL 手算。

只读脚本。用同一时间窗口分别走两条路径：

- 接口路径：``bff.ops_warehouse.load`` 取数 + ``bff.ops_metrics.compute`` 聚合；
- SQL 路径：直接对 ``workflow_runs`` / ``audit_events`` / ``log_evidence``
  做独立聚合（JSON 事件链用 ``JSON_TABLE`` 展开）。

两者逐项比对，用于真实环境抽样验收（分母、分子、比率、证据分布）。
SQL 侧依赖 MySQL 8.0+ 的 ``JSON_TABLE``；非 MySQL 数据面不适用。
退出码：0 = 全部一致；1 = 存在口径差异；2 = 环境不具备（非 production）。

用法：

    .venv/bin/python scripts/verify_ops_metrics_sql.py
    .venv/bin/python scripts/verify_ops_metrics_sql.py --days 7
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from aiops_agent import config, db as db_layer, log_health, mode  # noqa: E402,F401
from bff import escalations, ops_metrics, ops_warehouse  # noqa: E402

WINDOW = (
    "r.close_time IS NOT NULL AND r.close_time >= :cutoff "
    "AND r.exec_status <> 'RUNNING'"
)
INTERVENTION_OR = " OR ".join(
    f"a.action LIKE '{prefix}%'" for prefix in ops_metrics.INTERVENTION_PREFIXES
)
GATE_OR = " OR ".join(
    f"jt.ev LIKE '{prefix}%'" for prefix, _label in ops_metrics.GATE_FAMILIES
)
OBSERVABILITY = set(ops_metrics.OBSERVABILITY_STATUSES)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return round(ordered[index], 1)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def sql_metrics(conn, cutoff: datetime) -> dict:
    """独立 SQL 聚合；不调用 ops_metrics 的取数/映射代码。"""
    def one(sql: str, **params):
        return conn.execute(text(sql), {"cutoff": cutoff, **params}).scalar()

    def rows(sql: str, **params):
        return conn.execute(text(sql), {"cutoff": cutoff, **params}).mappings().all()

    result: dict = {}
    counts = rows(
        f"SELECT COUNT(*) closed, COALESCE(SUM(r.stage = 'DONE'), 0) done "
        f"FROM workflow_runs r WHERE {WINDOW}"
    )[0]
    result["closed_total"] = int(counts["closed"])
    result["done"] = int(counts["done"])

    durations = [
        float(row["duration_seconds"])
        for row in rows(
            f"SELECT r.duration_seconds FROM workflow_runs r WHERE {WINDOW} "
            "AND r.stage = 'DONE' AND r.duration_seconds IS NOT NULL"
        )
    ]
    result["mttr_avg"] = _avg(durations)
    result["mttr_p95"] = _p95(durations)
    result["mttr_sample"] = len(durations)

    result["by_kind"] = {
        str(row["kind"]): {"closed": int(row["closed"]), "done": int(row["done"])}
        for row in rows(
            "SELECT COALESCE(r.kind, 'alert') kind, COUNT(*) closed, "
            "COALESCE(SUM(r.stage = 'DONE'), 0) done "
            f"FROM workflow_runs r WHERE {WINDOW} GROUP BY kind"
        )
    }

    result["by_service"] = [
        {
            "service": str(row["service"]),
            "closed": int(row["closed"]),
            "done": int(row["done"]),
        }
        for row in rows(
            "SELECT COALESCE(NULLIF(NULLIF(JSON_UNQUOTE("
            "JSON_EXTRACT(r.alert, '$.service')), ''), 'null'), 'unknown') service, "
            f"COUNT(*) closed, COALESCE(SUM(r.stage = 'DONE'), 0) done "
            f"FROM workflow_runs r WHERE {WINDOW} "
            "GROUP BY service ORDER BY closed DESC, service ASC LIMIT 10"
        )
    ]

    # 注意：MySQL 8.0 对「相关 EXISTS + JSON_TABLE」会返回空集（0.0.46 实测），
    # 这里必须用直接 JOIN；接口侧走 result 字段解析，不受该限制影响。
    result["gate_breakdown"] = {
        prefix: int(
            one(
                f"SELECT COUNT(DISTINCT r.workflow_id) FROM workflow_runs r "
                "JOIN JSON_TABLE(r.result, '$.gate_events[*]' "
                "COLUMNS(ev VARCHAR(255) PATH '$')) jt "
                f"WHERE {WINDOW} AND LEFT(jt.ev, :plen) = :prefix",
                plen=len(prefix),
                prefix=prefix,
            )
            or 0
        )
        for prefix, _label in ops_metrics.GATE_FAMILIES
    }
    result["gate_blocked"] = int(
        one(
            f"SELECT COUNT(DISTINCT r.workflow_id) FROM workflow_runs r "
            "JOIN JSON_TABLE(r.result, '$.gate_events[*]' "
            f"COLUMNS(ev VARCHAR(255) PATH '$')) jt WHERE {WINDOW} AND ({GATE_OR})"
        )
        or 0
    )

    result["intervened"] = int(
        one(
            "SELECT COUNT(DISTINCT r.workflow_id) FROM workflow_runs r "
            f"JOIN audit_events a ON a.workflow_id = r.workflow_id "
            f"WHERE {WINDOW} AND a.ts >= :cutoff AND ({INTERVENTION_OR})"
        )
        or 0
    )

    started = {
        str(row["workflow_id"]): row["start_time"]
        for row in rows(
            f"SELECT r.workflow_id, r.start_time FROM workflow_runs r "
            f"WHERE {WINDOW} AND r.start_time IS NOT NULL"
        )
    }
    first = {
        str(row["workflow_id"]): row["ts"]
        for row in rows(
            "SELECT a.workflow_id, MIN(a.ts) ts FROM audit_events a "
            "WHERE a.ts >= :cutoff AND (" + INTERVENTION_OR + ") GROUP BY a.workflow_id"
        )
    }
    deltas = [
        max(0.0, (first[wf_id] - start).total_seconds())
        for wf_id, start in started.items()
        if wf_id in first and first[wf_id] >= start
    ]
    result["mtta_avg"] = _avg(deltas)
    result["mtta_p95"] = _p95(deltas)
    result["mtta_sample"] = len(deltas)

    statuses = {
        str(row["status"]): int(row["n"])
        for row in rows(
            "SELECT COALESCE(NULLIF(NULLIF(JSON_UNQUOTE("
            "JSON_EXTRACT(e.quality, '$.status')), ''), 'null'), 'UNKNOWN') status, "
            "COUNT(*) n FROM log_evidence e WHERE e.archived_at >= :cutoff "
            "GROUP BY status"
        )
    }
    total = sum(statuses.values())
    result["evidence_total"] = total
    result["evidence_statuses"] = statuses
    result["evidence_ok"] = statuses.get("OK", 0)
    result["evidence_observability"] = sum(
        count for status, count in statuses.items() if status in OBSERVABILITY
    )

    degraded = int(
        one(
            f"SELECT COUNT(DISTINCT r.workflow_id) FROM workflow_runs r "
            "JOIN JSON_TABLE(r.result, '$.gate_events[*]' "
            "COLUMNS(ev VARCHAR(255) PATH '$')) jt "
            f"WHERE {WINDOW} AND jt.ev LIKE 'canary:degraded%'"
        )
        or 0
    )
    released = int(
        one(
            f"SELECT COUNT(DISTINCT r.workflow_id) FROM workflow_runs r "
            "JOIN JSON_TABLE(r.result, '$.gate_events[*]' "
            "COLUMNS(ev VARCHAR(255) PATH '$')) jt "
            f"WHERE {WINDOW} AND (jt.ev LIKE 'canary:degraded%' "
            "OR jt.ev LIKE 'canary:healthy%')"
        )
        or 0
    )
    result["rollback_degraded"] = degraded
    result["rollback_released"] = released
    return result


def api_metrics(days: int, now: datetime) -> tuple[dict, dict]:
    plane = ops_warehouse.load(days, now)
    if plane["degraded"]:
        raise RuntimeError(f"数据面降级：{plane['degraded']}")
    data = ops_metrics.compute(
        plane["items"],
        plane["audit_rows"],
        escalations.stats(),
        days=days,
        now=now,
        esc_entries=escalations.list_all(),
        evidence_rows=plane["evidence_rows"],
        log_ship=log_health.snapshot(),
        managed_services=plane["managed_services"],
        source=plane["source"],
    )
    return plane, data


def compare(api: dict, sql: dict) -> list[tuple[str, object, object, bool]]:
    checks: list[tuple[str, object, object, bool]] = []

    def add(name: str, left, right, *, tol: float = 0.0):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            ok = abs(float(left) - float(right)) <= tol
        else:
            ok = left == right
        checks.append((name, left, right, ok))

    add("closed_total", api["closed_total"], sql["closed_total"])
    add("auto_fix_sample(done)", api["auto_fix_sample"], sql["done"])
    add(
        "auto_fix_rate",
        api["auto_fix_rate"],
        _rate(sql["done"], sql["closed_total"]),
        tol=1e-4,
    )
    add("mttr.sample", api["mttr"]["sample"], sql["mttr_sample"])
    add("mttr.avg_seconds", api["mttr"]["avg_seconds"], sql["mttr_avg"], tol=0.1)
    add("mttr.p95_seconds", api["mttr"]["p95_seconds"], sql["mttr_p95"], tol=0.1)
    add(
        "human_intervention_sample",
        api["human_intervention_sample"],
        sql["intervened"],
    )
    add(
        "human_intervention_rate",
        api["human_intervention_rate"],
        _rate(sql["intervened"], sql["closed_total"]),
        tol=1e-4,
    )
    add("mtta.sample", api["mtta"]["sample"], sql["mtta_sample"])
    add("mtta.avg_seconds", api["mtta"]["avg_seconds"], sql["mtta_avg"], tol=0.1)
    add("mtta.p95_seconds", api["mtta"]["p95_seconds"], sql["mtta_p95"], tol=0.1)
    add("gate_block_sample", api["gate_block_sample"], sql["gate_blocked"])
    add(
        "gate_block_rate",
        api["gate_block_rate"],
        _rate(sql["gate_blocked"], sql["closed_total"]),
        tol=1e-4,
    )
    for block in api["gate_breakdown"]:
        add(
            f"gate:{block['key']}",
            block["count"],
            sql["gate_breakdown"][block["key"]],
        )

    for kind, _label in ops_metrics.KINDS:
        block = api["by_kind"][kind]
        expected = sql["by_kind"].get(kind, {"closed": 0, "done": 0})
        add(f"by_kind:{kind}.closed", block["closed"], expected["closed"])
        add(f"by_kind:{kind}.done", block["done"], expected["done"])

    add(
        "by_service.top10",
        [(row["service"], row["closed"], row["done"]) for row in api["by_service"]],
        [
            (row["service"], row["closed"], row["done"])
            for row in sql["by_service"]
        ],
    )

    add("rollback.sample", api["rollback"]["sample"], sql["rollback_degraded"])
    add("rollback.released", api["rollback"]["released"], sql["rollback_released"])
    add(
        "rollback.rate",
        api["rollback"]["rate"],
        _rate(sql["rollback_degraded"], sql["rollback_released"]),
        tol=1e-4,
    )

    evidence = api["evidence"]
    add("evidence.sample", evidence["sample"], sql["evidence_total"])
    add(
        "evidence.ok_rate",
        evidence["ok_rate"],
        _rate(sql["evidence_ok"], sql["evidence_total"]),
        tol=1e-4,
    )
    add(
        "evidence.observability_share",
        evidence["observability_share"],
        _rate(sql["evidence_observability"], sql["evidence_total"]),
        tol=1e-4,
    )
    for entry in evidence["by_status"]:
        add(
            f"evidence.status:{entry['key']}",
            entry["count"],
            sql["evidence_statuses"].get(entry["key"], 0),
        )
    extra = sorted(set(sql["evidence_statuses"]) - set(ops_metrics.EVIDENCE_STATUSES))
    add("evidence.status:unexpected", [], extra)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 运营指标 SQL 抽样比对")
    parser.add_argument("--days", type=int, default=7, help="窗口天数（默认 7）")
    args = parser.parse_args()

    if not mode.is_production():
        print("环境不具备：仅 production 数据面可做 SQL 抽样比对")
        return 2

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.days)).astimezone(timezone.utc).replace(tzinfo=None)
    print(f"窗口 {args.days}d  截止 {now.isoformat(timespec='seconds')}  "
          f"cutoff(UTC naive) {cutoff.isoformat(timespec='seconds')}")

    engine = db_layer.get_engine()
    with engine.connect() as conn:
        sql = sql_metrics(conn, cutoff)
    plane, api = api_metrics(args.days, now)

    print(f"source={api['source']}  mirror_updated_at={plane['mirror_updated_at']}  "
          f"degraded={plane['degraded']}  closed={api['closed_total']}")
    print()
    print(f"{'指标':<34}{'接口值':<16}{'SQL 值':<16}结果")
    checks = compare(api, sql)
    for name, left, right, ok in checks:
        print(f"{name:<34}{str(left):<16}{str(right):<16}{'OK' if ok else 'MISMATCH'}")
    failed = [name for name, _left, _right, ok in checks if not ok]
    print()
    print(f"共 {len(checks)} 项，差异 {len(failed)} 项")
    if failed:
        print("差异项：" + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
