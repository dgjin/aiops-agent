"""飞书应用机器人 API 客户端（tenant_access_token + 卡片发送 + 群列表）。

与群自定义机器人 webhook（notify.NotificationSender 的既有通道）的区别：
**应用机器人发送的消息卡片支持「回传交互」**（按钮点击由飞书回调至应用），
而自定义机器人 webhook 发送的卡片按官方限制仅支持跳转 URL、不支持回传——
因此卡片审批交互的真实链路必须经应用通道发送（2026-10-14 接入）。

仅依赖标准库（urllib）；供 notify.NotificationSender（发送通道选择）与
feishu_gateway.py（--send-test-card / --list-chats 子命令）复用。
凭证：AIOPS_FEISHU_APP_ID / AIOPS_FEISHU_APP_SECRET；未配置或调用失败时
抛 FeishuAppError，由调用方按各自降级语义处理（通知链路不抛错打断主流程）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

FEISHU_BASE = "https://open.feishu.cn"
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_PATH = "/open-apis/im/v1/messages"
_CHATS_PATH = "/open-apis/im/v1/chats"
_TIMEOUT = 10
# tenant_access_token 有效期 2h；提前 5 分钟失效重取。
_TOKEN_SAFETY_MARGIN = 300.0

# 无代理 opener：同 watchdog / mcp_server 的 ProxyHandler({}) 惯例——全局
# opener 惰性快照可能缓存系统代理（见进程入口 install_opener 的注释）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_token_cache: dict[str, object] = {"app_id": "", "value": "", "expire_at": 0.0}


class FeishuAppError(Exception):
    """应用通道调用失败（凭证缺失 / 网络异常 / 应答业务码非零）。"""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _request_json(request: urllib.request.Request) -> dict:
    try:
        with _OPENER.open(request, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响主判定
            detail = ""
        raise FeishuAppError(f"HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FeishuAppError(f"连接失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise FeishuAppError(f"应答非 JSON：{exc}") from exc


def fetch_tenant_access_token(app_id: str | None = None, app_secret: str | None = None) -> str:
    """获取 tenant_access_token（进程内缓存，过期前 5 分钟自动重取）。"""
    app_id = app_id or _env("AIOPS_FEISHU_APP_ID")
    app_secret = app_secret or _env("AIOPS_FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise FeishuAppError("AIOPS_FEISHU_APP_ID / AIOPS_FEISHU_APP_SECRET 未配置")
    now = time.time()
    if _token_cache["app_id"] == app_id and _token_cache["value"] and float(_token_cache["expire_at"]) > now:
        return str(_token_cache["value"])
    body = {"app_id": app_id, "app_secret": app_secret}
    request = urllib.request.Request(
        FEISHU_BASE + _TOKEN_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    data = _request_json(request)
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise FeishuAppError(f"取 token 失败：code={data.get('code')} msg={data.get('msg')}")
    expire = float(data.get("expire") or 7200)
    _token_cache.update({
        "app_id": app_id,
        "value": data["tenant_access_token"],
        "expire_at": now + max(expire - _TOKEN_SAFETY_MARGIN, 60.0),
    })
    return str(data["tenant_access_token"])


def send_card(card: dict, receive_id: str, receive_id_type: str = "chat_id") -> str:
    """以应用身份发送交互卡片（im/v1/messages）；成功返回 message_id。"""
    receive_id = (receive_id or "").strip()
    if not receive_id:
        raise FeishuAppError("接收方 ID 未配置（AIOPS_FEISHU_RECEIVE_ID）")
    token = fetch_tenant_access_token()
    query = urllib.parse.urlencode({"receive_id_type": receive_id_type})
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    request = urllib.request.Request(
        f"{FEISHU_BASE}{_MESSAGE_PATH}?{query}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    data = _request_json(request)
    if data.get("code") != 0:
        raise FeishuAppError(f"发送失败：code={data.get('code')} msg={data.get('msg')}")
    return str((data.get("data") or {}).get("message_id") or "")


def list_chats(page_size: int = 50) -> list[dict]:
    """机器人所在的群列表（用于获取群 chat_id；需应用具备群信息读取权限）。"""
    token = fetch_tenant_access_token()
    query = urllib.parse.urlencode({"page_size": page_size})
    request = urllib.request.Request(
        f"{FEISHU_BASE}{_CHATS_PATH}?{query}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = _request_json(request)
    if data.get("code") != 0:
        raise FeishuAppError(f"取群列表失败：code={data.get('code')} msg={data.get('msg')}")
    return list((data.get("data") or {}).get("items") or [])
