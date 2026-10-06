"""KV 存储单测（修复评测夹具）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage.kv import KVStore


class TestKVStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "kv.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_kv_two_writes_persist(self) -> None:
        """连续写入不同键时必须保留既有数据并持久化。"""
        store = KVStore(self.path)
        store.put("a", 1)
        store.put("b", 2)
        reloaded = KVStore(self.path)
        self.assertEqual(reloaded.get("a"), 1)
        self.assertEqual(reloaded.get("b"), 2)

    def test_kv_get_int_coercion(self) -> None:
        """字符串存储的数值配置读取时应转为 int。"""
        store = KVStore(self.path)
        store.put("port", "8080")
        self.assertEqual(store.get_int("port"), 8080)

    def test_kv_get_int_default(self) -> None:
        self.assertEqual(KVStore(self.path).get_int("missing"), 0)

    def test_kv_none_key_rejected(self) -> None:
        """key 为 None 时必须抛 ValueError。"""
        store = KVStore(self.path)
        with self.assertRaises(ValueError):
            store.put(None, 1)


if __name__ == "__main__":
    unittest.main()
