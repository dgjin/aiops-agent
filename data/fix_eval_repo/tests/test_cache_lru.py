"""LRU 缓存单测（修复评测夹具）。"""

from __future__ import annotations

import unittest

from cache.lru import LRUCache


class TestLRUCache(unittest.TestCase):
    def test_cache_has_none_value(self) -> None:
        """值为 None 的键仍然存在。"""
        cache = LRUCache(2)
        cache.put("k", None)
        self.assertTrue(cache.has("k"))
        self.assertFalse(cache.has("missing"))

    def test_cache_get_refreshes_order(self) -> None:
        """get 命中后该键应刷新为最近使用。"""
        cache = LRUCache(4)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        cache.get("a")
        self.assertEqual(cache.keys(), ["b", "c", "a"])

    def test_cache_no_premature_eviction(self) -> None:
        """恰好等于容量时不得淘汰；超出时才淘汰。"""
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(len(cache), 2)
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        cache.put("c", 3)
        self.assertEqual(len(cache), 2)


if __name__ == "__main__":
    unittest.main()
