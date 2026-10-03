#!/usr/bin/env bash
# AIOps 一键演示脚本：探活依赖 → 启动 worker+BFF → 灌日志 → 起流程
# 用法：
#   bash scripts/demo-up.sh           启动演示（幂等，已运行则跳过重启）
#   bash scripts/demo-up.sh down      停止 worker 与 BFF
#   bash scripts/demo-up.sh status    查看运行状态
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
export AIOPS_MODE="${AIOPS_MODE:-demo}"   # 演示强制文件后端，绝不触达生产 MySQL
export AIOPS_POLICY_PATH="${AIOPS_POLICY_PATH:-./demo-policy.yaml}"
SERVICE="${AIOPS_DEMO_SERVICE:-order}"
ALERT_ID="demo-$(date +%H%M%S)"
WORKER_LOG="$ROOT/worker.log"
BFF_LOG="$ROOT/bff.log"

c_green=$'\033[32m'; c_red=$'\033[31m'; c_yellow=$'\033[33m'; c_dim=$'\033[2m'; c_reset=$'\033[0m'
info()  { echo "${c_green}✔${c_reset} $*"; }
warn()  { echo "${c_yellow}▲${c_reset} $*"; }
err()   { echo "${c_red}✘${c_reset} $*" >&2; }
running() { pgrep -f "$1" >/dev/null 2>&1; }
# 轮询等待进程就绪（最多 ~10 秒），避免冷启动时固定 sleep 不足导致误判
wait_up() { local i; for i in $(seq 1 10); do running "$1" && return 0; sleep 1; done; return 1; }

cmd_down() {
  pkill -f "aiops_agent.worker" 2>/dev/null && info "worker 已停止" || warn "worker 本未运行"
  pkill -f "uvicorn bff.app:app" 2>/dev/null && info "BFF 已停止" || warn "BFF 本未运行"
}

cmd_status() {
  local ok=0
  running "aiops_agent.worker" && info "worker 运行中" || { warn "worker 未运行"; ok=1; }
  running "uvicorn bff.app:app" && info "BFF 运行中（http://127.0.0.1:8600）" || { warn "BFF 未运行"; ok=1; }
  return $ok
}

cmd_up() {
  echo "${c_dim}== 1/5 环境自检 ==${c_reset}"
  # 只检查硬依赖（Temporal/Loki/Ollama/Docker/策略/前端），日志新鲜度由本脚本后续灌入
  if ! "$PY" demo_cli.py doctor --service "$SERVICE" 2>&1 | grep -v "近 10 分钟无日志"; then
    warn "部分依赖未就绪（见上），仍尝试继续；若失败请先按提示修复"
  fi

  echo "${c_dim}== 2/5 启动 worker ==${c_reset}"
  if running "aiops_agent.worker"; then
    info "worker 已在运行，跳过"
  else
    nohup "$PY" -m aiops_agent.worker >>"$WORKER_LOG" 2>&1 &
    wait_up "aiops_agent.worker" && info "worker 已启动（日志 ${WORKER_LOG}）" || { err "worker 启动失败，见 ${WORKER_LOG}"; exit 1; }
  fi

  echo "${c_dim}== 3/5 启动 BFF 控制台 ==${c_reset}"
  if running "uvicorn bff.app:app"; then
    info "BFF 已在运行，跳过"
  else
    nohup "$PY" -m uvicorn bff.app:app --host 127.0.0.1 --port 8600 >>"$BFF_LOG" 2>&1 &
    wait_up "uvicorn bff.app:app" && info "BFF 已启动（http://127.0.0.1:8600，日志 ${BFF_LOG}）" || { err "BFF 启动失败，见 ${BFF_LOG}"; exit 1; }
  fi

  echo "${c_dim}== 4/5 灌入演示日志 ==${c_reset}"
  "$PY" demo_log_generator.py --service "$SERVICE" --mode surge --count 800
  "$PY" demo_log_generator.py --service "$SERVICE" --mode normal --count 60
  info "已灌入 $SERVICE 故障日志（落在近 10 分钟窗口内）"

  echo "${c_dim}== 5/5 启动修复流程 ==${c_reset}"
  "$PY" demo_cli.py start --service "$SERVICE" --alert-id "$ALERT_ID" --description "一键演示"

  cat <<EOF

${c_green}演示已就绪${c_reset}
  控制台：  http://127.0.0.1:8600
  工作流：  aiops-fix-$SERVICE-$ALERT_ID

接下来到控制台操作：
  1. 「审批中心」→ 找到该流程 → 点「批准」
  2. 「发布窗口」→ 看 30 秒倒计时 → 点「立即发布」
  3. 「流程详情」→ 查看根因 / 补丁 diff / 测试报告 / 发布结果

停止：bash scripts/demo-up.sh down
EOF
}

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  *) err "未知参数 $1（支持 up / down / status）"; exit 1 ;;
esac
