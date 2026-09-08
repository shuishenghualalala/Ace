#!/bin/bash
# =============================================================================
# Crew macOS DMG 打包脚本
# -----------------------------------------------------------------------------
# 产物：crew-desktop_${VERSION}_${ARCH}.dmg（ARCH = arm64 / x64）
# 组成：crew-desktop.app（Electron）
#       + Contents/Resources/crew-gateway（源码树：crew/ + plugins/ + config/ 模板）
#       + crew-gateway/runtimes/python（python-build-standalone，gateway 与技能脚本共用）
#       + crew-gateway/runtimes/node（Node.js portable，已裁剪 corepack/docs，保留 npm）
#       + ace-security-runtime（macOS Seatbelt 安全运行组件）
# gateway 不再经 PyInstaller 打包：desktop 主进程用内嵌 Python 以
# `python3 -m crew.gateway.server` 直接跑源码（cwd=crew-gateway，PYTHONPATH=crew-gateway，
# CREW_PACKAGED=1 标记打包态）。
#
# 依赖清单唯一数据源：pyproject.toml（核心 + wiki/pdf/docx/xlsx extras）
# + deb-package/runtime-requirements.txt（技能脚本补充依赖）。
#
# 运行环境：macOS 主机，目标架构必须与主机架构一致
# （python-build-standalone / Electron 均不做 Mac 交叉构建）
# 用法：
#   ./deb-package/pack_mac.sh                    # 版本取 version.txt，架构取本机
#   ./deb-package/pack_mac.sh 0.29.0             # 显式指定版本号
#   ./deb-package/pack_mac.sh 0.29.0 arm64       # 显式指定架构（arm64/x64）
# =============================================================================
set -euo pipefail

# ----- 参数与路径 -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-$(tr -d '[:space:]' < "$SCRIPT_DIR/version.txt")}"
ARCH="${2:-$(uname -m)}"
case "$ARCH" in
    arm64 | aarch64)
        ARCH="arm64"
        ELECTRON_ARCH="--arm64"
        PYTHON_ARCH="aarch64-apple-darwin"
        NODE_ARCH="darwin-arm64"
        ;;
    x64 | x86_64 | amd64)
        ARCH="x64"
        ELECTRON_ARCH="--x64"
        PYTHON_ARCH="x86_64-apple-darwin"
        NODE_ARCH="darwin-x64"
        ;;
    *)
        echo "❌ 不支持的架构: ${ARCH}（仅支持 arm64/x64）" >&2
        exit 1
        ;;
esac
DMG_NAME="crew-desktop_${VERSION}_${ARCH}.dmg"

# 内嵌运行时版本（三平台脚本保持一致）
BUNDLED_PYTHON_VERSION="3.11.9"
BUNDLED_PYTHON_RELEASE="20240415"   # indygreg/python-build-standalone 发布批次
BUNDLED_NODE_VERSION="20.18.3"

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64 | aarch64) HOST_ARCH="arm64" ;;
    x86_64 | amd64) HOST_ARCH="x64" ;;
esac
if [ "$(uname -s)" != "Darwin" ] || [ "$HOST_ARCH" != "$ARCH" ]; then
    echo "❌ 本脚本必须在 macOS ${ARCH} 主机上运行（当前主机为 ${HOST_ARCH}）" >&2
    exit 1
fi

echo "==========================================="
echo " Crew macOS 安装包构建"
echo " 版本: $VERSION"
echo " 架构: $ARCH"
echo " 产物: $DMG_NAME"
echo "==========================================="

# ----- 0) 构建依赖检查 -----
# pip 统一走清华镜像（pypi.org 直连在代理环境下极易 TLS EOF，与 Dockerfile.pack 同源）
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

command -v node    >/dev/null || { echo "❌ 未找到 node，请先安装 Node.js 22+" >&2; exit 1; }
command -v npm     >/dev/null || { echo "❌ 未找到 npm" >&2; exit 1; }
command -v cargo   >/dev/null || { echo "❌ 未找到 cargo，请先安装 Rust stable 工具链" >&2; exit 1; }
command -v hdiutil >/dev/null || { echo "❌ 未找到 hdiutil（应为 macOS 自带）" >&2; exit 1; }
command -v curl    >/dev/null || { echo "❌ 未找到 curl" >&2; exit 1; }
command -v shasum  >/dev/null || { echo "❌ 未找到 shasum（应为 macOS 自带）" >&2; exit 1; }

