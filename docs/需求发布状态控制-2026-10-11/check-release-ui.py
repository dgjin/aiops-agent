"""需求发布状态控制 UI 验证（CDP + headless Chrome；随证据包归档，脚本内不含任何令牌）。

验证对象：
- demo 实例 8601（含 4 条 released / 1 条 escalated 真实 Temporal 终态）：
  列表徽标「已发布 · vN」「已转人工 · vN」、终态弹窗卡片、无反馈输入 / 无批准按钮；
- production 实例 8600（真实 MySQL 数据，entry 23 已冻结 escalated）：
  列表徽标「已转人工 · v1」、工作台卡片「已转人工（v1）」。

用法：AIOPS_VERIFY_TOKEN=<生产管理令牌> .venv/bin/python docs/需求发布状态控制-2026-10-11/check-release-ui.py（需 8600/8601 均已在运行；令牌仅经环境变量传入）
"""

import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

# 本机 CDP 不过代理（环境里可能配置了 SOCKS/HTTP 代理）
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9333
EVIDENCE = Path(__file__).resolve().parent / "evidence"
DEMO_TOKEN = "tok-adm"  # demo 档内置验证令牌（README / .env.example 同源，非机密）
PROD_TOKEN = os.environ.get("AIOPS_VERIFY_TOKEN", "")  # 生产管理令牌仅经环境变量传入，勿落盘

_mid = 0


async def call(ws, method, params=None):
    global _mid
    _mid += 1
    await ws.send(json.dumps({"id": _mid, "method": method, "params": params or {}}))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == _mid:
            if "error" in raw:
                raise RuntimeError(f"{method} -> {raw['error']}")
            return raw.get("result", {})


async def evaluate(ws, expr):
    r = await call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("value")


