#!/usr/bin/env python3
"""一次性验收脚本：系统状态页「最近发布」是否自动反映最新发布记录。

背景：修复 run_canary_direct 不写 ArgoCD 留痕后，最新直连发布（p-req-22 / v1.0.937）
应出现在「系统状态 → 发布记录 → 最近发布」首位。

用法：aiops-agent/.venv/bin/python verify_system_releases.py
产出：verify-system-releases.png
运行后自动清理临时 Chrome profile。
"""

import base64
import json
import shutil
import subprocess
import sys
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


def main() -> None:
    chrome = launch_chrome()
    ok = False
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

        # 同源注入令牌 → 进入系统状态页
        goto(f"{BASE}/login", 2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")
        goto(f"{BASE}/system", 5.0)

        items = eval_js(
            """
            (() => {
              const items = [...document.querySelectorAll('li')]
                .map(li => li.innerText.trim())
                .filter(t => t.includes('.json'));
              return items.slice(0, 5);
            })()
            """
        )
        print("最近发布（前 5 条）：")
        for it in items or []:
            print("  -", it.replace("\\n", " | "))
        print("首条：", (items or [""])[0].replace("\\n", " | "))
        first = (items or [""])[0]
        if "v1.0.937" in first and "demo-app-p-req-22-r0" in first:
            print("PASS: 最近发布首条 = 最新直连发布（p-req-22 / v1.0.937）")
            ok = True
        else:
            print("FAIL: 首条不是最新发布记录（预期 p-req-22 / v1.0.937）")

        time.sleep(1.0)
        data = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        path = OUT / "verify-system-releases.png"
        path.write_bytes(base64.b64decode(data["data"]))
        print("saved:", path)
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)
    print("DONE" if ok else "VERIFY_FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
