# =============================================================================
# Crew 一键安装脚本（Windows PowerShell）
# -----------------------------------------------------------------------------
# 在仓库根目录的 PowerShell 中运行：
#   pwsh ./scripts/install.ps1                # 安装后端 + 本地配置模板
#   pwsh ./scripts/install.ps1 -Dev           # 追加开发依赖（pytest/ruff）
#   pwsh ./scripts/install.ps1 -WithWeb       # 追加 Web 前端依赖与构建
#   pwsh ./scripts/install.ps1 -WithDesktop   # 追加桌面端依赖与构建
#   pwsh ./scripts/install.ps1 -All           # 以上全部
#
# 做的事：
#   1. 确保 uv 可用（缺失时按官方方式安装）
#   2. uv 创建 .venv（Python 3.11）并安装 crew 后端
#   3. 缺失时从 *.example 复制 config/config.yaml 与 config/.env
#   4. 可选：Web / Desktop 的 npm install 与构建（需要 Node.js >= 22.12）
# 不做的事：不写入系统目录、不配置任何模型密钥。
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$NoWiki,
    [switch]$WithWeb,
    [switch]$WithDesktop,
    [switch]$All
)

$ErrorActionPreference = 'Stop'

if ($All) { $Dev = $true; $WithWeb = $true; $WithDesktop = $true }

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Blue }
function Warn($msg) { Write-Host "⚠️  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "❌ $msg" -ForegroundColor Red; exit 1 }

# ----- 定位仓库根目录 -----
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $ScriptDir '..\pyproject.toml')) {
    $RootDir = (Resolve-Path (Join-Path $ScriptDir '..')).Path
} elseif ((Test-Path '.\pyproject.toml') -and (Test-Path '.\crew')) {
    $RootDir = (Resolve-Path '.').Path
} else {
    Die "未找到仓库根目录，请在 Ace 仓库内运行本脚本（或先 git clone https://github.com/shuishenghualalala/Ace.git）"
}
Set-Location $RootDir
Info "仓库目录: $RootDir"

# ----- 1. uv -----
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "未找到 uv，按官方方式安装"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Die "uv 安装失败，请手动安装: https://docs.astral.sh/uv/"
    }
}
Info "uv: $(uv --version)"

# ----- 2. Python 虚拟环境与后端依赖 -----
$extras = @()
if (-not $NoWiki) { $extras += 'wiki' }
if ($Dev)         { $extras += 'dev' }

Info "创建 .venv（Python 3.11）"
uv venv .venv --python 3.11
if ($LASTEXITCODE -ne 0) { Die "uv venv 失败" }

if ($extras.Count -gt 0) {
    $extraList = $extras -join ','
    Info "安装后端依赖（extras: $extraList）"
    uv pip install -e ".[$extraList]"
} else {
    Info "安装后端依赖"
    uv pip install -e .
}
if ($LASTEXITCODE -ne 0) { Die "依赖安装失败" }

# ----- 3. 本地配置模板（已存在则不覆盖） -----
if (-not (Test-Path 'config\config.yaml')) { Copy-Item 'config\config.yaml.example' 'config\config.yaml' }
if (-not (Test-Path 'config\.env'))        { Copy-Item 'config\.env.example' 'config\.env' }
Info "配置文件就绪: config/config.yaml, config/.env"

# ----- 4. 可选前端 -----
function Test-Node($what) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Warn "未找到 Node.js（需要 >= 22.12），跳过 $what；安装 Node 后可重跑本脚本"
        return $false
    }
    $major = [int](node -e 'process.stdout.write(process.versions.node.split(".")[0])')
    if ($major -lt 22) {
        Warn "Node.js 版本过低（需要 >= 22.12，当前 $(node --version)），跳过 $what"
        return $false
    }
    return $true
}

if ($WithWeb -and (Test-Node 'Web 前端')) {
    Info "安装并构建 Web 前端"
    Push-Location web
    npm ci; if ($LASTEXITCODE -ne 0) { Pop-Location; Die "web npm ci 失败" }
    npm run build; if ($LASTEXITCODE -ne 0) { Pop-Location; Die "web 构建失败" }
    Pop-Location
}

if ($WithDesktop -and (Test-Node '桌面端')) {
    Info "安装并构建桌面端"
    Push-Location desktop
    npm ci; if ($LASTEXITCODE -ne 0) { Pop-Location; Die "desktop npm ci 失败" }
    npm run build; if ($LASTEXITCODE -ne 0) { Pop-Location; Die "desktop 构建失败" }
    Pop-Location
}

# ----- 完成提示 -----
$desktopHint = if ($WithDesktop) { '' } else { '（需先 -WithDesktop）' }
Write-Host @"

✅ Crew 安装完成。下一步：

  激活虚拟环境:
    .venv\Scripts\Activate.ps1

  启动方式（三选一）:
    桌面端:   cd desktop && npm start       $desktopHint
    Web 端:   python -m crew.gateway.server + cd web && npm run dev
    CLI:      python -m crew.cli

  配置模型:
    桌面端「设置 → 模型 → 添加模型」，或编辑 config/config.yaml + config/.env
    详见 README.md「配置模型」一节。
"@
