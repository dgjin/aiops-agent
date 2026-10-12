#!/usr/bin/env python3
"""一次性验收脚本：系统清单「⋯」行内菜单交互（fixed 定位不被表格裁剪、动作联动、遮罩关闭）。

用法：aiops-agent/.venv/bin/python verify_apps_menu.py
产出：verify-pg-7-apps-menu.png
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
            time.sleep(0.8)
            data = cdp.send(
                "Page.captureScreenshot", format="png", captureBeyondViewport=True
            )
            path = OUT / name
            path.write_bytes(base64.b64decode(data["data"]))
            print("saved:", path)

        goto(f"{BASE}/login", 2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")
        goto(f"{BASE}/monitored-apps", 4.5)

        # 1. 点「⋯」→ 菜单出现（fixed 定位 + 4 个动作项）
        opened = eval_js(
            """
            (() => {
              const btn = document.querySelector('table tbody tr button[title^="更多操作"]');
              if (!btn) return 'NO_BUTTON';
              btn.click();
              return 'CLICKED';
            })()
            """
        )
        print("点开菜单：", opened)
        time.sleep(0.5)
        print(
            "菜单内容：",
            eval_js(
                """
                (() => {
                  const menu = document.querySelector('div.fixed.z-50');
                  if (!menu) return 'NO_MENU';
                  return JSON.stringify({
                    items: [...menu.querySelectorAll('button')].map(b => b.textContent.trim()),
                    pos: menu.style.cssText,
                  });
                })()
                """
            ),
        )
        shot("verify-pg-7-apps-menu.png")

        # 2. 点「编辑」→ 菜单关闭 + 编辑弹窗打开
        eval_js(
            "[...document.querySelectorAll('div.fixed.z-50 button')]"
            ".find(b => b.textContent.trim() === '编辑')?.click()"
        )
        time.sleep(0.8)
        print(
            "编辑弹窗：",
            eval_js(
                # 弹窗标题在普通 div（ConfirmDialog），非 h2/h3；菜单独有类 w-28 以区分同带 fixed z-50 的弹窗容器
                "(document.body.innerText.includes('编辑被监控应用') ? 'DIALOG_OPEN' : 'NO_DIALOG')"
                " + ' | menu=' + (document.querySelector('div.fixed.w-28') ? 'STILL_OPEN' : 'CLOSED')"
            ),
        )

        # 3. 先点遮罩关闭逻辑：关闭弹窗后重新打开菜单，点遮罩区域关闭
        eval_js(
            "[...document.querySelectorAll('button')]"
            ".find(b => b.textContent.trim() === '取消')?.click()"
        )
        time.sleep(0.5)
        eval_js(
            "document.querySelector('table tbody tr button[title^=\"更多操作\"]')?.click()"
        )
        time.sleep(0.4)
        print(
            "菜单再开：",
            eval_js("document.querySelector('div.fixed.w-28') ? 'OPEN' : 'NO_MENU'"),
        )
        eval_js("document.querySelector('div.fixed.inset-0.z-40')?.click()")
        time.sleep(0.4)
        print(
            "遮罩关闭后：",
            eval_js("document.querySelector('div.fixed.w-28') ? 'STILL_OPEN' : 'CLOSED'"),
        )
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
