"""数据清理 TTL（优化方案 3.6 / GAP-11，任务 P5-03）。

保留期（生产 / 演示，见方案 3.6 表）：

===================  =========  =========  ==========================================
数据类型              生产保留期  演示保留期  清理方式
===================  =========  =========  ==========================================
审计事件              365 天     30 天      文件（web-audit/*.jsonl）+ 表行 DELETE
沙箱工作区            7 天       1 天       目录 rm -rf（data/sandbox/<patch_id>）
通知留痕              90 天      7 天       文件删除（data/notify/*.json）
Qoder 工作区          7 天       1 天       目录 + *.run.json 删除（data/qoder/）
ArgoCD 留痕           90 天      7 天       文件删除（data/argocd/*.json）
工作流执行记录        365 天     30 天      workflow_runs 表行 DELETE
===================  =========  =========  ==========================================

入口：

- :func:`run_cleanup`：编排全部域并返回结构化报告（单域失败不阻塞其余域）；
- ``python -m aiops_agent.cleanup``：生产 K8s CronJob 定时执行（默认非 dry-run）；
- ``demo_cli.py cleanup``：演示/手动执行（``--dry-run`` 预演）。

安全约束：

- 工作区删除仅限指定根目录的**直接子目录**；文件域仅匹配固定模式，不递归越界；
- dry-run 只统计不删除；
- 审计 JSONL 以文件名日期（``audit-YYYYMMDD.jsonl``，UTC）判定过期，无法解析时回退 mtime；
- 表行删除仅在 production 模式执行（demo 不触达任何数据库）；
- Temporal 历史保留（30 天）由 Temporal namespace retention 配置（部署侧），不在此模块。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import mode

log = logging.getLogger("aiops.cleanup")

# 保留期（天）：键名与报告 deleted 字段一一对应（workflow_runs 与 audit 共用 DB 侧天数）
TTL_PRODUCTION = {
    "audit": 365,
    "sandbox": 7,
    "notify": 90,
    "qoder": 7,
    "argocd": 90,
    "workflow_runs": 365,
}
TTL_DEMO = {
    "audit": 30,
    "sandbox": 1,
    "notify": 7,
    "qoder": 1,
    "argocd": 7,
    "workflow_runs": 30,
}

_AUDIT_FILE_RE = re.compile(r"^audit-(\d{8})\.jsonl$")


def ttl_days() -> dict[str, int]:
    """当前模式的保留期配置（demo 30/1/7/1/7/30；production 365/7/90/7/90/365）。"""
    return dict(TTL_DEMO if mode.is_demo() else TTL_PRODUCTION)


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _remove_file(path: Path, dry_run: bool) -> int:
    if dry_run:
        return 1
    path.unlink()
    return 1


def _cleanup_files(
    directory: Path, cutoff: datetime, dry_run: bool, *, pattern: str = "*.json"
) -> int:
    """删除目录下 mtime 早于 cutoff 的匹配文件（不递归）。"""
    if not directory.is_dir():
        return 0
    count = 0
    for child in directory.iterdir():
        if not child.is_file() or not child.match(pattern):
            continue
        if _mtime(child) < cutoff:
            count += _remove_file(child, dry_run)
    return count


def _cleanup_workspaces(root: Path, cutoff: datetime, dry_run: bool) -> int:
    """删除根目录下 mtime 早于 cutoff 的直接子目录（工作区）。"""
    if not root.is_dir():
        return 0
    count = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if _mtime(child) < cutoff:
            if dry_run:
                count += 1
            else:
                shutil.rmtree(child)
                count += 1
    return count


# ----------------------------------------------------------------------
# 审计域
# ----------------------------------------------------------------------


def _cleanup_audit_files(directory: Path, cutoff: datetime, dry_run: bool) -> int:
    """审计 JSONL：优先按文件名日期（UTC 全天）判定，解析失败回退 mtime。

    仅处理 ``*.jsonl``（审计留痕固定形态），目录内其他文件不受影响。
    """
    if not directory.is_dir():
        return 0
    count = 0
    for child in directory.iterdir():
        if not child.is_file() or child.suffix != ".jsonl":
            continue
        match = _AUDIT_FILE_RE.match(child.name)
        expired = False
        if match:
            try:
                file_day = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
                expired = file_day < cutoff
            except ValueError:
                expired = _mtime(child) < cutoff
        else:
            expired = _mtime(child) < cutoff
        if expired:
            count += _remove_file(child, dry_run)
    return count


def _db_rows_expired(table, column, cutoff_naive: datetime, dry_run: bool) -> int:
    """删除表中 ``column < cutoff`` 的行（dry-run 时仅计数）。"""
    from sqlalchemy import delete, func, select

    from . import db as db_layer

    engine = db_layer.get_engine()
    if dry_run:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(table).where(column < cutoff_naive)
                ).scalar_one()
            )
    with engine.begin() as conn:
        return int(conn.execute(delete(table).where(column < cutoff_naive)).rowcount)


# ----------------------------------------------------------------------
# 编排入口
# ----------------------------------------------------------------------


def run_cleanup(
    *,
    dry_run: bool = False,
    ttl: dict[str, int] | None = None,
    audit_dir: Path | None = None,
    sandbox_root: Path | None = None,
    notify_dir: Path | None = None,
    qoder_root: Path | None = None,
    argocd_dir: Path | None = None,
) -> dict:
    """执行数据清理并返回结构化报告；单域失败记录 errors 但不阻塞其余域。

    各根目录参数默认与写入侧模块常量一致（可注入便于测试与特殊部署布局）。
    """
    from . import qoder_fix, release, sandbox
    from . import notify as notify_mod

    ttl = dict(ttl or ttl_days())
    now = datetime.now(timezone.utc)
    audit_dir = Path(audit_dir) if audit_dir is not None else mode.data_root() / "web-audit"
    sandbox_root = Path(sandbox_root) if sandbox_root is not None else sandbox.DEFAULT_SANDBOX_ROOT
    notify_dir = Path(notify_dir) if notify_dir is not None else notify_mod.NOTIFY_DIR
    qoder_root = (
        Path(qoder_root)
        if qoder_root is not None
        else Path(os.environ.get("AIOPS_QODER_ROOT") or qoder_fix.DEFAULT_QODER_ROOT)
    )
    argocd_dir = Path(argocd_dir) if argocd_dir is not None else release.ARGOCD_DIR

    deleted = {
        "audit_files": 0,
        "audit_rows": 0,
        "sandbox_workspaces": 0,
        "notify_files": 0,
        "qoder_workspaces": 0,
        "qoder_records": 0,
        "argocd_files": 0,
        "workflow_runs": 0,
    }
    errors: list[str] = []

    # 文件域（demo / production 通用；Qoder 拆为「工作区目录」与「run.json 留痕」两段）
    file_domains = [
        ("audit_files", f"审计文件（{audit_dir}）", lambda c: _cleanup_audit_files(audit_dir, c, dry_run)),
        ("sandbox_workspaces", f"沙箱工作区（{sandbox_root}）", lambda c: _cleanup_workspaces(sandbox_root, c, dry_run)),
        ("notify_files", f"通知留痕（{notify_dir}）", lambda c: _cleanup_files(notify_dir, c, dry_run)),
        ("qoder_workspaces", f"Qoder 工作区（{qoder_root}）", lambda c: _cleanup_workspaces(qoder_root, c, dry_run)),
        (
            "qoder_records",
            f"Qoder 运行留痕（{qoder_root}）",
            lambda c: _cleanup_files(qoder_root, c, dry_run, pattern="*.run.json"),
        ),
        ("argocd_files", f"ArgoCD 留痕（{argocd_dir}）", lambda c: _cleanup_files(argocd_dir, c, dry_run)),
    ]
    for key, label, fn in file_domains:
        try:
            deleted[key] = fn(now - timedelta(days=ttl[_ttl_key(key)]))
        except Exception as exc:  # noqa: BLE001 - 单域失败隔离，其余域继续
            errors.append(f"{label}: {exc}")

    # DB 域（仅 production；demo 不触达数据库）
    if mode.is_production():
        from . import db as db_layer

        cutoff_naive = (now - timedelta(days=ttl["audit"])).replace(tzinfo=None)
        try:
            deleted["audit_rows"] = _db_rows_expired(
                db_layer.audit_events, db_layer.audit_events.c.ts, cutoff_naive, dry_run
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"审计表行: {exc}")
        runs_cutoff = (now - timedelta(days=ttl["workflow_runs"])).replace(tzinfo=None)
        try:
            deleted["workflow_runs"] = _db_rows_expired(
                db_layer.workflow_runs, db_layer.workflow_runs.c.updated_at, runs_cutoff, dry_run
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"流程执行记录: {exc}")

    report = {
        "mode": mode.mode(),
        "dry_run": dry_run,
        "run_at": now.isoformat(),
        "ttl_days": ttl,
        "deleted": deleted,
        "errors": errors,
    }
    log.info(
        "[cleanup] mode=%s dry_run=%s deleted=%s errors=%d",
        report["mode"], dry_run, deleted, len(errors),
    )
    return report


def _ttl_key(report_key: str) -> str:
    """报告字段名 → TTL 配置键（audit_files/audit_rows → audit；其余同名）。"""
    if report_key.startswith("audit"):
        return "audit"
    if report_key.startswith("qoder"):
        return "qoder"
    if report_key.startswith("workflow"):
        return "workflow_runs"
    return report_key.replace("_files", "").replace("_workspaces", "")


# ----------------------------------------------------------------------
# CLI（生产 CronJob 入口：python -m aiops_agent.cleanup [--dry-run]）
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # 导入 config 触发 .env 自动加载（与 worker / BFF / demo_cli 一致的模式与库串口径）
    from . import config  # noqa: F401

    parser = argparse.ArgumentParser(
        prog="python -m aiops_agent.cleanup", description="AIOps 数据清理（TTL）"
    )
    parser.add_argument("--dry-run", action="store_true", help="预演：只统计不删除")
    args = parser.parse_args(argv)
    report = run_cleanup(dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
