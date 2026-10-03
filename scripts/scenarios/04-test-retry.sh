#!/usr/bin/env bash
# 场景 4：测试回炉后通过 —— 对应操作手册 6.4
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-retry}"
WF="aiops-fix-order-$ID"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID" --description "test-fail 演示"
echo "预期：tests:failed:attempt=0 → tests:passed:attempt=1 → 继续审批链路"
echo "查看：$PY demo_cli.py result --wf-id $WF"
