#!/usr/bin/env bash
# 兼容入口：所有命令、预检与进程管理只有 watchdog 一份实现。
# up/env 不生成演示数据、不自动启停共享基础设施；仅显式 demo 可播种。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "缺少仓库 .venv/bin/python；拒绝回退系统 Python" >&2
  exit 1
fi
if [ "$#" -eq 0 ]; then set -- up; fi
exec "$PY" "$ROOT/scripts/aiops-watchdog.py" "$@"
