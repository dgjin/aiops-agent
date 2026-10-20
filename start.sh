#!/usr/bin/env bash
# 唯一启动入口：环境变量 > .env > production；无隐式动作（不灌日志/不起流程/不开浏览器）。
# 常驻由 launchd 托管（watchdog 守护常驻组件 + Caddy HTTPS 入口，登录自启）。
# 生产控制台入口 https://<本机IP>:8643（本机 https://127.0.0.1:8643）。
#
# 启动前会校验仓库 .venv、控制台构建是否落后于源码（缺失/落后时自动 npm ci + build），
# 并打印当前档位横幅（production 红色警告）与入口地址。跳过构建：AIOPS_SKIP_WEB_BUILD=1。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
WEB="$ROOT/web"

info() { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：./start.sh [命令]

  up | back        一键启动：档位横幅 → 控制台构建检查 → 依赖/端口预检 → 常驻组件 + 守护
  web              先启动后端，再前台运行 vite dev（http://127.0.0.1:5273；Ctrl+C 只停前端）
  build            仅重建控制台静态产物（web/dist，tsc --noEmit + vite build）
  doctor           汇总自检：预检 + demo_cli doctor + 组件状态 + 控制台构建新鲜度
  logs [名称]      跟踪日志，默认 watchdog；可选 worker/bff/alert_webhook/app_prober/
                   ship_app_logs/log_surge_detector/feishu_gateway/all
  status           查看组件与守护状态（含档位快照）
  restart          受控重启（先校验依赖与构建；失败不停止现有业务）
  down | stop      停止全部（先停守护再停组件；保留 launchd 注册）
  env              打印有效启动配置（脱敏快照）
  preflight        仅做依赖与端口预检，不启动任何组件
  maintenance      进入人工维护（守护不再拉起组件）
  resume           退出维护并按当前配置恢复
  demo             演示档播种（灌日志并发起流程；production 档拒绝）
  install          将守护交 launchd 托管（登录自启；受控重装同命令）
  uninstall        卸载 launchd 托管并停止组件
  down-all         不自动停止共享依赖（显式拒绝）
  sync-ip          按当前网络地址同步生产部署（Caddyfile/settings/监控片段 + 重签 TLS、reload Caddy；支持 --dry-run/--ip）

环境变量：AIOPS_SKIP_WEB_BUILD=1 跳过启动前的控制台构建检查。
EOF
}

# 读取配置：环境变量优先，其次 .env，最后默认值（不 source，避免把配置当代码执行）。
env_get() {
  local key="$1" default="${2:-}" value
  value="$(printenv "$key" || true)"
  if [ -z "$value" ] && [ -f "$ROOT/.env" ]; then
    value="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true)"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
  fi
  printf '%s' "${value:-$default}"
}

effective_mode() {
  env_get AIOPS_MODE production | tr '[:upper:]' '[:lower:]'
}

# 档位横幅：production 红色警告，demo 黄色提示；非法档位交由 watchdog 拒绝。
print_banner() {
  local mode="$1" red="" yellow="" reset=""
  if [ -t 1 ]; then
    red=$'\033[1;31m'; yellow=$'\033[1;33m'; reset=$'\033[0m'
  fi
  case "$mode" in
    production) printf '%s[档位] production%s 生产档：修复 / 发布按策略全量生效\n' "$red" "$reset" ;;
    demo)       printf '%s[档位] demo%s 演示档：仅显式 demo 播种；不会自动灌日志\n' "$yellow" "$reset" ;;
    *)          printf '%s[档位] %s（非法档位，watchdog 将拒绝启动）%s\n' "$red" "$mode" "$reset" >&2 ;;
  esac
}

require_venv() {
  if [ -x "$ROOT/.venv/bin/python" ]; then
    return 0
  fi
  cat >&2 <<'EOF'
错误：缺少仓库虚拟环境 .venv/bin/python（production 档拒绝回退系统 Python）。
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
EOF
  exit 1
}

# 控制台产物是否缺失或落后于源码 / 构建配置。
web_build_needed() {
  local dist="$WEB/dist/index.html" newer
  if [ ! -f "$dist" ]; then
    return 0
  fi
  newer="$(find "$WEB/src" "$WEB/public" "$WEB/index.html" "$WEB/package.json" \
    "$WEB/package-lock.json" "$WEB/vite.config.ts" "$WEB/tsconfig.json" \
    -newer "$dist" -print -quit 2>/dev/null || true)"
  if [ -n "$newer" ]; then
    return 0
  fi
  return 1
}

ensure_web_deps() {
  if [ -d "$WEB/node_modules" ]; then
    return 0
  fi
  if ! command -v npm >/dev/null 2>&1; then
    die "缺少 npm（控制台构建需要 Node.js 18+）；或先复制已有 web/dist"
  fi
  info "[前端] 首次构建，安装依赖…"
  if [ -f "$WEB/package-lock.json" ]; then
    ( cd "$WEB" && npm ci )
  else
    ( cd "$WEB" && npm install )
  fi
}

