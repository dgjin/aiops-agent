"""时间窗工具单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from scheduling.window import WindowError, in_window, minutes_to_seconds, windows


class TestWindow(unittest.TestCase):
    def test_window_right_open_interval(self) -> None:
        """右端为开区间：ts == end 不算命中。"""
        self.assertTrue(in_window(99, 0, 100))
        self.assertFalse(in_window(100, 0, 100))

    def test_window_left_closed(self) -> None:
        self.assertTrue(in_window(0, 0, 100))

    def test_window_minutes_to_seconds(self) -> None:
        self.assertEqual(minutes_to_seconds(5), 300)

    def test_window_negative_duration_rejected(self) -> None:
        """负数时长必须抛 WindowError。"""
        with self.assertRaises(WindowError):
            windows(-1)

    def test_window_positive_duration(self) -> None:
        self.assertEqual(windows(61), 2)


if __name__ == "__main__":
    unittest.main()
