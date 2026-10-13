#!/usr/bin/env python3
"""只读、脱敏基线；不导入应用、不加载 .env、不探测业务 HTTP。

默认仅收集 Git/本地包版本，未指定的数据源一律 unknown。
--host-inventory 只用定向 docker inspect / lsof / launchctl list；不读取 Env。
--demo-root/--production-root 显式指定文件盘点根，不自动推断当前运行模式。
--db-url-file 必须是 600 文件，且须 --approved-readonly-db 单独确认使用；
只支持现有 SQLite 文件或 loopback MySQL/PyMySQL，无建表、播种、登录接口。
--output 必须是仓库 data/production-baseline/ 中 Git 已忽略的新文件。
标识使用本次报告随机 HMAC（两种模式可比较，不跨报告关联）；不输出原始标识、
路径、URL、Env、口令、令牌或异常正文。Workflow 缓存不冒充 Temporal 实时查询。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from importlib import metadata
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TABLE_IDS = {"monitored_apps": "id", "console_users": "username", "console_tokens": None,
             "console_sessions": None, "escalations": "id", "requirement_analyses": "id",
             "audit_events": None, "workflow_runs": "workflow_id", "system_kv": None,
             "release_records": "id"}
FILE_LISTS = {"monitored_apps": "apps", "console_users": "users", "console_tokens": "tokens",
              "console_sessions": "sessions", "escalations": "items", "requirement_analyses": "items"}
UNKNOWN = {"status": "unknown"}
MAX_FILE = 16 * 1024 * 1024


class SafetyError(RuntimeError):
    """仅允许静态脱敏错误消息。"""


def plain_path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\n\r\x00"):
        raise SafetyError("必须指定规范绝对路径")
    # macOS /tmp 本身是系统链接；接受规范化后的路径，拒绝调用方提供的链接。
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SafetyError("拒绝符号链接路径；请使用规范绝对路径")
    if path.name == ".env":
        raise SafetyError("禁止读取 .env")
    return path.resolve()


def private_file(path: Path) -> str:
    path = plain_path(path)
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 8192):
        raise SafetyError("连接文件必须是当前用户拥有的独立 600 小文件")
    return path.read_text(encoding="utf-8").strip()


def clean_env() -> dict[str, str]:
    # 只读 Docker 需要本机 context 定位；不继承 DOCKER_HOST/CONFIG、代理或应用令牌。
    return {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()), "LC_ALL": "C", "COMPOSE_DISABLE_ENV_FILE": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def run_readonly(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, cwd=ROOT, env=clean_env(), capture_output=True,
                                text=True, timeout=20, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise SafetyError("只读命令不可用或失败；原始输出已隐藏") from None


def database_url(raw: str):
    from sqlalchemy.engine import make_url
    try:
        url = make_url(raw)
        if url.drivername not in {"sqlite", "mysql+pymysql"}:
            raise ValueError
        if url.drivername == "sqlite":
            if url.query or url.host or url.username or not url.database:
                raise ValueError
            path = plain_path(Path(url.database))
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError
            with path.open("rb") as stream:
                header = stream.read(100)
            # WAL 读者也可能创建共享内存文件；不把 immutable 用于仍在运行的库。
            if (header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01"
                    or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))):
                raise ValueError
        elif (url.host != "127.0.0.1" or not url.port or not url.database or not url.username
              or dict(url.query) not in ({}, {"charset": "utf8mb4"})):
            raise ValueError
        return url
    except Exception:
        raise SafetyError("数据库地址不符合显式本地只读连接约束") from None


@contextmanager
def readonly_connection(raw: str):
    """独立引擎 + 数据库级只读 + SQL 白名单；不引用 db.get_engine。

    MySQL SET 只改变此连接的会话；关闭/失败统一 rollback，不 commit。
    SQLite URI mode=ro 不会隐式创建不存在的文件，query_only 同时禁止临时表。
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool
    url = database_url(raw)
    if url.drivername == "sqlite":
        path = Path(url.database)
        engine = create_engine("sqlite://", poolclass=NullPool, hide_parameters=True,
                               creator=lambda: sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))
    else:
        engine = create_engine(url, poolclass=NullPool, hide_parameters=True,
                               isolation_level="AUTOCOMMIT", connect_args={
                                   "connect_timeout": 5, "read_timeout": 10, "write_timeout": 5})
    try:
        with engine.connect() as conn:
            if url.drivername == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only=ON")
            else:
                conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                conn.exec_driver_sql("SET SESSION MAX_EXECUTION_TIME=5000")
                conn.exec_driver_sql("START TRANSACTION READ ONLY")

            def only_read(connection, cursor, statement, parameters, context, executemany):
                sql = statement.strip().upper()
                if (re.search(r"\b(INTO|GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK|LOAD_FILE)\b|;", sql)
                        or not (sql.startswith("SELECT ") or sql.startswith("SHOW ") or
                                sql.startswith("PRAGMA MAIN.TABLE_") or sql.startswith("PRAGMA TEMP.TABLE_"))):
                    raise SafetyError("只读连接拒绝非查询语句")

            event.listen(conn, "before_cursor_execute", only_read)
            try:
                yield conn
            finally:
                conn.rollback()
    finally:
        engine.dispose()


