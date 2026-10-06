"""库存台账单测（修复评测夹具）。"""

from __future__ import annotations

import os
import unittest

from inventory.stock import InsufficientStock, StockLedger, over_threshold


class TestStock(unittest.TestCase):
    def test_stock_ship_rejects_insufficient(self) -> None:
        """库存不足时必须抛 InsufficientStock 且不得改变库存。"""
        ledger = StockLedger(5)
        with self.assertRaises(InsufficientStock):
            ledger.ship(10)
        self.assertEqual(ledger.count, 5)

    def test_stock_ship_valid(self) -> None:
        """正常出库（回归保护）。"""
        self.assertEqual(StockLedger(5).ship(3), 2)

    def test_stock_record_no_cross_call_leak(self) -> None:
        """标签登记不得跨调用共享状态。"""
        ledger = StockLedger()
        self.assertEqual(ledger.record("a"), ["a"])
        self.assertEqual(ledger.record("b"), ["b"])

    def test_stock_restock_counts_all_lots(self) -> None:
        """补货必须计入全部批次（含最后一批）。"""
        ledger = StockLedger(0)
        ledger.restock([5, 7])
        self.assertEqual(ledger.count, 12)

    def test_stock_over_threshold_env_numeric(self) -> None:
        """环境变量阈值必须按数值比较（字符串与 int 直接比较会抛 TypeError）。"""
        os.environ["STOCK_WIDTH_THRESHOLD"] = "120"
        try:
            self.assertTrue(over_threshold(150))
            self.assertFalse(over_threshold(100))
        finally:
            os.environ.pop("STOCK_WIDTH_THRESHOLD", None)


if __name__ == "__main__":
    unittest.main()
