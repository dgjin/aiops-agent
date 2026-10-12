#!/usr/bin/env bash
# 日志轮转（P0，copytruncate 模式）：工程根 *.log 超阈值即轮转（gzip），保留 N 份。
#
# 说明：常驻组件日志由 shell 重定向（nohup >> *.log）写入、进程持有文件 fd，
# 采用 copytruncate（先复制再截断原文件）使进程继续写同一 fd，无需重启组件；
# 代价是复制与截断之间存在毫秒级丢行窗口（仅影响正在写入的几行，可接受）。
#
# 用法：
#   bash scripts/rotate-logs.sh                  # 轮转 >1MB 的日志，保留 7 份
#   bash scripts/rotate-logs.sh --min-size 512   # 阈值 KB（默认 1024）
#   bash scripts/rotate-logs.sh --keep 14        # 保留份数（默认 7）
#   bash scripts/rotate-logs.sh --dry-run        # 只列出将轮转的文件
set -euo pipefail

cd "$(dirname "$0")/.."

MIN_KB=1024
KEEP=7
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --min-size)
      MIN_KB="${2:-}"
      case "${MIN_KB}" in ''|*[!0-9]*) echo "✘ --min-size 需要正整数（KB）"; exit 1 ;; esac
      shift 2
      ;;
    --keep)
      KEEP="${2:-}"
      case "${KEEP}" in ''|*[!0-9]*) echo "✘ --keep 需要正整数"; exit 1 ;; esac
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "✘ 未知参数 $1（支持 --min-size KB / --keep N / --dry-run）"; exit 1 ;;
  esac
done

rotated=0
for log in *.log; do
  [ -f "${log}" ] || continue
  size_bytes=$(stat -f%z "${log}")
  [ "${size_bytes}" -ge $((MIN_KB * 1024)) ] || continue
  size_human=$(du -h "${log}" | cut -f1)
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "  [将轮转] ${log}（${size_human}）"
    rotated=$((rotated + 1))
    continue
  fi
  # 顺延：删除最老一份，.N-1 → .N（从大到小搬移，避免覆盖）
  rm -f "${log}.${KEEP}.gz"
  i=$((KEEP - 1))
  while [ "${i}" -ge 1 ]; do
    [ -f "${log}.${i}.gz" ] && mv "${log}.${i}.gz" "${log}.$((i + 1)).gz"
    i=$((i - 1))
  done
  # copytruncate：复制 → 压缩为 .1.gz → 原地截断（进程 fd 不受影响）
  cp "${log}" "${log}.1"
  gzip -f "${log}.1"
  : > "${log}"
  echo "  ✔ ${log}（${size_human}）→ ${log}.1.gz"
  rotated=$((rotated + 1))
done

if [ "${rotated}" -eq 0 ]; then
  echo "无需轮转（阈值 ${MIN_KB}KB / 保留 ${KEEP} 份）。"
else
  prefix=""
  [ "${DRY_RUN}" -eq 1 ] && prefix="[dry-run] "
  echo "${prefix}共处理 ${rotated} 个日志（阈值 ${MIN_KB}KB / 保留 ${KEEP} 份）。"
fi
