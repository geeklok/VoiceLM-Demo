#!/usr/bin/env bash
# 一键部署: 本地构建前端 -> 同步代码 -> 同步最新 dist -> 重建并重启容器。
#
# 背景: 远端 /opt/asr 是 rsync 管理的仓库副本 (非 git)。前端镜像 Dockerfile 是
# `COPY dist /usr/share/nginx/html`, 用的是预构建产物。由于 dist/ 被 .gitignore
# 忽略, 早期 rsync 把它一并排除, 导致远端 dist 是旧构建, 重建 web 镜像打进去的
# 仍是旧 bundle (前端改动不生效)。本脚本固化「构建 + 单独同步最新 dist」这步,
# 避免再踩坑。
#
# 用法:
#   deploy/sync.sh                # 全流程: build dist + 同步 + 重建重启
#   deploy/sync.sh --no-build     # 跳过 npm build, 用已有 dist
#   deploy/sync.sh --no-restart   # 只同步, 不重建容器
#   deploy/sync.sh --web-only     # 只重建/重启 web (前端改动, 不动 backend)
#
# 目标参数全部经环境变量传入, 脚本内不写死任何线上信息 (IP / 私钥等):
#   DEPLOY_HOST   必填  远端主机 IP 或域名
#   DEPLOY_KEY    必填  SSH 私钥路径 (支持 ~)
#   DEPLOY_USER   可选  SSH 用户 (默认 root)
#   DEPLOY_PATH   可选  远端仓库根 (默认 /opt/asr)
#
# 为免每次手敲, 可把这些写进 deploy/sync.env (已 gitignore, 不进版本库):
#   DEPLOY_HOST=1.2.3.4
#   DEPLOY_KEY=~/.ssh/your_key.pem
# 脚本启动时若存在该文件会自动 source。也可用 DEPLOY_ENV_FILE 指定其他路径。
set -euo pipefail

# 自动加载本地配置文件 (不入库)。已在环境里显式传入的同名变量优先, 不被文件覆盖。
_ENV_FILE="${DEPLOY_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sync.env}"
if [ -f "$_ENV_FILE" ]; then
  _pre_host="${DEPLOY_HOST:-}"; _pre_key="${DEPLOY_KEY:-}"
  _pre_user="${DEPLOY_USER:-}"; _pre_path="${DEPLOY_PATH:-}"
  # shellcheck disable=SC1090
  set -a; . "$_ENV_FILE"; set +a
  [ -n "$_pre_host" ] && DEPLOY_HOST="$_pre_host"
  [ -n "$_pre_key" ] && DEPLOY_KEY="$_pre_key"
  [ -n "$_pre_user" ] && DEPLOY_USER="$_pre_user"
  [ -n "$_pre_path" ] && DEPLOY_PATH="$_pre_path"
fi

DEPLOY_HOST="${DEPLOY_HOST:-}"
DEPLOY_KEY="${DEPLOY_KEY:-}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/asr}"

_missing=0
if [ -z "$DEPLOY_HOST" ]; then
  echo "错误: 未设置远端主机。请用环境变量 DEPLOY_HOST 指定 (IP 或域名)。" >&2
  _missing=1
fi
if [ -z "$DEPLOY_KEY" ]; then
  echo "错误: 未设置 SSH 私钥。请用环境变量 DEPLOY_KEY 指定 (私钥路径)。" >&2
  _missing=1
fi
if [ "$_missing" -ne 0 ]; then
  echo "提示: 可一次性传入, 例如:" >&2
  echo "  DEPLOY_HOST=1.2.3.4 DEPLOY_KEY=~/path/to/key.pem deploy/sync.sh" >&2
  echo "或写入 ${_ENV_FILE} (已 gitignore) 后直接运行 deploy/sync.sh" >&2
  exit 2
fi

# 展开 ~ (环境变量里的 ~ 不会被 shell 自动展开)
DEPLOY_KEY="${DEPLOY_KEY/#\~/$HOME}"
if [ ! -f "$DEPLOY_KEY" ]; then
  echo "错误: SSH 私钥不存在: ${DEPLOY_KEY}" >&2
  exit 2
fi

DO_BUILD=1
DO_RESTART=1
WEB_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --no-restart) DO_RESTART=0 ;;
    --web-only) WEB_ONLY=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

# 仓库根 = 本脚本所在目录的上一级
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH="ssh -i ${DEPLOY_KEY} -o StrictHostKeyChecking=no"

echo "==> 目标: ${REMOTE}:${DEPLOY_PATH}"

# 1. 本地构建前端 (产出最新 dist/)
if [ "$DO_BUILD" -eq 1 ]; then
  echo "==> [1/4] 构建前端 (npm run build)"
  (cd "${ROOT}/frontend" && npm run build)
else
  echo "==> [1/4] 跳过前端构建 (--no-build), 使用已有 dist/"
fi

if [ ! -f "${ROOT}/frontend/dist/index.html" ]; then
  echo "错误: frontend/dist/index.html 不存在, 请先构建前端。" >&2
  exit 1
fi

# 2. 同步代码 (排除依赖/产物/密钥/挂载数据; dist 单独同步见第 3 步)
#    --delete 清理远端多余文件; 排除项受保护, 不会被误删。
echo "==> [2/4] 同步代码到远端 (排除 dist/models/voices/.venv 等)"
rsync -az --delete -e "${SSH}" \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  --exclude='*.egg-info/' \
  --exclude='.pytest_cache/' \
  --exclude='.venv/' \
  --exclude='.venv-*/' \
  --exclude='node_modules/' \
  --exclude='frontend/dist/' \
  --exclude='backend/models/' \
  --exclude='deploy/models/' \
  --exclude='deploy/voices/' \
  --exclude='deploy/certs/' \
  --exclude='deploy/.env.deploy' \
  --exclude='deploy/sync.env' \
  --exclude='**/test_audio.wav' \
  --exclude='**/*.pt' \
  --exclude='**/*.onnx' \
  "${ROOT}/" "${REMOTE}:${DEPLOY_PATH}/"

# 3. ★ 单独同步最新 dist (本脚本存在的核心原因) ★
#    web 镜像靠这份 dist 构建; 不同步 = 前端改动不生效。
echo "==> [3/4] 同步最新 frontend/dist -> 远端"
rsync -az --delete -e "${SSH}" \
  "${ROOT}/frontend/dist/" "${REMOTE}:${DEPLOY_PATH}/frontend/dist/"

# 4. 重建并重启容器
if [ "$DO_RESTART" -eq 1 ]; then
  if [ "$WEB_ONLY" -eq 1 ]; then
    echo "==> [4/4] 重建并重启 web 容器 (--web-only)"
    ${SSH} "${REMOTE}" "cd ${DEPLOY_PATH}/deploy && docker compose build web && docker compose up -d web"
  else
    echo "==> [4/4] 重建并重启全部容器"
    ${SSH} "${REMOTE}" "cd ${DEPLOY_PATH}/deploy && docker compose build && docker compose up -d"
  fi
else
  echo "==> [4/4] 跳过重建重启 (--no-restart)"
fi

echo "==> 完成。验证:"
echo "    ${SSH} ${REMOTE} 'curl -sk https://127.0.0.1/api/v1/models'"
