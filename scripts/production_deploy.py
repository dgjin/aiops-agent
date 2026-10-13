#!/usr/bin/env python3
"""生产配置渲染与只读基础设施预检；不加载应用、不读 .env、不启动或安装服务。"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("mysql", "redis", "loki", "temporal-prod")
VOLUME_KEYS = ("mysql", "redis", "loki", "temporal_dev")
PORTS = {"mysql": [(3316, 3306)], "redis": [(6380, 6379)], "loki": [(3101, 3100)],
         "temporal-prod": [(7233, 7233)], "temporal": [(7233, 7233), (8233, 8233)],
         "prometheus": [(9095, 9090)], "grafana": [(3200, 3000)]}


class DeployError(RuntimeError):
    """对外只显示脱敏、可操作的部署错误。"""


def clean_env() -> dict[str, str]:
    # 不继承应用凭证、Compose 文件选择、代理或自动 dotenv 设置。
    names = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
    return {**{k: os.environ[k] for k in names if k in os.environ},
            "COMPOSE_DISABLE_ENV_FILE": "1", "LC_ALL": "C"}


def run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=True,
                                timeout=60, env=env or clean_env(), cwd=ROOT)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        # 工具的 stderr 可能含配置口令，不向调用者原样转发。
        raise DeployError("外部校验失败或超时；检查工具安装、权限和目标，原始输出已隐藏") from None


def plain_path(path: Path) -> Path:
    path = path.absolute()
    if any(c in str(path) for c in '\n\r\x00\"\'${}\\`'):
        raise DeployError("路径含不安全的配置插值字符")
    if path.name == ".env" or ".env" in path.parts:
        raise DeployError("禁止读取或覆盖 .env")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise DeployError("拒绝符号链接路径")
    return path


def output_root(path: Path, test_only: bool = False) -> Path:
    path = plain_path(path).resolve()
    if test_only:
        base = Path(tempfile.gettempdir()).resolve()
        relative = path.relative_to(base) if path.is_relative_to(base) else None
        if not relative or not relative.parts or not relative.parts[0].startswith("aiops-deploy-test-"):
            raise DeployError("测试模式只允许专用系统临时目录")
    elif (not path.is_relative_to(ROOT / "data") or path == ROOT / "data"
          or not path.relative_to(ROOT / "data").parts[0].startswith("production-deploy")):
        raise DeployError("生产产物必须位于 Git 已忽略的 data/production-deploy* 下")
    return path


def private_dir(path: Path) -> None:
    plain_path(path)
    if not path.exists():
        if not path.parent.exists():
            private_dir(path.parent)
        path.mkdir(mode=0o700)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise DeployError("部署目录必须为 700；请人工核对后修复权限")


def atomic_write(path: Path, content: str | bytes) -> None:
    plain_path(path)
    private_dir(path.parent)
    data = content.encode() if isinstance(content, str) else content
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_secret(path: Path) -> str:
    path = plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise DeployError("凭证文件必须是当前用户拥有的 600 普通文件")
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise DeployError("凭证文件所在目录必须为 700")
    text = path.read_text().removesuffix("\n")
    if not 16 <= len(text) <= 4096 or any(c in text for c in "\r\n\x00"):
        raise DeployError("凭证必须为至少 16 字符的单行值")
    return text


def validate_settings(settings: dict) -> dict:
    if set(settings) != {"project", "lan_ip", "dns_names", "volumes", "caddy_binary"}:
        raise DeployError("配置仅允许 project、lan_ip、dns_names、volumes、caddy_binary")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", settings["project"]):
        raise DeployError("Compose 项目名非法")
    try:
        ip = ipaddress.IPv4Address(settings["lan_ip"])
    except ipaddress.AddressValueError:
        raise DeployError("必须显式指定已确认的局域网 IPv4") from None
    if not ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        raise DeployError("入口必须是非环回、非组播的局域网 IPv4")
    dns = settings["dns_names"]
    if not isinstance(dns, list) or len(dns) > 16 or any(
        not isinstance(n, str) or len(n) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", n)
        for n in dns
    ):
        raise DeployError("容器访问域名必须为明确的 DNS 名称，不允许通配符")
    if set(settings["volumes"]) != set(VOLUME_KEYS) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name)
        for name in settings["volumes"].values()
    ) or len(set(settings["volumes"].values())) != 4:
        raise DeployError("必须分别填写四个不同的原始卷名")
    binary = Path(settings["caddy_binary"])
    if not binary.is_absolute():
        raise DeployError("Caddy 必须指定固定绝对路径")
    # Homebrew 以符号链接发布 caddy（/opt/homebrew/bin/caddy -> Cellar/…）；
    # caddy_binary 允许自身为该形态，但保持不安全字符、.env 与目录链符号链接防护。
    if any(c in str(binary) for c in '\n\r\x00"\'${}\\`'):
        raise DeployError("路径含不安全的配置插值字符")
    if ".env" in binary.parts:
        raise DeployError("禁止读取或覆盖 .env")
    if any(p.is_symlink() for p in binary.parents):
        raise DeployError("拒绝符号链接路径")
    return {**settings, "caddy_binary": str(binary), "lan_ip": str(ip),
            "dns_names": sorted(set(["localhost", *dns]))}


def render(settings: dict, destination: Path, runtime_password: str,
           migration_password: str, metrics_hash: str, *, test_only: bool = False) -> Path:
    settings = validate_settings(settings)
    destination = output_root(destination, test_only)
    if destination.exists():
        raise DeployError("目标已存在；禁止覆盖活动配置，请渲染到新的受限目录后人工切换")
    if any(not 16 <= len(value) <= 4096 or any(c in value for c in "\r\n\x00")
           for value in (runtime_password, migration_password)):
        raise DeployError("数据库凭证必须为至少 16 字符的单行值")
    if runtime_password == migration_password:
        raise DeployError("runtime 与 migration 必须使用不同凭证")
    if not re.fullmatch(r"\$2[aby]\$(?:1[0-6])\$[./A-Za-z0-9]{53}", metrics_hash):
        raise DeployError("指标凭证必须是专用 bcrypt 哈希，成本参数要求 10 至 16")
    if os.getuid() == 0:
        raise DeployError("禁止以 root 生成运行配置")
    if not destination.parent.exists():
        private_dir(destination.parent)
    staging = Path(tempfile.mkdtemp(prefix=".render-", dir=destination.parent))
    try:
        template = (ROOT / "deploy/temporal/production.yaml").read_text()
        atomic_write(staging / "temporal/production.yaml",
                     template.replace("__RUNTIME_PASSWORD_JSON__", json.dumps(runtime_password)))
        atomic_write(staging / "migration-password", migration_password)
        hosts = [settings["lan_ip"], "127.0.0.1", "[::1]", *settings["dns_names"]]
        caddy = (ROOT / "deploy/caddy/Caddyfile").read_text()
        for key, value in {"__HTTPS_SITES__": ", ".join(f"https://{h}:8643" for h in hosts),
                           "__LAN_IP__": settings["lan_ip"], "__DEPLOY_DIR__": str(destination),
                           "__METRICS_BCRYPT__": metrics_hash}.items():
            caddy = caddy.replace(key, value)
        atomic_write(staging / "Caddyfile", caddy)
        plist = (ROOT / "deploy/launchd/com.aiops.caddy.plist").read_text()
        for key, value in {"__ROOT__": str(ROOT), "__DEPLOY_DIR__": str(destination),
                           "__CADDY__": settings["caddy_binary"]}.items():
            plist = plist.replace(key, escape(value))
        plistlib.loads(plist.encode())
        atomic_write(staging / "com.aiops.caddy.plist", plist)
        values = {"COMPOSE_PROJECT_NAME": settings["project"], "AIOPS_DEPLOY_DIR": str(destination),
                  "AIOPS_DEPLOY_UID": str(os.getuid()), "AIOPS_DEPLOY_GID": str(os.getgid())}
        values.update({f"AIOPS_{key.upper()}_VOLUME": name for key, name in settings["volumes"].items()})
        atomic_write(staging / "compose.env", "".join(f"{k}='{v}'\n" for k, v in values.items()))
        atomic_write(staging / "settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
        # JSON 是合法 YAML；只生成独立片段，绝不修改或重启已有监控栈。
        tls_config = {"ca_file": "/etc/aiops-tls/ca.crt"}
        jobs = [{"job_name": f"aiops-production-{name}", "scheme": "https",
                 "metrics_path": f"/internal-metrics/{name}", "tls_config": tls_config,
                 "basic_auth": {"username": "aiops_metrics", "password_file": "/run/secrets/aiops_metrics_password"},
                 "static_configs": [{"targets": [settings["lan_ip"] + ":8643"]}]}
                for name in ("bff", "worker")]
        atomic_write(staging / "prometheus.fragment.yml", json.dumps({"scrape_configs": jobs}, indent=2))
        receiver = {"name": "aiops-production", "webhook_configs": [{
            "url": "https://" + settings["lan_ip"] + ":8643/webhook", "send_resolved": True,
            "http_config": {"tls_config": tls_config, "authorization": {
                "type": "Bearer", "credentials_file": "/run/secrets/aiops_webhook_token"}}}]}
        atomic_write(staging / "alertmanager.fragment.yml", json.dumps({"receivers": [receiver]}, indent=2))
        # 配置和凭证作为一个完整目录发布；模板不得写入任何真实运行位置。
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return destination


def compose_command(destination: Path, project: str) -> list[str]:
    cli = [shutil.which("docker-compose")] if shutil.which("docker-compose") else ["docker", "compose"]
    return [*cli, "--project-name", project, "--env-file", str(destination / "compose.env"),
            "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.production.yml")]


def validate_compose(model: dict, settings: dict) -> None:
    services = model["services"]
    active = {name for name, spec in services.items() if not spec.get("profiles")}
    if active != set(SERVICES):
        raise DeployError("生产默认服务列表不符，禁止包含 start-dev、维护作业或可选 UI")
    for name, pairs in PORTS.items():
        ports = services[name].get("ports", [])
        actual = [(int(p["published"]), int(p["target"])) for p in ports]
        if sorted(actual) != sorted(pairs) or any(p.get("host_ip") != "127.0.0.1" for p in ports):
            raise DeployError("端口合并失败：必须精确替换且全部绑定 loopback")
    for name in SERVICES:
        if not re.search(r"@sha256:[a-f0-9]{64}$", services[name]["image"]):
            raise DeployError("生产镜像未固定 digest")
    expected = {"mysql-data": "mysql", "redis-existing": "redis", "loki-data": "loki", "temporal-data": "temporal_dev"}
    for volume, key in expected.items():
        actual = model["volumes"][volume]
        if not actual.get("external") or actual["name"] != settings["volumes"][key]:
            raise DeployError("原卷映射发生变化或未声明 external")
    for service, source, target in [("mysql", "mysql-data", "/var/lib/mysql"),
                                     ("redis", "redis-existing", "/data"),
                                     ("loki", "loki-data", "/loki"),
                                     ("temporal", "temporal-data", "/home/temporal")]:
        if not any(v.get("source") == source and v["target"] == target for v in services[service]["volumes"]):
            raise DeployError("原服务持久卷挂载被替换")
    if services["mysql"].get("environment") or services["temporal-prod"].get("secrets"):
        raise DeployError("发现遗留初始化凭证或 Server 挂载了迁移凭证")
    if "temporal" in services["temporal-prod"].get("depends_on", {}):
        raise DeployError("正式 Server 不能依赖 start-dev")


def inspect_existing(settings: dict, model: dict) -> None:
    for service, key, target in [("mysql", "mysql", "/var/lib/mysql"), ("redis", "redis", "/data"),
                                 ("loki", "loki", "/loki"), ("temporal", "temporal_dev", "/home/temporal")]:
        # 只读取挂载、项目标签和镜像标识；绝不读取容器 Env。
        template = '{{json .Mounts}}\n{{.Image}}\n{{index .Config.Labels "com.docker.compose.project"}}'
        lines = run(["docker", "inspect", f"aiops-{service}", "--format", template]).splitlines()
        mounts = json.loads(lines[0])
        if lines[2] != settings["project"] or not any(
            m["Type"] == "volume" and m["Destination"] == target and m["Name"] == settings["volumes"][key]
            for m in mounts
        ):
            raise DeployError("现有项目或卷映射不匹配；禁止创建替代卷")
        digests = json.loads(run(["docker", "image", "inspect", lines[1], "--format", "{{json .RepoDigests}}"] ))
        expected = model["services"][service]["image"].split("@")[-1]
        if expected not in [d.split("@")[-1] for d in (digests or [])]:
            raise DeployError("现有镜像与固定 digest 不同，必须重新评审，不自动升级")


def check_ports(lan_ip: str) -> None:
    # 只尝试占用后立即释放，不 listen、不抢占、不终止现有进程。
    for host, port in [(lan_ip, 8643), ("127.0.0.1", 8643), ("::1", 8643), ("127.0.0.1", 7233),
                       ("127.0.0.1", 8600), ("127.0.0.1", 8099), ("127.0.0.1", 9091)]:
        try:
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
                sock.bind((host, port))
        except OSError:
            raise DeployError(f"{host}:{port} 不可绑定；确认维护状态及占用者，不自动抢占") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "preflight"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--runtime-password-file", type=Path)
    parser.add_argument("--migration-password-file", type=Path)
    parser.add_argument("--metrics-hash-file", type=Path)
    parser.add_argument("--approved-credential-use", action="store_true")
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--inspect-existing", action="store_true")
    parser.add_argument("--check-ports", action="store_true")
    parser.add_argument("--validate-caddy", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination = output_root(args.output, args.test_only)
        if args.action == "render":
            if not args.approved_credential_use or not all([args.settings, args.runtime_password_file,
                                                           args.migration_password_file, args.metrics_hash_file]):
                raise DeployError("渲染前须单独确认凭证使用，并显式提供配置及三个受限凭证文件")
            settings = json.loads(plain_path(args.settings).read_text())
            render(settings, destination, read_secret(args.runtime_password_file),
                   read_secret(args.migration_password_file), read_secret(args.metrics_hash_file),
                   test_only=args.test_only)
            print("配置已原子写入受限目录；未创建凭证、未启动服务、未安装 launchd")
        else:
            settings = validate_settings(json.loads(plain_path(destination / "settings.json").read_text()))
            model = json.loads(run([*compose_command(destination, settings["project"]),
                                    "--profile", "*", "config", "--format", "json"]))
            validate_compose(model, settings)
            if args.inspect_existing:
                if args.test_only:
                    raise DeployError("测试模式禁止访问现有 Docker 资源")
                inspect_existing(settings, model)
            if args.check_ports:
                check_ports(settings["lan_ip"])
            if args.validate_caddy:
                run([settings["caddy_binary"], "validate", "--config", str(destination / "Caddyfile"),
                     "--adapter", "caddyfile"])
            print(json.dumps({"静态预检": "通过", "生产服务": SERVICES, "Temporal版本": "1.32.1",
                              "namespace": "aiops-production", "queue": "aiops-production-tasks",
                              "真实运行验收": "未验证，须完成 G2/G3；此结果不是就绪证明"}, ensure_ascii=False))
        return 0
    except (DeployError, OSError, ValueError, KeyError, TypeError):
        # 配置解析异常也可能夹带用户输入，只允许主动定义的错误消息对外显示。
        import sys
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, DeployError) else "配置不可读或格式不合法；未执行部署", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
