#!/usr/bin/env python3
"""一次性验收脚本：Chrome headless + CDP 截取需求基线页（搜索 / 排序 / 空态）。

用法：aiops-agent/.venv/bin/python verify_req_shots.py
产出：verify-req-1-layout.png / -2-search.png / -3-sort.png / -4-empty.png
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
          const input = document.querySelector('input[placeholder^="搜索标题"]');
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          return input.value;
        })(%s)
        """

        # 1. 同源页面注入令牌 → 进入需求基线
        goto(f"{BASE}/login", 2.5)
        eval_js("localStorage.setItem('aiops.token','tok-adm')")
        goto(f"{BASE}/requirements", 4.5)
        shot("verify-req-1-layout.png")

        # 2. 搜索「用户」→ 过滤生效
        eval_js(set_input % json.dumps("用户", ensure_ascii=False))
        shot("verify-req-2-search.png")

        # 3. 清空搜索 + 排序切换「最近更新」
        eval_js(set_input % json.dumps("", ensure_ascii=False))
        eval_js(
            """
            (() => {
              const select = [...document.querySelectorAll('select')].find(
                s => [...s.options].some(o => o.textContent.includes('排序')));
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value').set;
              setter.call(select, 'updated_desc');
              select.dispatchEvent(new Event('change', { bubbles: true }));
              return select.value;
            })()
            """
        )
        shot("verify-req-3-sort.png")

        # 4. 搜索「zzzz」→ 无匹配空态
        eval_js(set_input % json.dumps("zzzz", ensure_ascii=False))
        shot("verify-req-4-empty.png")

        # 5. 点「重置条件」→ 恢复全量（顺带确认重置按钮行为）
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
        count = eval_js(
            "document.body.innerText.match(/显示 \\d+ \\/ 共 \\d+ 条/)?.[0] ?? 'N/A'"
        )
        print("重置后计数：", count)
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
