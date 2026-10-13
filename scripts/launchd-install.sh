#!/usr/bin/env bash
# 用户级 launchd：stop 保留注册；restart 恢复托管；只有 uninstall 删除注册。
# install 显式执行预检并安装；只承诺用户登录后的恢复。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
case "${1:-status}" in
  install|uninstall|status|stop|restart|resume)
    exec bash "$ROOT/scripts/demo-up.sh" "${@:-status}"
    ;;
  *) echo "用法：launchd-install.sh {install|uninstall|status|stop|restart|resume}" >&2; exit 1 ;;
esac
