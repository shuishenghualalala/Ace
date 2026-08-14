param (
    [string]$Version = ""
)



$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = $PWD.Path }
Set-Location $ProjectRoot

# 获取版本号
if (-not $Version) {
    $versionFile = Join-Path $PSScriptRoot "version.txt"
    if (Test-Path $versionFile) {
        $Version = (Get-Content $versionFile -Raw).Trim()
    } else {
        $Version = "1.0.0"
    }
}

$ErrorActionPreference = "Stop"

# -----------------------------------------------------------------------------
# 同步构建版本 + 平台标识到 desktop/package.json：
#   app 运行时上报版本 / 拼接更新下载 URL / 显示平台标签都依赖它。
# 只改写 "version" 和 "platform" 字段，保持文件其余内容字节不变。
# -----------------------------------------------------------------------------
$DesktopPkgPath = Join-Path $ProjectRoot "desktop\package.json"
$BuildPlatform = "Win amd64"
if (Test-Path $DesktopPkgPath) {
    $pkgContent = [System.IO.File]::ReadAllText($DesktopPkgPath)
    # 同步 version
    $versionRegex = [regex]'"version"\s*:\s*"[^"]*"'
    $pkgPatched = $versionRegex.Replace($pkgContent, "`"version`": `"$Version`"", 1)
    # 注入 platform（若已存在则替换，否则在 version 后追加）
    $platformRegex = [regex]'"platform"\s*:\s*"[^"]*"'
    if ($platformRegex.IsMatch($pkgPatched)) {
        $pkgPatched = $platformRegex.Replace($pkgPatched, "`"platform`": `"$BuildPlatform`"", 1)
    } else {
        $pkgPatched = $pkgPatched -replace '("version"\s*:\s*"[^"]*")', "`$1,`n  `"platform`": `"$BuildPlatform`""
    }
    if ($pkgPatched -ne $pkgContent) {
        [System.IO.File]::WriteAllText($DesktopPkgPath, $pkgPatched, (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Synced desktop/package.json version -> $Version, platform -> $BuildPlatform" -ForegroundColor Green
    }
} else {
    Write-Warning "desktop/package.json not found at $DesktopPkgPath; skip version sync"
}

# 打包内嵌运行时版本
$BundledPythonVersion = "3.11.9"
$BundledNodeVersion    = "20.18.3"

Write-Host "===========================================" -ForegroundColor Cyan
Write-Host " 构建 Windows amd64 安装包 (Inno Setup)" -ForegroundColor Cyan
Write-Host " 版本: $Version" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan

# -----------------------------------------------------------------------------
# 1-4 步与之前相同：构建环境准备与前后端编译
# -----------------------------------------------------------------------------
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "正在创建 uv 虚拟环境..." -ForegroundColor Yellow
    uv venv .venv --python 3.11
}

if (-not (Test-Path ".\.venv\Scripts\pyinstaller.exe")) {
    Write-Host "正在安装项目与 PyInstaller..." -ForegroundColor Yellow
    uv pip install -e ".[dev]"
    uv pip install pyinstaller
}

Write-Host "正在构建 Web 前端..." -ForegroundColor Yellow
Push-Location web
try {
    if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund }
    npm run build
} finally { Pop-Location }

Write-Host "正在构建 Electron 桌面端..." -ForegroundColor Yellow
Push-Location desktop
try {
    if (-not (Test-Path "node_modules")) {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败 (exit $LASTEXITCODE)" }
    }
    # 清理 electron-builder 旧产物：win-unpacked 若残留，electron-builder 可能复用
    # 旧 asar（含旧 version），导致「装新版桌面端仍显示旧版本号」。
    if (Test-Path "release") { Remove-Item "release" -Recurse -Force }
    Write-Host "正在构建原生安全 runtime..." -ForegroundColor Yellow
    cargo build --release --manifest-path "..\security-runtime\Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "cargo build security-runtime 失败 (exit $LASTEXITCODE)" }
    node scripts/prepare-security-runtime.mjs --runtime "..\security-runtime\target\release\ace-security-runtime.exe"
    if ($LASTEXITCODE -ne 0) { throw "准备 security-runtime 失败 (exit $LASTEXITCODE)" }
    npm run security:verify
    if ($LASTEXITCODE -ne 0) { throw "security-runtime 校验失败 (exit $LASTEXITCODE)" }
    npm run dist:win
    # PowerShell 的 $ErrorActionPreference=Stop 对 native 命令(npm)不生效——
    # npm 失败只设 $LASTEXITCODE，不抛异常，脚本会继续跑后续步骤（PyInstaller），
    # 直到 Copy-Item 找不到 win-unpacked 才暴露错误。这里显式检查并中止。
    if ($LASTEXITCODE -ne 0) { throw "npm run dist:win 失败 (exit $LASTEXITCODE)，electron-builder 未生成 release/win-unpacked" }
    if (-not (Test-Path "release\win-unpacked")) {
        throw "electron-builder 未产出 release/win-unpacked，请检查 desktop/electron-builder.yml 与 package.json"
    }
} finally { Pop-Location }

Write-Host "正在使用 PyInstaller 打包 Gateway..." -ForegroundColor Yellow
# 清理 PyInstaller 旧产物：--clean 只清 build/ 缓存，不清 dist/crew-gateway。
# 残留旧 exe 会被后续 staging 直接复用，导致装新版仍跑旧 gateway 代码。
if (Test-Path "dist\crew-gateway") { Remove-Item "dist\crew-gateway" -Recurse -Force }

# 只向 PyInstaller 提供可发布配置。开发者本机的 config/.env 和 config/config.yaml
# 都不会进入产物；安装后从两个 example 初始化用户私有文件。
$packConfigDir = Join-Path $ProjectRoot "dist\pack-config"
if (Test-Path $packConfigDir) { Remove-Item $packConfigDir -Recurse -Force }
New-Item -ItemType Directory -Path $packConfigDir | Out-Null
Get-ChildItem (Join-Path $ProjectRoot "config") -Force |
    Where-Object {
        $_.Name -eq ".env.example" -or
        ($_.Name -notlike ".env*" -and $_.Name -ne "config.yaml")
    } |
    ForEach-Object { Copy-Item $_.FullName -Destination $packConfigDir -Recurse -Force }
$unexpectedPrivateEnv = Get-ChildItem $packConfigDir -Force -Filter ".env*" |
    Where-Object { $_.Name -ne ".env.example" }
if ($unexpectedPrivateEnv -or (Test-Path (Join-Path $packConfigDir "config.yaml"))) {
    throw "安全检查失败: PyInstaller 配置暂存目录中不应出现本地 .env 或 config.yaml"
}
if (-not (Test-Path (Join-Path $packConfigDir ".env.example"))) {
    throw "构建失败: config/.env.example 未进入配置暂存目录"
}
if (-not (Test-Path (Join-Path $packConfigDir "config.yaml.example"))) {
    throw "构建失败: config/config.yaml.example 未进入配置暂存目录"
}

$pyinstallerArgs = @(
    "--name", "crew-gateway",
    "--onedir",
    "--add-data", "web\dist;web\dist",
    "--add-data", "$packConfigDir;config",
    "--add-data", "crew\skills;crew\skills",
    "--add-data", "crew\scenarios;crew\scenarios",
    "--add-data", "crew\mcp_servers;crew\mcp_servers",
    "--add-data", "crew\agent\subagent\presets;crew\agent\subagent\presets",
    "--add-data", "plugins;plugins",
    "--clean",
    "-y",
    "crew\gateway\server.py"
)
uv run pyinstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败 (exit $LASTEXITCODE)" }

# -----------------------------------------------------------------------------
# 5. 组装待打包目录 (Staging)
# -----------------------------------------------------------------------------
$stage = Join-Path $ProjectRoot "dist\windows-staging"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null

Write-Host "正在组装文件到 $stage..." -ForegroundColor Yellow
Copy-Item -Path "dist\crew-gateway" -Destination (Join-Path $stage "crew-gateway") -Recurse
Copy-Item -Path "desktop\release\win-unpacked" -Destination (Join-Path $stage "crew-desktop") -Recurse

# PyInstaller --onedir 将 add-data 文件存放在 _internal/ 下。
# 发布产物只显式携带两个 example；本地配置与真实密钥必须由用户在本机生成。
$pyiConfigDir = Join-Path $stage "crew-gateway\config"
if (-not (Test-Path $pyiConfigDir)) {
    New-Item -ItemType Directory -Path $pyiConfigDir -Force | Out-Null
}
$sourceEnvExample = Join-Path $ProjectRoot "config\.env.example"
$destEnvExample   = Join-Path $pyiConfigDir ".env.example"
$sourceConfigExample = Join-Path $ProjectRoot "config\config.yaml.example"
$destConfigExample   = Join-Path $pyiConfigDir "config.yaml.example"
if (Test-Path $sourceEnvExample) {
    Copy-Item -Path $sourceEnvExample -Destination $destEnvExample -Force
    Write-Host "✓ config/.env.example 已显式复制到 $pyiConfigDir" -ForegroundColor Green
} else {
    throw "❌ 构建失败: 配置模板 config/.env.example 不存在 ($sourceEnvExample)。"
}
if (Test-Path $sourceConfigExample) {
    Copy-Item -Path $sourceConfigExample -Destination $destConfigExample -Force
    Write-Host "✓ config/config.yaml.example 已显式复制到 $pyiConfigDir" -ForegroundColor Green
} else {
    throw "❌ 构建失败: 配置模板 config/config.yaml.example 不存在 ($sourceConfigExample)。"
}

# 兜底验证：确认两个模板最终存在于安装包产物中
$bundledEnvExample = Join-Path $stage "crew-gateway\config\.env.example"
$bundledConfigExample = Join-Path $stage "crew-gateway\config\config.yaml.example"
if ((Test-Path $bundledEnvExample) -and (Test-Path $bundledConfigExample)) {
    Write-Host "✓ 配置与环境变量 example 已预置到安装包中" -ForegroundColor Green
} else {
    throw "❌ 构建失败: 安装包中缺少 config.yaml.example 或 .env.example。"
}

# -----------------------------------------------------------------------------
# 5b. 兜底修复：PyInstaller 的 --add-data 在某些环境下未完整递归复制 config 子目录，
#     显式复制关键配置子目录到 _internal/config/ 以确保运行时路径解析正确
# -----------------------------------------------------------------------------
$pyiInternalConfigDir = Join-Path $stage "crew-gateway\_internal\config"
$sourceConfigDir = Join-Path $ProjectRoot "config"
$requiredConfigSubDirs = @("prompts")

if (-not (Test-Path $pyiInternalConfigDir)) {
    New-Item -ItemType Directory -Path $pyiInternalConfigDir -Force | Out-Null
}

foreach ($subDir in $requiredConfigSubDirs) {
    $sourceDir = Join-Path $sourceConfigDir $subDir
    $destDir = Join-Path $pyiInternalConfigDir $subDir
    if (Test-Path $sourceDir) {
        if (Test-Path $destDir) {
            Remove-Item $destDir -Recurse -Force
        }
        Copy-Item -Path $sourceDir -Destination $destDir -Recurse -Force
        $fileCount = (Get-ChildItem -Path $destDir -Recurse -File).Count
        Write-Host "✓ config/$subDir 已显式复制到 _internal/config/$subDir ($fileCount 个文件)" -ForegroundColor Green
    } else {
        Write-Host "⚠️ 源目录不存在，跳过: $sourceDir" -ForegroundColor Yellow
    }
}

# 额外兜底：同时确保这些目录在 exe 同级 config/ 中也存在（兼容多种路径解析方式）
$exeLevelConfigDir = Join-Path $stage "crew-gateway\config"
foreach ($subDir in $requiredConfigSubDirs) {
    $sourceDir = Join-Path $sourceConfigDir $subDir
    $destDir = Join-Path $exeLevelConfigDir $subDir
    if (Test-Path $sourceDir) {
        if (Test-Path $destDir) {
            Remove-Item $destDir -Recurse -Force
        }
        Copy-Item -Path $sourceDir -Destination $destDir -Recurse -Force
    }
}

# -----------------------------------------------------------------------------
# 5c. 打包内嵌运行时 (Python embeddable + Node.js portable)
#     解决目标系统无 Python / Node.js 时技能脚本无法执行的问题。
#     运行时放入 _internal/runtimes/，运行时由 home.py 的
#     _bundled_runtime_paths() 检测并前置到子进程 PATH。
# -----------------------------------------------------------------------------
$runtimesDir = Join-Path $stage "crew-gateway\_internal\runtimes"
if (-not (Test-Path $runtimesDir)) {
    New-Item -ItemType Directory -Path $runtimesDir -Force | Out-Null
}

# 运行时缓存目录（位于 staging 之外，避免每次构建重新下载 ~200MB）
$runtimesCache = Join-Path $ProjectRoot "dist\runtimes-cache"
if (-not (Test-Path $runtimesCache)) {
    New-Item -ItemType Directory -Path $runtimesCache -Force | Out-Null
}

# 确保 TLS 1.2 可用（部分 Windows 默认仅 TLS 1.0）
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

# ---- Python embeddable ----
$pythonCacheDir = Join-Path $runtimesCache "python"
$pythonCacheExe = Join-Path $pythonCacheDir "python.exe"
$pythonRtDir  = Join-Path $runtimesDir "python"

if (-not (Test-Path $pythonCacheExe)) {
    Write-Host "正在下载 Python $BundledPythonVersion embeddable..." -ForegroundColor Yellow
    $pyZipUrl  = "https://www.python.org/ftp/python/$BundledPythonVersion/python-$BundledPythonVersion-embed-amd64.zip"
    $pyZipPath = Join-Path $ProjectRoot "dist\python-embed.zip"
    Invoke-WebRequest -Uri $pyZipUrl -OutFile $pyZipPath -UseBasicParsing
    New-Item -ItemType Directory -Path $pythonCacheDir -Force | Out-Null
    Expand-Archive -Path $pyZipPath -DestinationPath $pythonCacheDir -Force
    Remove-Item $pyZipPath -Force

    # 启用 site-packages：取消注释 _pth 文件中的 import site
    $pthFile = Get-ChildItem -Path $pythonCacheDir -Filter "python*._pth" | Select-Object -First 1
    if ($pthFile) {
        $pthContent = Get-Content $pthFile.FullName -Raw
        $pthContent = $pthContent -replace "#import site", "import site"
        Set-Content -Path $pthFile.FullName -Value $pthContent -NoNewline
    }

    # 安装 pip（get-pip.py）
    Write-Host "正在为内嵌 Python 安装 pip..." -ForegroundColor Yellow
    $getPipPath = Join-Path $ProjectRoot "dist\get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPipPath -UseBasicParsing
    $pythonExe = Join-Path $pythonCacheDir "python.exe"
    & $pythonExe $getPipPath --no-warn-script-location 2>&1 | Out-Null
    Remove-Item $getPipPath -Force

    # 安装技能脚本常用的第三方包
    # 注意：markitdown 会拖入 sympy(72MB) + onnxruntime(35MB) + numpy(33MB) 等重型依赖，
    # 技能脚本只需基础文档转换功能，使用 --no-deps 手动指定轻量依赖组合。
    Write-Host "正在为内嵌 Python 安装技能依赖包..." -ForegroundColor Yellow
    & $pythonExe -m pip install --no-warn-script-location `
        requests lxml python-pptx openpyxl xlsxwriter `
        beautifulsoup4 markdown charset-normalizer `
        reportlab matplotlib pikepdf pdfplumber markdown2 "xhtml2pdf>=0.2.15" pypdfium2 Pillow defusedxml pandas 2>&1 | Out-Null
    # markitdown 核心（跳过重型可选依赖 onnxruntime/sympy/numpy）
    & $pythonExe -m pip install --no-warn-script-location --no-deps `
        markitdown 2>&1 | Out-Null

    # 创建 python3.cmd 别名（SKILL.md 中部分技能使用 python3 命令）
    $python3Cmd = Join-Path $pythonCacheDir "python3.cmd"
    Set-Content -Path $python3Cmd -Value '@"%~dp0python.exe" %*' -Encoding ASCII -NoNewline

    # 创建 pip.cmd / pip3.cmd 别名（技能脚本直接用 pip install 而非 python -m pip）
    $pipCmd = Join-Path $pythonCacheDir "pip.cmd"
    Set-Content -Path $pipCmd -Value '@"%~dp0python.exe" -m pip %*' -Encoding ASCII -NoNewline
    $pip3Cmd = Join-Path $pythonCacheDir "pip3.cmd"
    Set-Content -Path $pip3Cmd -Value '@"%~dp0python.exe" -m pip %*' -Encoding ASCII -NoNewline

    Write-Host "✓ Python $BundledPythonVersion embeddable 已缓存到 runtimes-cache/python/" -ForegroundColor Green
} else {
    Write-Host "✓ Python embeddable 缓存已存在，跳过下载" -ForegroundColor Green
}

# 确保 Python MCP server 依赖已装（幂等，不依赖缓存是否首次创建）。
# Python MCP server 通过 ${CREW_PYTHON}（内嵌 Python）启动，需要 mcp 包（含
# fastmcp）；若旧缓存缺少该包，server 会在启动时 ImportError。这里每次构建都检测补装。
$pythonCacheExe = Join-Path $pythonCacheDir "python.exe"
$mcpPkgDir = Join-Path $pythonCacheDir "Lib\site-packages\mcp"
if (-not (Test-Path $mcpPkgDir)) {
    Write-Host "内嵌 Python 缺 mcp 包，正在补装（含 fastmcp 及依赖）..." -ForegroundColor Yellow
    & $pythonCacheExe -m pip install --no-warn-script-location mcp 2>&1 | Out-Null
    if (Test-Path $mcpPkgDir) {
        Write-Host "✓ mcp 包已装入内嵌 Python" -ForegroundColor Green
    } else {
        Write-Warning "mcp 包安装失败，Python MCP server 将无法启动"
    }
} else {
    Write-Host "✓ 内嵌 Python 已含 mcp 包" -ForegroundColor Green
}

# 确保办公技能（xlsx/docx/pdf）依赖已装（幂等，不依赖缓存是否首次创建）。
# 这批包是后期增补的，旧缓存缺少它们，技能脚本运行时会 ImportError。以 pandas
# 目录为标记检测，缺失则整批补装。
$skillPkgMarker = Join-Path $pythonCacheDir "Lib\site-packages\pandas"
if (-not (Test-Path $skillPkgMarker)) {
    Write-Host "内嵌 Python 缺办公技能依赖，正在补装（reportlab/pandas 等）..." -ForegroundColor Yellow
    & $pythonCacheExe -m pip install --no-warn-script-location `
        reportlab matplotlib pikepdf pdfplumber markdown2 "xhtml2pdf>=0.2.15" pypdfium2 Pillow defusedxml pandas 2>&1 | Out-Null
    if (Test-Path $skillPkgMarker) {
        Write-Host "✓ 办公技能依赖已装入内嵌 Python" -ForegroundColor Green
    } else {
        Write-Warning "办公技能依赖安装失败，xlsx/docx/pdf 技能脚本将运行失败"
    }
} else {
    Write-Host "✓ 内嵌 Python 已含办公技能依赖" -ForegroundColor Green
}

# 复制缓存到 staging
if (Test-Path $pythonRtDir) { Remove-Item $pythonRtDir -Recurse -Force }
Copy-Item -Path $pythonCacheDir -Destination $pythonRtDir -Recurse -Force

# 清理 __pycache__ 目录（减少文件数量和路径长度）
Get-ChildItem -Path $pythonRtDir -Directory -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force

# 删除不必要的大型依赖（如果存在）
$heavyDeps = @("sympy", "onnxruntime", "numpy", "scipy", "pandas")
foreach ($dep in $heavyDeps) {
    $depPath = Join-Path $pythonRtDir "Lib\site-packages\$dep"
    if (Test-Path $depPath) {
        Remove-Item $depPath -Recurse -Force
        Write-Host "  已删除不必要的依赖: $dep" -ForegroundColor Gray
    }
    # 删除 dist-info
    $distInfo = Get-ChildItem -Path (Join-Path $pythonRtDir "Lib\site-packages") -Directory -Filter "$dep*" -ErrorAction SilentlyContinue
    if ($distInfo) {
        $distInfo | Remove-Item -Recurse -Force
    }
}

Write-Host "✓ Python 运行时已复制到 staging" -ForegroundColor Green

# ---- Node.js portable ----
$nodeCacheDir = Join-Path $runtimesCache "node"
$nodeCacheExe = Join-Path $nodeCacheDir "node.exe"
$nodeRtDir  = Join-Path $runtimesDir "node"

if (-not (Test-Path $nodeCacheExe)) {
    Write-Host "正在下载 Node.js v$BundledNodeVersion portable..." -ForegroundColor Yellow
    $nodeZipUrl  = "https://nodejs.org/dist/v$BundledNodeVersion/node-v$BundledNodeVersion-win-x64.zip"
    $nodeZipPath = Join-Path $ProjectRoot "dist\node-portable.zip"
    Invoke-WebRequest -Uri $nodeZipUrl -OutFile $nodeZipPath -UseBasicParsing

    # 解压到临时目录，然后移动内层目录到目标位置
    $tempExtract = Join-Path $ProjectRoot "dist\_node_extract"
    if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
    Expand-Archive -Path $nodeZipPath -DestinationPath $tempExtract -Force
    Remove-Item $nodeZipPath -Force

    $innerDir = Get-ChildItem -Path $tempExtract -Directory | Select-Object -First 1
    Move-Item -Path $innerDir.FullName -Destination $nodeCacheDir -Force
    Remove-Item $tempExtract -Recurse -Force

    if (-not (Test-Path (Join-Path $nodeCacheDir "node.exe"))) {
        throw "❌ Node.js 解压后未找到 node.exe"
    }
    Write-Host "✓ Node.js v$BundledNodeVersion portable 已缓存到 runtimes-cache/node/" -ForegroundColor Green
} else {
    Write-Host "✓ Node.js portable 缓存已存在，跳过下载" -ForegroundColor Green
}
# 复制缓存到 staging
if (Test-Path $nodeRtDir) { Remove-Item $nodeRtDir -Recurse -Force }
Copy-Item -Path $nodeCacheDir -Destination $nodeRtDir -Recurse -Force
Write-Host "✓ Node.js 运行时已复制到 staging" -ForegroundColor Green

# -----------------------------------------------------------------------------
# 6. 动态生成 Inno Setup 脚本 (.iss)
# -----------------------------------------------------------------------------
Write-Host "正在生成 Inno Setup 配置文件..." -ForegroundColor Yellow
$issPath = Join-Path $ProjectRoot "dist\installer.iss"
$outputDir = Join-Path $ProjectRoot "dist"
$setupFileName = "Crew_Setup_v$Version"

# 确保存在 .ico 图标（Inno Setup SetupIconFile 必须使用 ICO 格式）
$iconPath = Join-Path $ProjectRoot "desktop\assets\icon.ico"
$pngPath  = Join-Path $ProjectRoot "desktop\assets\icon.png"


# 将图标文件复制到 staging 根目录，供 Inno Setup 直接引用（避免依赖 EXE 内嵌图标）
$stageIconPath = Join-Path $stage "icon.ico"
if ($iconPath -and (Test-Path $iconPath)) {
    Copy-Item -Path $iconPath -Destination $stageIconPath -Force
    Write-Host "✓ icon.ico 已复制到 staging 目录" -ForegroundColor Green
} else {
    $stageIconPath = ""
}

$iconConfig = ""
$iconFileRef = ""
if ($stageIconPath -and (Test-Path $stageIconPath)) {
    $iconConfig = "SetupIconFile=$iconPath"
    $iconFileRef = "{app}\icon.ico"
} else {
    #  fallback：如果没有独立的 .ico，则使用 EXE 自身的图标
    $iconFileRef = "{app}\crew-desktop\crew-desktop.exe"
}

# 发布体积报告：分别记录 Electron、Gateway、完整 staging 与最终安装包，
# 便于持续追踪包体变化，且不再维护独立浏览器运行时口径。
$desktopBytes = (Get-ChildItem (Join-Path $stage "crew-desktop") -File -Recurse | Measure-Object Length -Sum).Sum
$gatewayBytes = (Get-ChildItem (Join-Path $stage "crew-gateway") -File -Recurse | Measure-Object Length -Sum).Sum
$stagedBytes = (Get-ChildItem $stage -File -Recurse | Measure-Object Length -Sum).Sum
if ($null -eq $desktopBytes) { $desktopBytes = 0 }
if ($null -eq $gatewayBytes) { $gatewayBytes = 0 }
if ($null -eq $stagedBytes) { $stagedBytes = 0 }
$packageSizeReport = Join-Path $ProjectRoot "dist\package-size-report-windows.txt"
Set-Content -Path $packageSizeReport -Value @(
    "electron_desktop_bytes=$desktopBytes"
    "gateway_bytes=$gatewayBytes"
    "staged_tree_bytes=$stagedBytes"
) -Encoding ASCII

$issContent = @"
[Setup]
AppName=Crew
AppVersion=$Version
AppPublisher=Crew Contributors
DefaultDirName={autopf}\Crew
DefaultGroupName=Crew
UninstallDisplayIcon=$iconFileRef
Compression=lzma2
SolidCompression=yes
OutputDir=$outputDir
OutputBaseFilename=$setupFileName
PrivilegesRequired=lowest
WizardStyle=modern
$iconConfig

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "$stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Crew"; Filename: "{app}\crew-desktop\crew-desktop.exe"; IconFilename: "$iconFileRef"
Name: "{autodesktop}\Crew"; Filename: "{app}\crew-desktop\crew-desktop.exe"; IconFilename: "$iconFileRef"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[UninstallDelete]
; 🌟 兜底清理：Inno Setup 标准卸载可能因文件被锁定而跳过 _internal/runtimes
; 下的 Python/Node.js 运行时文件。显式声明确保卸载时强制尝试删除。
Type: filesandordirs; Name: "{app}\crew-gateway\_internal\runtimes"

[Run]
Filename: "{app}\crew-desktop\crew-desktop.exe"; Description: "{cm:LaunchProgram,Crew}"; Flags: nowait postinstall

"@

[System.IO.File]::WriteAllText($issPath, $issContent, [System.Text.Encoding]::UTF8)

# -----------------------------------------------------------------------------
# 7. 调用 Inno Setup 编译器 (ISCC) 进行最终打包
# -----------------------------------------------------------------------------
$isccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

if (-not (Test-Path $isccPath)) {
    Write-Host "⚠️ 找不到 Inno Setup 编译器！请前往 https://jrsoftware.org/isinfo.php 下载安装。" -ForegroundColor Red
    Write-Host "安装后再次运行此脚本即可完成打包。" -ForegroundColor Red
    exit 1
}

Write-Host "正在编译最终的 .exe 安装包 (这可能需要几分钟)..." -ForegroundColor Yellow
$process = Start-Process -FilePath $isccPath -ArgumentList "`"$issPath`"" -Wait -NoNewWindow -PassThru
if ($process.ExitCode -ne 0) {
    throw "Inno Setup 编译失败，退出码: $($process.ExitCode)"
}
$installerPath = Join-Path $outputDir "$setupFileName.exe"
$installerBytes = (Get-Item $installerPath).Length
Add-Content -Path $packageSizeReport -Value "installer_bytes=$installerBytes" -Encoding ASCII

Write-Host "===========================================" -ForegroundColor Green
Write-Host " Windows 安装包构建成功！" -ForegroundColor Green
Write-Host " 产物路径: $outputDir\$setupFileName.exe" -ForegroundColor Green
Write-Host " 体积报告: $packageSizeReport" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green

# 构建成功后自动递增 patch 版本号
$versionFile = Join-Path $PSScriptRoot "version.txt"
$parts = $Version.Split('.')
if ($parts.Count -eq 3) {
    $parts[2] = [string]([int]$parts[2] + 1)
    $nextVersion = $parts -join '.'
    Set-Content -Path $versionFile -Value $nextVersion -NoNewline
    Write-Host " Version bumped: $Version -> $nextVersion" -ForegroundColor Green
}
