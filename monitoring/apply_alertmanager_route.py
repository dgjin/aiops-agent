#!/usr/bin/env python3
"""AIOps Agent 接入脚本（WP1）：向 nl2sql-monitoring 栈的 Alertmanager 模板添加转发路由。

背景：本机复用「智能问数据分析系统」的 Prometheus/Alertmanager 栈作为 WP1 感知层数据源。
本脚本对其 deploy/alertmanager.yml（容器模板）做**最小侵入**修改：
    1. routes 最前新增子路由 matchers: ['aiops_demo = "true"'] → receiver aiops-agent
       （仅匹配显式标记的告警；放在 critical 子路由之前保证优先匹配，其余路由不受影响）；
    2. receivers 追加 aiops-agent（指向 AIOps 告警接入服务 http://host.docker.internal:8099/webhook）。

生效方式：修改模板后需重启 alertmanager 容器（entrypoint 启动时重新渲染模板）：
    docker restart nl2sql-monitoring-alertmanager-1

安全设计：
    - 修改前自动备份原文件（alertmanager.yml.bak.YYYYmmddHHMMSS）；
    - 幂等：已包含 aiops-agent 接收器时直接跳过；
    - 锚点校验：模板结构与预期不符时拒绝修改（绝不盲改）。

用法：
    python3 apply_alertmanager_route.py            # 默认路径
    python3 apply_alertmanager_route.py --file /path/to/alertmanager.yml
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sys
from pathlib import Path

DEFAULT_FILE = Path("/Users/dgjin/dgjinapp/智能问数据分析系统/deploy/alertmanager.yml")

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
    args = parser.parse_args()

    path: Path = args.file
    if not path.is_file():
        sys.exit(f"[FAIL] 文件不存在: {path}")
    text = path.read_text(encoding="utf-8")

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
