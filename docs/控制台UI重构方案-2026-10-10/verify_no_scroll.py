#!/usr/bin/env python3
"""一次性验收脚本：系统清单表格「无嵌套滚动条」验证（table-fixed 百分比列宽恒适配容器）。

用法：aiops-agent/.venv/bin/python verify_no_scroll.py
产出：verify-pg-8-apps-no-scroll.png
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

CHECK_JS = """
(() => {
  const table = document.querySelector('table');
  if (!table) return JSON.stringify({ error: 'NO_TABLE' });
  const box = table.closest('div.mt-5');
  const root = document.documentElement;
  // 无截断：td 内文本元素横向内容不超容器（truncate 时会 scrollWidth > clientWidth）
  const clipped = [...table.querySelectorAll('tbody td span, tbody td div')]
    .filter((el) => el.textContent.trim() && el.scrollWidth > el.clientWidth + 1)
    .map((el) => el.textContent.trim().slice(0, 40));
  const addrSpan = document.querySelector('table tbody tr td:nth-child(5) span');
  const logSpan = document.querySelector('table tbody tr td:nth-child(7) span');
  const nameDiv = document.querySelector('table tbody tr td:nth-child(2) div');
  return JSON.stringify({
    viewport: window.innerWidth,
    boxScrollW: box.scrollWidth,
    boxClientW: box.clientWidth,
    noNestedScroll: box.scrollWidth <= box.clientWidth + 1,
    noPageScrollX: root.scrollWidth <= root.clientWidth + 1,
    noClipped: clipped.length === 0,
    clippedCells: clipped,
    addrText: addrSpan ? addrSpan.innerText : null,
    addrH: addrSpan ? addrSpan.offsetHeight : null,
    logH: logSpan ? logSpan.offsetHeight : null,
    nameH: nameDiv ? nameDiv.offsetHeight : null,
  });
})()
"""


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

        cdp.send("Page.navigate", url=f"{BASE}/login")
        time.sleep(2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")
        cdp.send("Page.navigate", url=f"{BASE}/monitored-apps")
        time.sleep(4.5)

        ok = True
        for width in (1280, 1440, 1680, 1920):
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                width=width,
                height=900,
                deviceScaleFactor=1,
                mobile=False,
            )
            time.sleep(0.8)
            result = json.loads(eval_js(CHECK_JS))
            passed = (
                result.get("noNestedScroll")
                and result.get("noPageScrollX")
                and result.get("noClipped")
            )
            ok = ok and passed
            print(f"@{width}px:", json.dumps(result, ensure_ascii=False), "PASS" if passed else "FAIL")

        # 恢复 1440 截图
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            width=1440,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        time.sleep(1.0)
        data = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        path = OUT / "verify-pg-8-apps-no-scroll.png"
        path.write_bytes(base64.b64decode(data["data"]))
        print("saved:", path)
        print("ALL_PASS" if ok else "HAS_FAIL")
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