class Redactor:
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def identifier(self, value) -> str:
        return hmac.new(self.key, str(value).encode(), hashlib.sha256).hexdigest()[:24]


def summarize_records(rows: list[dict], id_key: str | None, redactor: Redactor) -> dict:
    result = {"status": "ok", "count": len(rows)}
    if id_key:
        result["ids"] = sorted({redactor.identifier(row[id_key]) for row in rows if row.get(id_key) is not None})
    # 状态值只允许有限枚举，异常字段不原样输出。
    allowed = {"open", "assigned", "closed", "active", "disabled", "revoked", "previous",
               "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "WAIT_APPROVAL", "WAIT_SECOND_APPROVAL",
               "WAIT_NOTIFY", "DONE", "SHADOWED", "ESCALATED", "CANARY", "NOTIFY_WINDOW"}
    counts = {}
    for row in rows:
        for field in ("status", "stage", "exec_status", "state"):
            if field in row:
                value = str(row[field])
                key = field + ":" + (value if value in allowed else "other")
                counts[key] = counts.get(key, 0) + 1
    if counts:
        result["states"] = counts
    if id_key == "id" and rows and all("url" in row and "repo" in row for row in rows):
        from urllib.parse import urlsplit
        fingerprints = {}
        for row in rows:
            if row.get("id") is None:
                continue
            try:
                target = urlsplit(row["url"] or "")
                # 不将 URL 的认证、query、fragment 纳入报告或摘要。
                endpoint = (target.scheme, target.hostname, target.port, target.path)
                fields = [endpoint, row.get("repo") or "", row.get("service") or "", bool(row.get("enabled", True))]
                fingerprints[redactor.identifier(row["id"])] = redactor.identifier(json.dumps(fields, sort_keys=True))
            except (TypeError, ValueError):
                continue
        result["config_fingerprints"] = fingerprints
    return result


def inventory_database(raw: str, redactor: Redactor) -> dict:
    from sqlalchemy import Column, MetaData, Table, func, inspect, select
    result = {"status": "ok", "tables": {}, "running_workflows": "unknown: Temporal not queried",
              "approvals": "unknown: cached stages only", "consistency": "single read-only connection"}
    with readonly_connection(raw) as conn:
        inspector = inspect(conn)
        names = set(inspector.get_table_names())
        result["existing_table_count"] = len(names)
        result["unrecognized_tables"] = {}
        for name in sorted(names - TABLE_IDS.keys()):
            table = Table(name, MetaData())
            result["unrecognized_tables"][redactor.identifier(name)] = {
                "count": conn.execute(select(func.count()).select_from(table)).scalar_one()}
        for name, id_key in TABLE_IDS.items():
            if name not in names:
                result["tables"][name] = {"status": "absent"}
                continue
            columns = {c["name"] for c in inspector.get_columns(name)}
            candidates = (id_key, "status", "stage", "exec_status", "state")
            if name == "monitored_apps":
                candidates += ("url", "repo", "service", "enabled")
            safe = [c for c in candidates if c and c in columns]
            table = Table(name, MetaData(), *(Column(c) for c in safe))
            total = conn.execute(select(func.count()).select_from(table)).scalar_one()
            item = {"status": "ok", "count": total}
            if safe:
                rows = [dict(r) for r in conn.execute(select(*(table.c[c] for c in safe)).limit(10001)).mappings()]
                item.update(summarize_records(rows[:10000], id_key, redactor))
                item.update(count=total, truncated=total > 10000)
            # 不读取 token_hash、password_hash、kv_value、JSON payload 或审计详情。
            result["tables"][name] = item
    return result


