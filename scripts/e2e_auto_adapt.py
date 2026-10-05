#!/usr/bin/env python3
"""被监控应用「自动适配」端到端验证（前台添加 → 全链路生效）。

前置：demo-app 已启动::

    cd demo-app && PORT=8123 ../.venv/bin/python http_server.py

验证流程（全程隔离：清单写临时文件、索引运行后清理，不触碰真实清单与生产库）：
    1. 真实探测标准接口（Manifest v1.0）→ 等价控制台「从标准接口探测」预填；
    2. 清单保存（等价控制台「保存」，写隔离的临时清单文件）；
    3. 热生效：修复注册表**立即**解析到新应用（repo / URL / 关键字契约）；
    4. 可用性探测（BFF aggregator 真实 HTTP 请求 + 关键字校验）；
    5. 日志采集目标动态包含新应用（采集器每轮读同一份清单）；
    6. 代码索引自动适配：专属索引缺失即构建，检索路由到该应用专属索引。

Ollama 可用时索引构建与检索全真实；不可用时以桩替代嵌入环节并明确标注
（单飞锁 / 构建 / 路由逻辑的完整单测见 tests/test_code_rag_fix.py::TestRetrieveIndexAutoAdapt）。

用法::

    .venv/bin/python scripts/e2e_auto_adapt.py [--port 8123]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock

# 隔离注入必须在 import 项目模块之前：demo 档位 → 文件后端，绝不连接生产 MySQL
os.environ["AIOPS_MODE"] = "demo"

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiops_agent import activities, app_manifest, app_registry, code_rag  # noqa: E402
from aiops_agent.models import Alert, RootCause  # noqa: E402
from bff import aggregator, monitored_apps  # noqa: E402

SERVICE = "demo-e2e"
OLLAMA_URL = "http://localhost:11434/api/tags"


def _ollama_available() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 探测类调用允许失败
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="被监控应用自动适配端到端验证")
    parser.add_argument("--port", type=int, default=8123, help="demo-app 端口（默认 8123）")
    args = parser.parse_args()
    base_url = f"http://localhost:{args.port}"

    failures: list[str] = []
    print("== AIOps 自动适配端到端验证 ==")
    print(f"目标：{base_url}（service={SERVICE}）\n")

    # 1. 标准接口探测
    result = app_manifest.fetch_manifest(base_url)
    if not result.get("ok"):
        print(f"[1/6] 标准接口探测  ✗ {result.get('error')}")
        print("      请先启动 demo-app：cd demo-app && PORT=8123 ../.venv/bin/python http_server.py")
        return 1
    manifest = result["manifest"]
    keyword = manifest.get("probe_keyword") or ""
    health_path = manifest.get("health_path") or ""
    print(
        f"[1/6] 标准接口探测  ✓ spec_version={manifest['spec_version']} service={manifest['service']}"
        f"（预填 name={manifest.get('name')!r} keyword={keyword!r} health={health_path!r}）"
    )

    index_path = code_rag.app_index_path(SERVICE)
    faiss_path = index_path.with_suffix(".faiss")
    if index_path.exists() or faiss_path.exists():
        print(f"中止：{index_path} 已存在（疑似真实应用占用），请人工确认后处理")
        return 1

    tmp_ctx = tempfile.TemporaryDirectory(prefix="aiops-e2e-")
    try:
        tmp = Path(tmp_ctx.name)
        repo_dir = tmp / "repo"
        repo_dir.mkdir()
        (repo_dir / "order_service.py").write_text(
            'def submit(coupon):\n    """提交订单（e2e 演示）。"""\n'
            '    if coupon is None:\n        raise ValueError("coupon required")\n'
            '    return {"ok": True}\n',
            encoding="utf-8",
        )
        log_file = tmp / "app.log"
        log_file.write_text('{"level": "INFO", "msg": "e2e boot"}\n', encoding="utf-8")

        apps_path = tmp / "monitored_apps.json"
        os.environ["AIOPS_MONITORED_APPS_PATH"] = str(apps_path)  # 注册表读同一份隔离清单

        with mock.patch.object(monitored_apps, "DATA_PATH", apps_path):
            # 2. 前台保存（隔离清单，等价控制台「保存」）
            app = monitored_apps.add(
                name="demo-e2e 订单服务",
                url=base_url,
                service=SERVICE,
                log_path=str(log_file),
                probe_keyword=keyword,
                health_path=health_path,
                repo=str(repo_dir),
                note="e2e 自动适配验证（临时）",
            )
            print(f"[2/6] 清单保存  ✓ id={app['id']}（隔离文件，真实清单不受影响）")

            # 3. 热生效：修复注册表立即解析
            entry = app_registry.resolve(SERVICE)
            if (
                entry is not None
                and entry["repo"] == repo_dir
                and entry["url"] == base_url
                and (entry.get("contract") or {}).get("keyword") == keyword
            ):
                print(f"[3/6] 修复注册表热生效  ✓ repo={entry['repo']}")
            else:
                failures.append("注册表未解析到新应用")
                print(f"[3/6] 修复注册表热生效  ✗ resolve()={entry}")

            # 4. 可用性探测（真实 HTTP + 健康路径 + 关键字）
            probe = aggregator.probe_monitored_app(url=base_url, keyword=keyword, health_path=health_path)
            if probe.get("running"):
                print(f"[4/6] 可用性探测  ✓ HTTP {probe.get('status_code')} 延迟 {probe.get('latency_ms')}ms")
            else:
                failures.append("可用性探测失败")
                print(f"[4/6] 可用性探测  ✗ {probe.get('error')}")

            # 5. 日志采集目标
            targets = monitored_apps.log_targets()
            hit = next((t for t in targets if t.get("service") == SERVICE), None)
            if hit and hit.get("log_path") == str(log_file):
                print(f"[5/6] 日志采集目标  ✓ {hit['name']} → {hit['log_path']}")
            else:
                failures.append("日志采集目标未包含新应用")
                print(f"[5/6] 日志采集目标  ✗ targets={[t.get('service') for t in targets]}")

        # 6. 代码索引自动适配（检索活动全链路）
        alert = Alert(alert_id="e2e-adapt-1", service=SERVICE, description="E2E 自动适配验证")
        root = RootCause(
            error_type="ValueError",
            suspect_files=["order_service.py"],
            confidence=0.9,
            summary="submit 未校验 coupon",
        )
        if _ollama_available():
            refs = asyncio.run(activities.retrieve_similar_fixes(alert, root))
            repo_ok = index_path.is_file() and activities._index_repo_of(index_path) == str(repo_dir)
            if repo_ok:
                print(
                    f"[6/6] 代码索引自动适配  ✓ 真实构建 {index_path.name}（repo 匹配），检索返回 {len(refs)} 条"
                )
            else:
                failures.append("真实构建未产出预期索引")
                print(f"[6/6] 代码索引自动适配  ✗ 索引存在={index_path.is_file()} repo_ok={repo_ok}")
        else:
            calls = {"build": 0}
            seen: dict = {}
            stub_hit = {
                "kind": "code",
                "file": "order_service.py",
                "qualname": "submit",
                "start_line": 1,
                "end_line": 4,
                "snippet": "def submit(coupon):",
                "similarity": 0.99,
            }

            def _fake_build(repo, index_path=None, **kwargs):
                calls["build"] += 1
                Path(index_path).write_text(
                    json.dumps({"repo": str(repo), "chunks": []}), encoding="utf-8"
                )
                return {"files": 1, "chunks": 0}

            def _fake_search(query, index_path=None, **kwargs):
                seen["index_path"] = Path(index_path) if index_path else None
                return [stub_hit]

            with mock.patch.object(code_rag, "build_index", side_effect=_fake_build), \
                    mock.patch.object(code_rag, "search_index", side_effect=_fake_search):
                refs = asyncio.run(activities.retrieve_similar_fixes(alert, root))

            routed = seen.get("index_path") == index_path
            if calls["build"] >= 1 and routed and refs == [stub_hit]:
                print("[6/6] 代码索引自动适配  ✓ 缺失即构建（Ollama 不可用→嵌入桩），检索路由到专属索引")
            else:
                failures.append("索引自动构建/路由未达预期")
                print(f"[6/6] 代码索引自动适配  ✗ build={calls['build']} routed={routed} refs={refs}")
    finally:
        for path in (index_path, faiss_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        tmp_ctx.cleanup()

    print()
    if failures:
        print(f"结果：失败 {len(failures)} 项：{'；'.join(failures)}")
        return 1
    print("结果：前台添加 → 清单热生效 → 探测 / 日志 / 索引全链路自动适配 全部通过（临时产物已清理）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
