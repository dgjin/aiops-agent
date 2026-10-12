#!/usr/bin/env bash
# 沙箱产物清理（P0）：删除 data/sandbox/ 与 data/qoder/ 中 N 天前（默认 7）的流程产物。
#
# 背景：每次修复流程在 data/sandbox/ 留下工作区副本（代码+依赖），Qoder 修复在
# data/qoder/ 留下快照——无 TTL 时持续线性增长（2026-10 实测两目录合计 132GB）。
#
# 用法：
#   bash scripts/cleanup-sandbox.sh                # 清理 7 天前产物（先列清单再删）
#   bash scripts/cleanup-sandbox.sh --days 3       # 自定义保留天数
#   bash scripts/cleanup-sandbox.sh --dry-run      # 只列出将删除的条目（不删）
#   bash scripts/cleanup-sandbox.sh --status       # 只显示占用（不删）
set -euo pipefail

cd "$(dirname "$0")/.."
TARGETS="data/sandbox data/qoder"

DAYS=7
DRY_RUN=0
STATUS_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --days)
      DAYS="${2:-}"
      case "${DAYS}" in ''|*[!0-9]*) echo "✘ --days 需要正整数"; exit 1 ;; esac
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    --status)  STATUS_ONLY=1; shift ;;
    *) echo "✘ 未知参数 $1（支持 --days N / --dry-run / --status）"; exit 1 ;;
  esac
done

echo "== 沙箱产物当前占用 =="
for dir in ${TARGETS}; do
  [ -d "${dir}" ] && du -sh "${dir}" 2>/dev/null || true
done

if [ "${STATUS_ONLY}" -eq 1 ]; then
  exit 0
fi

total_expired=0
for dir in ${TARGETS}; do
  [ -d "${dir}" ] || continue
  found=$(find "${dir}" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
  expired=$(find "${dir}" -mindepth 1 -maxdepth 1 -mtime +"${DAYS}" | wc -l | tr -d ' ')
  total_expired=$((total_expired + expired))
  echo "== ${dir}：共 ${found} 项，其中 ${expired} 项超过 ${DAYS} 天 =="
  if [ "${expired}" -gt 0 ] && [ "${DRY_RUN}" -eq 1 ]; then
    find "${dir}" -mindepth 1 -maxdepth 1 -mtime +"${DAYS}" | sed 's/^/  [将删除] /'
  fi
done

if [ "${total_expired}" -eq 0 ]; then
  echo "无需清理（保留 ${DAYS} 天内的产物）。"
  exit 0
fi
if [ "${DRY_RUN}" -eq 1 ]; then
  echo "[dry-run] 共 ${total_expired} 项将被删除，未执行（去掉 --dry-run 实施）。"
  exit 0
fi

deleted=0
for dir in ${TARGETS}; do
  [ -d "${dir}" ] || continue
  while IFS= read -r -d '' item; do
    rm -rf -- "${item}"
    deleted=$((deleted + 1))
  done < <(find "${dir}" -mindepth 1 -maxdepth 1 -mtime +"${DAYS}" -print0)
done

echo "✔ 已删除 ${deleted} 项（保留最近 ${DAYS} 天）"
echo "== 清理后占用 =="
for dir in ${TARGETS}; do
  [ -d "${dir}" ] && du -sh "${dir}" 2>/dev/null || true
done
