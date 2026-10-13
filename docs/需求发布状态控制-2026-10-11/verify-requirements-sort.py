#!/usr/bin/env python3
"""需求基线「全清单」排序交互验收（一次性 CDP 脚本；随证据包归档）。

背景：静态令牌注册表为 hash 存储、明文不可得，故以 Fetch 域拦截
``/api/requirements*`` 注入构造数据（localStorage 占位令牌通过 RequireAuth
存在性检查，不解锁任何真实 API），直接验证「真实构建产物」的排序交互。

断言链：
1. 默认按更新时间降序：aria-sort=descending 且首行为构造数据中最新条目（ID 7）
2. 点击「ID」表头 → ascending 且前 5 行 ID 升序；「重置条件」按钮出现
3. 再点「ID」→ descending 且前 5 行 ID 降序（同列翻转）
4. 点击「优先级」→ ascending 且前 3 行均 P0（换列取直觉默认方向）
5. 点击「重置条件」→ 恢复默认（更新时间 descending、首行回 ID 7、按钮消失）

用法：cd <仓库根> && .venv/bin/python docs/需求发布状态控制-2026-10-11/verify-requirements-sort.py（需 8600 运行）
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websockets  # 17.x：connect 需显式 proxy=None（系统代理探测坑）

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9337
BASE = "http://127.0.0.1:8600"
PROFILE = "/tmp/aiops-cdp-sort-profile"  # 无头 Chrome 临时 profile（不落仓库目录）


def make_entry(eid: int, updated: str, priority: str, kind: str = "REQUIREMENT") -> dict:
    return {
        "id": eid,
        "kind": kind,
        "title": f"需求条目 {eid}",
        "content": f"内容 {eid}",
        "status": "BASELINE",
        "priority": priority,
        "baselineVersion": f"v1.{eid}",
        "assessment": f"评估 {eid}",
        "submitter": f"提交人{eid}",
        "department": "研发部",
        "reviewer": "admin",
        "reviewedAt": None,
        "createdAt": "2026-09-20T08:00:00",
        "updatedAt": updated,
        "revisions": [],
    }


# 12 条：更新时间与 ID 顺序刻意错开（id7 最新 → 默认时间降序首行必须是 7）
ENTRIES = [
    make_entry(1, "2026-10-01T10:00:00", "P2"),
    make_entry(2, "2026-10-02T10:00:00", "P1", "SUGGESTION"),
    make_entry(3, "2026-09-28T10:00:00", "P0", "BUG"),
    make_entry(4, "2026-10-05T10:00:00", "P3"),
    make_entry(5, "2026-09-30T10:00:00", ""),
    make_entry(6, "2026-10-08T10:00:00", "P0"),
    make_entry(7, "2026-10-10T09:00:00", "P0", "BUG"),
    make_entry(8, "2026-09-25T10:00:00", "P2", "OTHER"),
    make_entry(9, "2026-10-03T10:00:00", ""),
    make_entry(10, "2026-10-07T10:00:00", "P1"),
    make_entry(11, "2026-10-04T10:00:00", "P3", "SUGGESTION"),
    make_entry(12, "2026-10-09T10:00:00", "P1"),
]

APP = {"id": "demo-app", "name": "演示应用", "url": "http://demo.local", "service": "order", "enabled": True}
REQ_BODY = {
    "ok": True,
    "error": "",
    "warnings": [],
    "apps": [APP],
    "app": APP,
    "url": "http://demo.local/api/requirements/export",
    "service": "order",
    "system": "排序验收（mock）",
    "exported_at": "2026-10-11T10:00:00",
    "filter": {},
    "returned": len(ENTRIES),
    "entries": ENTRIES,
}
ANALYSES_BODY = {"app_id": "demo-app", "analyses": [], "returned": 0}

# 快照：表头 aria-sort（0=ID/2=优先级/6=更新时间）+ 前 5 行 ID + 前 3 行优先级 + 重置按钮
SNAPSHOT_JS = """(() => {
  const ths = [...document.querySelectorAll('table thead th')];
  const rows = [...document.querySelectorAll('table tbody tr')];
  return JSON.stringify({
    idSort: ths[0] ? ths[0].getAttribute('aria-sort') : null,
    prSort: ths[2] ? ths[2].getAttribute('aria-sort') : null,
    upSort: ths[6] ? ths[6].getAttribute('aria-sort') : null,
    ids: rows.slice(0, 5).map(tr => tr.cells[0].innerText.trim()),
    prs: rows.slice(0, 3).map(tr => tr.cells[2].innerText.trim()),
    reset: [...document.querySelectorAll('button')].some(b => b.innerText.trim() === '重置条件'),
  });
})()"""


def click_th(label: str) -> str:
    return (
        "(() => {"
        "  const b = [...document.querySelectorAll('table thead th button')]"
        f"    .find(x => x.innerText.trim().startsWith({json.dumps(label)}));"
        "  if (!b) return 'NOT_FOUND';"
        "  b.click(); return 'CLICKED';"
        "})()"
    )


CLICK_RESET = """(() => {
  const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim() === '重置条件');
  if (b) { b.click(); return 'CLICKED'; }
  return 'NOT_FOUND';
})()"""


async def main() -> int:
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")

    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={PROFILE}",
            "--window-size=1440,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1) as resp:
                    tabs = json.load(resp)
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        assert ws_url, "DevTools 端口未就绪"

        async with websockets.connect(ws_url, proxy=None, max_size=64 * 1024 * 1024) as ws:
            pending: dict[int, asyncio.Future] = {}
            seq = {"id": 0}

            async def send(method: str, params: dict | None = None, timeout: float = 15.0):
                seq["id"] += 1
                fut: asyncio.Future = asyncio.get_event_loop().create_future()
                pending[seq["id"]] = fut
                await ws.send(json.dumps({"id": seq["id"], "method": method, "params": params or {}}))
                return await asyncio.wait_for(fut, timeout=timeout)

            async def fulfill(params: dict) -> None:
                url = params["request"]["url"]
                body = ANALYSES_BODY if "/analyses" in url else REQ_BODY
                await ws.send(
                    json.dumps(
                        {
                            "id": 0,
                            "method": "Fetch.fulfillRequest",
                            "params": {
                                "requestId": params["requestId"],
                                "responseCode": 200,
                                "responseHeaders": [
                                    {"name": "Content-Type", "value": "application/json"},
                                    {"name": "Cache-Control", "value": "no-store"},
                                ],
                                "body": base64.b64encode(json.dumps(body).encode()).decode(),
                            },
                        }
                    )
                )

            async def reader() -> None:
                async for raw in ws:
                    data = json.loads(raw)
                    mid = data.get("id")
                    if mid in pending:
                        pending.pop(mid).set_result(data)
                    elif data.get("method") == "Fetch.requestPaused":
                        await fulfill(data["params"])

            reader_task = asyncio.create_task(reader())

            async def eval_js(expr: str):
                data = await send(
                    "Runtime.evaluate",
                    {"expression": expr, "returnByValue": True},
                )
                res = data.get("result", {})
                if "exceptionDetails" in res:
                    return None
                return res.get("result", {}).get("value")

            # 1) 注入占位令牌（仅通过 RequireAuth 存在性检查，不解锁真实 API）
            await send("Page.navigate", {"url": f"{BASE}/login"})
            await asyncio.sleep(1.2)
            await eval_js("localStorage.setItem('aiops.token','verify-placeholder'); 'set'")
            # 2) 启用 mock 拦截后进入需求基线页
            await send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*api/requirements*", "requestStage": "Request"}]},
            )
            await send("Page.navigate", {"url": f"{BASE}/requirements"})

            rendered = False
            for _ in range(60):
                if await eval_js("!!document.querySelector('table thead th button')"):
                    rendered = True
                    break
                await asyncio.sleep(0.4)
            if not rendered:
                check("表格渲染（未在 24s 内出现）", False)
            else:
                async def snapshot() -> dict:
                    await asyncio.sleep(0.35)  # 等 React 重渲染落帧
                    return json.loads(await eval_js(SNAPSHOT_JS))

                snap = await snapshot()
                check("默认：更新时间列 aria-sort=descending", snap["upSort"] == "descending")
                check("默认：首行为最新条目（ID 7）", snap["ids"][0] == "7")
                check("默认：无「重置条件」按钮", snap["reset"] is False)

                await eval_js(click_th("ID"))
                snap = await snapshot()
                check("点「ID」→ aria-sort=ascending", snap["idSort"] == "ascending")
                check("点「ID」→ 前 5 行升序 1–5", snap["ids"] == ["1", "2", "3", "4", "5"])
                check("偏离默认排序 → 出现「重置条件」", snap["reset"] is True)

                await eval_js(click_th("ID"))
                snap = await snapshot()
                check("再点「ID」→ descending 翻转", snap["idSort"] == "descending")
                check("再点「ID」→ 前 5 行降序 12–8", snap["ids"] == ["12", "11", "10", "9", "8"])

                await eval_js(click_th("优先级"))
                snap = await snapshot()
                check("点「优先级」→ ascending（换列直觉默认）", snap["prSort"] == "ascending")
                check("优先级升序 → 前 3 行均 P0", snap["prs"] == ["P0", "P0", "P0"])

                await eval_js(CLICK_RESET)
                snap = await snapshot()
                check("重置 → 恢复更新时间降序且首行 ID 7", snap["upSort"] == "descending" and snap["ids"][0] == "7")
                check("重置 → 「重置条件」按钮消失", snap["reset"] is False)

            reader_task.cancel()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [name for name, ok in results if not ok]
    print(f"\n共 {len(results)} 项断言，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：", "；".join(failed))
        return 1
    print("全部通过：需求基线全清单排序交互验收 OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
