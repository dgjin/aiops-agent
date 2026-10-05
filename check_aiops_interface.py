#!/usr/bin/env python3
"""被监控应用标准接口（AIOps Manifest v1.0）校验工具。

对被监控系统做「接入前体检」：探测并校验 ``GET /.well-known/aiops.json``，
按 Manifest 声明验证健康页与 probe_keyword；``--extended`` 时再验证指标路径
与日志文件，帮助在控制台「从标准接口自动探测」之前确认应用侧改造是否到位。

用法::

    .venv/bin/python check_aiops_interface.py --url http://localhost:8000
    .venv/bin/python check_aiops_interface.py --url order.internal:8000 --extended

退出码：core 检查（Manifest / 健康页 / 关键字）全部通过为 0，否则 1。
扩展检查仅告警不判失败（日志文件可能尚未产生）。
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiops_agent import app_manifest  # noqa: E402 - 需先补齐 sys.path

MAX_BODY = 256 * 1024
_GLOB_CHARS = re.compile(r"[*?\[]")


def _normalize_url(raw: str) -> str:
    """无 scheme 时默认补 ``http://``，并去掉尾斜杠。"""
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


def _get(url: str, timeout: float) -> tuple[int, bytes, str]:
    """GET 请求；返回 ``(status, body, error)``，网络异常时 status=0。"""
    request = urllib.request.Request(url, headers={"User-Agent": "aiops-agent/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY), ""
    except urllib.error.HTTPError as exc:
        return exc.code, b"", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 超时/拒连/DNS 等一律转为检查失败
        return 0, b"", str(exc)


def _resolve_log_files(log_path: str) -> list[Path]:
    """与采集器 ``ship_app_logs.resolve_log_files`` 同口径解析日志文件。"""
    path = Path(log_path).expanduser()
    if not _GLOB_CHARS.search(path.name):
        return [path] if path.is_file() else []
    return sorted(p for p in path.parent.glob(path.name) if p.is_file())


def run_check(base_url: str, timeout: float, extended: bool) -> int:
    core_total = 3
    core_passed = 0
    warnings = 0
    print("== AIOps 标准接口校验 ==")
    print(f"目标：{base_url}\n")

    result = app_manifest.fetch_manifest(base_url, timeout=timeout)
    manifest = result.get("manifest") or {}
    if result.get("ok"):
        core_passed += 1
        detail = f"spec_version={manifest.get('spec_version')}, service={manifest.get('service')}"
        if manifest.get("name"):
            detail += f", name={manifest.get('name')}"
        print(f"[1/{core_total}] Manifest {app_manifest.MANIFEST_PATH}  ✓ 通过（{detail}）")
    else:
        print(f"[1/{core_total}] Manifest {app_manifest.MANIFEST_PATH}  ✗ 未通过")
        print(f"        原因：{result.get('error') or '未知'}")

    for warn in result.get("warnings") or []:
        warnings += 1
        print(f"        ! {warn}")

    health_path = (manifest.get("health_path") or "/").strip() or "/"
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    status, body, error = _get(base_url + health_path, timeout)
    if 200 <= status < 400:
        core_passed += 1
        print(f"[2/{core_total}] 健康检查 {health_path}  ✓ HTTP {status}（{len(body)} 字节）")
    else:
        print(f"[2/{core_total}] 健康检查 {health_path}  ✗ {error or f'HTTP {status}'}")

    keyword = (manifest.get("probe_keyword") or "").strip()
    if not keyword:
        core_passed += 1
        print(f"[3/{core_total}] 关键字  · Manifest 未声明 probe_keyword，跳过")
    elif keyword in body.decode("utf-8", errors="replace"):
        core_passed += 1
        print(f"[3/{core_total}] 关键字 {keyword!r}  ✓ 命中健康页响应")
    else:
        print(f"[3/{core_total}] 关键字 {keyword!r}  ✗ 未命中健康页响应（{health_path}）")

    if extended:
        metrics_path = (manifest.get("metrics_path") or "").strip()
        if metrics_path:
            status, _, error = _get(base_url + metrics_path, timeout)
            if 200 <= status < 400:
                print(f"[ext] 指标路径 {metrics_path}  ✓ HTTP {status}")
            else:
                warnings += 1
                print(f"[ext] 指标路径 {metrics_path}  ! {error or f'HTTP {status}'}（观测增强，可选）")
        else:
            print("[ext] 指标路径  · 未配置，跳过")

        log_path = (manifest.get("log_path") or "").strip()
        if log_path:
            files = _resolve_log_files(log_path)
            if files:
                preview = "、".join(str(p) for p in files[:3])
                print(f"[ext] 日志路径 {log_path}  ✓ 命中 {len(files)} 个文件（{preview}）")
            else:
                warnings += 1
                print(
                    f"[ext] 日志路径 {log_path}  ! 未命中文件"
                    "（文件可能尚未产生；控制台清单配置后采集器自动生效）"
                )
        else:
            warnings += 1
            print("[ext] 日志路径  · 未配置（无 log_path 则日志采集 / 突增检测不可用）")

    print()
    if core_passed == core_total:
        extra = f"，扩展告警 {warnings} 项" if warnings else ""
        print(f"结果：core 检查全部通过（{core_passed}/{core_total}{extra}）。")
        print("可接入 AIOps：控制台「被监控应用 → 新增 → 从标准接口探测」。")
        return 0
    print(f"结果：core 检查未通过（{core_passed}/{core_total}）。")
    print("请按《AIOps 被监控系统标准接口改造方案》补齐后重试。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="被监控应用 AIOps 标准接口（Manifest v1.0）校验"
    )
    parser.add_argument("--url", required=True, help="被监控应用根地址（无 scheme 时默认 http://）")
    parser.add_argument("--extended", action="store_true", help="附加检查 metrics_path 与 log_path")
    parser.add_argument(
        "--timeout",
        type=float,
        default=app_manifest.DEFAULT_TIMEOUT,
        help=f"单次请求超时秒（默认 {app_manifest.DEFAULT_TIMEOUT}）",
    )
    args = parser.parse_args()
    return run_check(_normalize_url(args.url), args.timeout, args.extended)


if __name__ == "__main__":
    sys.exit(main())
