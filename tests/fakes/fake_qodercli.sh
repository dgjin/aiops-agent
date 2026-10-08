#!/usr/bin/env sh
# 测试用 Qoder CLI 桩：模拟 `qoder --version` 与 `qoder -p <prompt> ... -w <workspace>` 的行为。
# 目的：单测不依赖真实 Qoder CLI（未安装也能全绿）。
#
# 行为由环境变量驱动：
#   FAKE_QODER_MODE   = good(默认) | noop | fail | timeout | broken | warn_model | newfile
#   FAKE_QODER_TARGET = 目标文件相对路径（默认 order_service.py）
#
#   good       正常路径：对目标文件做语义修复（coupon 空值防护），产生可应用且可编译的改动
#   noop       不产生任何改动（空 diff）
#   fail       非零退出（模拟鉴权失败/调用失败）
#   timeout    长时间阻塞（配合 AIOPS_QODER_TIMEOUT 触发超时）
#   broken     写入语法错误内容（模拟非法改动，应由编译校验拦截）
#   warn_model 模拟「无效模型名 → 静默回退 auto」：仅 stderr 警告，仍成功产出改动
#   newfile    新增一个此前不存在的文件（需求实现常见形态，验证 diff 采集含未跟踪文件）
set -u

# qoder_available 依赖 --version 探测
if [ "${1:-}" = "--version" ]; then
  echo "fake-qodercli 9.9.9"
  exit 0
fi

mode="${FAKE_QODER_MODE:-good}"
target_rel="${FAKE_QODER_TARGET:-order_service.py}"

# 解析 -w / --cwd 指定的工作目录
workdir=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-w" ] || [ "$prev" = "--cwd" ]; then
    workdir="$arg"
  fi
  prev="$arg"
done
[ -n "$workdir" ] || workdir="."

file="$workdir/$target_rel"

do_edit() {
  if [ ! -f "$file" ]; then
    echo "fake qoder: 目标文件不存在: $file" >&2
    exit 2
  fi
  python3 - "$file" <<'PY'
import pathlib
import sys

p = pathlib.Path(sys.argv[1])
text = p.read_text(encoding="utf-8")
needle = 'discount = coupon["discount"]'
if needle in text:
    text = text.replace(needle, 'discount = coupon["discount"] if coupon else 0')
else:
    text = text.rstrip("\n") + "\n# qoder-fix\n"
p.write_text(text, encoding="utf-8")
PY
}

case "$mode" in
  fail)
    echo "fake qoder: 模拟调用失败（非零退出）" >&2
    exit 1
    ;;
  timeout)
    exec sleep 3600
    ;;
  noop)
    echo '{"result":"未产生改动"}'
    exit 0
    ;;
  broken)
    printf 'def broken(:\n' > "$file"
    echo '{"result":"已编辑（语法错误）"}'
    exit 0
    ;;
  newfile)
    mkdir -p "$(dirname "$file")"
    printf 'def guard(coupon):\n    return coupon or 0\n' > "$file"
    echo '{"result":"已新增文件"}'
    exit 0
    ;;
  warn_model)
    echo 'Model "deepseek-flash" is not available right now; using "auto" instead. Use /model to choose another model.' >&2
    do_edit
    echo '{"result":"已修复（模型已静默回退）"}'
    exit 0
    ;;
  *)
    do_edit
    echo '{"result":"已修复"}'
    exit 0
    ;;
esac
