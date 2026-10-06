"""指标聚合（统计域）。

修复评测夹具：本模块包含 3 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations


def avg(values: list[float]) -> float:
    """算术平均；空列表按 0.0 处理。"""
    return sum(values) / len(values)  # 缺陷 H1：空列表时 ZeroDivisionError


def median(values: list[float]) -> float:
    """中位数：偶数个样本取中间两个值的平均；空列表按 0.0 处理。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[len(ordered) // 2]  # 缺陷 H2：偶数个样本未取中间两值的平均


def percentile(values: list[float], p: float) -> float:
    """第 p 百分位（最近秩法）：p 截断到 0~100；p=100 表示最大值；空列表按 0.0 处理。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    p = min(max(p, 0.0), 100.0)
    idx = int(len(ordered) * p / 100)  # 缺陷 H3：p=100 时索引等于长度，越界
    return ordered[idx]
