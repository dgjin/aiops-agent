#!/usr/bin/env python3
"""一次性验收脚本：Chrome headless + CDP 截取转人工待办页（搜索 / 仅看超时 / 空态）。

用法：aiops-agent/.venv/bin/python verify_esc_shots.py
产出：verify-esc-1-layout.png / -2-search.png / -3-overdue.png / -4-empty.png
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
            time.sleep(1.2)
            data = cdp.send(
                "Page.captureScreenshot", format="png", captureBeyondViewport=True
            )
            path = OUT / name
            path.write_bytes(base64.b64decode(data["data"]))
            print("saved:", path)

        set_input = """
        (value => {
          const input = document.querySelector('input[placeholder^="搜索服务"]');
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          return input.value;
        })(%s)
        """

        count_js = (
            "document.body.innerText.match(/显示 \\d+ \\/ 共 \\d+ 条/)?.[0] ?? 'N/A'"
        )

        # 1. 同源页面注入令牌 → 进入转人工待办
        goto(f"{BASE}/login", 2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")
        goto(f"{BASE}/escalations", 4.5)
        print("默认计数：", eval_js(count_js))
        shot("verify-esc-1-layout.png")

        # 2. 搜索「沙箱」→ 过滤生效
        eval_js(set_input % json.dumps("沙箱", ensure_ascii=False))
        print("搜索沙箱后计数：", eval_js(count_js))
        shot("verify-esc-2-search.png")

        # 3. 清空搜索 → 点击「仅看超时」
        eval_js(set_input % json.dumps("", ensure_ascii=False))
        click_overdue = eval_js(
            """
            (() => {
              const button = [...document.querySelectorAll('button')].find(
                b => b.textContent.trim().startsWith('仅看超时'));
              if (!button) return 'NO_BUTTON';
              button.click();
              return button.textContent.trim();
            })()
            """
        )
        print("仅看超时按钮：", click_overdue)
        print("仅看超时后计数：", eval_js(count_js))
        shot("verify-esc-3-overdue.png")

        # 4. 取消仅看超时 → 搜索「zzzz」→ 无匹配空态
        eval_js(
            """
            (() => {
              const button = [...document.querySelectorAll('button')].find(
                b => b.textContent.trim().startsWith('仅看超时'));
              if (button) button.click();
            })()
            """
        )
        eval_js(set_input % json.dumps("zzzz", ensure_ascii=False))
        print("无匹配计数：", eval_js(count_js))
        shot("verify-esc-4-empty.png")

        # 5. 点「重置条件」→ 恢复全量
        eval_js(
            """
            (() => {
              const button = [...document.querySelectorAll('button')].find(
                b => b.textContent.trim() === '重置条件');
              if (!button) return 'NO_BUTTON';
              button.click();
              return 'CLICKED';
            })()
            """
        )
        time.sleep(1.0)
        print("重置后计数：", eval_js(count_js))
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
