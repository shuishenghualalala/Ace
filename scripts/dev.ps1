# =============================================================================
# Crew 一键开发启动脚本（Windows PowerShell）
# -----------------------------------------------------------------------------
# 在仓库根目录的 PowerShell 中运行：
#   pwsh ./scripts/dev.ps1
#
# 做的事：
#   1. 激活 .venv（不存在时先用 uv 创建）
#   2. 缺失时从 *.example 复制本地配置文件
#   3. 缺失时安装桌面端 npm 依赖
#   4. 启动桌面开发模式：cd desktop && npm run dev
# =============================================================================
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $ScriptDir '..\pyproject.toml')) {
    $RootDir = (Resolve-Path (Join-Path $ScriptDir '..')).Path
} elseif ((Test-Path '.\pyproject.toml') -and (Test-Path '.\crew')) {
    $RootDir = (Resolve-Path '.').Path
} else {
    Write-Host "未找到仓库根目录，请在 Ace 仓库内运行本脚本" -ForegroundColor Red
    exit 1
}
Set-Location $RootDir

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Blue }
function Warn($msg) { Write-Host "⚠️  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "❌ $msg" -ForegroundColor Red; exit 1 }

Info "仓库目录: $RootDir"

# ----- 0. 依赖工具 -----
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "未找到 uv，请先安装 uv（https://docs.astral.sh/uv/）或运行 scripts/install.ps1"
}

# ----- 1. 虚拟环境与后端依赖 -----
if (-not (Test-Path '.venv')) {
    Info "创建 .venv（Python 3.11）"
    uv venv .venv --python 3.11
    if ($LASTEXITCODE -ne 0) { Die "uv venv 失败" }
}

if (-not (Test-Path '.venv\Scripts\Activate.ps1')) {
    Die "未找到 .venv\Scripts\Activate.ps1，请检查虚拟环境"
}

$checkCrew = .venv\Scripts\python.exe -c "import crew" 2>&1
if ($LASTEXITCODE -ne 0) {
    Info "安装后端依赖（extras: dev, wiki）"
    uv pip install -e ".[dev,wiki]"
    if ($LASTEXITCODE -ne 0) { Die "后端依赖安装失败" }
}

# ----- 2. 本地配置模板 -----
if (-not (Test-Path 'config\config.yaml')) { Copy-Item 'config\config.yaml.example' 'config\config.yaml' }
if (-not (Test-Path 'config\.env'))        { Copy-Item 'config\.env.example' 'config\.env' }
Info "配置文件就绪: config/config.yaml, config/.env"

# ----- 3. 检查 Node.js -----
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Die "未找到 Node.js（需要 >= 22.12），请先安装 Node.js"
}
$major = [int](node -e 'process.stdout.write(process.versions.node.split(".")[0])')
if ($major -lt 22) { Die "Node.js 版本过低（需要 >= 22.12，当前 $(node --version)）" }

# ----- 4. 桌面端依赖 -----
if (-not (Test-Path 'desktop\node_modules')) {
    Warn "未找到 desktop\node_modules，执行 npm install..."
    Push-Location desktop
    npm install; if ($LASTEXITCODE -ne 0) { Pop-Location; Die "desktop npm install 失败" }
    Pop-Location
}

# ----- 5. 启动开发模式 -----
Info "启动 Crew 桌面开发模式"
.venv\Scripts\Activate.ps1
Set-Location desktop
npm run dev
