#!/usr/bin/env python3
"""隔离存储只读冒烟（不是全链路/发布/生产就绪验收）。

必须显式传 --test-id <32位hex> --data-root <规范临时路径>
--database-url-file <数据根内600文件>。不会从环境变量/.env补齐任何目标。
数据根必须为 /tmp 的真实路径下 aiops-smoke-<test-id>-* 私有目录；需预置
.aiops-isolation.json：test_id、backend(sqlite/mysql)、namespace(aiops-smoke-<id>)、
app_port(20000以上)、target_repo(数据根内无外部链接的夹具目录)。
MySQL 还需 container_id(64位)、server_uuid、compose_project(aiops-smoke-<id>)。
实际容器名/卷/网络分别为 aiops-smoke-<id>-mysql / -mysql / -private，资源标签
io.aiops.test-id=<id>，独立内部网络只能有该容器，独立卷不得与任何其他容器共享。
DB 必须预置 aiops_test_identity(test_id, purpose, server_uuid) 唯一一行，
purpose='production-smoke'；SQLite server_uuid='sqlite-fixture'。
本工具绝不创建标记、建表、播种、改急停、清理审计或调用应用/Temporal/真实仓库。
SQLite 仅验证夹具；MySQL 检查现有表，不冒充业务写链路验证。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

# 只导入无应用/.env副作用的辅助工具；也支持 importlib 隔离加载本脚本。
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from production_baseline import (SafetyError, clean_env, database_url, plain_path,
                                 private_file, read_json, readonly_connection, run_readonly)

EXPECTED_TABLES = {"system_kv", "console_tokens", "monitored_apps", "audit_events", "workflow_runs"}
REAL_PORTS = {3000, 3100, 3101, 3200, 3306, 3316, 6379, 6380, 7233, 8099, 8233,
              8600, 8643, 9090, 9091, 9095, 11434, 18080, 18081, 19999}


def test_port(port) -> int:
    if type(port) is not int or not 20000 <= port <= 65535 or port in REAL_PORTS:
        raise SafetyError("拒绝真实服务端口；测试端口必须显式隔离")
    return port


def validate_inputs(test_id: str, root: Path, url_file: Path) -> tuple[dict, str]:
    if not re.fullmatch(r"[a-f0-9]{32}", test_id):
        raise SafetyError("必须显式指定32位测试标记")
    root = plain_path(root)
    base = Path("/tmp").resolve()
    if root.parent != base or not root.name.startswith("aiops-smoke-" + test_id + "-"):
        raise SafetyError("拒绝真实数据根；只接受独立系统临时目录")
    info = root.stat()
    if not root.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SafetyError("隔离数据根必须由当前用户拥有且权限为700")
    config = read_json(root / ".aiops-isolation.json")
    project = "aiops-smoke-" + test_id
    if not isinstance(config, dict) or config.get("test_id") != test_id or config.get("namespace") != project:
        raise SafetyError("目录标记或隔离 namespace 不匹配")
    keys = {"test_id", "namespace", "backend", "app_port", "target_repo"}
    if config.get("backend") == "mysql":
        keys |= {"container_id", "server_uuid", "compose_project"}
    if set(config) != keys:
        raise SafetyError("隔离清单含未知或缺失字段；禁止忽略真实目标覆盖项")
    test_port(config.get("app_port"))
    repo = plain_path(Path(config.get("target_repo", "")))
    if not repo.is_relative_to(root) or repo == root or not repo.is_dir():
        raise SafetyError("拒绝真实仓库；仅允许隔离根内的夹具目录")
    # 不允许工作树 .git 指向真实仓库，亦不允许夹具内部链接到真实文件。
    if (repo / ".git").exists() or any(p.is_symlink() or (p.is_file() and p.stat().st_nlink != 1)
                                      for p in repo.rglob("*")):
        raise SafetyError("仓库夹具不得包含 Git 工作树或符号链接")
    url_file = plain_path(url_file)
    if url_file.parent != root:
        raise SafetyError("连接文件必须在隔离数据根内")
    raw = private_file(url_file)
    from sqlalchemy.engine import make_url
    candidate = make_url(raw)
    if candidate.drivername == "sqlite" and Path(candidate.database or "").parent != root:
        raise SafetyError("拒绝读取隔离根外的SQLite文件，即使连接文件带测试标记")
    url = database_url(raw)
    if url.drivername == "sqlite":
        if config.get("backend") != "sqlite" or Path(url.database).parent != root:
            raise SafetyError("SQLite仅接受隔离根内已存在的独立夹具")
    else:
        if (config.get("backend") != "mysql" or url.username != "aiops_test"
                or url.database != "aiops_test_" + test_id or config.get("compose_project") != project):
            raise SafetyError("拒绝运行库/运行身份；需要独立测试库、用户及Compose项目")
        if test_port(url.port) == config["app_port"]:
            raise SafetyError("数据库与应用测试端口不能重叠")
        if (not re.fullmatch(r"[a-f0-9]{64}", config.get("container_id", "")) or
                not re.fullmatch(r"[a-f0-9-]{36}", config.get("server_uuid", ""))):
            raise SafetyError("必须显式预置容器完整ID和数据库实例UUID")
    return {**config, "root": root, "test_id": test_id, "port": url.port}, raw


def verify_container(config: dict) -> None:
    """独立只读核对，名字/标签本身不是授权：检查实际ID、网络、卷和端口。"""
    project = "aiops-smoke-" + config["test_id"]
    container, volume, network = project + "-mysql", project + "-mysql", project + "-private"
    cid = config["container_id"]
    template = ('{"id":{{json .Id}},"name":{{json .Name}},'
                '"test_id":{{json (index .Config.Labels "io.aiops.test-id")}},'
                '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                '"running":{{json .State.Running}},"privileged":{{json .HostConfig.Privileged}},'
                '"network":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}},'
                '"mounts":{{json .Mounts}},"ports":{{json .NetworkSettings.Ports}}}')
    info = json.loads(run_readonly(["docker", "inspect", "--type=container", cid, "--format", template]))
    mounts = info.get("mounts", [])
    ports = {k: v for k, v in (info.get("ports") or {}).items() if v}
    if (info.get("id") != cid or info.get("name") != "/" + container
            or info.get("test_id") != config["test_id"] or info.get("project") != project
            or info.get("running") is not True or info.get("privileged") is not False
            or info.get("network") != network or set(info.get("networks") or {}) != {network}
            or len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != volume or mounts[0].get("Destination") != "/var/lib/mysql"
            or ports != {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(config["port"])}]}):
        raise SafetyError("容器实际身份/端口/卷/网络不匹配；拒绝连接数据库")
    net = json.loads(run_readonly(["docker", "network", "inspect", network, "--format",
                                  '{"internal":{{json .Internal}},"test_id":{{json (index .Labels "io.aiops.test-id")}},'
                                  '"containers":{{json .Containers}}}']))
    if (net.get("internal") is not True or net.get("test_id") != config["test_id"]
            or set(net.get("containers") or {}) != {cid}):
        raise SafetyError("测试网络并非独立内部网络")
    label = run_readonly(["docker", "volume", "inspect", volume, "--format",
                          '{{index .Labels "io.aiops.test-id"}}']).strip()
    owners = run_readonly(["docker", "ps", "-aq", "--no-trunc", "--filter", "volume=" + volume]).split()
    if label != config["test_id"] or owners != [cid]:
        raise SafetyError("测试卷标记不匹配或被其他容器共享")


def verify_database(conn, config: dict) -> dict:
    from sqlalchemy import inspect, text
    tables = set(inspect(conn).get_table_names())
    if "aiops_test_identity" not in tables:
        raise SafetyError("隔离数据库标记不存在；禁止自动创建")
    rows = conn.execute(text("SELECT test_id, purpose, server_uuid FROM aiops_test_identity LIMIT 2")).mappings().all()
    expected_uuid = config.get("server_uuid") if config["backend"] == "mysql" else "sqlite-fixture"
    if (len(rows) != 1 or rows[0]["test_id"] != config["test_id"]
            or rows[0]["purpose"] != "production-smoke" or rows[0]["server_uuid"] != expected_uuid):
        raise SafetyError("隔离数据库标记不匹配")
    if config["backend"] == "mysql":
        actual = conn.execute(text("SELECT @@server_uuid, DATABASE()" )).one()
        if actual[0] != expected_uuid or actual[1] != "aiops_test_" + config["test_id"]:
            raise SafetyError("实际数据库实例/库身份与预置标记不匹配")
    if not EXPECTED_TABLES <= tables:
        raise SafetyError("隔离库缺少必要存储表；不会自动建表")
    # 拒绝预先灌入的真实目标，即使库名/标记看起来属于测试。
    for row in conn.execute(text("SELECT repo, url FROM monitored_apps")).mappings():
        from urllib.parse import urlsplit
        repo = row["repo"]
        if repo and plain_path(Path(repo)) != plain_path(Path(config["target_repo"])):
            raise SafetyError("隔离库含真实仓库目标")
        target = urlsplit(row["url"])
        if (target.scheme != "http" or target.hostname != "127.0.0.1" or
                target.port != config["app_port"] or target.username or target.password or target.query):
            raise SafetyError("隔离库含真实地址/端口；名称或测试标记不能放行")
    return {"status": "pass", "backend": config["backend"], "schema": "existing tables only",
            "identity_verified": True, "writes": 0, "application_imported": False,
            "token_seed": "not performed", "kill_switch": "not read or changed",
            "workflow_release_e2e": "not tested"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--database-url-file", type=Path, required=True)
    args = parser.parse_args(argv)
    # 不保留任何应用、云、LLM、代理、Git令牌或连接串，不导入应用加载器。
    env = clean_env()
    os.environ.clear()
    os.environ.update(env, PYTHON_DOTENV_DISABLED="1")
    try:
        config, raw = validate_inputs(args.test_id, args.data_root, args.database_url_file)
        if config["backend"] == "mysql":
            verify_container(config)
        with readonly_connection(raw) as conn:
            report = verify_database(conn, config)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception:
        print("冒烟已拒绝或失败；未执行任何写入，原始配置/凭证/异常正文已隐藏", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
