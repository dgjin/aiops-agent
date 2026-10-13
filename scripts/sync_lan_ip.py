#!/usr/bin/env python3
"""LAN IP 动态同步：探测当前局域网地址，更新生产部署的地址相关配置并重签 TLS、
reload Caddy；绝不修改或重启外部监控栈——只生成片段材料与人工步骤指引。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys

if __package__:
    from .production_deploy import DeployError, atomic_write, plain_path, private_dir, run
else:
    from production_deploy import DeployError, atomic_write, plain_path, private_dir, run

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOY_DIR = ROOT / "data/production-deploy"
DEFAULT_REVIEW_DIR = ROOT / "data/monitoring-wiring-review"
DEFAULT_MIRROR_SETTINGS = (ROOT / "data/production-deploy-input/settings.json",)
PORT = 8643
TLS_CA_FILE = "/etc/aiops-tls/ca.crt"
MONITORING_STACK = "/Users/dgjin/dgjinapp/智能问数据分析系统"


def detect_lan_ip() -> str:
    for iface in ("en0", "en1"):
        try:
            candidate = run(["ipconfig", "getifaddr", iface]).strip()
        except DeployError:
            continue
        if candidate:
            return validate_lan_ip(candidate)
    raise DeployError("无法探测当前 LAN 地址；请用 --ip 显式指定")


def validate_lan_ip(text: str) -> str:
    try:
        address = ipaddress.IPv4Address(text.strip())
    except (ipaddress.AddressValueError, ValueError):
        raise DeployError("LAN 地址必须是 IPv4") from None
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise DeployError("LAN 地址不能是回环、链路本地、组播或未指定地址")
    if not address.is_private:
        raise DeployError("LAN 地址必须位于私网段")
    return str(address)


def check_bind(lan_ip: str) -> None:
    # 只尝试占用 8643 后立即释放，不 listen、不抢占、不终止现有进程。
    try:
        with socket.socket(socket.AF_INET) as probe:
            probe.bind((lan_ip, PORT))
    except OSError:
        raise DeployError(f"{lan_ip}:{PORT} 不可绑定（地址不在本机或端口被占用）；未做任何修改") from None


def build_fragments(lan_ip: str) -> tuple[str, str]:
    """与 production_deploy.render 输出同构（JSON 是合法 YAML）；只生成独立片段。"""
    tls_config = {"ca_file": TLS_CA_FILE}
    jobs = [{"job_name": f"aiops-production-{name}", "scheme": "https",
             "metrics_path": f"/internal-metrics/{name}", "tls_config": tls_config,
             "basic_auth": {"username": "aiops_metrics",
                            "password_file": "/run/secrets/aiops_metrics_password"},
             "static_configs": [{"targets": [f"{lan_ip}:{PORT}"]}]}
            for name in ("bff", "worker")]
    prometheus = json.dumps({"scrape_configs": jobs}, indent=2)
    receiver = {"name": "aiops-production", "webhook_configs": [{
        "url": f"https://{lan_ip}:{PORT}/webhook", "send_resolved": True,
        "http_config": {"tls_config": tls_config, "authorization": {
            "type": "Bearer", "credentials_file": "/run/secrets/aiops_webhook_token"}}}]}
    alertmanager = json.dumps({"receivers": [receiver]}, indent=2)
    return prometheus, alertmanager


def stack_instructions(old_ip: str, new_ip: str) -> list[str]:
    return [
        f"监控栈仓库（{MONITORING_STACK}）deploy/prometheus.yml："
        f"targets ['{old_ip}:{PORT}'] → ['{new_ip}:{PORT}']（两处）",
        f"监控栈仓库 deploy/alertmanager.yml：aiops-agent receiver 的 "
        f"'https://{old_ip}:{PORT}/webhook' → 'https://{new_ip}:{PORT}/webhook'",
        "替换前保留 .bak.<时间戳> 备份；Alertmanager 由 entrypoint 渲染模板，改模板后须重启容器才生效",
        "重启加载：docker restart nl2sql-monitoring-prometheus-1 nl2sql-monitoring-alertmanager-1",
        "验证：Prometheus targets aiops-production-bff / aiops-production-worker 为 up",
    ]


def review_note(old_ip: str, new_ip: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# LAN 地址迁移材料（{stamp}）", "",
             f"- 旧地址：{old_ip}；新地址：{new_ip}",
             "- 本目录片段为对应新地址的合并材料；外部监控栈的替换与重启须人工执行：", ""]
    lines += [f"{index}. {step}" for index, step in enumerate(stack_instructions(old_ip, new_ip), 1)]
    return "\n".join(lines) + "\n"


def _renew_via_cli(deploy_dir: Path) -> dict:
    script = Path(__file__).resolve().with_name("production_tls.py")
    result = subprocess.run([sys.executable, str(script), "renew", "--deploy-dir", str(deploy_dir),
                             "--approved-credentials-and-reload"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise DeployError("叶证书续签或 Caddy reload 失败："
                          + (detail[-1] if detail else "见证书日志")
                          + "；磁盘地址配置已同步，可重跑本命令")
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        raise DeployError("续签输出无法解析；请检查 production_tls 日志") from None


def sync(deploy_dir: Path, new_ip: str, *, dry_run: bool = False,
         mirror_settings: tuple[Path, ...] = (), review_dir: Path | None = None,
         renew_fn=None, bind_check: bool = True) -> dict:
    new_ip = validate_lan_ip(new_ip)
    deploy_dir = plain_path(deploy_dir)
    settings_path = deploy_dir / "settings.json"
    caddy_path = deploy_dir / "Caddyfile"
    for path in (settings_path, caddy_path):
        if not path.is_file():
            raise DeployError("生产部署目录缺少 settings.json 或 Caddyfile；请先完成渲染")
    settings = json.loads(plain_path(settings_path).read_text())
    old_ip = validate_lan_ip(str(settings["lan_ip"]))
    report = {"旧地址": old_ip, "新地址": new_ip, "dry_run": dry_run}

    if old_ip == new_ip:
        report["变更"] = {"说明": "地址未变化（仍执行证书核查）"}
        report["后续人工步骤"] = ["无需人工操作"]
    else:
        if bind_check:
            check_bind(new_ip)
        caddy = plain_path(caddy_path).read_text()
        count = caddy.count(old_ip)
        if count == 0:
            raise DeployError("Caddyfile 中未找到旧地址，拒绝部分替换")
        settings["lan_ip"] = new_ip
        prometheus, alertmanager = build_fragments(new_ip)
        if dry_run:
            report["变更"] = {"说明": f"dry-run：将替换 Caddyfile {count} 处、更新 settings 与监控片段"}
        else:
            atomic_write(caddy_path, caddy.replace(old_ip, new_ip))
            atomic_write(settings_path, json.dumps(settings, ensure_ascii=False, indent=2))
            atomic_write(deploy_dir / "prometheus.fragment.yml", prometheus)
            atomic_write(deploy_dir / "alertmanager.fragment.yml", alertmanager)
            mirrors = 0
            for path in mirror_settings:
                path = plain_path(path)
                if not path.is_file():
                    continue
                data = json.loads(path.read_text())
                if data.get("lan_ip") == old_ip:
                    data["lan_ip"] = new_ip
                    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
                    mirrors += 1
            bundle = None
            if review_dir is not None:
                bundle = plain_path(review_dir) / f"lan-ip-{new_ip}"
                private_dir(bundle)
                atomic_write(bundle / "prometheus.fragment.yml", prometheus)
                atomic_write(bundle / "alertmanager.fragment.yml", alertmanager)
                atomic_write(bundle / "说明.md", review_note(old_ip, new_ip))
            report["变更"] = {"Caddyfile": f"已替换 {count} 处", "settings.json": "已更新",
                              "prometheus.fragment.yml": "已更新", "alertmanager.fragment.yml": "已更新",
                              "镜像 settings": f"已更新 {mirrors} 个文件" if mirrors else "无需更新",
                              "审阅材料": str(bundle) if bundle else "未生成"}
        report["后续人工步骤"] = stack_instructions(old_ip, new_ip)

    if dry_run:
        report["证书"] = "dry-run：将执行 renew（SAN 变化时重签并 reload Caddy）"
    else:
        result = (renew_fn or _renew_via_cli)(deploy_dir)
        report["证书"] = ("已重签并 reload Caddy" if result.get("已续签")
                          else "无需变更（SAN 已一致且有效期充足）")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", help="显式指定新 LAN 地址（缺省自动探测 en0/en1）")
    parser.add_argument("--deploy-dir", type=Path, default=DEFAULT_DEPLOY_DIR)
    parser.add_argument("--dry-run", action="store_true", help="只报告将执行的变更；不写文件、不 reload")
    args = parser.parse_args(argv)
    try:
        new_ip = validate_lan_ip(args.ip) if args.ip else detect_lan_ip()
        report = sync(args.deploy_dir, new_ip, dry_run=args.dry_run,
                      mirror_settings=DEFAULT_MIRROR_SETTINGS, review_dir=DEFAULT_REVIEW_DIR)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (DeployError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, DeployError) else "地址同步失败；未完成的部分可重跑本命令",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