ensure_web_build() {
  if [ "${AIOPS_SKIP_WEB_BUILD:-0}" = "1" ]; then
    info "[前端] 已跳过构建检查（AIOPS_SKIP_WEB_BUILD=1）"
    return 0
  fi
  if ! web_build_needed; then
    return 0
  fi
  ensure_web_deps
  info "[前端] 控制台产物缺失或落后于源码，开始构建…"
  if ! ( cd "$WEB" && npm run build ); then
    die "控制台构建失败；修复后重试（或 AIOPS_SKIP_WEB_BUILD=1 跳过，但会继续使用旧产物）"
  fi
  info "[前端] 构建完成：web/dist"
}

build_console() {
  ensure_web_deps
  info "[前端] 构建控制台（tsc --noEmit + vite build）…"
  if ! ( cd "$WEB" && npm run build ); then
    die "控制台构建失败"
  fi
  info "[前端] 完成：web/dist"
}

print_endpoints() {
  local bff_port webhook_port
  bff_port="$(env_get AIOPS_BFF_PORT 8600)"
  webhook_port="$(env_get AIOPS_WEBHOOK_PORT 8099)"
  info ""
  info "控制台：https://127.0.0.1:8643（局域网 https://<本机IP>:8643，需 com.aiops.caddy 在运行）"
  info "BFF 直连：http://127.0.0.1:${bff_port}"
  info "告警接入：http://127.0.0.1:${webhook_port}/webhook"
  if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    if ! launchctl print "gui/$(id -u)/com.aiops.caddy" >/dev/null 2>&1; then
      info "提示：com.aiops.caddy 未注册，HTTPS 入口暂不可用（见部署手册「生产入口」）"
    fi
  fi
}

command_logs() {
  local name="${1:-watchdog}" log
  case "$name" in
    all)
      exec tail -n 80 -F "$ROOT/worker.log" "$ROOT/bff.log" "$ROOT/alert_webhook.log" \
        "$ROOT/app_prober.log" "$ROOT/ship_app_logs.log" "$ROOT/log_surge_detector.log" \
        "$ROOT/feishu_gateway.log" "$ROOT/aiops_watchdog.log"
      ;;
    watchdog) log="$ROOT/aiops_watchdog.log" ;;
    worker|bff|alert_webhook|app_prober|ship_app_logs|log_surge_detector|feishu_gateway)
      log="$ROOT/${name}.log"
      ;;
    *)
      die "未知日志：${name}（可选 worker/bff/alert_webhook/app_prober/ship_app_logs/log_surge_detector/feishu_gateway/watchdog/all）"
      ;;
  esac
  exec tail -n 100 -F "$log"
}

command_doctor() {
  require_venv
  print_banner "$(effective_mode)"
  info "[1/4] 依赖与端口预检"
  if ! bash "$ROOT/scripts/demo-up.sh" preflight; then
    warn "依赖/端口预检未通过（见上）；继续执行环境自检以定位原因"
  fi
  info "[2/4] 环境自检（demo_cli doctor）"
  if ! ( cd "$ROOT" && "$ROOT/.venv/bin/python" demo_cli.py doctor ); then
    warn "环境自检存在未通过项（见上），按提示修复后重试"
  fi
  info "[3/4] 组件状态"
  bash "$ROOT/scripts/demo-up.sh" status || true
  info "[4/4] 控制台构建"
  if web_build_needed; then
    info "web/dist 缺失或落后于源码 → 执行 ./start.sh build"
  else
    info "web/dist 为最新构建"
  fi
  print_endpoints
}

CMD="${1:-up}"

case "$CMD" in
  help|-h|--help|usage)
    usage
    ;;
  back|up)
    print_banner "$(effective_mode)"
    require_venv
    if [ ! -f "$ROOT/.env" ]; then
      warn "缺少 .env，将按默认配置启动；可 cp .env.example .env 后按需修改"
    fi
    ensure_web_build
    bash "$ROOT/scripts/demo-up.sh" up
    print_endpoints
    ;;
  restart)
    print_banner "$(effective_mode)"
    require_venv
    ensure_web_build
    bash "$ROOT/scripts/demo-up.sh" restart
    print_endpoints
    ;;
  install)
    require_venv
    ensure_web_build
    bash "$ROOT/scripts/demo-up.sh" install
    print_endpoints
    ;;
  demo)
    print_banner "$(effective_mode)"
    require_venv
    ensure_web_build
    bash "$ROOT/scripts/demo-up.sh" demo
    print_endpoints
    ;;
  web)
    print_banner "$(effective_mode)"
    require_venv
    ensure_web_deps
    bash "$ROOT/scripts/demo-up.sh" up
    info ""
    info "前端 dev：http://127.0.0.1:5273（热更；Ctrl+C 只停前端，后端保持运行）"
    cd "$WEB"
    exec npm run dev
    ;;
  build|build-web)
    build_console
    ;;
  doctor)
    command_doctor
    ;;
  logs)
    shift || true
    command_logs "${1:-watchdog}"
    ;;
  sync-ip)
    shift || true
    exec "$ROOT/.venv/bin/python" "$ROOT/scripts/sync_lan_ip.py" "$@"
    ;;
  down|stop|status|env|preflight|maintenance|resume|uninstall|down-all)
    exec bash "$ROOT/scripts/demo-up.sh" "$CMD"
    ;;
  *)
    printf '未知命令：%s\n\n' "$CMD" >&2
    usage >&2
    exit 1
    ;;
esac
