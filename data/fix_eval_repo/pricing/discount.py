"""折扣计算（价格域）。

修复评测夹具：本模块包含 4 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations


def apply_fixed(amount: float, coupon: float) -> float:
    """固定金额券后的应付金额；优惠超过订单金额时按 0 元计（不得为负）。"""
    return amount - coupon  # 缺陷 C1：未做 0 元下限截断


def apply_percent(amount: float, percent: float) -> float:
    """百分比折扣后的应付金额；percent 为 0~1，超过 100% 按 100% 截断。"""
    return amount * (1 - percent)  # 缺陷 C2：未把 percent 的上限截断到 1.0


def best_tier(tiers: list[float]) -> float:
    """最优档位折扣率 = 档位列表中的最大值；空列表按 0.0 处理。"""
    return max(tiers)  # 缺陷 C3：空列表 max() 抛 ValueError


def stack(amount: float, percent: float, fixed: float) -> float:
    """叠加规则：先应用固定金额券，再对余额应用百分比折扣。"""
    return amount * (1 - percent) - fixed  # 缺陷 C4：应用顺序颠倒（应先减固定券再打折）
