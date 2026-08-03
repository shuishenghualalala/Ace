#!/bin/bash
# =============================================================================
# Crew macOS (Apple Silicon) DMG 打包脚本
# -----------------------------------------------------------------------------
# 产物：crew-desktop_${VERSION}_arm64.dmg
# 组成：crew-desktop.app（Electron）+ Contents/Resources/crew-gateway（PyInstaller）
#       + _internal/runtimes（内嵌 Python standalone + Node.js portable）
# 运行环境：macOS arm64 主机（PyInstaller / Electron 均不可交叉构建 Mac 二进制）
# 用法：
#   ./deb-package/pack_mac.sh            # 版本号取 deb-package/version.txt
#   ./deb-package/pack_mac.sh 0.29.0     # 显式指定版本号
# =============================================================================
set -euo pipefail

# ----- 参数与路径 -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-$(tr -d '[:space:]' < "$SCRIPT_DIR/version.txt")}"
DMG_NAME="crew-desktop_${VERSION}_arm64.dmg"
BUNDLED_PYTHON_VERSION="3.11.9"
BUNDLED_NODE_VERSION="20.18.3"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "❌ 本脚本必须在 macOS arm64 (Apple Silicon) 主机上运行" >&2
    exit 1
fi

echo "==========================================="
echo " Crew macOS 安装包构建"
echo " 版本: $VERSION"
echo " 产物: $DMG_NAME"
echo "==========================================="

# ----- 0) 构建依赖检查 -----
# pip/uv 统一走清华镜像（pypi.org 直连在代理环境下极易 TLS EOF，与 Dockerfile.pack 同源）
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
export UV_INDEX_URL="${UV_INDEX_URL:-$PIP_INDEX_URL}"

VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "❌ 未找到 .venv，请先在项目根创建虚拟环境并安装项目依赖" >&2
    exit 1
fi

# .venv 可能由 uv 创建而不带 pip：优先 pip，缺 pip 时退回 uv / ensurepip
venv_pip_install() {
    if "$VENV_PY" -m pip --version >/dev/null 2>&1; then
        "$VENV_PY" -m pip install --quiet "$@"
    elif command -v uv >/dev/null; then
        uv pip install --quiet --python "$VENV_PY" "$@"
    else
        "$VENV_PY" -m ensurepip -q >/dev/null 2>&1
        "$VENV_PY" -m pip install --quiet "$@"
    fi
}

if [ ! -x "$ROOT_DIR/.venv/bin/pyinstaller" ]; then
    echo "→ .venv 缺少 pyinstaller，自动安装..."
    venv_pip_install pyinstaller
fi
command -v node >/dev/null || { echo "❌ 未找到 node，请先安装 Node.js 22+" >&2; exit 1; }
command -v npm  >/dev/null || { echo "❌ 未找到 npm" >&2; exit 1; }
command -v hdiutil >/dev/null || { echo "❌ 未找到 hdiutil（应为 macOS 自带）" >&2; exit 1; }

# ----- 1) web 前端构建（gateway 内嵌管理台静态资源） -----
echo ""
echo "→ [1/6] 构建 web 前端..."
(cd web && npm install --no-audit --no-fund && npm run build)

# ----- 2) PyInstaller 打包 crew-gateway -----
echo ""
echo "→ [2/6] PyInstaller 打包 crew-gateway..."
venv_pip_install --no-deps .
rm -rf dist build
.venv/bin/pyinstaller --name crew-gateway \
    --onedir --noconfirm --clean \
    --add-data "web/dist:web/dist" \
    --add-data "config:config" \
    --add-data "crew/skills:crew/skills" \
    --add-data "crew/scenarios:crew/scenarios" \
    --add-data "crew/mcp_servers:crew/mcp_servers" \
    --add-data "crew/agent/subagent/presets:crew/agent/subagent/presets" \
    --add-data "plugins:plugins" \
    crew/gateway/server.py

