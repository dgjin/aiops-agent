"""文件型键值存储（本地 JSON 文件，UTF-8）。

修复评测夹具：本模块包含 3 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

import json
from pathlib import Path


class KVStore:
    """极简 JSON 文件 KV：启动时加载已有数据，写入即持久化。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict = {}
        if self.path.is_file():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def put(self, key: str, value) -> None:
        """写入键值并立即持久化：key 为 None 时抛 ValueError；不得影响其他已写入的键。"""
        self._data = {key: value}  # 缺陷 F1/F3：覆盖全量数据；key=None 未被校验拦截
        self._persist()

    def get_int(self, key: str, default: int = 0) -> int:
        """读取整数配置：值可能以字符串形式存储（如 "8080"），统一转换为 int 返回。"""
        raw = self._data.get(key, default)
        return raw  # 缺陷 F2：字符串值未转换为 int

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