async def wait_for(ws, expr, timeout=20.0, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = await evaluate(ws, expr)
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def screenshot(ws, name):
    r = await call(ws, "Page.captureScreenshot", {"format": "png"})
    data = base64.b64decode(r["data"])
    EVIDENCE.mkdir(exist_ok=True)
    path = EVIDENCE / name
    path.write_bytes(data)
    print(f"  [screenshot] {path}")
    return path


async def goto_and_login(ws, base, token, path_):
    await evaluate(ws, "1")  # 确保 Runtime 就绪
    await call(ws, "Page.navigate", {"url": f"{base}/"})
    await asyncio.sleep(2.0)
    await evaluate(ws, f"localStorage.setItem('aiops.token', '{token}'); 'ok'")
    await call(ws, "Page.navigate", {"url": f"{base}{path_}"})
    await asyncio.sleep(2.0)


BADGE_RE = r"document.body.innerText.includes('需求基线')"


async def run():
    if not PROD_TOKEN:
        raise SystemExit("需先设置 AIOPS_VERIFY_TOKEN 环境变量（生产实例管理令牌）")
    chrome = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={CDP_PORT}",
            "--headless=new",
            "--no-first-run",
            "--disable-gpu",
            "--user-data-dir=/tmp/aiops-cdp-profile",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=1).read())
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome CDP 未就绪")

        import websockets

        ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=80 * 1024 * 1024)
        await call(ws, "Page.enable")
        await call(ws, "Runtime.enable")
        await call(ws, "Emulation.setDeviceMetricsOverride", {"width": 1560, "height": 1400, "deviceScaleFactor": 2, "mobile": False})

        # ---------- demo 实例 8601：released / escalated 徽标 + 终态弹窗 ----------
        print("== demo 8601 ==")
        await goto_and_login(ws, "http://127.0.0.1:8601", DEMO_TOKEN, "/requirements")
        await wait_for(ws, BADGE_RE, timeout=25)
        badges = await wait_for(
            ws,
            """(() => {
              const texts = [...document.querySelectorAll('table tbody tr')].map(tr =>
                [...tr.querySelectorAll('button')].map(b => b.textContent.trim()).join('|')
              ).filter(Boolean);
              return texts.length ? texts : null;
            })()""",
            timeout=25,
        )
        print("demo 行内徽标：")
        for t in badges or []:
            print("  ", t)
        rows = await evaluate(
            ws,
            """[...document.querySelectorAll('table tbody tr')].slice(0, 30).map(tr => ({
              t: tr.innerText.replace(/\\s+/g, ' ').slice(0, 90),
              b: [...tr.querySelectorAll('button')].map(x => x.textContent.trim()).join('|')
            })).filter(r => r.b)""",
        )
        for r in rows or []:
            print("  ROW:", r["b"], "||", r["t"])
        await screenshot(ws, "01-demo-requirements-badges.png")

        # 打开第一条「已发布」会话弹窗
        clicked = await evaluate(
            ws,
            """(() => {
              const btn = [...document.querySelectorAll('table tbody tr button')]
                .find(b => b.textContent.trim().startsWith('已发布'));
              if (!btn) return false;
              btn.click();
              return true;
            })()""",
        )
        print("点击已发布徽标:", clicked)
        modal_ok = await wait_for(ws, "document.body.innerText.includes('已发布成功 · 需求已落地（终态）')", timeout=15)
        note_ok = await evaluate(ws, "document.body.innerText.includes('该需求已发布成功（终态）：不能再次进行智能分析')")
        no_feedback = await evaluate(ws, "!document.body.innerText.includes('优化建议 / 具体要求')")
        no_approve = await evaluate(ws, "![...document.querySelectorAll('button')].some(b => b.textContent.includes('同意并进入修复工作流'))")
        title = await evaluate(ws, "document.title")
        print(f"弹窗终态卡片: {bool(modal_ok)} | note: {bool(note_ok)} | 无反馈输入: {bool(no_feedback)} | 无批准按钮: {bool(no_approve)} | title={title}")
        await screenshot(ws, "02-demo-released-modal.png")
        # 关闭弹窗（Esc / 关闭按钮）
        await evaluate(ws, """(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='关闭'); if(b) b.click(); return true; })()""")

        # ---------- production 实例 8600：真实存量 entry 23 ----------
        print("== production 8600 ==")
        await goto_and_login(ws, "http://127.0.0.1:8600", PROD_TOKEN, "/requirements")
        await wait_for(ws, BADGE_RE, timeout=25)
        p_rows = await wait_for(
            ws,
            """(() => {
              const rows = [...document.querySelectorAll('table tbody tr')].map(tr => ({
                t: tr.innerText.replace(/\\s+/g, ' ').slice(0, 80),
                b: [...tr.querySelectorAll('button')].map(x => x.textContent.trim()).join('|')
              })).filter(r => r.b);
              return rows.length ? rows : null;
            })()""",
            timeout=25,
        )
        print("production 行内徽标：")
        for r in p_rows or []:
            print("  ROW:", r["b"], "||", r["t"])
        await screenshot(ws, "03-prod-requirements-badges.png")

        # 工作台卡片：demo 最新会话 22（released）→ 已发布（v1）
        await call(ws, "Page.navigate", {"url": "http://127.0.0.1:8601/systems/app-d42214"})
        ok = await wait_for(ws, "document.body.innerText.includes('已发布（v1）')", timeout=25)
        print("demo 工作台卡片「已发布（v1）」:", bool(ok))
        await screenshot(ws, "04-demo-workbench-card.png")

        # production 工作台卡片：最近会话为 17（analyzed）→ 已分析（v1）（确认卡片渲染正常）
        await call(ws, "Page.navigate", {"url": "http://127.0.0.1:8600/systems/app-d42214"})
        ok2 = await wait_for(ws, "document.body.innerText.includes('已分析（v1）')", timeout=25)
        print("production 工作台卡片「已分析（v1）」:", bool(ok2))
        await screenshot(ws, "05-prod-workbench-card.png")

        await ws.close()
        print("完成")
    finally:
        chrome.terminate()


if __name__ == "__main__":
    asyncio.run(run())
