#!/usr/bin/env bash
# 场景 8：窗口内取消（队列保留、队首自动承接）—— 对应操作手册 6.8
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
ID1="${1:-demo-001}"
ID3="${2:-demo-003}"
WF1="aiops-fix-order-$ID1"
WF3="aiops-fix-order-$ID3"

$PY demo_log_generator.py --service order --mode surge --count 800
$PY demo_cli.py start --service order --alert-id "$ID1"
echo ""
echo "等 $WF1 进入 NOTIFYING 后执行："
echo "  $PY demo_cli.py approve --wf-id $WF1"
echo "  $PY demo_cli.py queue-patch --wf-id $WF1 --new-wf-id $WF3 --service order --alert-id $ID3"
echo "  $PY demo_cli.py cancel --wf-id $WF1"
echo "  $PY demo_cli.py status --wf-id $WF3       # 新周期已自动启动"
echo "预期：$WF1 stage=CANCELLED（gate3:cancelled），$WF3 进入 TRIAGING"