# 代理/网络抖动环境下 HTTP2 易断（curl 16/SSL EOF），统一走 HTTP1.1 + 全错误重试
CURL_OPTS=(-fsSL --http1.1 --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20)

# staging 与运行时缓存的家：dist/ 下只清理本脚本创建的目录，不做整体 rm
STAGE_DIR="$ROOT_DIR/dist/mac-staging"
RUNTIMES_CACHE="$ROOT_DIR/dist/runtimes-cache/mac-${ARCH}"
mkdir -p "$STAGE_DIR" "$RUNTIMES_CACHE"

# tar 管道复制目录树（macOS BSD tar 与 GNU tar 均支持 --exclude）
copy_tree() {
    local src="$1" dst="$2"
    shift 2
    mkdir -p "$dst"
    (cd "$src" && tar "$@" -cf - .) | (cd "$dst" && tar -xf -)
}

# ----- 1) Electron 桌面端构建（mac dir target，含原生安全运行组件） -----
echo ""
echo "→ [1/7] 构建 crew-desktop Electron 客户端..."
# Electron / electron-builder 二进制统一走 npmmirror（GitHub 直连在代理环境下易超时，与 Dockerfile.pack 同源）
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
(cd desktop && npm ci --no-audit --no-fund)

# 注入版本号与平台标识（须在 npm ci 之后，避免被 lockfile 校验覆盖）
node -e "const fs=require('fs');const p=JSON.parse(fs.readFileSync('desktop/package.json','utf8'));p.version='${VERSION}';p.platform='macOS ${ARCH}';fs.writeFileSync('desktop/package.json',JSON.stringify(p,null,2)+'\n');"

cargo build --manifest-path security-runtime/Cargo.toml --release --locked
node desktop/scripts/prepare-security-runtime.mjs \
    --runtime security-runtime/target/release/ace-security-runtime \
    --output desktop/security-runtime-bin
node desktop/scripts/verify-security-runtime.mjs desktop/security-runtime-bin

(cd desktop && npm run build && npx electron-builder --mac "$ELECTRON_ARCH" --config electron-builder.yml)

APP_PATH="$(find desktop/release -maxdepth 2 -name 'crew-desktop.app' -print -quit)"
if [ -z "$APP_PATH" ]; then
    echo "❌ 未找到 electron-builder 产物 crew-desktop.app" >&2
    exit 1
fi
echo "✓ Electron 客户端: $APP_PATH（含 macOS Seatbelt 安全运行组件）"

