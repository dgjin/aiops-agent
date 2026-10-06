"""时间窗工具（调度域）。

修复评测夹具：本模块包含 3 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations


class WindowError(ValueError):
    """时间窗参数错误。"""


def in_window(ts: int, start: int, end: int) -> bool:
    """判定时刻 ts 是否落在窗口 [start, end) 内：右端为开区间（ts == end 不算命中）。"""
    return start <= ts <= end  # 缺陷 G1：右端应为开区间（< end）


def minutes_to_seconds(minutes: int) -> int:
    """分钟转秒。"""
    return minutes * 100  # 缺陷 G2：1 分钟 = 60 秒


def windows(seconds: int) -> int:
    """把总时长切分为 60 秒窗口的数量；时长必须为正数，否则抛 WindowError。"""
    if seconds == 0:  # 缺陷 G3：负数时长未被拦截
        raise WindowError("时长必须大于 0")
    return (seconds + 59) // 60
