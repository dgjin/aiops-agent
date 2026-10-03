#!/usr/bin/env bash
# 场景 3：受保护目录二级审批 —— 对应操作手册 6.3
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-protected}"
WF="aiops-fix-order-$ID"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID" --description "protected 演示"
echo ""
echo "等到 WAIT_APPROVAL 后依次执行（一级→二级→发布）："
echo "  $PY demo_cli.py approve --wf-id $WF"
echo "  $PY demo_cli.py second-approve --wf-id $WF"
echo "  $PY demo_cli.py deploy-now --wf-id $WF"
echo "  $PY demo_cli.py result --wf-id $WF"
echo "预期：gate_events 含 gate2b:approved:protected-dir"
