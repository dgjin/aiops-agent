#!/usr/bin/env python3
"""Qoder 自检守卫（PreToolUse hook）：只放行受限测试命令（P2⑤/P2⑥，2026-10-12）。

定位：
    qoder_fix.run_qoder_cli 在自检模式（AIOPS_QODER_SELF_CHECK=1，默认）下经
    `--settings` 注入本脚本为 PreToolUse hook（见 build_self_check_settings）。
    P2⑥ 实测（2026-10-12）证明 `--allowed-tools` 的 `Bash(...)` 模式并非封闭白名单
    （白名单外的 echo 也照常执行、Monitor 等工具通道不受其约束），因此命令级封闭
    改由本 hook 承担：CLI 在工具执行前调用本脚本，命中违规命令直接拒绝（exit 2）。

契约（对齐 Qoder CLI hooks 参考文档）：
    stdin  : hook 事件 JSON（tool_name / tool_input 等）。
    stdout : 不输出（stdout 被 CLI 按 JSON 解析，勿打印调试文本）。
    stderr : 拒绝原因（CLI 将其作为工具调用错误回显给模型）。
    exit   : 0=放行；2=拒绝（stderr 为原因）；其余=非阻塞警告。
             解析失败等一切异常同样按 2 拒绝（fail-closed）。

白名单：
    - 命令前缀默认与 qoder_fix.SELF_CHECK_COMMAND_PREFIXES 一致
      （一致性由 tests.test_qoder_fix 用例锁定；修改需同时改两处）；
    - 环境变量 AIOPS_QODER_HOOK_ALLOWED_PREFIXES（逗号分隔）可覆盖，便于运维调整；
    - 命令字符须全部落在安全正向集内（空白/字母数字/-_./:=,@+），防 `;`、`|`、
      `$()`、反引号等拼接注入；复合命令因含空格外字符被整体拒绝。
"""

from __future__ import annotations

import json
import os
import re
import sys

# 与 qoder_fix.SELF_CHECK_COMMAND_PREFIXES 保持一致（有回归用例锁定；修改需同时改两处）
DEFAULT_ALLOWED_PREFIXES = (
    "python -m unittest",
    "python3 -m unittest",
    "python -m pytest",
    "python3 -m pytest",
)
# 当前 CLI 已知的命令执行工具通道（P2⑥ 实测：Monitor 可直接执行命令且被模型偏好使用）
_COMMAND_TOOLS = frozenset({"Bash", "Monitor"})
# 命令白名单字符集（正向集）：未列举字符一律拒绝，防 shell 元字符拼接注入
_SAFE_COMMAND_RE = re.compile(r"[A-Za-z0-9_\-./:=,@+ ]+")
_PREFIXES_ENV = "AIOPS_QODER_HOOK_ALLOWED_PREFIXES"


def _allowed_prefixes() -> tuple[str, ...]:
    raw = os.environ.get(_PREFIXES_ENV, "")
    prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
    return prefixes or DEFAULT_ALLOWED_PREFIXES


def decide(event: dict) -> tuple[int, str]:
    """核心判定：返回 (exit_code, stderr 文本)。0=放行，2=拒绝。"""
    name = str(event.get("tool_name") or "")
    if name not in _COMMAND_TOOLS:
        return 0, ""
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return 2, f"guard: 无法识别 {name} 的命令内容，保守拒绝（仅允许受限测试命令）"
    cmd = command.strip()
    if not _SAFE_COMMAND_RE.fullmatch(cmd):
        return 2, f"guard: 命令含白名单外字符（防拼接注入），已拒绝: {cmd[:120]}"
    if any(cmd == p or cmd.startswith(p + " ") for p in _allowed_prefixes()):
        return 0, ""
    return 2, f"guard: 仅允许受限测试命令，已拒绝: {cmd[:120]}"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as exc:
        sys.stderr.write(f"guard: hook 事件 JSON 解析失败（{exc}），保守拒绝\n")
        return 2
    if not isinstance(event, dict):
        sys.stderr.write("guard: hook 事件格式异常，保守拒绝\n")
        return 2
    code, reason = decide(event)
    if reason:
        sys.stderr.write(reason + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
