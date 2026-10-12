#!/usr/bin/env python3
"""一次性验收脚本：Chrome headless + CDP 验证分页（流程 / 需求基线 / 转人工待办 / 审计回看）
与系统清单显示优化（不回行、不截断裸奔）。

用法：aiops-agent/.venv/bin/python verify_pagination_shots.py
产出：verify-pg-1-flows-p1.png ... verify-pg-6-apps.png
运行后自动清理临时 Chrome profile。
"""

import base64
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

OUT = Path(__file__).resolve().parent
PROFILE = OUT / ".chrome-verify-profile"
BASE = "http://127.0.0.1:8600"
PORT = 9333
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def launch_chrome() -> subprocess.Popen:
    return subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--window-size=1440,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_target() -> str:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as resp:
                tabs = json.load(resp)
            page = next((t for t in tabs if t.get("type") == "page"), None)
            if page:
                return page["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("Chrome 调试端口未就绪")


class CDP:
    def __init__(self, url: str):
        self.ws = connect(url, proxy=None, max_size=64 * 1024 * 1024)
        self.seq = 0

    def send(self, method: str, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.seq:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})


RANGE_JS = "document.body.innerText.match(/第 \\d+–\\d+ 条 · 共 \\d+ 条/)?.[0] ?? 'NO_PAGER'"

# 分页条数字按钮：类含 h-7（与其他页面的数字文本按钮区分）
PAGE_BTN = """
(p => {
  const btn = [...document.querySelectorAll('button')].find(
    b => b.textContent.trim() === String(p) && b.classList.contains('h-7'));
  if (!btn) return 'NO_BUTTON';
  btn.click();
  return 'CLICKED';
})(%d)
"""

SET_SIZE = """
(v => {
  const label = [...document.querySelectorAll('label')].find(
    l => l.textContent.trim().startsWith('每页'));
  const select = label && label.querySelector('select');
  if (!select) return 'NO_SELECT';
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLSelectElement.prototype, 'value').set;
  setter.call(select, String(v));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return select.value;
})(%d)
"""


def main() -> None:
    chrome = launch_chrome()
    try:
        cdp = CDP(wait_target())
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        def eval_js(expr: str):
            result = cdp.send(
                "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True
            )
            return result.get("result", {}).get("value")

        def goto(url: str, wait: float) -> None:
            cdp.send("Page.navigate", url=url)
            time.sleep(wait)

        def shot(name: str) -> None:
            time.sleep(1.0)
            data = cdp.send(
                "Page.captureScreenshot", format="png", captureBeyondViewport=True
            )
            path = OUT / name
            path.write_bytes(base64.b64decode(data["data"]))
            print("saved:", path)

        def range_text() -> str:
            return eval_js(RANGE_JS)

        # 同源页面注入令牌 → 各页面复用
        goto(f"{BASE}/login", 2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")

        # 1. 流程列表：21 条 → 20/页 共 2 页
        goto(f"{BASE}/flows", 4.5)
        first1 = eval_js("document.querySelector('tbody tr td')?.textContent ?? 'N/A'")
        print("flows p1 范围：", range_text(), "| 首行 wf：", first1)
        shot("verify-pg-1-flows-p1.png")
        print("flows 点第 2 页：", eval_js(PAGE_BTN % 2))
        time.sleep(0.8)
        first2 = eval_js("document.querySelector('tbody tr td')?.textContent ?? 'N/A'")
        print("flows p2 范围：", range_text(), "| 首行 wf：", first2)
        shot("verify-pg-2-flows-p2.png")

        # 2. 需求基线：11 条 → 10/页 共 2 页
        goto(f"{BASE}/requirements", 5.0)
        print("req p1 范围：", range_text())
        print("req 点第 2 页：", eval_js(PAGE_BTN % 2))
        time.sleep(0.8)
        print("req p2 范围：", range_text())
        shot("verify-pg-3-req-p2.png")

        # 3. 转人工待办：22 条 → 10/页 共 3 页
        goto(f"{BASE}/escalations", 4.5)
        print("esc p1 范围：", range_text())
        print("esc 点第 3 页：", eval_js(PAGE_BTN % 3))
        time.sleep(0.8)
        print("esc p3 范围：", range_text())
        shot("verify-pg-4-esc-p3.png")

        # 4. 审计回看：操作审计 100 条 → 20/页 共 5 页；每页改 50 → 2 页
        goto(f"{BASE}/audit", 4.5)
        eval_js(
            "[...document.querySelectorAll('button')]"
            ".find(b => b.textContent.trim() === '操作审计')?.click()"
        )
        time.sleep(1.0)
        print("audit ops p1 范围：", range_text())
        print("audit 点第 5 页：", eval_js(PAGE_BTN % 5))
        time.sleep(0.8)
        print("audit p5 范围：", range_text())
        shot("verify-pg-5-audit-ops-p5.png")
        print("audit 每页改 50：", eval_js(SET_SIZE % 50))
        time.sleep(0.8)
        print("audit 50/页 范围：", range_text())

        # 5. 系统清单：1 条（分页条不渲染）+ 关键列单行检查
        goto(f"{BASE}/monitored-apps", 4.5)
        checks = eval_js(
            """
            (() => {
              const rows = document.querySelectorAll('table tbody tr');
              const tds = document.querySelectorAll('table tbody tr td');
              const nameDiv = document.querySelector('table tbody tr td:nth-child(2) div');
              const probeSpan = document.querySelector('table tbody tr td:nth-child(3) span');
              const timeTd = document.querySelector('table tbody tr td:nth-child(4)');
              const urlSpan = document.querySelector('table tbody tr td:nth-child(5) span');
              const table = document.querySelector('table');
              return JSON.stringify({
                rows: rows.length,
                nameSingleLine: nameDiv ? nameDiv.offsetHeight < 24 : null,
                probeSingleLine: probeSpan ? probeSpan.offsetHeight < 20 : null,
                timeContent: timeTd ? timeTd.textContent : null,
                urlSingleLine: urlSpan ? urlSpan.offsetHeight < 20 : null,
                tdCount: tds.length,
                tableScrollW: table ? table.scrollWidth : null,
                pager: (document.body.innerText.match(/第 \\d+–\\d+ 条/) || [null])[0],
              });
            })()
            """
        )
        print("apps 检查：", checks)
        shot("verify-pg-6-apps.png")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)
    print("DONE")


if __name__ == "__main__":
    main()
