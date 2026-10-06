#!/usr/bin/env python3
"""AIOps Agent 接入脚本（WP1）：向 nl2sql-monitoring 栈的 Alertmanager 模板添加转发路由与接入鉴权。

背景：本机复用「智能问数据分析系统」的 Prometheus/Alertmanager 栈作为 WP1 感知层数据源。
本脚本对其 deploy/alertmanager.yml（容器模板）做**最小侵入**修改：
    1. routes 最前新增子路由 matchers: ['aiops_demo = "true"'] → receiver aiops-agent
       （仅匹配显式标记的告警；放在 critical 子路由之前保证优先匹配，其余路由不受影响）；
    2. receivers 追加 aiops-agent（指向 AIOps 告警接入服务 http://host.docker.internal:8099/webhook）；
    3. （--set-webhook-token）向 aiops-agent receiver 写入 http_config.authorization 头，
       把共享密钥带到告警接入服务（webhook 侧 P0-1 鉴权），幂等 upsert。

生效方式：修改模板后需重启 alertmanager 容器（entrypoint 启动时重新渲染模板）：
    docker restart nl2sql-monitoring-alertmanager-1

安全设计：
    - 修改前自动备份原文件（alertmanager.yml.bak.YYYYmmddHHMMSS）；
    - 幂等：已包含 aiops-agent 接收器时直接跳过；令牌一致时同样跳过；
    - 锚点校验：模板结构与预期不符时拒绝修改（绝不盲改）；
    - 令牌仅允许 URL 安全字符（[A-Za-z0-9_.~-]，8~128 位），拒绝注入非法内容。

用法：
    python3 apply_alertmanager_route.py                                # 默认路径，仅加路由
    python3 apply_alertmanager_route.py --set-webhook-token <TOKEN>    # 写入/更新鉴权令牌
    python3 apply_alertmanager_route.py --file /path/to/alertmanager.yml
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path

DEFAULT_FILE = Path("/Users/dgjin/dgjinapp/智能问数据分析系统/deploy/alertmanager.yml")

WEBHOOK_URL_LINE = "      - url: 'http://host.docker.internal:8099/webhook'\n"
SEND_RESOLVED_LINE = "        send_resolved: false\n"
AUTH_BLOCK = (
    "        http_config:\n"
    "          authorization:\n"
    "            type: Bearer\n"
    "            credentials: '{token}'\n"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_.~-]{8,128}")


def _receiver_block_range(text: str, name: str) -> tuple[int, int]:
    """定位指定 receiver 块 [start, end)；不存在返回 (-1, -1)。"""
    anchor = f"  - name: {name}\n"
    start = text.find(anchor)
    if start < 0:
        return -1, -1
    nxt = text.find("\n  - name: ", start + 1)
    return start, (len(text) if nxt < 0 else nxt + 1)


def apply_webhook_token(text: str, token: str) -> str:
    """将共享密钥写入 aiops-agent receiver 的 http_config.authorization（幂等 upsert）。"""
    start, end = _receiver_block_range(text, "aiops-agent")
    if start < 0 or WEBHOOK_URL_LINE.strip() not in text[start:end]:
        sys.exit("[FAIL] 未找到 aiops-agent 接收器（请先运行默认模式完成路由接入），拒绝修改")
    block = text[start:end]
    if "http_config:" in block:
        marker = "credentials: '"
        idx = block.find(marker)
        if idx < 0:
            sys.exit("[FAIL] aiops-agent 接收器已有 http_config 但无 credentials 字段，结构不符拒绝修改")
        endq = block.find("'", idx + len(marker))
        if endq < 0:
            sys.exit("[FAIL] credentials 引号不闭合，结构不符拒绝修改")
        if block[idx + len(marker):endq] == token:
            return text  # 令牌已一致，幂等跳过
        block = block[: idx + len(marker)] + token + block[endq:]
    else:
        if SEND_RESOLVED_LINE not in block:
            sys.exit("[FAIL] aiops-agent 接收器缺少 send_resolved 锚点，拒绝修改")
        block = block.replace(SEND_RESOLVED_LINE, SEND_RESOLVED_LINE + AUTH_BLOCK.format(token=token), 1)
    return text[:start] + block + text[end:]

ROUTES_OLD = """  routes:
    # critical 告警更频繁提醒