def read_json(path: Path):
    path = plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
        raise SafetyError("只盘点受限大小的独立普通 JSON 文件")
    return json.loads(path.read_text(encoding="utf-8"))


def inventory_files(root: Path, redactor: Redactor) -> dict:
    root = plain_path(root)
    if not root.is_dir():
        raise SafetyError("文件数据根不存在")
    result = {"status": "ok", "root_id": redactor.identifier(root), "tables": {},
              "running_workflows": "unknown: Temporal not queried", "approvals": "unknown: no live workflow query",
              "consistency": "best-effort files; not an atomic snapshot"}
    for table, key in FILE_LISTS.items():
        path = root / (table + ".json")
        if not path.exists():
            result["tables"][table] = {"status": "absent"}
            continue
        try:
            data = read_json(path)
            rows = data if isinstance(data, list) else data.get(key)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError
            result["tables"][table] = summarize_records(rows, TABLE_IDS[table], redactor)
        except (OSError, ValueError, TypeError, AttributeError, SafetyError):
            result["tables"][table] = {"status": "unknown", "reason": "unreadable or unsupported format"}
    # 仅数既有发布文件；不打开补丁/恢复材料，不把数量视为成功发布。
    releases = root / "argocd"
    if releases.exists():
        try:
            plain_path(releases)
            result["release_files"] = {"status": "ok", "count": sum(1 for p in releases.glob("*.json") if p.is_file())}
        except (OSError, SafetyError):
            result["release_files"] = UNKNOWN.copy()
    else:
        result["release_files"] = {"status": "absent"}
    result["kill_switch"] = "not read or changed"
    return result


def differences(demo: dict, production: dict) -> dict:
    out = {}
    for name in TABLE_IDS:
        left, right = (source.get("tables", {}).get(name, UNKNOWN) for source in (demo, production))
        if left.get("status") != "ok" or right.get("status") != "ok":
            out[name] = UNKNOWN.copy()
            continue
        item = {"count_delta_production_minus_demo": right["count"] - left["count"]}
        if "ids" in left and "ids" in right and not left.get("truncated") and not right.get("truncated"):
            a, b = set(left["ids"]), set(right["ids"])
            item.update(demo_only=sorted(a - b), production_only=sorted(b - a), shared_count=len(a & b))
            if "config_fingerprints" in left and "config_fingerprints" in right:
                lcfg, rcfg = left["config_fingerprints"], right["config_fingerprints"]
                item["changed_config_ids"] = sorted(key for key in lcfg.keys() & rcfg.keys() if lcfg[key] != rcfg[key])
        out[name] = item
    return out


