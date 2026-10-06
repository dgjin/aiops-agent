"""库存台账（入库 / 出库 / 阈值判定）。

修复评测夹具：本模块包含 4 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

import os


class InsufficientStock(Exception):
    """库存不足。"""


class StockLedger:
    """单 SKU 库存台账。"""

    def __init__(self, initial: int = 0) -> None:
        self.count = initial

    def ship(self, qty: int) -> int:
        """出库：库存不足（qty > 当前库存）时抛 InsufficientStock，且不得改变库存。"""
        self.count -= qty  # 缺陷 B1：未校验库存不足，会出现负库存
        return self.count

    def record(self, tag: str, tags: list[str] = []) -> list[str]:  # noqa: B006
        """登记一个标签并返回当前标签列表；每次调用彼此独立（未传 tags 时为空列表）。"""
        tags.append(tag)  # 缺陷 B2：可变默认参数跨调用共享（应改为 None 兜底）
        return tags

    def restock(self, lots: list[int]) -> None:
        """按批次补货：每个批次的到货数量全部计入库存。"""
        for i in range(len(lots) - 1):  # 缺陷 B3：跳过了最后一批
            self.count += lots[i]


def over_threshold(width: int) -> bool:
    """货架宽度是否超过告警阈值（阈值来自环境变量 STOCK_WIDTH_THRESHOLD，默认 100）。"""
    threshold = os.environ.get("STOCK_WIDTH_THRESHOLD", "100")
    return width > threshold  # 缺陷 B4：阈值是字符串，与整数比较在 Python 3 抛 TypeError
