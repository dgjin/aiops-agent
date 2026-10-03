#!/usr/bin/env bash
# 本仓库代码自身 SAST 扫描（优化方案 P2-04：Bandit 作为第二道深度扫描）。
#
# 说明：
#   - 沙箱内对「被修复补丁」的扫描由 aiops_agent/sandbox.py 的 run_bandit 执行；
#   - 本脚本面向 AIOps 自身代码库，供 CI / 发布前检查；
#   - 已知可接受告警（均有历史审计结论）：B310（urlopen 目标来自策略/配置而非外部输入）、
#     B104（容器内服务绑定 0.0.0.0）、B108（沙箱隔离环境使用 /tmp）、B610（误报：walrus 运算符）。
#
# 用法：
#   bash scripts/security-scan.sh            # Medium 及以上（发布门禁）
#   bash scripts/security-scan.sh -a         # 全部级别（含 Low，供审阅）
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

LEVEL="-ll"
[ "${1:-}" = "-a" ] && LEVEL="-l"

echo "== Bandit SAST 扫描（Medium 及以上；HIGH 必须为 0）=="
"$PY" -m bandit -r aiops_agent bff \
    alert_webhook.py demo_cli.py index_codebase.py ship_app_logs.py \
    "$LEVEL" 2>&1 | tail -12

echo
echo "提示：依赖漏洞扫描可选执行 pip-audit："
echo "  $PY -m pip install pip-audit && $PY -m pip-audit -r requirements.txt"
