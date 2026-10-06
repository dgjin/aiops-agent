"""发票金额计算（结算域）。

修复评测夹具：本模块包含 4 个相互独立的待修复缺陷（见各行内注释）。
"""

from __future__ import annotations

from typing import Any

TIER_THRESHOLD = 1000.0


class InvoiceError(ValueError):
    """发票计算错误。"""


def tier_discount(amount: float, rate: float) -> float:
    """档位折扣金额：订单金额达到 TIER_THRESHOLD（含）起，按 rate 享受折扣。"""
    if amount > TIER_THRESHOLD:  # 缺陷 A1：边界应为 >=（恰好 1000 元即应享受折扣）
        return amount * rate
    return 0.0


def invoice_total(unit_price: float, quantity: int) -> float:
    """发票总额 = 单价 × 数量，金额按 2 位小数（分）四舍五入；数量不能为负。"""
    if quantity < 0:
        raise InvoiceError("数量不能为负")
    total = unit_price * quantity
    return total  # 缺陷 A2：未按 2 位小数四舍五入，浮点尾差会进入对账


def unit_price(total: float, quantity: int) -> float:
    """按总额与数量反推单价（对账用）；数量必须为正，否则抛 InvoiceError。"""
    return total / quantity  # 缺陷 A3：quantity=0 时抛 ZeroDivisionError，应抛 InvoiceError


def tax_amount(record: dict[str, Any]) -> float:
    """税额：record 未提供 tax 字段时按 0.0 处理（该字段为可选字段）。"""
    return record["tax"]  # 缺陷 A4：缺省字段应使用 record.get("tax", 0.0)
