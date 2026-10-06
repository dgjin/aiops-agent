"""配置行与时长解析（文本域）。

修复评测夹具：本模块包含 4 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

import re


class ParseError(ValueError):
    """解析错误。"""


def parse_line(line: str) -> tuple[str, str]:
    """解析 "key: value" 行：键去除两侧空白；值为第一个冒号后的剩余内容（可含冒号）；
    空行返回 ("", "")。"""
    if not line.strip():
        return "", ""
    key, value = line.split(":", 1)
    return key, value.strip()  # 缺陷 E1：键未去除两侧空白（应 key.strip()）


def parse_duration(text: str) -> int:
    """解析时长文本为秒数：后缀 s=秒，m=分钟（如 "5m" → 300）。"""
    unit = text[-1]
    amount = int(text[:-1])
    if unit == "m":
        return amount  # 缺陷 E2：分钟分支应换算为秒（×60）
    return amount


def dump_bytes(text: str) -> bytes:
    """把文本序列化为 UTF-8 字节（支持中文与 emoji）。"""
    return text.encode("ascii")  # 缺陷 E3：编码应为 utf-8


TAG_RE = re.compile(r"\[(.*)\]")  # 缺陷 E4：贪婪匹配，多组方括号时应取第一组（.*?）


def first_tag(text: str) -> str:
    """取文本中第一对方括号内的标签内容（如 "a[host]b[port]" → "host"）。"""
    match = TAG_RE.search(text)
    return match.group(1) if match else ""
