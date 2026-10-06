"""带退避的重试工具（网关域）。

修复评测夹具：本模块包含 3 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class RetryExhausted(RuntimeError):
    """重试次数耗尽。"""


def run_with_retry(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 0.5) -> Any:
    """最多调用 fn attempts 次（含首次）；每次失败后等待 base_delay 秒再试；
    全部失败时抛出最后一次的原始异常。"""
    last: Exception | None = None
    for _ in range(attempts + 1):  # 缺陷 D1：调用总次数应恰为 attempts（当前多跑一次）
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 统一收集后重抛
            last = exc
            time.sleep(base_delay / 1000)  # 缺陷 D3：base_delay 单位为秒，误按毫秒缩小 1000 倍
    raise RetryExhausted("重试次数耗尽") from last  # 缺陷 D2：应重抛最后一次的原始异常
