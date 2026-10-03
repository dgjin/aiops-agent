#!/usr/bin/env bash
# 场景 7：窗口内补丁排队（FIFO，不重置倒计时）—— 对应操作手册 6.7
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID1="${1:-demo-001}"
ID2="${2:-demo-002}"
WF1="aiops-fix-order-$ID1"
WF2="aiops-fix-order-$ID2"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID1"
echo ""
echo "等 $WF1 进入 NOTIFYING（公告倒计时）后执行："
echo "  $PY demo_cli.py approve --wf-id $WF1      # 先批准进入窗口"
echo "  $PY demo_cli.py queue-patch --wf-id $WF1 --new-wf-id $WF2 --service order --alert-id $ID2"
echo "  $PY demo_cli.py status --wf-id $WF1       # queued_patches 出现 $ID2"
echo "  $PY demo_cli.py deploy-now --wf-id $WF1"
echo "  $PY demo_cli.py status --wf-id $WF2       # 当前流程结束后自动开启新周期"
