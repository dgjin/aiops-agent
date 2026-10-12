#!/usr/bin/env bash
# start.sh — AIOps Agent 一键启动入口（仓库根）
#
# 用法：
#   ./start.sh           一键启动：环境准备 + 7 常驻组件（幂等）→ 等控制台就绪 → 自动打开浏览器
#   ./start.sh web       一键启动（开发档）：后端全家桶 + 前台启动前端 vite dev（热更，5273）
#   ./start.sh back      仅后端全家桶（不自动打开浏览器；无头 / 脚本化环境用）
#   ./start.sh down      停止全部常驻组件（透传 scripts/demo-up.sh）
#   ./start.sh status    查看组件状态与局域网控制台地址（透传）
#   ./start.sh env       仅准备依赖环境（Colima / Temporal / Loki，透传）
#   ./start.sh down-all  停止组件与依赖容器（保留数据卷，透传）
#
# 底层完全复用 scripts/demo-up.sh（10 步、幂等）；本脚本只做三件事：
#   入口收短（根目录一条命令）+ 就绪等待（轮询 BFF）+ 浏览器打开。
set -u

cd "$(dirname "$0")"

c_green=$'\033[32m'
c_yellow=$'\033[33m'
c_dim=$'\033[2m'
c_reset=$'\033[0m'

info() { echo "${c_green}[start]${c_reset} $*"; }
warn() { echo "${c_yellow}[start]${c_reset} $*"; }

BFF_URL="http://127.0.0.1:8600"
WEB_URL="http://127.0.0.1:5273"

# 轮询 URL 直到 2xx（curl 直连忽略代理；--max-time 防挂起）
wait_http() {
  local url="${1}" timeout="${2:-30}" i=0
  while [ "${i}" -lt "${timeout}" ]; do
    if curl -sf -o /dev/null --noproxy '*' --max-time 2 "${url}" 2>/dev/null; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "${1}" >/dev/null 2>&1 && info "已打开浏览器：${1}"
  else
    warn "未找到 open 命令，请手动访问：${1}"
  fi
}

cmd_back() {
  bash scripts/demo-up.sh up || { warn "启动未完成，见上方输出"; exit 1; }
  if wait_http "${BFF_URL}/" 15; then
    info "控制台就绪：${BFF_URL}"
  else
    warn "控制台暂未就绪（稍后自动可用；可运行 ./start.sh status 复查）"
  fi
}

case "${1:-__default__}" in
  __default__)
    cmd_back
    open_browser "${BFF_URL}"
    ;;
  web)
    cmd_back
    echo "${c_dim}== 启动前端 vite dev（开发热更，/api 代理 → ${BFF_URL}）==${c_reset}"
    echo "   开发地址：${WEB_URL}    停止前端：Ctrl+C（后端组件仍在运行）"
    echo "   整体停止：./start.sh down"
    command -v npm >/dev/null 2>&1 || { warn "未找到 npm，无法启动前端 dev"; exit 1; }
    cd web
    exec npm run dev
    ;;
  back)
    cmd_back
    ;;
  down|status|env|down-all)
    bash scripts/demo-up.sh "${1}"
    ;;
  *)
    warn "未知参数 ${1}（支持：无参 / web / back / down / status / env / down-all）"
    exit 1
    ;;
esac
