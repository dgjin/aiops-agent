#!/usr/bin/env bash
# 组件守护开机自启（P1，launchd 用户级代理）：安装 / 卸载 / 状态。
#
# 语义：launchd 只托管 watchdog 一个进程（登录自启、崩溃自动重启）；6 个业务组件仍由
# watchdog 巡检拉起。KeepAlive=SuccessfulExit:false —— 正常退出（如单实例锁被占）不重启，
# 崩溃自动重启；ThrottleInterval 30s 限速兜底（细节见 deploy/launchd/com.aiops.watchdog.plist）。
#
# 用法：
#   bash scripts/launchd-install.sh install     # 安装并立即生效（自动接管手动启动的守护）
#   bash scripts/launchd-install.sh uninstall   # 卸载（组件保持运行，守护职责回到手动模式）
#   bash scripts/launchd-install.sh status      # 查看托管与守护进程状态
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.aiops.watchdog"
PLIST_SRC="$ROOT/deploy/launchd/${LABEL}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_reset=$'\033[0m'
info() { echo "${c_green}✔${c_reset} $*"; }
warn() { echo "${c_yellow}▲${c_reset} $*"; }

# 匹配真实守护进程（脚本内正则以字面量出现，本脚本自身命令行不含该模式，不会误杀）
WD_PATTERN='aiops-watchdog\.py'

cmd_install() {
  [ -f "${PLIST_SRC}" ] || { echo "✘ 缺少模板 ${PLIST_SRC}" >&2; exit 1; }
  [ -x "$ROOT/.venv/bin/python" ] || warn "未找到 .venv，将使用 ${PY}"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|__ROOT__|${ROOT}|g" -e "s|__PYTHON__|${PY}|g" "${PLIST_SRC}" > "${PLIST_DST}"

  # 先摘旧任务（重装场景），再停手动守护，最后挂载——避免 KeepAlive 与 pkill 竞态
  launchctl bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
  if pgrep -f "${WD_PATTERN}" >/dev/null 2>&1; then
    warn "检测到手动启动的守护，正在接管（组件保持运行）..."
    pkill -f "${WD_PATTERN}" || true
    sleep 1
  fi
  launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
  sleep 1
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    info "已安装并启动：${LABEL}（登录自启；崩溃自动重启）"
    info "守护日志：${ROOT}/aiops_watchdog.log（bash scripts/rotate-logs.sh 可轮转）"
    info "卸载：bash scripts/launchd-install.sh uninstall"
  else
    echo "✘ bootstrap 后未查到任务，请检查 ${PLIST_DST}" >&2
    exit 1
  fi
}

cmd_uninstall() {
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    launchctl bootout "${DOMAIN}/${LABEL}"
    info "已卸载：${LABEL}（常驻组件保持运行，守护职责回到手动模式）"
  else
    warn "任务未加载（可能已卸载）"
  fi
  rm -f "${PLIST_DST}"
  info "已移除 plist：${PLIST_DST}"
}

cmd_status() {
  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    local state
    state="$(launchctl print "${DOMAIN}/${LABEL}" | grep -m1 -E 'state = ' | sed 's/^[[:space:]]*//')"
    info "launchd 已托管：${LABEL}（${state}）"
  else
    warn "launchd 未托管（手动模式：由 demo-up.sh 启动的组件守护）"
  fi
  if pgrep -f "${WD_PATTERN}" >/dev/null 2>&1; then
    pgrep -fl "${WD_PATTERN}" | sed 's/^/    /'
    info "守护进程运行中"
  else
    warn "守护进程未运行"
  fi
}

case "${1:-install}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  *) echo "✘ 未知参数 ${1}（支持 install / uninstall / status）" >&2; exit 1 ;;
esac
