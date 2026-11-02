#!/usr/bin/env bash
# 预构建 Node 仓库的 Linux 离线依赖树（沙箱 --network none 下可构建/测试）。
#
# 背景：沙箱工作区排除 node_modules（宿主 macOS 依赖树含 darwin 原生模块，
# 在 Linux 容器内 rollup/esbuild 会因缺少 linux 二进制直接失败）；容器又禁止联网，
# 无法在沙箱内 npm ci。这里用与运行期完全相同的固定镜像联网安装一次，
# 产物落在 data/sandbox/node-deps/<仓库目录名>/，由 run_patch_tests 复制进工作区。
#
# 用法：scripts/prepare_node_sandbox_deps.sh <被监控仓库目录>
# 依赖变更（package-lock.json 变化）后需重新执行；沙箱会校验摘要，过期即失败。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

usage() {
  cat <<'EOF'
用法：scripts/prepare_node_sandbox_deps.sh <被监控仓库目录>

用 AIOPS_SANDBOX_NODE_IMAGE（需 linux/宿主 CPU 架构）执行 npm ci，
生成 <AIOPS_SANDBOX_NODE_DEPS>/<仓库目录名>/node_modules 离线依赖树。
EOF
}

if [ "${1:-}" = "" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 2
fi

REPO="$(cd "$1" && pwd -P)"
[ -f "$REPO/package.json" ] || { printf '错误：%s 缺少 package.json\n' "$REPO" >&2; exit 1; }
[ -f "$REPO/package-lock.json" ] || { printf '错误：%s 缺少 package-lock.json（npm ci 必需）\n' "$REPO" >&2; exit 1; }

# 与 start.sh 同规则：环境变量 > .env > 默认值（不 source，避免把配置当代码执行）
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

IMAGE="$(env_get AIOPS_SANDBOX_NODE_IMAGE node:22-bookworm-slim)"
DEPS_ROOT="$(env_get AIOPS_SANDBOX_NODE_DEPS "$ROOT/data/sandbox/node-deps")"
TARGET="$DEPS_ROOT/$(basename "$REPO")"

command -v docker >/dev/null 2>&1 || { printf '错误：缺少 docker\n' >&2; exit 1; }

printf '[node-deps] 仓库：%s\n' "$REPO"
printf '[node-deps] 镜像：%s\n' "$IMAGE"
printf '[node-deps] 目标：%s\n' "$TARGET"

mkdir -p "$TARGET"
cp "$REPO/package.json" "$REPO/package-lock.json" "$TARGET"/
rm -rf "$TARGET/node_modules"

# --ignore-scripts：不执行依赖的安装期脚本（prepare/husky 等），降低供应链风险；
# rollup/esbuild 的平台二进制走 optionalDependencies，无需安装脚本。
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$TARGET":/deps \
  -w /deps \
  "$IMAGE" \
  npm ci --ignore-scripts --no-audit --no-fund

if command -v shasum >/dev/null 2>&1; then
  LOCK_SHA="$(shasum -a 256 "$REPO/package-lock.json" | awk '{print $1}')"
else
  LOCK_SHA="$(sha256sum "$REPO/package-lock.json" | awk '{print $1}')"
fi
printf '%s\n' "$LOCK_SHA" > "$TARGET/package-lock.sha256"

printf '[node-deps] 完成：%s（包 %s 个，%s）\n' \
  "$TARGET/node_modules" \
  "$(find "$TARGET/node_modules" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')" \
  "$(du -sh "$TARGET/node_modules" | awk '{print $1}')"
printf '[node-deps] 锁文件摘要：%s\n' "$LOCK_SHA"
