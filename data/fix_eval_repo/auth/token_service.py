"""访问令牌服务（与 demo-app 基线一致，含 F02 缺陷：畸形 expiry 抛裸 ValueError）。"""

from __future__ import annotations

import hashlib
import hmac


class TokenError(Exception):
    """令牌校验失败。"""


class TokenService:
    """极简 HMAC 令牌校验：令牌格式 <expiry>.<signature>。"""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    def _sign(self, expiry: str) -> str:
        return hmac.new(self.secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()[:16]

    def issue(self, expiry: int) -> str:
        """签发令牌（expiry 为 unix 秒）。"""
        text = str(expiry)
        return f"{text}.{self._sign(text)}"

    def verify(self, token: str, now: int) -> bool:
        """校验令牌：签名不符或已过期返回 False，格式非法抛 TokenError。"""
        expiry_text, _, signature = token.partition(".")
        expiry = int(expiry_text)  # 缺陷路径：空串 / 非数字 expiry 在此抛裸 ValueError
        if not hmac.compare_digest(signature, self._sign(expiry_text)):
            return False
        return now < expiry