"""

ROUTES_NEW = """  routes:
    # 【AIOps Agent 接入点】带 aiops_demo="true" 标签的告警转发至本地自动修复智能体（WP1 感知层）。
    # 置于最前以保证优先匹配（演示告警可带 critical 标签，且不会落入下方 critical 子路由）；
    # 接入服务：AIOps Agent 工作区 aiops-agent/alert_webhook.py（默认端口 8099）。
    - matchers: ['aiops_demo = "true"']
      receiver: aiops-agent
      group_wait: 5s
      repeat_interval: 1h

    # critical 告警更频繁提醒
"""

RECEIVER_OLD = """  - name: webhook
    webhook_configs:
      - url: '__ALERT_WEBHOOK_URL__'
        send_resolved: true
"""

RECEIVER_NEW = """  - name: webhook
    webhook_configs:
      - url: '__ALERT_WEBHOOK_URL__'
        send_resolved: true

  # 【AIOps Agent 接入点】本地告警接入服务（aiops-agent/alert_webhook.py，默认 8099）
  - name: aiops-agent
    webhook_configs:
      - url: 'http://host.docker.internal:8099/webhook'
        send_resolved: false
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="向 Alertmanager 模板添加 AIOps 转发路由（幂等）")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument(
        "--set-webhook-token",
        metavar="TOKEN",
        default="",
        help="将共享密钥写入 aiops-agent receiver 的 http_config.authorization（幂等 upsert）",
    )
    args = parser.parse_args()

    path: Path = args.file
    if not path.is_file():
        sys.exit(f"[FAIL] 文件不存在: {path}")
    text = path.read_text(encoding="utf-8")

    if args.set_webhook_token:
        token = args.set_webhook_token.strip()
        if not TOKEN_RE.fullmatch(token):
            sys.exit("[FAIL] 令牌格式非法（要求 8~128 位 URL 安全字符 [A-Za-z0-9_.~-]）")
        new_text = apply_webhook_token(text, token)
        if new_text == text:
            print(f"[SKIP] 鉴权令牌已一致，无需修改: {path}")
            return
        backup = path.with_name(f"{path.name}.bak.{datetime.datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        path.write_text(new_text, encoding="utf-8")
        print(f"[OK] 已写入 aiops-agent receiver 鉴权令牌: {path}")
        print(f"[OK] 原文件已备份: {backup}")
        print("[NEXT] 重启容器生效: docker restart nl2sql-monitoring-alertmanager-1")
        return

    if "aiops-agent" in text:
        print(f"[SKIP] 已包含 aiops-agent 接入点，无需修改: {path}")
        return

    for name, anchor, _ in (
        ("routes 锚点", ROUTES_OLD, ROUTES_NEW),
        ("receivers 锚点", RECEIVER_OLD, RECEIVER_NEW),
    ):
        if anchor not in text:
            sys.exit(f"[FAIL] 模板结构与预期不符（未找到{name}），拒绝修改: {path}")

    backup = path.with_name(f"{path.name}.bak.{datetime.datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(path, backup)

    text = text.replace(ROUTES_OLD, ROUTES_NEW, 1)
    text = text.replace(RECEIVER_OLD, RECEIVER_NEW, 1)
    path.write_text(text, encoding="utf-8")
    print(f"[OK] 已添加 AIOps 转发路由与接收器: {path}")
    print(f"[OK] 原文件已备份: {backup}")
    print("[NEXT] 重启容器生效: docker restart nl2sql-monitoring-alertmanager-1")


if __name__ == "__main__":
    main()
