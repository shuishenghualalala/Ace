#!/usr/bin/env bash
# =============================================================================
# Crew 一键安装脚本（macOS / Linux）
# -----------------------------------------------------------------------------
# 在一检出的仓库根目录运行：
#   bash scripts/install.sh                # 安装后端 + 本地配置模板
#   bash scripts/install.sh --dev          # 追加开发依赖（pytest/ruff）
#   bash scripts/install.sh --with-web     # 追加 Web 前端依赖与构建
#   bash scripts/install.sh --with-desktop # 追加桌面端依赖与构建
#   bash scripts/install.sh --all          # 以上全部
#
# 也可以直接 curl 执行（会先把仓库克隆到当前目录的 Ace/ 下）：
#   curl -fsSL <raw-url>/scripts/install.sh | bash
#
# 做的事：
#   1. 确保 uv 可用（缺失时按官方方式安装到 ~/.local/bin）
#   2. uv 创建 .venv（Python 3.11）并安装 crew 后端
#   3. 缺失时从 *.example 复制 config/config.yaml 与 config/.env
#   4. 可选：Web / Desktop 的 npm install 与构建（需要 Node.js >= 22.12）
# 不做的事：不写入系统目录、不修改 shell 配置文件、不配置任何模型密钥。
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/shuishenghualalala/Ace.git"

WITH_DEV=0
WITH_WIKI=1
WITH_WEB=0
WITH_DESKTOP=0

for arg in "$@"; do
    case "$arg" in
        --dev)          WITH_DEV=1 ;;
        --no-wiki)      WITH_WIKI=0 ;;
        --with-web)     WITH_WEB=1 ;;
        --with-desktop) WITH_DESKTOP=1 ;;
        --all)          WITH_DEV=1; WITH_WEB=1; WITH_DESKTOP=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]:-$0}"
            exit 0
            ;;
        *)
            echo "❌ 未知参数: $arg（用 --help 查看用法）" >&2
            exit 1
            ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

# ----- 定位仓库根目录；脚本被 curl 管道执行时先克隆仓库 -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null || pwd)"
if [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$PWD/pyproject.toml" ] && [ -d "$PWD/crew" ]; then
    ROOT_DIR="$PWD"
else
    command -v git >/dev/null 2>&1 || die "需要 git 来克隆仓库，请先安装 git"
    info "未在仓库内运行，克隆 $REPO_URL 到 ./Ace"
    git clone --depth 1 "$REPO_URL" Ace
    ROOT_DIR="$PWD/Ace"
fi
cd "$ROOT_DIR"
info "仓库目录: $ROOT_DIR"

# ----- 1. uv -----
if ! command -v uv >/dev/null 2>&1; then
    info "未找到 uv，按官方方式安装（~/.local/bin）"
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv 安装失败，请手动安装: https://docs.astral.sh/uv/"
fi
info "uv: $(uv --version)"

# ----- 2. Python 虚拟环境与后端依赖 -----
EXTRAS=""
[ "$WITH_WIKI" = "1" ] && EXTRAS="wiki"
if [ "$WITH_DEV" = "1" ]; then
    EXTRAS="${EXTRAS:+$EXTRAS,}dev"
fi

info "创建 .venv（Python 3.11）"
uv venv .venv --python 3.11

if [ -n "$EXTRAS" ]; then
    info "安装后端依赖（extras: $EXTRAS）"
    uv pip install -e ".[$EXTRAS]"
else
    info "安装后端依赖"
    uv pip install -e .
fi

# ----- 3. 本地配置模板（已存在则不覆盖） -----
[ -f config/config.yaml ] || cp config/config.yaml.example config/config.yaml
[ -f config/.env ] || cp config/.env.example config/.env
info "配置文件就绪: config/config.yaml, config/.env"

# ----- 4. 可选前端 -----
need_node() {
    if ! command -v node >/dev/null 2>&1; then
        warn "未找到 Node.js（需要 >= 22.12），跳过 $1；安装 Node 后可重跑本脚本"
        return 1
    fi
    NODE_MAJOR="$(node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22 ? 1 : 0)' 2>/dev/null && echo ok || echo old)"
    if [ "$NODE_MAJOR" != "ok" ]; then
        warn "Node.js 版本过低（需要 >= 22.12，当前 $(node --version)），跳过 $1"
        return 1
    fi
    return 0
}

if [ "$WITH_WEB" = "1" ] && need_node "Web 前端"; then
    info "安装并构建 Web 前端"
    (cd web && npm ci && npm run build)
fi

if [ "$WITH_DESKTOP" = "1" ] && need_node "桌面端"; then
    info "安装并构建桌面端"
    (cd desktop && npm ci && npm run build)
fi

# ----- 完成提示 -----
cat <<EOF

✅ Crew 安装完成。下一步：

  激活虚拟环境:
    source .venv/bin/activate

  启动方式（三选一）:
    桌面端:   cd desktop && npm start       $( [ "$WITH_DESKTOP" = "1" ] || echo "（需先 --with-desktop）" )
    Web 端:   python -m crew.gateway.server + cd web && npm run dev
    CLI:      python -m crew.cli

  配置模型:
    桌面端「设置 → 模型 → 添加模型」，或编辑 config/config.yaml + config/.env
    详见 README.md「配置模型」一节。
EOF
