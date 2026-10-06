"""重试工具单测（修复评测夹具）。"""

from __future__ import annotations

import unittest
from unittest import mock

from resilience.retry import run_with_retry


def _boom() -> None:
    raise RuntimeError("boom")


class TestRetry(unittest.TestCase):
    def test_retry_attempts_is_total_calls(self) -> None:
        """attempts 为总调用次数（含首次）。"""
        calls: list[int] = []

        def counting_boom() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        with mock.patch("time.sleep"):
            with self.assertRaises(Exception):
                run_with_retry(counting_boom, attempts=3)
        self.assertEqual(len(calls), 3)

    def test_retry_raises_original_error(self) -> None:
        """重试耗尽后必须抛出最后一次的原始异常（而非包装异常）。"""
        with mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                run_with_retry(_boom, attempts=2)
        self.assertEqual(str(ctx.exception), "boom")

    def test_retry_backoff_unit_seconds(self) -> None:
        """base_delay 单位为秒：每次退避等待都应等于 base_delay。"""
        slept: list[float] = []
        with mock.patch("time.sleep", side_effect=slept.append):
            with self.assertRaises(Exception):
                run_with_retry(_boom, attempts=2, base_delay=0.5)
        self.assertTrue(slept)
        self.assertEqual(set(slept), {0.5})


if __name__ == "__main__":
    unittest.main()
