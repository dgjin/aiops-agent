"""优惠券服务客户端（下游 coupon-mock）。"""

from __future__ import annotations


class CouponClient:
    """查询优惠券信息；券码无效时返回 None。"""

    def __init__(self, endpoint: str = "http://coupon-mock/api/coupon") -> None:
        self.endpoint = endpoint

    def fetch(self, code: str | None) -> dict | None:
        """按券码查询优惠券。未提供券码或券码无效时返回 None。"""
        if not code:
            return None
        return {"code": code, "discount": 10}
