#!/usr/bin/env python3
"""数据备份（P1）：MySQL 逻辑导出 + 关键 JSON + .env → backups/ 归档（含保留策略）。

背景：MySQL 与 data/ 下的 JSON 是流程/审批/审计/注册表的唯一事实源，此前无任何
备份路径（2026-10-12 评估：备份与恢复演练缺失）。归档含 .env（密钥），故目录
700、归档 600，仅限本机使用；异地容灾需另行同步 backups/ 至远端。

用法：
  .venv/bin/python scripts/backup-data.py             # 全量备份（默认保留 7 份）
  .venv/bin/python scripts/backup-data.py --keep 14   # 保留份数
  .venv/bin/python scripts/backup-data.py --dry-run   # 列出将备份的内容（不落盘）
  .venv/bin/python scripts/backup-data.py --list      # 列出已有备份
  .venv/bin/python scripts/backup-data.py --restore-test  # 恢复演练：最新归档灌入临时库并校验
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

_ROOT = Path(__file__).resolve().parent.parent
DATA = _ROOT / "data"
BACKUP_DIR = _ROOT / "backups"

# 关键 JSON 白名单（演示档回退存储与运行状态；RAG 索引/评测样本可重建或已入 git）
JSON_FILES = [
    "monitored_apps.json",        # 被监控应用清单（demo 档回退）
    "console_users.json",         # 控制台用户与角色（注册表回退）
    "console_tokens.json",        # 控制台令牌注册表（注册表回退）
    "console_sessions.json",      # 控制台会话
    "escalations.json",           # 升级人工记录
    "historical_tickets.json",    # 历史工单
    "log_ship_positions.json",    # 日志采集位点
    "probe_status.json",          # 可用性巡检状态
    "requirement_analyses.json",  # 需求分析记录
]


def load_database_url() -> str | None:
    """环境变量优先（与 config.load_dotenv(override=False) 语义一致），回落 .env。"""
    value = os.environ.get("AIOPS_DATABASE_URL")
    if value:
        return value
    env_file = _ROOT / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip() == "AIOPS_DATABASE_URL":
            return raw.strip().strip("'\"")
    return None


def parse_mysql_url(url: str) -> dict:
    parts = urlsplit(url)
    if not parts.hostname or not parts.username:
        raise ValueError("无法解析 AIOPS_DATABASE_URL（期望 mysql+pymysql://user:pass@host:port/db）")
    return {
        "user": unquote(parts.username),
        "password": unquote(parts.password or ""),
        "host": parts.hostname,
        "port": parts.port or 3306,
        "database": (parts.path or "/").lstrip("/") or "aiops",
    }


def dump_mysql(conn: dict, target: Path) -> None:
    """mysqldump 经临时 defaults-extra-file 传密码（600，避免明文进入 ps 命令行）。"""
    tool = shutil.which("mysqldump")
    if tool is None:
        raise RuntimeError("未找到 mysqldump（brew install mysql-client 后重试）")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as fh:
        fh.write(f"[client]\nuser={conn['user']}\npassword={conn['password']}\n")
        fh.write(f"host={conn['host']}\nport={conn['port']}\n")
        cnf = Path(fh.name)
    os.chmod(cnf, 0o600)
    try:
        cmd = [
            tool, f"--defaults-extra-file={cnf}", "--single-transaction",
            "--skip-lock-tables", "--routines", "--triggers",
            "--databases", conn["database"],
        ]
        # 注意：不能把 gzip.GzipFile 直接传给 stdout（subprocess 走 fileno() 会绕过
        # 压缩层直写底层 fd）——先 PIPE 捕获原始 SQL 再写入 gzip。
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"mysqldump 失败（{conn['host']}:{conn['port']}）：{detail}")
        with gzip.open(target, "wb") as out:
            out.write(proc.stdout)
        if target.stat().st_size < 100:
            raise RuntimeError("mysqldump 输出异常（疑似空导出）")
    finally:
        cnf.unlink(missing_ok=True)


def write_manifest(conn: dict | None, json_names: list[str], stamp: str) -> str:
    lines = [
        "AIOps 数据备份清单",
        f"时间：{stamp}",
        f"主机：{os.uname().nodename}",
    ]
    if conn:
        lines.append(
            f"数据库：{conn['database']}@{conn['host']}:{conn['port']}"
            f"（mysql/{conn['database']}.sql.gz；恢复：gunzip -c ... | mysql -u<user> -p；"
            "演练：scripts/backup-data.py --restore-test）"
        )
    else:
        lines.append("数据库：（未配置 AIOPS_DATABASE_URL，本次仅备份文件）")
    lines.append(f"数据文件：data_files/（{len(json_names)} 个；恢复：拷贝回工程 data/）")
    lines.append("环境文件：env/.env（含密钥；恢复：拷贝回工程根）")
    return "\n".join(lines) + "\n"


def create_backup(keep: int, dry_run: bool) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"aiops-backup-{stamp}.tar.gz"

    conn = None
    url = load_database_url()
    if url:
        try:
            conn = parse_mysql_url(url)
        except ValueError as exc:
            print(f"✘ {exc}")
            return 1

    json_names = [name for name in JSON_FILES if (DATA / name).is_file()]
    skipped = [name for name in JSON_FILES if not (DATA / name).is_file()]
    env_file = _ROOT / ".env"

    if dry_run:
        print(f"[dry-run] 将生成 {archive.relative_to(_ROOT)}：")
        db_note = f"{conn['database']}@{conn['host']}:{conn['port']}" if conn else "（跳过：未配置）"
        print(f"  MySQL：{db_note}")
        sample = f"（{'、'.join(json_names[:3])} …）" if json_names else ""
        print(f"  data：{len(json_names)} 个关键 JSON{sample}")
        if skipped:
            print(f"  （不存在跳过：{'、'.join(skipped)}）")
        if env_file.is_file():
            print("  .env：包含（归档 600 权限保护）")
        return 0

    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)
        staging = Path(tempfile.mkdtemp(prefix="aiops-backup-"))
        try:
            if conn:
                dump_mysql(conn, staging / "mysql" / f"{conn['database']}.sql.gz")
            if json_names:
                data_dir = staging / "data_files"
                data_dir.mkdir()
                for name in json_names:
                    shutil.copy2(DATA / name, data_dir / name)
            if env_file.is_file():
                env_dir = staging / "env"
                env_dir.mkdir()
                shutil.copy2(env_file, env_dir / ".env")
            (staging / "MANIFEST.txt").write_text(
                write_manifest(conn, json_names, stamp), encoding="utf-8"
            )
            with tarfile.open(archive, "w:gz") as tar:
                for item in sorted(staging.iterdir()):
                    tar.add(item, arcname=item.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        os.chmod(archive, 0o600)
    except RuntimeError as exc:
        print(f"✘ 备份失败：{exc}")
        return 1

    size_mb = archive.stat().st_size / (1 << 20)
    db_note = f"MySQL {conn['database']}" if conn else "无数据库"
    print(f"✔ 备份完成：{archive.relative_to(_ROOT)}（{size_mb:.1f} MB；{db_note} + {len(json_names)} 个 JSON）")

    backups = sorted(BACKUP_DIR.glob("aiops-backup-*.tar.gz"))
    removed = []
    while len(backups) > keep:
        old = backups.pop(0)
        old.unlink()
        removed.append(old.name)
    if removed:
        print(f"  保留策略：{keep} 份，已清理 {'、'.join(removed)}")
    return 0


def list_backups() -> int:
    backups = sorted(BACKUP_DIR.glob("aiops-backup-*.tar.gz"))
    if not backups:
        print(f"暂无备份（目录：{BACKUP_DIR.relative_to(_ROOT)}/）。")
        return 0
    print(f"已有备份（{BACKUP_DIR.relative_to(_ROOT)}/）：")
    for path in backups:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {path.name}  {path.stat().st_size / (1 << 20):7.1f} MB  {mtime}")
    return 0


DRILL_DB = "aiops_restore_drill"


def _container_mysql(container: str, sql: str | None, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """在 MySQL 容器内以 root 通道执行 SQL（密码取自容器环境变量，不经命令行）。"""
    cmd = ["docker", "exec", "-i", container, "sh", "-c",
           'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --default-character-set=utf8mb4 -N']
    if sql is not None:
        cmd[-1] += f' -e "{sql}"'
    return subprocess.run(cmd, input=stdin, capture_output=True)


def restore_test(container: str, keep_drill: bool) -> int:
    """恢复演练：最新归档灌入临时库 → 数表对比源库 → 清理（--keep-drill 保留）。"""
    backups = sorted(BACKUP_DIR.glob("aiops-backup-*.tar.gz"))
    if not backups:
        print("✘ 暂无备份可演练，先运行本脚本生成备份")
        return 1
    archive = backups[-1]
    url = load_database_url()
    if not url:
        print("✘ 未配置 AIOPS_DATABASE_URL，无法对比源库")
        return 1
    conn = parse_mysql_url(url)

    try:
        probe = _container_mysql(container, "SELECT 1")
    except FileNotFoundError:
        print("✘ 未找到 docker 命令（恢复演练依赖容器 root 通道）")
        return 1
    if probe.returncode != 0:
        print(f"✘ 容器 {container} root 通道不可用：{probe.stderr.decode('utf-8', 'replace').strip()}")
        return 1

    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(f"mysql/{conn['database']}.sql.gz")
        if member is None:
            print(f"✘ 归档 {archive.name} 中没有 MySQL 导出")
            return 1
        raw = gzip.decompress(member.read()).decode("utf-8")
    # 改写库名：CREATE DATABASE / USE 指向演练库（表名不会与纯库名标识符冲突）
    script = raw.replace(f"`{conn['database']}`", f"`{DRILL_DB}`")

    # 注：SQL 经 sh -c 双引号传入，库名不放反引号（避免命令替换）；普通标识符无需转义
    drop = _container_mysql(
        container, f"DROP DATABASE IF EXISTS {DRILL_DB}; CREATE DATABASE {DRILL_DB} CHARACTER SET utf8mb4"
    )
    if drop.returncode != 0:
        print(f"✘ 创建演练库失败：{drop.stderr.decode('utf-8', 'replace').strip()}")
        return 1

    load = _container_mysql(container, None, stdin=script.encode("utf-8"))
    if load.returncode != 0:
        print(f"✘ 灌入失败：{load.stderr.decode('utf-8', 'replace').strip()}")
        return 1

    counts = _container_mysql(
        container,
        "SELECT table_schema, COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema IN ('{conn['database']}', '{DRILL_DB}') GROUP BY table_schema",
    )
    parsed = dict(
        line.split("\t") for line in counts.stdout.decode("utf-8").splitlines() if "\t" in line
    )
    src, drill = int(parsed.get(conn["database"], 0)), int(parsed.get(DRILL_DB, 0))

    ok = drill > 0 and src == drill
    mark = "✔" if ok else "✘"
    print(f"{mark} 恢复演练（{archive.name}）：源库 {src} 张表 → 演练库 {drill} 张表")
    if not keep_drill:
        _container_mysql(container, f"DROP DATABASE IF EXISTS {DRILL_DB}")
        print(f"  演练库 {DRILL_DB} 已清理（--keep-drill 可保留检查）")
    else:
        print(f"  演练库 {DRILL_DB} 保留（手动清理：DROP DATABASE {DRILL_DB}）")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AIOps 数据备份（MySQL + data 关键 JSON + .env）")
    parser.add_argument("--keep", type=int, default=7, help="保留份数（默认 7）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将备份的内容")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--restore-test", action="store_true", help="恢复演练：最新归档灌入临时库并校验")
    parser.add_argument("--mysql-container", default="aiops-mysql", help="MySQL 容器名（默认 aiops-mysql）")
    parser.add_argument("--keep-drill", action="store_true", help="保留演练库供检查")
    args = parser.parse_args()
    if args.keep < 1:
        print("✘ --keep 需要正整数")
        return 1
    if args.list:
        return list_backups()
    if args.restore_test:
        return restore_test(args.mysql_container, args.keep_drill)
    return create_backup(args.keep, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
