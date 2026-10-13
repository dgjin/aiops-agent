"""Controlled lock helper: pip report -> selected release hashes; no install/freeze.

Usage: python scripts/ci_dependencies.py REPORT [--check-lock LOCK]
stdout is lock content, or a native/cross-platform dependency closure check.
Run pip --isolated install --dry-run --ignore-installed --report first.
This does NOT claim a cross-download is a native Linux runtime test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def selected(filename):
    # CPython 3.12: pure wheels, stable ABI wheels, native arm64 macOS/x86_64 Linux.
    if not filename.endswith(".whl"):
        return True  # hash-pinned source needed by drain3; CI disables build isolation
    if filename.endswith("-none-any.whl"):
        return True
    python, abi, platform = filename.rsplit("-", 3)[1:]
    interpreter = python == "cp312" or (abi == "abi3" and python.startswith("cp") and int(python[2:]) <= 312)
    return interpreter and (("macosx" in platform and ("arm64" in platform or "universal2" in platform))
                            or ("manylinux" in platform and "x86_64" in platform))


def check_closure(report, lock):
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.version import Version

    pins = dict(re.findall(r"^([a-z0-9.-]+)==([^\s;]+)", lock, re.M))
    errors = []
    for item in report["install"]:
        name, version = canonical(item["metadata"]["name"]), item["metadata"]["version"]
        if pins.get(name) != version:
            errors.append(f"report/lock pin mismatch: {name}=={version}")
        block = re.search(r"^" + re.escape(name) + r"==[^\n]+(?:\n[ \t]+[^\n]+)*", lock, re.M)
        if not block or item["download_info"]["archive_info"]["hashes"]["sha256"] not in block[0]:
            errors.append(f"report hash not locked: {name}")
    for system, machine, sys_platform in (("Linux", "x86_64", "linux"), ("Darwin", "arm64", "darwin")):
        env = {**default_environment(), "platform_system": system, "platform_machine": machine,
               "sys_platform": sys_platform, "python_version": "3.12", "python_full_version": "3.12.0"}
        for item in report["install"]:
            for raw in item["metadata"].get("requires_dist", []):
                req = Requirement(raw)
                # Include all actually requested extras, not every optional integration.
                extras = [""] + item.get("requested_extras", [])
                if req.marker and not any(req.marker.evaluate({**env, "extra": extra}) for extra in extras):
                    continue
                version = pins.get(canonical(req.name))
                if not version or Version(version) not in req.specifier:
                    errors.append(f"{system}: {item['metadata']['name']} requires {req}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Dependency metadata closure checked: Python 3.12 Linux x86_64 / macOS arm64; NOT native execution")


def verify_downloads(report, lock):
    """Stream/hash PyPI artifacts only; never install, execute metadata, or retain wheels."""
    check_closure(report, lock)
    opener = build_opener(ProxyHandler({}))
    count = 0
    for item in report["install"]:
        name, version = canonical(item["metadata"]["name"]), item["metadata"]["version"]
        with opener.open(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            release = json.load(response)
        entries = [e for e in release["urls"] if not e.get("yanked") and selected(e["filename"])]
        native_hash = item["download_info"]["archive_info"]["hashes"]["sha256"]
        native = next(e for e in entries if e["digests"]["sha256"] == native_hash)
        linux = next((e for e in entries if "manylinux" in e["filename"] and "x86_64" in e["filename"]), native)
        for entry in {e["url"]: e for e in (native, linux)}.values():
            parsed = urlsplit(entry["url"])
            if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org" or parsed.username:
                raise SystemExit("unexpected artifact origin")
            print(f"Downloading for hash check: {entry['filename']}", flush=True)
            digest = hashlib.sha256()
            started, size = time.monotonic(), 0
            with opener.open(entry["url"], timeout=30) as response:
                while chunk := response.read1(64 * 1024):
                    size += len(chunk)
                    if time.monotonic() - started > 180 or size > 256 * 1024 * 1024:
                        raise SystemExit(f"download verification budget exceeded: {name}; NOT verified")
                    digest.update(chunk)
            if digest.hexdigest() != entry["digests"]["sha256"] or digest.hexdigest() not in lock:
                raise SystemExit(f"download hash mismatch: {name}")
            count += 1
        print(f"Verified download: {name}=={version}", flush=True)
    print(f"{count} artifact hashes verified; no install, no native Linux execution")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--check-lock", type=Path)
    parser.add_argument("--verify-downloads", type=Path, metavar="LOCK")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if args.verify_downloads:
        items = sorted(report["install"], key=lambda i: canonical(i["metadata"]["name"]))
        verify_downloads({"install": items[args.offset:args.offset + args.limit]}, args.verify_downloads.read_text())
        return
    if args.check_lock:
        check_closure(report, args.check_lock.read_text())
        return
    opener = build_opener(ProxyHandler({}))
    lines = ["# Generated from controlled pip --dry-run --ignore-installed report, not pip freeze.",
             "# CPython 3.12 macOS arm64 / Linux x86_64 artifacts; native Linux CI still required.",
             "# Hashes pin downloads, not vulnerability status. No security exceptions approved.",
             "# Install build lock first, then --require-hashes --no-build-isolation."]
    items = sorted(report["install"], key=lambda i: canonical(i["metadata"]["name"]))
    if args.offset:
        lines = []
    for item in items[args.offset:args.offset + args.limit]:
        name, version = canonical(item["metadata"]["name"]), item["metadata"]["version"]
        if not re.fullmatch(r"[a-z0-9.-]+", name) or not re.fullmatch(r"[0-9a-zA-Z.+!-]+", version):
            raise SystemExit("unexpected package identity")
        with opener.open(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            release = json.load(response)
        hashes = sorted({entry["digests"]["sha256"] for entry in release["urls"]
                         if not entry.get("yanked") and selected(entry["filename"])})
        report_hash = item["download_info"]["archive_info"]["hashes"]["sha256"]
        if report_hash not in hashes or not all(re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes):
            raise SystemExit(f"report/release hash mismatch: {name}")
        lines.append(name + "==" + version + " \\")
        lines.extend("    --hash=sha256:" + value + (" \\" if i < len(hashes) - 1 else "")
                     for i, value in enumerate(hashes))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
