"""LRU 缓存（会话域）。

修复评测夹具：本模块包含 3 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

from collections import OrderedDict


class LRUCache:
    """固定容量 LRU 缓存。"""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("容量必须为正")
        self.capacity = capacity
        self._data: OrderedDict[str, object] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> list[str]:
        """按使用顺序（最旧 → 最新）返回全部键。"""
        return list(self._data.keys())

    def has(self, key: str) -> bool:
        """键是否存在（与值是否为 None 无关）。"""
        return self._data.get(key) is not None  # 缺陷 I1：值为 None 的键被误判为不存在

    def get(self, key: str):
        """读取（未命中返回 None）；命中的键刷新为最近使用。"""
        if key not in self._data:
            return None
        return self._data[key]  # 缺陷 I2：未 move_to_end(key)，读取不刷新使用顺序

    def put(self, key: str, value) -> None:
        """写入：仅在超出容量时淘汰最久未使用的键（恰好等于容量时不得淘汰）。"""
        self._data[key] = value
        if len(self._data) >= self.capacity:  # 缺陷 I3：应为 >（恰好等于容量时不得淘汰）
            self._data.popitem(last=False)