def host_inventory(redactor: Redactor) -> dict:
    result = {}
    # 复用 E/F 的白名单 inspect 思路；不调用 compose config、docker exec 或 launchctl print。
    for name in ("mysql", "redis", "loki", "temporal", "temporal-prod", "stable-order"):
        try:
            template = ('{"image":{{json .Image}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                        '"mounts":{{json .Mounts}},"ports":{{json .NetworkSettings.Ports}},'
                        '"running":{{json .State.Running}}}')
            data = json.loads(run_readonly(["docker", "inspect", "--type=container", "aiops-" + name, "--format", template]))
            image = data["image"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise SafetyError("镜像 ID 非法")
            digests = json.loads(run_readonly(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"] ))
            result[name] = {"running": data.get("running") is True, "image_id": image,
                            "digests": sorted({d.rsplit("@", 1)[-1] for d in (digests or [])
                                               if re.fullmatch(r".+@sha256:[a-f0-9]{64}", d)}),
                            "project_id": redactor.identifier(data.get("project")),
                            "mounts": [{"type": m.get("Type") if m.get("Type") in {"volume", "bind", "tmpfs"} else "other",
                                        "source_id": redactor.identifier(m.get("Name") or m.get("Source")),
                                        "target_id": redactor.identifier(m.get("Destination")),
                                        "rw": m.get("RW") is True} for m in data.get("mounts", [])],
                            "ports": port_bindings(data.get("ports") or {})}
        except (SafetyError, ValueError, KeyError, TypeError):
            result[name] = UNKNOWN.copy()
    try:
        text = run_readonly(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fn"])
        result["listen_ports"] = sorted({int(m.group(1)) for line in text.splitlines()
                                        if (m := re.search(r":(\d+)$", line)) and 0 < int(m.group(1)) <= 65535})
    except SafetyError:
        result["listen_ports"] = UNKNOWN.copy()
    try:
        text = run_readonly(["launchctl", "list"])
        known = {"com.aiops.watchdog", "com.aiops.backup", "com.aiops.caddy"}
        jobs = {}
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] in known:
                jobs[fields[2]] = {"running": fields[0].isdigit(),
                                   "last_exit": int(fields[1]) if re.fullmatch(r"-?\d+", fields[1]) else None}
        result["launchd"] = {label: jobs.get(label, {"status": "not listed"}) for label in sorted(known)}
    except SafetyError:
        result["launchd"] = UNKNOWN.copy()
    return result


def port_bindings(ports: dict) -> list[dict]:
    out = []
    for target, bindings in ports.items():
        if not re.fullmatch(r"\d+/(tcp|udp)", target):
            continue
        for binding in bindings or []:
            port = str(binding.get("HostPort", ""))
            if port.isdigit() and 0 < int(port) <= 65535:
                out.append({"target": target, "host_port": int(port),
                            "loopback": binding.get("HostIp") in {"127.0.0.1", "::1"}})
    return out


def local_inventory() -> dict:
    result = {"python": sys.version.split()[0], "packages": {}}
    for name in ("SQLAlchemy", "PyMySQL", "temporalio", "fastapi", "redis", "cryptography", "python-dotenv"):
        try:
            value = metadata.version(name)
            result["packages"][name] = value if re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", value) else "unknown"
        except metadata.PackageNotFoundError:
            result["packages"][name] = "not installed"
    try:
        sha = run_readonly(["git", "rev-parse", "HEAD"]).strip()
        result["git_sha"] = sha if re.fullmatch(r"[a-f0-9]{40,64}", sha) else "unknown"
        result["git_dirty"] = bool(run_readonly(["git", "status", "--porcelain", "--untracked-files=normal"]))
    except SafetyError:
        result["git_sha"] = "unknown"
    result["policy_candidates"] = {}
    for name in ("release-gate-policy.yaml", "demo-policy.yaml"):
        try:
            path = plain_path(ROOT / name)
            result["policy_candidates"][name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                  "effective": "unknown"}
        except (OSError, SafetyError):
            result["policy_candidates"][name] = UNKNOWN.copy()
    return result


def validate_output(path: Path) -> Path:
    path = plain_path(path)
    base = ROOT / "data" / "production-baseline"
    if path.parent != base or path.suffix != ".json" or path.exists():
        raise SafetyError("输出必须是 data/production-baseline/ 下新的 JSON 文件")
    run_readonly(["git", "check-ignore", "-q", "--", str(path)])
    return path


def write_report(path: Path, report: dict) -> None:
    path = validate_output(path)
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SafetyError("基线目录必须由当前用户拥有且权限为 700")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo-root", type=Path)
    parser.add_argument("--production-root", type=Path)
    parser.add_argument("--db-url-file", type=Path)
    parser.add_argument("--approved-readonly-db", action="store_true")
    parser.add_argument("--host-inventory", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination = validate_output(args.output)
        if args.db_url_file and not args.approved_readonly_db:
            raise SafetyError("未单独确认只读数据库凭证使用；拒绝读取连接文件")
        redactor = Redactor()
        report = {"created_at": datetime.now(timezone.utc).isoformat(), "local": local_inventory(),
                  "effective_runtime_config": "unknown: no process Env or .env read",
                  "runtime_component_versions": "unknown: no container exec",
                  "demo": UNKNOWN.copy(), "production_files": UNKNOWN.copy(), "production_db": UNKNOWN.copy(),
                  "host": UNKNOWN.copy(), "acceptance": "baseline only; not G1/G2/G3 acceptance"}
        for arg, field in ((args.demo_root, "demo"), (args.production_root, "production_files")):
            if arg:
                report[field] = inventory_files(arg, redactor)
        if args.db_url_file:
            try:
                report["production_db"] = inventory_database(private_file(args.db_url_file), redactor)
            except Exception:
                report["production_db"] = {"status": "unknown", "reason": "readonly query failed; details suppressed"}
        report["demo_vs_production_db"] = differences(report["demo"], report["production_db"])
        report["demo_vs_production_files"] = differences(report["demo"], report["production_files"])
        if args.host_inventory:
            report["host"] = host_inventory(redactor)
        write_report(destination, report)
        print("脱敏基线已写入 Git 忽略目录；未知项未视为通过；未修改运行状态")
        return 0
    except Exception:
        print("基线拒绝或失败；原始路径、配置和错误详情已隐藏", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
