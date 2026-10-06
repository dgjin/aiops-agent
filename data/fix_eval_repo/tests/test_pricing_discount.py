"""折扣计算单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from pricing.discount import apply_fixed, apply_percent, best_tier, stack


class TestDiscount(unittest.TestCase):
    def test_pricing_fixed_no_negative(self) -> None:
        """优惠超过订单金额时应付金额按 0 元计。"""
        self.assertEqual(apply_fixed(30.0, 50.0), 0.0)

    def test_pricing_fixed_regular(self) -> None:
        """常规固定券（回归保护）。"""
        self.assertEqual(apply_fixed(100.0, 20.0), 80.0)

    def test_pricing_percent_clamp(self) -> None:
        """percent 超过 100% 按 100% 截断（应付不得为负）。"""
        self.assertEqual(apply_percent(100.0, 1.2), 0.0)

    def test_pricing_percent_regular(self) -> None:
        self.assertEqual(apply_percent(100.0, 0.1), 90.0)

    def test_pricing_best_tier_empty(self) -> None:
        """空档位列表按 0.0 处理，不得抛 ValueError。"""
        self.assertEqual(best_tier([]), 0.0)

    def test_pricing_best_tier_regular(self) -> None:
        self.assertEqual(best_tier([0.05, 0.10]), 0.10)

    def test_pricing_stack_order(self) -> None:
        """叠加顺序：先减固定券，再对余额打折。"""
        self.assertEqual(stack(100.0, 0.1, 20.0), 72.0)


if __name__ == "__main__":
    unittest.main()
