#!/usr/bin/env bash
# 场景 6：金丝雀劣化自动回滚 —— 对应操作手册 6.6
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-bad}"
WF="aiops-fix-order-$ID"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID" --description "canary-bad 演示"
echo ""
echo "等到 WAIT_APPROVAL 后执行："
echo "  $PY demo_cli.py approve --wf-id $WF"
echo "  $PY demo_cli.py deploy-now --wf-id $WF"
echo "  $PY demo_cli.py result --wf-id $WF"
echo "预期：canary 错误率约 50% → canary:degraded->rollback → ESCALATED，rolled_back=true"
