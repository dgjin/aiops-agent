#!/usr/bin/env bash
# 场景 1：正常全链路（推荐主剧本）—— 对应操作手册 6.1
# 灌日志 → 起流程 → 等待到审批点，后续到控制台或 CLI 完成审批/发布。
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-001}"
WF="aiops-fix-order-$ID"

echo "== 灌入 order 故障日志 =="
$PY demo_log_generator.py --service order --mode surge --count 800
echo "== 启动流程 $WF =="
$PY demo_cli.py start --service order --alert-id "$ID"
echo ""
echo "已到审批前置。后续操作（二选一）："
echo "  控制台：http://127.0.0.1:8600 → 审批中心批准 → 发布窗口立即发布"
echo "  CLI："
echo "    $PY demo_cli.py approve --wf-id $WF"
echo "    $PY demo_cli.py deploy-now --wf-id $WF"
echo "    $PY demo_cli.py result --wf-id $WF"
