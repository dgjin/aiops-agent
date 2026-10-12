#!/usr/bin/env python3
"""P3 验收截图（Phase 2）：系统总览聚合页 + 工作台活动时间线。

前置：
- BFF 运行于 127.0.0.1:8600（静态托管 web/dist）；
- 提供可登录账号：`AIOPS_VERIFY_USER` / `AIOPS_VERIFY_PASSWORD` 环境变量
  （production 档静态令牌已不可用，须走真实登录换取会话令牌）。

用法：AIOPS_VERIFY_USER=... AIOPS_VERIFY_PASSWORD=... .venv/bin/python verify_p3_shots.py
产出：verify-p3-1-overview.png（总览：计数带 + 未纳管提示行）
      verify-p3-2-workbench-timeline.png（工作台：多流程分组活动时间线）
运行后自动清理临时 Chrome profile。
"""

import base64
import json
import os
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
APP_ID = "app-d42214"


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
    user = os.environ.get("AIOPS_VERIFY_USER", "")
    password = os.environ.get("AIOPS_VERIFY_PASSWORD", "")
    if not user or not password:
        raise SystemExit("需要 AIOPS_VERIFY_USER / AIOPS_VERIFY_PASSWORD 环境变量")

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
            data = cdp.send(
                "Page.captureScreenshot", format="png", captureBeyondViewport=True
            )
            path = OUT / name
            path.write_bytes(base64.b64decode(data["data"]))
            print("saved:", path)

        # 1. 同源页面真实登录 → 写入会话令牌 + 固定深色主题
        goto(f"{BASE}/login", 2.5)
        payload = json.dumps({"username": user, "password": password})
        outcome = eval_js(
            "(async () => {"
            f"const r = await fetch('/api/auth/login',{{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:{json.dumps(payload)} }});"
            "const d = await r.json();"
            "if (!r.ok) return 'ERROR:' + (d.error || r.status);"
            "localStorage.setItem('aiops.token', d.token);"
            "localStorage.setItem('aiops.theme','dark');"
            "localStorage.setItem('aiops.accent','teal');"
            "localStorage.setItem('aiops.sidebar','expanded');"
            "return 'ok:' + d.user + '/' + d.role;"
            "})()"
        )
        print("login:", outcome)
        if not str(outcome).startswith("ok:"):
            raise SystemExit(f"登录失败：{outcome}")

        # 2. 系统总览：聚合端点（计数带 + 系统卡 + 未纳管提示行）
        goto(f"{BASE}/", 4.0)
        shot("verify-p3-1-overview.png")

        # 3. 工作台：活动时间线（服务端聚合，多流程分组）
        goto(f"{BASE}/systems/{APP_ID}", 4.0)
        shot("verify-p3-2-workbench-timeline.png")
    finally:
        chrome.terminate()
        chrome.wait()
        shutil.rmtree(PROFILE, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    main()
