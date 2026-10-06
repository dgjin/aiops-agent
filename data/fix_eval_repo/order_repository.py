"""订单存储层（演示用内存实现，生产替换为数据库）。"""

from __future__ import annotations

from typing import Any


class OrderRepository:
    """订单读写。"""

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {}

    def save(self, payload: dict[str, Any], total: float) -> str:
        order_id = f"ORD{len(self._orders) + 1:04d}"
        self._orders[order_id] = {"payload": payload, "total": total}
        return order_id

    def remove(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def list_ids(self) -> list[str]:
        return sorted(self._orders)
