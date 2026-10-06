"""发票金额计算单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from billing.invoice import InvoiceError, invoice_total, tax_amount, tier_discount, unit_price


class TestInvoice(unittest.TestCase):
    def test_invoice_tier_boundary(self) -> None:
        """金额恰好等于 1000 元时应享受档位折扣（含边界）。"""
        self.assertEqual(tier_discount(1000.0, 0.10), 100.0)

    def test_invoice_tier_below_threshold(self) -> None:
        """不足 1000 元不打折（回归保护）。"""
        self.assertEqual(tier_discount(999.0, 0.10), 0.0)

    def test_invoice_total_rounding(self) -> None:
        """总额按 2 位小数（分）四舍五入，浮点尾差不得进入结果。"""
        self.assertEqual(invoice_total(0.1, 3), 0.3)

    def test_invoice_unit_price_zero_quantity(self) -> None:
        """数量为 0 反推单价应抛 InvoiceError（而非 ZeroDivisionError）。"""
        with self.assertRaises(InvoiceError):
            unit_price(100.0, 0)

    def test_invoice_unit_price_regular(self) -> None:
        """正常反推单价（回归保护）。"""
        self.assertEqual(unit_price(100.0, 4), 25.0)

    def test_invoice_tax_default(self) -> None:
        """tax 字段缺省时税额按 0.0 处理。"""
        self.assertEqual(tax_amount({"amount": 100}), 0.0)

    def test_invoice_tax_present(self) -> None:
        """tax 字段存在时按值返回（回归保护）。"""
        self.assertEqual(tax_amount({"amount": 100, "tax": 5.5}), 5.5)


if __name__ == "__main__":
    unittest.main()
