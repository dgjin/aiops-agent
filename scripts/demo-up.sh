#!/usr/bin/env bash
# AIOps 一键启动脚本：环境准备（Colima/依赖容器）→ 自检 → 启动 worker/BFF/告警接入/可用性巡检 → 灌日志 → 起流程
# 用法：
#   bash scripts/demo-up.sh           一键启动（幂等，已运行则跳过重启）
#   bash scripts/demo-up.sh env       仅环境准备（Colima + Temporal/Loki 容器 + 等就绪）
#   bash scripts/demo-up.sh down      停止 worker 与 BFF
#   bash scripts/demo-up.sh down-all  停止应用与依赖容器（保留数据卷）
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
WEBHOOK_LOG="$ROOT/alert_webhook.log"
PROBER_LOG="$ROOT/app_prober.log"
TEMPORAL_ADDR="127.0.0.1:7233"
LOKI_URL="http://localhost:3101"

c_green=$'\033[32m'; c_red=$'\033[31m'; c_yellow=$'\033[33m'; c_dim=$'\033[2m'; c_reset=$'\033[0m'
info()  { echo "${c_green}✔${c_reset} $*"; }
warn()  { echo "${c_yellow}▲${c_reset} $*"; }
err()   { echo "${c_red}✘${c_reset} $*" >&2; }
running() { pgrep -f "$1" >/dev/null 2>&1; }
# 轮询等待进程就绪（最多 ~10 秒），避免冷启动时固定 sleep 不足导致误判
wait_up() { local i; for i in $(seq 1 10); do running "$1" && return 0; sleep 1; done; return 1; }
# 兼容两种 compose：优先 docker compose 插件，缺失则回退独立版 docker-compose
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    printf 'docker compose'
  else
    printf 'docker-compose'
  fi
}
# TCP 端口探活（最多 t 秒）
wait_tcp() { # host port timeout_s
  local host="$1" port="$2" t="${3:-60}" i
  for i in $(seq 1 "$t"); do
    nc -z "$host" "$port" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
# HTTP 200 探活（最多 t 秒）
wait_http() { # url timeout_s
  local url="$1" t="${2:-60}" i code
  for i in $(seq 1 "$t"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    [ "$code" = "200" ] && return 0
    sleep 1
  done
  return 1
}

cmd_down() {
  pkill -f "aiops_agent.worker" 2>/dev/null && info "worker 已停止" || warn "worker 本未运行"
  pkill -f "uvicorn bff.app:app" 2>/dev/null && info "BFF 已停止" || warn "BFF 本未运行"
  pkill -f "alert_webhook.py" 2>/dev/null && info "告警接入服务已停止" || warn "告警接入服务本未运行"
  pkill -f "app_prober.py" 2>/dev/null && info "可用性巡检已停止" || warn "可用性巡检本未运行"
}

# 停止应用 + 依赖容器（stop 而非 down：保留容器与数据卷，下次启动更快）
cmd_down_all() {
  cmd_down
  if docker info >/dev/null 2>&1; then
    local cc; cc="$(compose_cmd)"
    # shellcheck disable=SC2086
    $cc stop 2>&1 | sed 's/^/    /' || true
    info "依赖容器已停止（数据卷保留，下次启动自动恢复）"
  else
    warn "Docker 守护进程未运行，无需停止容器"
  fi
}

cmd_status() {
  local ok=0
  running "aiops_agent.worker" && info "worker 运行中" || { warn "worker 未运行"; ok=1; }
  running "uvicorn bff.app:app" && info "BFF 运行中（http://127.0.0.1:8600）" || { warn "BFF 未运行"; ok=1; }
  running "alert_webhook.py" && info "告警接入服务运行中（127.0.0.1:8099）" || { warn "告警接入服务未运行"; ok=1; }
  running "app_prober.py" && info "可用性巡检运行中（被监控应用 → 自动修复流程）" || { warn "可用性巡检未运行"; ok=1; }
  if docker info >/dev/null 2>&1; then
    docker ps --format '{{.Names}}' | grep -qx aiops-temporal && info "Temporal 运行中（127.0.0.1:7233）" || { warn "Temporal 未运行"; ok=1; }
    docker ps --format '{{.Names}}' | grep -qx aiops-loki && info "Loki 运行中（http://localhost:3101）" || { warn "Loki 未运行"; ok=1; }
  else
    warn "Docker 守护进程未运行（Colima 未启动）"
    ok=1
  fi
  return $ok
}

# 环境准备（幂等）：Colima 守护进程 → Temporal/Loki 容器 → 等端口就绪
cmd_env() {
  echo "${c_dim}== 环境准备：Colima / Docker / Temporal / Loki ==${c_reset}"
  if command -v colima >/dev/null 2>&1 && ! colima status >/dev/null 2>&1; then
    warn "Colima 未运行，正在启动（镜像已就绪时约 5~10 秒）..."
    colima start 2>&1 | tail -2 | sed 's/^/    /' || { err "Colima 启动失败，请手动运行 colima start 排查"; exit 1; }
  fi
  local waited=0
  until docker info >/dev/null 2>&1; do
    waited=$((waited + 1))
    if [ "$waited" -ge 30 ]; then break; fi
    if [ "$waited" -eq 1 ]; then warn "等待 Docker 守护进程就绪 ..."; fi
    sleep 1
  done
  docker info >/dev/null 2>&1 || { err "Docker 守护进程不可用（Colima / Docker Desktop 未就绪）"; exit 1; }
  info "Docker 守护进程可用"

  local cc; cc="$(compose_cmd)"
  # 幂等：容器已在运行无变化；缺失/停止时创建拉起（restart: unless-stopped 通常已随 daemon 自动恢复）
  # shellcheck disable=SC2086
  $cc up -d 2>&1 | sed 's/^/    /' || { err "依赖容器启动失败（请检查 ${cc} 与 docker-compose.yml）"; exit 1; }
  wait_tcp 127.0.0.1 7233 90 || { err "Temporal 未就绪（${TEMPORAL_ADDR}，超过 90 秒）"; exit 1; }
  info "Temporal 已就绪（${TEMPORAL_ADDR}，UI http://localhost:8233）"
  wait_http "$LOKI_URL/ready" 90 || { err "Loki 未就绪（${LOKI_URL}，超过 90 秒）"; exit 1; }
  info "Loki 已就绪（${LOKI_URL}）"
}

cmd_up() {
  cmd_env

  echo "${c_dim}== 1/5 环境自检 ==${c_reset}"
  # 检查硬依赖（Temporal/Loki/Ollama/Docker/策略/前端）；日志新鲜度由第 4 步灌入、
  # BFF 由第 3 步启动，这两项失败属预期噪声，从展示与告警判断中剔除
  local dout dshown
  dout="$("$PY" demo_cli.py doctor --service "$SERVICE" 2>&1 || true)"
  dshown="$(printf '%s\n' "$dout" | grep -v -e "近 10 分钟无日志" -e "BFF 控制台" -e "共 .* 项未就绪" || true)"
  printf '%s\n' "$dshown"
  if printf '%s\n' "$dshown" | grep -qE "❌|执行失败"; then
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

  echo "${c_dim}== 4/7 启动告警接入服务 ==${c_reset}"
  if running "alert_webhook.py"; then
    info "告警接入服务已在运行，跳过"
  else
    nohup "$PY" alert_webhook.py --port 8099 >>"$WEBHOOK_LOG" 2>&1 &
    wait_up "alert_webhook.py" && info "告警接入服务已启动（http://127.0.0.1:8099，日志 ${WEBHOOK_LOG}）" || { err "告警接入服务启动失败，见 ${WEBHOOK_LOG}"; exit 1; }
  fi

  echo "${c_dim}== 5/7 启动可用性巡检 ==${c_reset}"
  if running "app_prober.py"; then
    info "可用性巡检已在运行，跳过"
  else
    nohup "$PY" app_prober.py >>"$PROBER_LOG" 2>&1 &
    wait_up "app_prober.py" && info "可用性巡检已启动（连续 3 次失败自动触发修复流程，日志 ${PROBER_LOG}）" || { err "可用性巡检启动失败，见 ${PROBER_LOG}"; exit 1; }
  fi

  echo "${c_dim}== 6/7 灌入演示日志 ==${c_reset}"
  "$PY" demo_log_generator.py --service "$SERVICE" --mode surge --count 800
  "$PY" demo_log_generator.py --service "$SERVICE" --mode normal --count 60
  info "已灌入 $SERVICE 故障日志（落在近 10 分钟窗口内）"

  echo "${c_dim}== 7/7 启动修复流程 ==${c_reset}"
  "$PY" demo_cli.py start --service "$SERVICE" --alert-id "$ALERT_ID" --description "一键演示"

  cat <<EOF

${c_green}启动完成${c_reset}
  控制台：  http://127.0.0.1:8600
  工作流：  aiops-fix-$SERVICE-$ALERT_ID
  巡检：    每 15 秒探测「被监控应用」，连续 3 次不可达自动触发修复流程

接下来到控制台操作：
  1. 「审批中心」→ 找到该流程 → 点「批准」
  2. 「发布窗口」→ 看 30 秒倒计时 → 点「立即发布」
  3. 「流程详情」→ 查看根因 / 补丁 diff / 测试报告 / 发布结果

停止应用：  bash scripts/demo-up.sh down
停止全部：  bash scripts/demo-up.sh down-all（含依赖容器，保留数据卷）
EOF
}

case "${1:-up}" in
  up)       cmd_up ;;
  env)      cmd_env ;;
  down)     cmd_down ;;
  down-all) cmd_down_all ;;
  status)   cmd_status ;;
  *) err "未知参数 ${1}（支持 up / env / down / down-all / status）"; exit 1 ;;
esac
