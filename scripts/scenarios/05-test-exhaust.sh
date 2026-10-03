#!/usr/bin/env bash
# 场景 5：重试耗尽转人工 —— 对应操作手册 6.5
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-exhaust}"
WF="aiops-fix-order-$ID"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID" --description "test-always-fail 演示"
echo "预期：tests:failed:attempt=0/1/2 后 stage=ESCALATED（不进入审批）"
echo "查看：$PY demo_cli.py result --wf-id $WF"
