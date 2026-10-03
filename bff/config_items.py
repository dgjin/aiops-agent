"""控制台配置项元数据：当前值 / 来源 / 生效方式 / 对运行中流程是否生效。

背景（评估报告 A1/A2）：控制台此前只能展示闸门策略的「来源路径」一个字符串，运维无法
判断三件事——**哪些配置能在控制台改**、**改完要不要重启**、**对正在跑的流程是否生效**。
结果是同一控制台里"生效方式"不一致却毫无提示。

生效方式（``effect``）：

======== ==========================================================
hot      下一次请求即生效（读盘/读环境变量，不缓存）
restart  需重启对应进程（worker / BFF）后才生效
snapshot 工作流**启动时快照**；改后需重启，且对运行中的流程无效
======== ==========================================================

安全：敏感项（令牌）只回「已配置（N 个）」而**不回值**。
"""

from __future__ import annotations

import json
import os

HOT = "hot"
RESTART = "restart"
SNAPSHOT = "snapshot"

_SECRET_KEYS = {"AIOPS_CONSOLE_AUTH_TOKENS"}


def _display(key: str, default: str = "（未设置，使用默认值）") -> str:
    """展示配置值；敏感项脱敏。"""
    raw = os.environ.get(key)
    if key in _SECRET_KEYS:
        if not raw:
            return "（未配置 → fail-closed，拒绝所有 /api 请求）"
        try:
            count = len(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return "（已配置，但 JSON 非法 → fail-closed）"
        return f"已配置（{count} 个令牌，值不展示）"
    return raw if raw else default


def _item(
    key: str,
    label: str,
    value: str,
    *,
    source: str,
    effect: str,
    owner: str,
    applies_to_running: bool,
    note: str = "",
) -> dict:
    return {
        "key": key,
        "label": label,
        "value": value,
        "source": source,
        "effect": effect,
        "owner": owner,
        "applies_to_running": applies_to_running,
        "note": note,
    }


def collect(
    *,
    policy_source: str | None = None,
    policy_summary: str | None = None,
    monitored_app_count: int = 0,
) -> list[dict]:
    """汇总控制台可见的配置项（纯函数：仅读环境变量与传入快照，便于单测）。"""
    env = "环境变量 · .env"
    return [
        _item(
            "release-gate-policy.yaml",
            "闸门策略",
            policy_summary or "（未加载）",
            source=f"YAML · {policy_source or '未知'}",
            effect=SNAPSHOT,
            owner="worker / BFF",
            applies_to_running=False,
            note="工作流启动时快照；改后需重启，且对运行中的流程无效",
        ),
        _item(
            "data/monitored_apps.json",
            "被监控应用清单",
            f"{monitored_app_count} 个应用",
            source="控制台 · 被监控应用页",
            effect=HOT,
            owner="BFF",
            applies_to_running=True,
            note="每次请求读盘、不做进程缓存，保存即生效",
        ),
        _item(
            "AIOPS_CONSOLE_AUTH_TOKENS",
            "控制台访问令牌",
            _display("AIOPS_CONSOLE_AUTH_TOKENS"),
            source=env,
            effect=HOT,
            owner="BFF",
            applies_to_running=True,
            note="每次请求重新读取；未配置则 fail-closed（503）",
        ),
        _item(
            "AIOPS_FIX_PROVIDER",
            "修复提供者",
            _display("AIOPS_FIX_PROVIDER", "ollama（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_QODER_MODEL",
            "Qoder 修复模型",
            _display("AIOPS_QODER_MODEL", "DeepSeek-Flash（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
            note="模型 ID 区分大小写，写错会静默回退 auto",
        ),
        _item(
            "AIOPS_QODER_BIN",
            "Qoder CLI 路径",
            _display("AIOPS_QODER_BIN", "qoder（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_QODER_TIMEOUT",
            "Qoder 子进程超时",
            _display("AIOPS_QODER_TIMEOUT", "180s（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
            note="必须小于修复活动超时 300s，否则会被 Temporal 取消",
        ),
        _item(
            "AIOPS_FIX_MODEL",
            "本地修复模型（ollama）",
            _display("AIOPS_FIX_MODEL", "复用 triage 默认模型"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_SANDBOX_IMAGE",
            "测试沙箱镜像",
            _display("AIOPS_SANDBOX_IMAGE", "python:3.12-slim（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_CODE_INDEX",
            "代码索引路径",
            _display("AIOPS_CODE_INDEX", "data/code_index.json（默认）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_STABLE_CONTAINER / AIOPS_STABLE_PORT",
            "稳定版容器与端口",
            f"{_display('AIOPS_STABLE_CONTAINER', 'aiops-stable-order')} : "
            f"{_display('AIOPS_STABLE_PORT', '18080')}",
            source=env,
            effect=RESTART,
            owner="worker / BFF",
            applies_to_running=False,
        ),
        _item(
            "AIOPS_NOTIFY_PROVIDER",
            "通知提供者",
            _display("AIOPS_NOTIFY_PROVIDER", "未配置 → 留痕模式（recorded）"),
            source=env,
            effect=RESTART,
            owner="worker",
            applies_to_running=False,
        ),
        _item(
            "TEMPORAL_ADDRESS / AIOPS_LOKI_URL / AIOPS_OLLAMA_URL",
            "依赖服务地址",
            f"{_display('TEMPORAL_ADDRESS', 'localhost:7233')} · "
            f"{_display('AIOPS_LOKI_URL', 'http://localhost:3101')} · "
            f"{_display('AIOPS_OLLAMA_URL', 'http://localhost:11434')}",
            source=env,
            effect=RESTART,
            owner="worker / BFF",
            applies_to_running=False,
        ),
    ]
