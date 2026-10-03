#!/usr/bin/env python
"""生产模式全链路冒烟（批次 9 验收 / 优化方案 P6-01）。

直连 MySQL 验证双模改造的全部存储链路（不依赖 Temporal / LLM / 集群）：

    模式判定 → 引擎与自动建表 → 审计事件（audit_events）→ 令牌注册表（console_tokens）
    → 被监控应用 CRUD（monitored_apps）→ 流程速查落库（workflow_runs）
    → kill switch（system_kv）→ 清理 TTL（DB 域 dry-run）

用法（先起本地 MySQL：docker-compose --profile prod up -d mysql redis）：

    AIOPS_DATABASE_URL='mysql+pymysql://aiops:aiops-demo-pass@127.0.0.1:3316/aiops?charset=utf8mb4' \
    AIOPS_CONSOLE_AUTH_TOKENS='{"smoke-token":{"user":"smoke","role":"admin"}}' \
    .venv/bin/python scripts/production-smoke.py

退出码：0 = 全部通过；1 = 存在失败项（逐项打印）。脚本自清理写入的测试行。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, inspect, select  # noqa: E402

from aiops_agent import cleanup, db as db_layer, kill_switch, mode  # noqa: E402
from bff import audit, auth, monitored_apps, runs_store  # noqa: E402

WF_SMOKE_ID = "smoke-wf-production-smoke"
ACTOR = "smoke"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def main() -> int:
    print(f"== AIOps 生产模式冒烟（{time.strftime('%Y-%m-%d %H:%M:%S')}）==")

    # 0) 前置：production 模式 + 已配置数据库
    if not check("模式判定为 production", mode.is_production(), f"AIOPS_MODE={mode.mode()}"):
        print("请显式设置 AIOPS_MODE=production 后重试")
        return 1
    if not check("AIOPS_DATABASE_URL 已配置", bool(mode.db_url())):
        print("请设置 AIOPS_DATABASE_URL 指向可用的 MySQL")
        return 1

    # 1) 引擎连通 + 自动建表
    engine = db_layer.get_engine()
    tables = set(inspect(engine).get_table_names())
    expected = {"system_kv", "console_tokens", "monitored_apps", "audit_events", "workflow_runs"}
    check("MySQL 连通且自动建表（create_all 幂等）", expected <= tables,
          f"缺失：{expected - tables or '无'}")

    # 2) 审计事件：写入 → 读回 → 落库校验
    record = audit.write_audit(actor=ACTOR, action="smoke:audit", target="production-smoke")
    recent = audit.read_audit(limit=5)
    hit = any(r.get("action") == "smoke:audit" for r in recent)
    with engine.connect() as conn:
        db_hit = conn.execute(
            select(func.count()).select_from(db_layer.audit_events).where(
                db_layer.audit_events.c.actor == ACTOR,
                db_layer.audit_events.c.action == "smoke:audit",
            )
        ).scalar()
    check("审计事件写入 MySQL 并可读回", hit and db_hit and db_hit >= 1,
          f"ts={record['ts']} 落库行数={db_hit}")

    # 3) 令牌注册表：播种（env）→ registry_tokens 走 DB（production 键为 sha256 指纹，
    #    用 resolve_token 走与 BFF 相同的鉴权匹配路径验证）
    raw = os.environ.get("AIOPS_CONSOLE_AUTH_TOKENS", "")
    if raw:
        tokens = auth.registry_tokens()
        try:
            identity, _record = auth.resolve_token("Bearer smoke-token", tokens=tokens)
            resolved = identity.user == "smoke" and identity.role == "admin"
        except Exception:  # noqa: BLE001 - 任何异常均视为该链路失败
            resolved = False
        with engine.connect() as conn:
            token_rows = conn.execute(
                select(func.count()).select_from(db_layer.console_tokens)
            ).scalar()
        check("令牌注册表写入/读取 MySQL（含指纹鉴权匹配）", resolved and token_rows >= 1,
              f"注册表 {len(tokens)} 项，DB 行数 {token_rows}")
    else:
        check("令牌注册表（跳过：未设置 AIOPS_CONSOLE_AUTH_TOKENS）", True, "SKIP")

    # 4) 被监控应用 CRUD（新增 → 查询 → 删除，自清理）
    app = monitored_apps.add(name="冒烟应用", url="http://127.0.0.1:19999/", note="production-smoke")
    fetched = monitored_apps.get(app["id"])
    removed = monitored_apps.remove(app["id"])
    with engine.connect() as conn:
        rows_left = conn.execute(
            select(func.count()).select_from(db_layer.monitored_apps).where(
                db_layer.monitored_apps.c.id == app["id"]
            )
        ).scalar()
    check("被监控应用 CRUD 走 MySQL", bool(fetched) and bool(removed) and rows_left == 0,
          f"id={app['id']}")

    # 5) 流程速查落库（workflow_runs upsert）
    synced = runs_store.sync_runs([{
        "wf_id": WF_SMOKE_ID, "run_id": "run-smoke", "stage": "DONE",
        "exec_status": "COMPLETED", "alert": {"service": "order"}, "confidence": 0.9,
        "patch_id": "smoke-patch", "duration_seconds": 42, "result": "ok",
    }])
    with engine.connect() as conn:
        run_row = conn.execute(
            select(db_layer.workflow_runs).where(
                db_layer.workflow_runs.c.workflow_id == WF_SMOKE_ID
            )
        ).mappings().first()
    check("workflow_runs 落库（BFF 汇总 upsert）", synced >= 1 and bool(run_row),
          f"stage={run_row['stage'] if run_row else '无'}")

    # 6) kill switch：激活 → 读回 → 复位
    kill_switch.set_state(active=True, actor=ACTOR, reason="smoke")
    active = kill_switch.is_active()
    kill_switch.set_state(active=False, actor=ACTOR, reason="smoke 复位")
    restored = not kill_switch.is_active()
    check("kill switch 状态读写（system_kv）", active and restored)

    # 7) 清理 TTL：production 分支（文件域 + DB 域）dry-run
    report = cleanup.run_cleanup(dry_run=True)
    check("cleanup dry-run（含 DB 域）", report["errors"] == [] and "workflow_runs" in report["deleted"],
          f"mode={report['mode']} errors={report['errors']}")

    # 8) 自清理：删除本脚本写入的 audit 行与 workflow_runs 行
    with engine.begin() as conn:
        conn.execute(delete(db_layer.audit_events).where(
            db_layer.audit_events.c.actor == ACTOR,
            db_layer.audit_events.c.action == "smoke:audit",
        ))
        conn.execute(delete(db_layer.workflow_runs).where(
            db_layer.workflow_runs.c.workflow_id == WF_SMOKE_ID
        ))
    print("已清理冒烟测试数据（audit_events / workflow_runs）")

    failed = [name for name, ok, _ in _results if not ok]
    print("== 结果：%d/%d 通过 ==" % (len(_results) - len(failed), len(_results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