# PyInstaller --add-data 偶尔漏拷 config 子目录，显式兜底（与 Dockerfile.pack 一致）
for subdir in prompts; do
    src="config/$subdir"
    for dst in "dist/crew-gateway/_internal/config/$subdir" "dist/crew-gateway/config/$subdir"; do
        if [ -d "$src" ]; then
            mkdir -p "$dst"
            cp -R "$src"/* "$dst"/ 2>/dev/null || true
        fi
    done
done

# 安装包只携带可发布的配置模板；本地配置和真实密钥绝不进入发布产物
mkdir -p dist/crew-gateway/config
cp config/.env.example dist/crew-gateway/config/.env.example
cp config/config.yaml.example dist/crew-gateway/config/config.yaml.example
echo "✓ crew-gateway 打包完成"

# ----- 3) 内嵌运行时（Python standalone + Node.js portable） -----
echo ""
echo "→ [3/6] 下载内嵌运行时..."
RUNTIMES="dist/crew-gateway/_internal/runtimes"
mkdir -p "$RUNTIMES/python" "$RUNTIMES/node"

# 代理/网络抖动环境下 HTTP2 易断（curl 16/SSL EOF），统一走 HTTP1.1 + 全错误重试
CURL_OPTS=(-fsSL --http1.1 --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20)

PYTHON_REL="indygreg/python-build-standalone/releases/download/20240415/cpython-${BUNDLED_PYTHON_VERSION}+20240415-aarch64-apple-darwin-install_only.tar.gz"
curl "${CURL_OPTS[@]}" "https://github.com/${PYTHON_REL}" -o /tmp/crew-python.tar.gz
tar -xzf /tmp/crew-python.tar.gz -C "$RUNTIMES/python" --strip-components=1
rm /tmp/crew-python.tar.gz
"$RUNTIMES/python/bin/python3" --version
"$RUNTIMES/python/bin/python3" -m pip install --quiet --no-cache-dir \
    requests lxml python-pptx openpyxl xlsxwriter beautifulsoup4 markdown charset-normalizer
"$RUNTIMES/python/bin/python3" -m pip install --quiet --no-cache-dir --no-deps markitdown
"$RUNTIMES/python/bin/python3" -m pip install --quiet --no-cache-dir "mcp==1.28.0"
ln -sf python3 "$RUNTIMES/python/bin/python"
"$RUNTIMES/python/bin/python3" -c "from mcp.server.fastmcp import FastMCP; print('✓ mcp.server.fastmcp importable')"

curl "${CURL_OPTS[@]}" \
    "https://npmmirror.com/mirrors/node/v${BUNDLED_NODE_VERSION}/node-v${BUNDLED_NODE_VERSION}-darwin-arm64.tar.gz" \
    -o /tmp/crew-node.tar.gz
tar -xzf /tmp/crew-node.tar.gz -C "$RUNTIMES/node" --strip-components=1
rm /tmp/crew-node.tar.gz
"$RUNTIMES/node/bin/node" --version
chmod -R 755 "$RUNTIMES"
echo "✓ 内嵌运行时就绪（Python ${BUNDLED_PYTHON_VERSION} + Node v${BUNDLED_NODE_VERSION}）"

# ----- 4) Electron 桌面端构建（mac dir target） -----
echo ""
echo "→ [4/6] 构建 crew-desktop Electron 客户端..."
# Electron / electron-builder 二进制统一走 npmmirror（GitHub 直连在代理环境下易超时，与 Dockerfile.pack 同源）
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
(cd desktop && npm ci --no-audit --no-fund)

# 注入版本号与平台标识（须在 npm ci 之后，避免被 lockfile 校验覆盖）
node -e "const fs=require('fs');const p=JSON.parse(fs.readFileSync('desktop/package.json','utf8'));p.version='${VERSION}';p.platform='macOS arm64';fs.writeFileSync('desktop/package.json',JSON.stringify(p,null,2)+'\n');"

(cd desktop && npm run build && npx electron-builder --mac --arm64 --config electron-builder.yml)

APP_PATH="$(find desktop/release -maxdepth 2 -name 'crew-desktop.app' -print -quit)"
if [ -z "$APP_PATH" ]; then
    echo "❌ 未找到 electron-builder 产物 crew-desktop.app" >&2
    exit 1
fi
echo "✓ Electron 客户端: $APP_PATH"

# ----- 5) 组装 .app：gateway 放进 Contents/Resources/crew-gateway -----
# desktop 主进程约定路径：path.join(process.resourcesPath, 'crew-gateway', 'crew-gateway')
echo ""
echo "→ [5/6] 组装 .app..."
rm -rf "$APP_PATH/Contents/Resources/crew-gateway"
cp -R dist/crew-gateway "$APP_PATH/Contents/Resources/crew-gateway"
chmod -R 755 "$APP_PATH/Contents/Resources/crew-gateway"
echo "✓ gateway 已嵌入 $APP_PATH/Contents/Resources/crew-gateway"

# ----- 6) 生成 DMG -----
echo ""
echo "→ [6/6] 生成 DMG..."
STAGE="$(mktemp -d /tmp/crew-dmg-stage.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP_PATH" "$STAGE/crew-desktop.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG_NAME"
hdiutil create -volname "Crew" -srcfolder "$STAGE" -ov -format UDZO "$DMG_NAME" >/dev/null
echo "✓ $DMG_NAME ($(du -h "$DMG_NAME" | cut -f1))"

# ----- 汇总 + 版本号递增（与 pack_deb.ps1 一致） -----
DESKTOP_BYTES="$(du -sk "$APP_PATH" | cut -f1)"
GATEWAY_BYTES="$(du -sk "$APP_PATH/Contents/Resources/crew-gateway" | cut -f1)"
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
echo " 注意: 未做 Apple 签名/公证，首次打开需右键 → 打开"
echo "==========================================="
