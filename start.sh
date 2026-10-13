#!/usr/bin/env bash
# 唯一启动入口：环境变量 > .env > production；无隐式演示流量。
# ./start.sh [back|up|restart|down|status|env|preflight|maintenance|resume|demo]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
case "${1:-back}" in
  back|up) exec bash "$ROOT/scripts/demo-up.sh" up ;;
  web)
    bash "$ROOT/scripts/demo-up.sh" up
    cd "$ROOT/web"
    exec npm run dev
    ;;
  restart|down|stop|status|env|preflight|maintenance|resume|demo|down-all)
    exec bash "$ROOT/scripts/demo-up.sh" "$@"
    ;;
  *) echo "未知命令；支持 back/up/restart/down/status/env/preflight/maintenance/resume/demo/web" >&2; exit 1 ;;
esac
