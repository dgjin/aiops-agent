#!/usr/bin/env python3
"""OpenSSL 本地 CA、90 天叶证书与提前 30 天续签；不安装信任、不自动生成真实密钥。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
from typing import Callable

if __package__:
    from .production_deploy import (DeployError, atomic_write, output_root, plain_path,
                                    private_dir, run, validate_settings)
else:
    from production_deploy import (DeployError, atomic_write, output_root, plain_path,
                                   private_dir, run, validate_settings)

LEAF_DAYS = 90
RENEW_DAYS = 30


def openssl(*args: str) -> str:
    binary = shutil.which("openssl")
    if not binary:
        raise DeployError("缺少 OpenSSL；不会自动安装")
    return run([binary, *map(str, args)])


def desired_sans(settings: dict) -> set[str]:
    return {f"IP:{settings['lan_ip']}", "IP:127.0.0.1", "IP:::1",
            *[f"DNS:{name}" for name in settings["dns_names"]], "DNS:localhost"}


def certificate_sans(path: Path) -> set[str]:
    text = openssl("x509", "-in", str(path), "-noout", "-ext", "subjectAltName")
    found = set()
    for item in " ".join(text.splitlines()[1:]).split(","):
        item = item.strip()
        if item.startswith("IP Address:"):
            found.add("IP:" + str(ipaddress.ip_address(item.split(":", 1)[1])))
        elif item.startswith("DNS:"):
            found.add(item)
    return found


def days_remaining(path: Path) -> float:
    text = openssl("x509", "-in", str(path), "-noout", "-enddate").strip().split("=", 1)[1]
    expiry = datetime.strptime(text, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (expiry - datetime.now(timezone.utc)).total_seconds() / 86400


def check_pair(cert: Path, key: Path) -> None:
    for path in (cert, key):
        plain_path(path)
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600 or path.stat().st_uid != os.getuid():
            raise DeployError("证书及密钥必须是当前用户拥有的 600 普通文件")
        if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
            raise DeployError("证书及密钥所在目录必须为 700")
    if openssl("x509", "-in", str(cert), "-pubkey", "-noout") != openssl("pkey", "-in", str(key), "-pubout"):
        raise DeployError("证书与私钥不匹配")


def validate_leaf(leaf: Path, ca: Path, sans: set[str] | None = None, *, allow_expired: bool = False) -> None:
    check_pair(leaf / "leaf.crt", leaf / "leaf.key")
    openssl("verify", *(["-no_check_time"] if allow_expired else []), "-purpose", "sslserver",
            "-CAfile", str(ca / "ca.crt"), str(leaf / "leaf.crt"))
    if sans is not None and certificate_sans(leaf / "leaf.crt") != sans:
        raise DeployError("证书 SAN 与已确认 IP/域名不一致")


def initialize_ca(tls: Path) -> None:
    ca = tls / "ca"
    if ca.exists():
        raise DeployError("本地 CA 已存在；禁止隐式轮换，须另行确认信任撤销与重新分发")
    pending = Path(tempfile.mkdtemp(prefix=".ca-", dir=tls))
    try:
        for name in ("ca.key", "ca.crt"):
            atomic_write(pending / name, b"")
        openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "3650",
                "-subj", "/CN=AIOps Local Production CA", "-keyout", str(pending / "ca.key"),
                "-out", str(pending / "ca.crt"), "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        check_pair(pending / "ca.crt", pending / "ca.key")
        openssl("verify", "-CAfile", str(pending / "ca.crt"), str(pending / "ca.crt"))
        pending.rename(ca)
    except BaseException:
        shutil.rmtree(pending)
        raise


def make_leaf(tls: Path, settings: dict) -> Path:
    ca = tls / "ca"
    check_pair(ca / "ca.crt", ca / "ca.key")
    if days_remaining(ca / "ca.crt") <= LEAF_DAYS + 1:
        raise DeployError("CA 有效期不足以签发 90 天证书；须人工确认 CA 轮换")
    leaf = Path(tempfile.mkdtemp(prefix="leaf-", dir=tls))
    try:
        sans = desired_sans(settings)
        extensions = ("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                      "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(sorted(sans)) + "\n")
        atomic_write(leaf / "extensions.cnf", extensions)
        for name in ("leaf.key", "leaf.csr", "leaf.crt"):
            atomic_write(leaf / name, b"")
        openssl("req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
                "-subj", "/CN=AIOps Production", "-keyout", str(leaf / "leaf.key"),
                "-out", str(leaf / "leaf.csr"))
        openssl("x509", "-req", "-in", str(leaf / "leaf.csr"), "-CA", str(ca / "ca.crt"),
                "-CAkey", str(ca / "ca.key"), "-set_serial", str(secrets.randbits(159) or 1),
                "-days", str(LEAF_DAYS), "-sha256", "-extfile", str(leaf / "extensions.cnf"),
                "-out", str(leaf / "leaf.crt"))
        validate_leaf(leaf, ca, sans)
        if not LEAF_DAYS - 1 < days_remaining(leaf / "leaf.crt") <= LEAF_DAYS:
            raise DeployError("新证书有效期不符合 90 天要求")
        return leaf
    except BaseException:
        shutil.rmtree(leaf)
        raise


def current_leaf(tls: Path) -> Path | None:
    current = tls / "current"
    if not current.is_symlink():
        if current.exists():
            raise DeployError("current 必须为工具管理的原子版本链接")
        return None
    name = os.readlink(current)
    if not name.startswith("leaf-") or Path(name).name != name:
        raise DeployError("证书链接越界")
    target = plain_path(tls / name)
    if not target.is_dir() or stat.S_IMODE(target.stat().st_mode) != 0o700:
        raise DeployError("证书版本目录缺失或权限不是 700")
    return target


def switch_leaf(tls: Path, leaf: Path) -> None:
    if leaf.parent != tls or not leaf.name.startswith("leaf-"):
        raise DeployError("证书版本目录越界")
    pending = tls / (".link-" + secrets.token_hex(8))
    try:
        pending.symlink_to(leaf.name)
        os.replace(pending, tls / "current")
    finally:
        pending.unlink(missing_ok=True)


def renew(tls: Path, settings: dict, reload_fn: Callable[[], None]) -> bool:
    previous = current_leaf(tls)
    if previous is None:
        raise DeployError("尚无服务证书，请先签发并验证首次启动")
    validate_leaf(previous, tls / "ca", allow_expired=True)
    if (days_remaining(previous / "leaf.crt") > RENEW_DAYS
            and certificate_sans(previous / "leaf.crt") == desired_sans(settings)):
        return False
    fresh = make_leaf(tls, settings)
    journal = tls / "transition.json"
    atomic_write(journal, json.dumps({"previous": previous.name, "candidate": fresh.name}))
    try:
        switch_leaf(tls, fresh)
        reload_fn()
    except BaseException:
        switch_leaf(tls, previous)
        try:
            # 即使 reload 超时但实际上已生效，也尝试强制装回旧证书。
            reload_fn()
        except Exception:
            raise DeployError("续签加载失败；磁盘已恢复旧证书，但进程状态待核对，请执行 recover") from None
        journal.unlink()
        raise DeployError("续签加载失败；已恢复旧证书，原密钥和证书均保留") from None
    journal.unlink()
    return True


def reload_caddy(destination: Path, binary: str) -> None:
    config = str(destination / "Caddyfile")
    run([binary, "validate", "--config", config, "--adapter", "caddyfile"])
    # 静态路径不变时必须 --force，否则相同配置可能不会重新读取证书。
    run([binary, "reload", "--config", config, "--adapter", "caddyfile", "--force",
         "--address", "unix/" + str(destination / "caddy-admin.sock")])


@contextmanager
def exclusive(tls: Path):
    private_dir(tls)
    path = plain_path(tls / ".cert.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise DeployError("证书维护已在运行，不并发签发或切换") from None
    finally:
        os.close(fd)


def operate(action: str, destination: Path, settings: dict, approved: bool,
            *, test_only: bool = False, reload_fn: Callable[[], None] | None = None) -> dict:
    destination = output_root(destination, test_only)
    settings = validate_settings(settings)
    tls = destination / "tls"
    if action == "check":
        # 只读到期检查不创建锁、目录或密钥，不输出私钥内容。
        if (tls / "transition.json").exists():
            raise DeployError("存在未完成切换，需恢复并核对 Caddy 实际证书")
        current = current_leaf(tls)
        if current is None:
            raise DeployError("尚未签发证书")
        validate_leaf(current, tls / "ca", desired_sans(settings))
        days = days_remaining(current / "leaf.crt")
        return {"剩余天数": round(days, 2), "需要续签": days <= RENEW_DAYS,
                "CA指纹": openssl("x509", "-in", str(tls / "ca/ca.crt"), "-noout", "-fingerprint", "-sha256").strip()}
    if not approved:
        raise DeployError("生成或轮换密钥、证书及 reload 须用户单独确认")
    if test_only and reload_fn is None and action in ("renew", "recover"):
        raise DeployError("测试模式禁止调用真实 Caddy reload")
    reload_fn = reload_fn or (lambda: reload_caddy(destination, settings["caddy_binary"]))
    with exclusive(tls):
        journal = tls / "transition.json"
        if journal.exists() and action != "recover":
            raise DeployError("发现未完成证书切换，必须先 recover 并核对进程")
        if action == "init-ca":
            initialize_ca(tls)
        elif action == "issue":
            if current_leaf(tls) is not None:
                raise DeployError("已有服务证书，请使用受控 renew")
            switch_leaf(tls, make_leaf(tls, settings))
        elif action == "renew":
            return {"已续签": renew(tls, settings, reload_fn)}
        elif action == "recover":
            record = json.loads(plain_path(journal).read_text())
            name = record["previous"]
            if Path(name).name != name or not name.startswith("leaf-"):
                raise DeployError("恢复记录越界")
            previous = plain_path(tls / name)
            validate_leaf(previous, tls / "ca", allow_expired=True)
            switch_leaf(tls, previous)
            reload_fn()
            journal.unlink()
        else:
            raise DeployError("未知证书操作")
    return {"操作": action, "完成": True, "信任库": "未修改"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-ca", "issue", "check", "renew", "recover"])
    parser.add_argument("--deploy-dir", type=Path, required=True)
    parser.add_argument("--approved-credentials-and-reload", action="store_true")
    parser.add_argument("--test-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination = output_root(args.deploy_dir, args.test_only)
        settings = json.loads(plain_path(destination / "settings.json").read_text())
        report = operate(args.action, destination, settings, args.approved_credentials_and_reload,
                         test_only=args.test_only)
        print(json.dumps(report, ensure_ascii=False))
        return 2 if report.get("需要续签") else 0
    except (DeployError, OSError, ValueError, KeyError, TypeError) as error:
        import sys
        print(str(error) if isinstance(error, DeployError) else "证书操作失败；原有证书未主动删除，请检查受限目录", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
