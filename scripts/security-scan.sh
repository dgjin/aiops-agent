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
# 注意：bandit 只要发现问题（含 Medium）退出码即为 1，若直传会让「HIGH=0 即通过」
# 的门禁语义失真（文件头列出的历史接受 Medium 项会误杀 CI）。故用 --exit-zero 压制其
# 退出码，门禁判定统一由下方 High 计数完成。
OUT="$("$PY" -m bandit -r aiops_agent bff \
    alert_webhook.py demo_cli.py index_codebase.py ship_app_logs.py \
    scripts/aiops-watchdog.py \
    "$LEVEL" --exit-zero 2>&1)"
printf '%s\n' "$OUT" | tail -12

HIGH_COUNT="$(printf '%s\n' "$OUT" | sed -n 's/^[[:space:]]*High: \([0-9][0-9]*\)$/\1/p' | head -1)"
if [ "${HIGH_COUNT:-1}" != "0" ]; then
    echo "❌ 发布门禁未通过：HIGH 告警 ${HIGH_COUNT:-未知} 项（必须为 0）"
    exit 1
fi
echo "✅ 发布门禁通过：HIGH=0（Medium 项为历史审计接受的已知类别，见文件头说明）"

echo
echo "提示：依赖漏洞扫描可选执行 pip-audit："
echo "  $PY -m pip install pip-audit && $PY -m pip-audit -r requirements.txt"
