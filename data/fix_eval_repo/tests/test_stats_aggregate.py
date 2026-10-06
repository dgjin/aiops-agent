"""指标聚合单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from stats.aggregate import avg, median, percentile


class TestAggregate(unittest.TestCase):
    def test_stats_avg_empty(self) -> None:
        """空列表平均值按 0.0 处理。"""
        self.assertEqual(avg([]), 0.0)

    def test_stats_avg_regular(self) -> None:
        self.assertEqual(avg([1, 2, 3]), 2.0)

    def test_stats_median_even_count(self) -> None:
        """偶数个样本取中间两值的平均。"""
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_stats_median_odd_count(self) -> None:
        self.assertEqual(median([3, 1, 2]), 2.0)

    def test_stats_percentile_p100(self) -> None:
        """p=100 取最大值，不得越界。"""
        self.assertEqual(percentile([1, 2, 3], 100), 3)

    def test_stats_percentile_regular(self) -> None:
        self.assertEqual(percentile([1, 2, 3], 50), 2)


if __name__ == "__main__":
    unittest.main()