# ----- 2) 裁剪 Electron locales：只保留 en / zh_CN / zh_TW -----
echo ""
echo "→ [2/7] 裁剪 Electron locales..."
# 只删 *.lproj 目录，框架内其余文件（.pak / 库 / Info.plist 等）一律不动
LOCALES_DIR="$APP_PATH/Contents/Frameworks/Electron Framework.framework/Versions/A/Resources"
if [ -d "$LOCALES_DIR" ]; then
    removed=0
    for lproj in "$LOCALES_DIR"/*.lproj; do
        [ -d "$lproj" ] || continue
        case "$(basename "$lproj")" in
            en.lproj | zh_CN.lproj | zh_TW.lproj) ;;
            *) rm -rf "$lproj"; removed=$((removed + 1)) ;;
        esac
    done
    echo "✓ Electron locales 裁剪完成（删除 ${removed} 个语言包，保留 en / zh_CN / zh_TW）"
else
    echo "⚠️ 未找到 Electron locales 目录，跳过裁剪"
fi

# ----- 3) staging：组装 crew-gateway 源码树 -----
echo ""
echo "→ [3/7] 组装 crew-gateway 源码树到 staging..."
rm -rf "$STAGE_DIR"
STAGE_GW="$STAGE_DIR/crew-gateway"
mkdir -p "$STAGE_GW"

# gateway 源码全量，排除缓存与编译产物
copy_tree "$ROOT_DIR/crew" "$STAGE_GW/crew" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' --exclude='.pytest_cache'
copy_tree "$ROOT_DIR/plugins" "$STAGE_GW/plugins" \
    --exclude='__pycache__' --exclude='node_modules' --exclude='.git' --exclude='*.pyc' --exclude='*.pyo'

# 只携带可发布配置：两个 example 模板 + prompts/。
# 开发本机的 config/.env 与 config/config.yaml 绝不进入产物；
# 安装后由 gateway 首次运行时从 example 初始化用户私有文件（~/.Crew/）。
STAGE_CFG="$STAGE_GW/config"
mkdir -p "$STAGE_CFG"
cp "$ROOT_DIR/config/.env.example" "$STAGE_CFG/.env.example"
cp "$ROOT_DIR/config/config.yaml.example" "$STAGE_CFG/config.yaml.example"
copy_tree "$ROOT_DIR/config/prompts" "$STAGE_CFG/prompts"

# 安全检查：本地私有配置一旦混入 staging 立即中止
if [ -e "$STAGE_CFG/config.yaml" ] || \
   [ -n "$(find "$STAGE_CFG" -name '.env*' ! -name '.env.example' -print -quit)" ]; then
    echo "❌ 安全检查失败: staging config 目录中不应出现本地 .env 或 config.yaml" >&2
    exit 1
fi
for required in .env.example config.yaml.example; do
    if [ ! -f "$STAGE_CFG/$required" ]; then
        echo "❌ 构建失败: staging config 缺少 $required" >&2
        exit 1
    fi
done
echo "✓ gateway 源码树与配置模板已入 staging"

# ----- 4) 内嵌 Python（python-build-standalone，缓存于 dist/runtimes-cache） -----
echo ""
echo "→ [4/7] 准备内嵌 Python ${BUNDLED_PYTHON_VERSION}..."
PY_CACHE="$RUNTIMES_CACHE/python"
PY_CACHE_BIN="$PY_CACHE/bin/python3"

if [ ! -x "$PY_CACHE_BIN" ]; then
    echo "  下载 python-build-standalone ${BUNDLED_PYTHON_VERSION}+${BUNDLED_PYTHON_RELEASE} (${PYTHON_ARCH})..."
    PY_URL="https://github.com/indygreg/python-build-standalone/releases/download/${BUNDLED_PYTHON_RELEASE}/cpython-${BUNDLED_PYTHON_VERSION}+${BUNDLED_PYTHON_RELEASE}-${PYTHON_ARCH}-install_only.tar.gz"
    PY_TAR="$ROOT_DIR/dist/python-standalone.tar.gz"
    curl "${CURL_OPTS[@]}" "$PY_URL" -o "$PY_TAR"
    # install_only 归档内层为 python/ 目录，解出后整体移动到缓存目录
    rm -rf "$ROOT_DIR/dist/_python_extract"
    mkdir -p "$ROOT_DIR/dist/_python_extract"
    tar -xzf "$PY_TAR" -C "$ROOT_DIR/dist/_python_extract"
    rm -f "$PY_TAR"
    rm -rf "$PY_CACHE"
    mv "$ROOT_DIR/dist/_python_extract/python" "$PY_CACHE"
    rmdir "$ROOT_DIR/dist/_python_extract"
    if [ ! -x "$PY_CACHE_BIN" ]; then
        echo "❌ Python standalone 解压后未找到 bin/python3" >&2
        exit 1
    fi
    ln -sf python3 "$PY_CACHE/bin/python"
    echo "✓ Python standalone 已缓存"
else
    echo "✓ Python standalone 缓存已存在，跳过下载"
fi
"$PY_CACHE_BIN" --version

# 依赖同步：以 pyproject.toml + runtime-requirements.txt 的内容哈希为戳，
# 变更时才重新安装；不变时跳过（pip 全量 satisfied 检查也要分钟级，直接省掉）。
DEP_STAMP="$PY_CACHE/.deps-stamp"
DEP_HASH="$( { shasum -a 256 "$ROOT_DIR/pyproject.toml"; shasum -a 256 "$SCRIPT_DIR/runtime-requirements.txt"; } | shasum -a 256 | cut -d' ' -f1 )"

if [ ! -f "$DEP_STAMP" ] || [ "$(tr -d '[:space:]' < "$DEP_STAMP")" != "$DEP_HASH" ]; then
    echo "  为内嵌 Python 安装 gateway + 技能依赖..."
    # 依赖清单变更时清空 site-packages 重装：pip 只增不删，清单里移除的包
    # 若不清空会作为孤儿残留进安装包。
    rm -rf "$PY_CACHE/lib/python3.11/site-packages"
    # pip 自身也在 site-packages 里，清空后需先用 ensurepip 恢复
    "$PY_CACHE_BIN" -m ensurepip --upgrade >/dev/null 2>&1
    "$PY_CACHE_BIN" -m pip --version >/dev/null
    # 1) 核心依赖 + 技能 extras（pyproject 为唯一数据源）；--no-warn-script-location 降噪
    "$PY_CACHE_BIN" -m pip install --no-warn-script-location ".[wiki,pdf,docx,xlsx]"
    # 2) crew 源码由安装包按源码树携带，不复制进 site-packages
    "$PY_CACHE_BIN" -m pip uninstall -y crew || true
    # 3) 技能脚本补充依赖（pyproject 未覆盖部分）
    "$PY_CACHE_BIN" -m pip install --no-warn-script-location -r "$SCRIPT_DIR/runtime-requirements.txt"
    # 4) markitdown 钉 0.0.2 并 --no-deps：0.1.x 顶层 import magika（拖入 onnxruntime 超 100MB）；
    #    0.0.2 的轻量硬依赖已在 runtime-requirements.txt 中显式列出
    "$PY_CACHE_BIN" -m pip install --no-warn-script-location --no-deps "markitdown==0.0.2"
    printf '%s' "$DEP_HASH" > "$DEP_STAMP"
    echo "✓ 内嵌 Python 依赖安装完成"
else
    echo "✓ 内嵌 Python 依赖戳未变，跳过安装"
fi

# 关键依赖自检：gateway 入口 + 技能高频依赖 + MCP
"$PY_CACHE_BIN" -c "import fastapi, uvicorn, openai, pandas, markitdown; from mcp.server.mcpserver import MCPServer; print('deps ok')"

# 清理缓存内的字节码缓存，减少文件数量
find "$PY_CACHE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# 复制到 staging
PY_RT="$STAGE_GW/runtimes/python"
rm -rf "$PY_RT"
mkdir -p "$STAGE_GW/runtimes"
cp -R "$PY_CACHE" "$PY_RT"
rm -f "$PY_RT/.deps-stamp"
echo "✓ Python 运行时已复制到 staging"

# ----- 5) Node.js portable（裁剪 corepack/docs，保留 npm：docx/webapp 技能需要） -----
echo ""
echo "→ [5/7] 准备内嵌 Node.js v${BUNDLED_NODE_VERSION}..."
NODE_CACHE="$RUNTIMES_CACHE/node"
NODE_CACHE_BIN="$NODE_CACHE/bin/node"

if [ ! -x "$NODE_CACHE_BIN" ]; then
    echo "  下载 Node.js v${BUNDLED_NODE_VERSION} portable (${NODE_ARCH})..."
    NODE_TAR="$ROOT_DIR/dist/node-portable.tar.gz"
    curl "${CURL_OPTS[@]}" \
        "https://npmmirror.com/mirrors/node/v${BUNDLED_NODE_VERSION}/node-v${BUNDLED_NODE_VERSION}-${NODE_ARCH}.tar.gz" \
        -o "$NODE_TAR"
    rm -rf "$NODE_CACHE"
    mkdir -p "$NODE_CACHE"
    tar -xzf "$NODE_TAR" -C "$NODE_CACHE" --strip-components=1
    rm -f "$NODE_TAR"
    if [ ! -x "$NODE_CACHE_BIN" ]; then
        echo "❌ Node.js 解压后未找到 bin/node" >&2
        exit 1
    fi
    # 裁剪：corepack / 头文件 / 文档对运行时无用；保留 npm/npx（技能需要 npm install）
    rm -rf "$NODE_CACHE/lib/node_modules/corepack"
    rm -f  "$NODE_CACHE/bin/corepack"
    rm -rf "$NODE_CACHE/share" "$NODE_CACHE/include"
    rm -f  "$NODE_CACHE/CHANGELOG.md" "$NODE_CACHE/README.md"
    echo "✓ Node.js portable 已缓存并裁剪"
else
    echo "✓ Node.js portable 缓存已存在，跳过下载"
fi
"$NODE_CACHE_BIN" --version

NODE_RT="$STAGE_GW/runtimes/node"
rm -rf "$NODE_RT"
cp -R "$NODE_CACHE" "$NODE_RT"
chmod -R 755 "$STAGE_GW/runtimes"
echo "✓ Node.js 运行时已复制到 staging"

# ----- 6) staging 冒烟验证 -----
# 用 staging 内的内嵌 Python 以源码模式导入 gateway 入口，
# 提前暴露依赖缺失 / 路径布局错误，而不是等安装后才在用户机器上炸。
echo ""
echo "→ [6/7] 冒烟验证 staging 内的 gateway..."
PYTHONPATH="$STAGE_GW" CREW_PACKAGED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PY_RT/bin/python3" -c "import crew.gateway.server; print('gateway import ok')" || {
    echo "❌ staging gateway 冒烟验证失败：内嵌 Python 无法导入 crew.gateway.server" >&2
    exit 1
}
echo "✓ staging gateway 冒烟验证通过"

# ----- 7) 组装 .app + 生成 DMG -----
# desktop 主进程约定路径：Contents/Resources/crew-gateway/，
# 以 runtimes/python/bin/python3 -m crew.gateway.server 启动
echo ""
echo "→ [7/7] 组装 .app 并生成 DMG..."
APP_GW="$APP_PATH/Contents/Resources/crew-gateway"
rm -rf "$APP_GW"
cp -R "$STAGE_GW" "$APP_GW"
chmod -R 755 "$APP_GW/runtimes"
echo "✓ gateway 已嵌入 $APP_GW"

STAGE_DMG="$(mktemp -d /tmp/crew-dmg-stage.XXXXXX)"
trap 'rm -rf "$STAGE_DMG"' EXIT
cp -R "$APP_PATH" "$STAGE_DMG/crew-desktop.app"
ln -s /Applications "$STAGE_DMG/Applications"
rm -f "$DMG_NAME"
hdiutil create -volname "Crew" -srcfolder "$STAGE_DMG" -ov -format UDZO "$DMG_NAME" >/dev/null
echo "✓ $DMG_NAME ($(du -h "$DMG_NAME" | cut -f1))"

# ----- 汇总 + 版本号递增（与 pack_deb.ps1 一致） -----
DESKTOP_BYTES="$(du -sk "$APP_PATH" | cut -f1)"
GATEWAY_BYTES="$(du -sk "$APP_GW" | cut -f1)"
DMG_BYTES="$(stat -f %z "$DMG_NAME")"
printf 'electron_desktop_bytes=%s\ngateway_bytes=%s\ndmg_bytes=%s\n' \
    "$((DESKTOP_BYTES * 1024))" "$((GATEWAY_BYTES * 1024))" "$DMG_BYTES" \
    > package-size-report-mac.txt

IFS='.' read -r v1 v2 v3 <<< "$VERSION"
if [ -n "${v3:-}" ]; then
    NEXT_VERSION="$v1.$v2.$((v3 + 1))"
    printf '%s' "$NEXT_VERSION" > "$SCRIPT_DIR/version.txt"
    echo " Version bumped: $VERSION -> $NEXT_VERSION"
fi

echo ""
echo "==========================================="
echo " 构建完成！"
echo " 产物: $ROOT_DIR/$DMG_NAME"
echo " 体积报告: $ROOT_DIR/package-size-report-mac.txt"
echo " 注意: 未做 Apple 签名/公证，首次打开需右键 → 打开"
echo "==========================================="
