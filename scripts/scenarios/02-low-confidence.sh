#!/usr/bin/env bash
# 场景 2：闸门 1 低置信度转人工 —— 对应操作手册 6.2
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID="${1:-demo-low}"
WF="aiops-fix-order-$ID"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID" --description "low-conf 演示"
echo "预期：stage=ESCALATED，gate_events 含 gate1:blocked:confidence=0.55<0.8，不产生 patch"
echo "查看：$PY demo_cli.py result --wf-id $WF"
