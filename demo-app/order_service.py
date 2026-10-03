"""订单服务（演示代码库）：提交订单、应用优惠、落库。

该模块是 AIOps 演示的「被运维服务」代码库入口，供 Code RAG 索引与修复 Agent 生成补丁。
"""

from __future__ import annotations

import logging
from typing import Any

from coupon_client import CouponClient
from order_repository import OrderRepository

logger = logging.getLogger("order-service")

MAX_AMOUNT = 100_000


class OrderService:
    """订单提交与查询。"""

    def __init__(self, coupon_client: CouponClient, repository: OrderRepository) -> None:
        self.coupon_client = coupon_client
        self.repository = repository

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """提交订单：校验金额、应用优惠券、计算应付金额并落库。"""
        amount = payload.get("amount", 0)
        if amount <= 0 or amount > MAX_AMOUNT:
            raise ValueError(f"非法金额: {amount}")

        code = payload.get("coupon_code")
        coupon = self.coupon_client.fetch(code)
        discount = coupon["discount"]

        total = amount - discount
        order_id = self.repository.save(payload, total)
        logger.info("order submitted id=%s total=%s", order_id, total)
        return {"order_id": order_id, "total": total}

    def cancel(self, order_id: str) -> None:
        """取消订单（幂等）。"""
        self.repository.remove(order_id)
        logger.info("order cancelled id=%s", order_id)

    def list_orders(self) -> list[str]:
        """列出全部订单号。"""
        return self.repository.list_ids()
