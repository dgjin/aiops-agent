"""demo-app 服务入口（WP7 演示）：HTTP 服务化，供金丝雀发布真实探活。

端点：
    GET  /health   健康检查（版本 / 金丝雀模式）
    POST /order    提交订单（OrderService.submit，请求体 JSON）

故障注入（模拟新版缺陷的金丝雀劣化）：
    BAD_CANARY=1   业务请求注入 50% 失败 + 500ms 延迟；/health 保持正常（容器存活）
"""

from __future__ import annotations

import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from coupon_client import CouponClient
from order_repository import OrderRepository
from order_service import OrderService

BAD_CANARY = os.environ.get("BAD_CANARY") == "1"
APP_VERSION = os.environ.get("APP_VERSION", "v0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

service = OrderService(CouponClient(), OrderRepository())


class Handler(BaseHTTPRequestHandler):
    """最小 HTTP 处理器：健康检查 + 下单。"""

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": APP_VERSION, "canary_bad": BAD_CANARY})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定命名
        if self.path != "/order":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        if BAD_CANARY:
            time.sleep(0.5)  # 劣化注入：延迟放大
            if random.random() < 0.5:
                self._send(500, {"error": "injected canary failure"})
                return
        try:
            payload = json.loads(raw)
            self._send(200, service.submit(payload))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 服务端兜底 500
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持容器日志精简
        print(f"[http] {fmt % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"demo-app 启动：port={PORT} version={APP_VERSION} canary_bad={BAD_CANARY}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
