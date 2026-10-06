"""解析工具单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from parsing.parser import dump_bytes, first_tag, parse_duration, parse_line


class TestParser(unittest.TestCase):
    def test_parser_key_trimmed(self) -> None:
        """键必须去除两侧空白。"""
        self.assertEqual(parse_line(" host : db:5432"), ("host", "db:5432"))

    def test_parser_empty_line(self) -> None:
        self.assertEqual(parse_line("   "), ("", ""))

    def test_parser_duration_minutes(self) -> None:
        """m 后缀按分钟换算为秒。"""
        self.assertEqual(parse_duration("5m"), 300)

    def test_parser_duration_seconds(self) -> None:
        self.assertEqual(parse_duration("30s"), 30)

    def test_parser_dump_bytes_utf8(self) -> None:
        """中文必须可按 UTF-8 序列化。"""
        self.assertEqual(dump_bytes("订单").decode("utf-8"), "订单")

    def test_parser_first_tag_greedy(self) -> None:
        """多组方括号时必须取第一组。"""
        self.assertEqual(first_tag("a[host]b[port]"), "host")


if __name__ == "__main__":
    unittest.main()
