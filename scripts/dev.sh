#!/usr/bin/env bash
# =============================================================================
# Crew 一键开发启动脚本（macOS / Linux）
# -----------------------------------------------------------------------------
# 在已检出的仓库根目录运行：
#   bash scripts/dev.sh
#
# 做的事：
#   1. 激活 .venv（不存在时先用 uv 创建）
#   2. 缺失时从 *.example 复制本地配置文件
#   3. 缺失时安装桌面端 npm 依赖
#   4. 启动桌面开发模式：cd desktop && npm run dev
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null || pwd)"
if [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$PWD/pyproject.toml" ] && [ -d "$PWD/crew" ]; then
    ROOT_DIR="$PWD"
else
    echo "未找到仓库根目录，请在 Ace 仓库内运行本脚本" >&2
    exit 1
fi
cd "$ROOT_DIR"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

info "仓库目录: $ROOT_DIR"

# ----- 0. 依赖工具 -----
if ! command -v uv >/dev/null 2>&1; then
    die "未找到 uv，请先安装 uv（https://docs.astral.sh/uv/）或运行 scripts/install.sh"
fi

# ----- 1. 虚拟环境与后端依赖 -----
if [ ! -d ".venv" ]; then
    info "创建 .venv（Python 3.11）"
    uv venv .venv --python 3.11
fi

if [ -f ".venv/bin/activate" ]; then
    VENV_ACTIVATE=".venv/bin/activate"
    VENV_PYTHON=".venv/bin/python"
elif [ -f ".venv/Scripts/activate" ]; then
    VENV_ACTIVATE=".venv/Scripts/activate"
    VENV_PYTHON=".venv/Scripts/python.exe"
else
    die "未找到虚拟环境激活脚本，请检查 .venv"
fi

if ! "$VENV_PYTHON" -c "import crew" >/dev/null 2>&1; then
    info "安装后端依赖（extras: dev, wiki）"
    uv pip install -e ".[dev,wiki]"
fi

# ----- 2. 本地配置模板 -----
[ -f config/config.yaml ] || cp config/config.yaml.example config/config.yaml
[ -f config/.env ] || cp config/.env.example config/.env
info "配置文件就绪: config/config.yaml, config/.env"

# ----- 3. 检查 Node.js -----
if ! command -v node >/dev/null 2>&1; then
    die "未找到 Node.js（需要 >= 22.12），请先安装 Node.js"
fi
NODE_VERSION="$(node --version)"
if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22 ? 1 : 0)' 2>/dev/null; then
    die "Node.js 版本过低（需要 >= 22.12，当前 $NODE_VERSION）"
fi

# ----- 4. 桌面端依赖 -----
if [ ! -d "desktop/node_modules" ]; then
    warn "未找到 desktop/node_modules，执行 npm install..."
    (cd desktop && npm install)
fi

# ----- 5. 启动开发模式 -----
info "启动 Crew 桌面开发模式"
source "$VENV_ACTIVATE"
cd desktop
npm run dev
