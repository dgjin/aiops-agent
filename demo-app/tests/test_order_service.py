"""订单服务单测（供修复 Agent 的语义参考）。"""

from __future__ import annotations

import unittest

from coupon_client import CouponClient
from order_repository import OrderRepository
from order_service import OrderService


class TestOrderService(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OrderService(CouponClient(), OrderRepository())

    def test_submit_with_valid_coupon(self) -> None:
        result = self.service.submit({"amount": 100, "coupon_code": "SAVE10"})
        self.assertEqual(result["total"], 90)

    def test_submit_without_coupon_code(self) -> None:
        """未携带券码时按原价提交（修复验收：不得抛异常，total=amount）。"""
        result = self.service.submit({"amount": 100})
        self.assertEqual(result["total"], 100)

    def test_submit_rejects_bad_amount(self) -> None:
        with self.assertRaises(ValueError):
            self.service.submit({"amount": 0})


if __name__ == "__main__":
    unittest.main()
