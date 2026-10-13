#!/usr/bin/env bash
# 唯一启动入口：环境变量 > .env > production；无隐式动作（不灌日志/不起流程/不开浏览器）。
# 常驻由 launchd 托管（watchdog 守护 6 组件 + Caddy HTTPS 入口，登录自启）。
# 生产控制台入口 https://<本机IP>:8643（本机 https://127.0.0.1:8643）。
# ./start.sh [back|up|web|restart|down|stop|status|env|preflight|maintenance|resume|demo|install|uninstall|down-all]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
case "${1:-back}" in
  back|up) exec bash "$ROOT/scripts/demo-up.sh" up ;;
  web)
    bash "$ROOT/scripts/demo-up.sh" up
    cd "$ROOT/web"
    exec npm run dev
    ;;
  restart|down|stop|status|env|preflight|maintenance|resume|demo|install|uninstall|down-all)
    exec bash "$ROOT/scripts/demo-up.sh" "$@"
    ;;
  *) echo "未知命令；支持 back/up/web/restart/down/stop/status/env/preflight/maintenance/resume/demo/install/uninstall/down-all" >&2; exit 1 ;;
esac
