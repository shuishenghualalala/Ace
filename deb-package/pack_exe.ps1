param (
    [string]$Version = ""
)

# =============================================================================
# Crew Windows 安装包打包脚本（Inno Setup）
# -----------------------------------------------------------------------------
# 产物：dist/Crew_Setup_v<版本>.exe
# 组成：crew-desktop（Electron win-unpacked）
#       + crew-gateway（源码树：crew/ + plugins/ + config/ 模板）
#       + crew-gateway/runtimes/python（python-build-standalone，gateway 与技能脚本共用）
#       + crew-gateway/runtimes/node（Node.js portable，已裁剪 corepack/docs）
# gateway 不再经 PyInstaller 打包：desktop 主进程用内嵌 Python 以
# `python -m crew.gateway.server` 直接跑源码（CREW_PACKAGED=1 标记打包态）。
#
# 依赖清单唯一数据源：pyproject.toml（核心 + wiki/pdf/docx/xlsx extras）
# + deb-package/runtime-requirements.txt（技能脚本补充依赖）。
# 用法：pwsh ./deb-package/pack_exe.ps1 [-Version 0.29.0]
# =============================================================================

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
    $versionRegex = [regex]'"version"\s*:\s*"[^"]*"'
    $pkgPatched = $versionRegex.Replace($pkgContent, "`"version`": `"$Version`"", 1)
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

# 内嵌运行时版本（三平台脚本保持一致）
$BundledPythonVersion = "3.11.9"
$BundledPythonRelease = "20240415"   # indygreg/python-build-standalone 发布批次
$BundledNodeVersion   = "20.18.3"

Write-Host "===========================================" -ForegroundColor Cyan
Write-Host " 构建 Windows amd64 安装包 (Inno Setup)" -ForegroundColor Cyan
Write-Host " 版本: $Version" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan

# 确保 TLS 1.2+ 可用（部分 Windows 默认仅 TLS 1.0）
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

# Electron / electron-builder 二进制统一走 npmmirror（GitHub 直连在国内易超时，
# winCodeSign/signtool 下载失败会导致 dist:win 在最后一步报错）
if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/" }
if (-not $env:ELECTRON_BUILDER_BINARIES_MIRROR) { $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/" }

# -----------------------------------------------------------------------------
# 1. 构建 Electron 桌面端（含原生安全 runtime）
# -----------------------------------------------------------------------------
if (-not (Test-Path ".\desktop\node_modules")) {
    Write-Host "正在安装 desktop 依赖..." -ForegroundColor Yellow
    Push-Location desktop
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm ci 失败 (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
}

Write-Host "正在构建 Electron 桌面端..." -ForegroundColor Yellow
Push-Location desktop
try {
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
    # npm 失败只设 $LASTEXITCODE，不抛异常。这里显式检查并中止。
    if ($LASTEXITCODE -ne 0) { throw "npm run dist:win 失败 (exit $LASTEXITCODE)，electron-builder 未生成 release/win-unpacked" }
    if (-not (Test-Path "release\win-unpacked")) {
        throw "electron-builder 未产出 release/win-unpacked，请检查 desktop/electron-builder.yml 与 package.json"
    }
} finally { Pop-Location }

# -----------------------------------------------------------------------------
# 1b. 裁剪 Electron locales：只保留 en-US / zh-CN，其余语言 pak 全部删除
# -----------------------------------------------------------------------------
$localesDir = Join-Path $ProjectRoot "desktop\release\win-unpacked\locales"
if (Test-Path $localesDir) {
    $keepLocales = @("en-US.pak", "zh-CN.pak")
    $removed = 0
    Get-ChildItem $localesDir -Filter "*.pak" |
        Where-Object { $keepLocales -notcontains $_.Name } |
        ForEach-Object { Remove-Item $_.FullName -Force; $removed++ }
    Write-Host "✓ Electron locales 裁剪完成（删除 $removed 个语言包，保留 $($keepLocales -join ', ')）" -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 2. 组装 staging：crew-desktop + crew-gateway 源码树
# -----------------------------------------------------------------------------
$stage = Join-Path $ProjectRoot "dist\windows-staging"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null

Write-Host "正在组装文件到 $stage..." -ForegroundColor Yellow
Copy-Item -Path "desktop\release\win-unpacked" -Destination (Join-Path $stage "crew-desktop") -Recurse

$stageGw = Join-Path $stage "crew-gateway"
New-Item -ItemType Directory -Path $stageGw | Out-Null

# gateway 源码树：robocopy 镜像复制，排除缓存与编译产物
# （robocopy 退出码 0-7 均为成功，仅 >=8 为失败）
function Invoke-RoboCopy([string]$Src, [string]$Dst, [string[]]$ExcludeDirs, [string[]]$ExcludeFiles) {
    $args = @($Src, $Dst, "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($ExcludeDirs) { $args += @("/XD") + $ExcludeDirs }
    if ($ExcludeFiles) { $args += @("/XF") + $ExcludeFiles }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败: $Src -> $Dst (exit $LASTEXITCODE)" }
}

Invoke-RoboCopy (Join-Path $ProjectRoot "crew") (Join-Path $stageGw "crew") `
    @("__pycache__", ".pytest_cache") @("*.pyc", "*.pyo")
Invoke-RoboCopy (Join-Path $ProjectRoot "plugins") (Join-Path $stageGw "plugins") `
    @("__pycache__", "node_modules", ".git") @("*.pyc", "*.pyo")

# 只携带可发布配置：两个 example 模板 + prompts/。
# 开发本机的 config/.env 与 config/config.yaml 绝不进入产物；
# 安装后由 gateway 首次运行时从 example 初始化用户私有文件（~/.Crew/）。
$stageCfg = Join-Path $stageGw "config"
New-Item -ItemType Directory -Path $stageCfg | Out-Null
Copy-Item (Join-Path $ProjectRoot "config\.env.example") $stageCfg -Force
Copy-Item (Join-Path $ProjectRoot "config\config.yaml.example") $stageCfg -Force
Invoke-RoboCopy (Join-Path $ProjectRoot "config\prompts") (Join-Path $stageCfg "prompts") @() @()

$leakedPrivate = Get-ChildItem $stageCfg -Force -Filter ".env*" | Where-Object { $_.Name -ne ".env.example" }
if ($leakedPrivate -or (Test-Path (Join-Path $stageCfg "config.yaml"))) {
    throw "安全检查失败: staging config 目录中不应出现本地 .env 或 config.yaml"
}
foreach ($required in @(".env.example", "config.yaml.example")) {
    if (-not (Test-Path (Join-Path $stageCfg $required))) {
        throw "构建失败: staging config 缺少 $required"
    }
}
Write-Host "✓ gateway 源码树与配置模板已入 staging" -ForegroundColor Green

# -----------------------------------------------------------------------------
# 3. 内嵌运行时（缓存于 dist/runtimes-cache，跨构建复用）
# -----------------------------------------------------------------------------
$runtimesDir = Join-Path $stageGw "runtimes"
New-Item -ItemType Directory -Path $runtimesDir -Force | Out-Null
$runtimesCache = Join-Path $ProjectRoot "dist\runtimes-cache"
New-Item -ItemType Directory -Path $runtimesCache -Force | Out-Null

# ---- 3a. Python (python-build-standalone install_only) ----
$pythonCacheDir = Join-Path $runtimesCache "python"
$pythonCacheExe = Join-Path $pythonCacheDir "python.exe"

if (-not (Test-Path $pythonCacheExe)) {
    Write-Host "正在下载 Python $BundledPythonVersion standalone..." -ForegroundColor Yellow
    $pyArchive = "cpython-$BundledPythonVersion+$BundledPythonRelease-x86_64-pc-windows-msvc-install_only.tar.gz"
    # GitHub 直连在国内易断，失败后回退 npmmirror 二进制镜像；可用环境变量覆盖整个列表
    $pyUrls = @(
        "https://github.com/indygreg/python-build-standalone/releases/download/$BundledPythonRelease/$pyArchive",
        "https://registry.npmmirror.com/-/binary/python-build-standalone/$BundledPythonRelease/$pyArchive"
    )
    if ($env:ACE_PYTHON_STANDALONE_URL) { $pyUrls = @($env:ACE_PYTHON_STANDALONE_URL) + $pyUrls }
    $pyTar = Join-Path $ProjectRoot "dist\python-standalone.tar.gz"
    $pyDownloaded = $false
    foreach ($u in $pyUrls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $pyTar -UseBasicParsing -TimeoutSec 300
            $pyDownloaded = $true
            break
        } catch {
            Write-Host "  下载失败，尝试下一个源: $u ($($_.Exception.Message))" -ForegroundColor Yellow
        }
    }
    if (-not $pyDownloaded) { throw "Python standalone 所有下载源均失败" }
    $tempExtract = Join-Path $ProjectRoot "dist\_python_extract"
    if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
    # install_only 归档内层为 python/ 目录，解出后整体移动到缓存目录。
    # 必须显式用系统 bsdtar：PATH 中 Git Bash 的 GNU tar 会把 D:\ 盘符当远程主机名解析
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) { $tarExe = "tar.exe" }
    & $tarExe -xzf $pyTar -C (Join-Path $ProjectRoot "dist")
    if ($LASTEXITCODE -ne 0) { throw "Python 归档解压失败 (exit $LASTEXITCODE)" }
    Remove-Item $pyTar -Force
    Move-Item (Join-Path $ProjectRoot "dist\python") $pythonCacheDir -Force
    if (-not (Test-Path $pythonCacheExe)) { throw "Python standalone 解压后未找到 python.exe" }
    Write-Host "✓ Python $BundledPythonVersion standalone 已缓存" -ForegroundColor Green
} else {
    Write-Host "✓ Python standalone 缓存已存在，跳过下载" -ForegroundColor Green
}

# 依赖同步：以 pyproject.toml + runtime-requirements.txt 的内容哈希为戳，
# 变更时才重新安装；不变时跳过（pip 全量 satisfied 检查也要分钟级，直接省掉）。
$depStampFile = Join-Path $pythonCacheDir ".deps-stamp"
$depHash = (Get-FileHash (Join-Path $ProjectRoot "pyproject.toml") -Algorithm SHA256).Hash +
           (Get-FileHash (Join-Path $PSScriptRoot "runtime-requirements.txt") -Algorithm SHA256).Hash
$depHash = [BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($depHash))).Replace("-", "")
$needDeps = -not (Test-Path $depStampFile) -or ((Get-Content $depStampFile -Raw).Trim() -ne $depHash)

if ($needDeps) {
    Write-Host "正在为内嵌 Python 安装 gateway + 技能依赖..." -ForegroundColor Yellow
    # 依赖清单变更时清空 site-packages 重装：pip 只增不删，清单里移除的包
    # （如 speech_recognition）若不清空会作为孤儿残留进安装包。
    $sitePackages = Join-Path $pythonCacheDir "Lib\site-packages"
    if (Test-Path $sitePackages) { Remove-Item $sitePackages -Recurse -Force }
    # pip 自身也在 site-packages 里，清空后需先用 ensurepip 恢复。
    # 注意不能加 2>&1：PS 5.1 下 $ErrorActionPreference=Stop 会把 ensurepip
    # 写往 stderr 的 WARNING 升级成 NativeCommandError。
    & $pythonCacheExe -m ensurepip --upgrade | Out-Null
    & $pythonCacheExe -m pip --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ensurepip 恢复 pip 失败" }
    # 1) 核心依赖 + 技能 extras（pyproject 为唯一数据源）；--no-warn-script-location 降噪
    & $pythonCacheExe -m pip install --no-warn-script-location ".[wiki,pdf,docx,xlsx]"
    if ($LASTEXITCODE -ne 0) { throw "pip install .[wiki,pdf,docx,xlsx] 失败 (exit $LASTEXITCODE)" }
    # 2) crew 源码由安装包按源码树携带，不复制进 site-packages
    & $pythonCacheExe -m pip uninstall -y crew | Out-Null
    # 3) 技能脚本补充依赖（pyproject 未覆盖部分）
    & $pythonCacheExe -m pip install --no-warn-script-location -r (Join-Path $PSScriptRoot "runtime-requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install runtime-requirements.txt 失败 (exit $LASTEXITCODE)" }
    # 4) markitdown 钉 0.0.2 并 --no-deps：0.1.x 顶层 import magika（拖入 onnxruntime 超 100MB）；
    #    0.0.2 的轻量硬依赖已在 runtime-requirements.txt 中显式列出
    & $pythonCacheExe -m pip install --no-warn-script-location --no-deps "markitdown==0.0.2"
    if ($LASTEXITCODE -ne 0) { throw "pip install markitdown 失败 (exit $LASTEXITCODE)" }
    Set-Content -Path $depStampFile -Value $depHash -NoNewline -Encoding ASCII
    Write-Host "✓ 内嵌 Python 依赖安装完成" -ForegroundColor Green
} else {
    Write-Host "✓ 内嵌 Python 依赖戳未变，跳过安装" -ForegroundColor Green
}

# 关键依赖自检：gateway 入口 + 技能高频依赖 + MCP
& $pythonCacheExe -c "import fastapi, uvicorn, openai, pandas, markitdown; from mcp.server.mcpserver import MCPServer; print('deps ok')"
if ($LASTEXITCODE -ne 0) { throw "内嵌 Python 关键依赖自检失败" }

# 清理缓存内的字节码缓存，减少文件数量与路径长度
Get-ChildItem -Path $pythonCacheDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 复制到 staging
$pythonRtDir = Join-Path $runtimesDir "python"
if (Test-Path $pythonRtDir) { Remove-Item $pythonRtDir -Recurse -Force }
Copy-Item -Path $pythonCacheDir -Destination $pythonRtDir -Recurse -Force
Write-Host "✓ Python 运行时已复制到 staging" -ForegroundColor Green

# ---- 3b. Node.js portable（裁剪 corepack/docs，保留 npm：docx/webapp 技能需要） ----
$nodeCacheDir = Join-Path $runtimesCache "node"
$nodeCacheExe = Join-Path $nodeCacheDir "node.exe"

if (-not (Test-Path $nodeCacheExe)) {
    Write-Host "正在下载 Node.js v$BundledNodeVersion portable..." -ForegroundColor Yellow
    $nodeZipUrl  = "https://nodejs.org/dist/v$BundledNodeVersion/node-v$BundledNodeVersion-win-x64.zip"
    $nodeZipPath = Join-Path $ProjectRoot "dist\node-portable.zip"
    Invoke-WebRequest -Uri $nodeZipUrl -OutFile $nodeZipPath -UseBasicParsing

    $tempExtract = Join-Path $ProjectRoot "dist\_node_extract"
    if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
    Expand-Archive -Path $nodeZipPath -DestinationPath $tempExtract -Force
    Remove-Item $nodeZipPath -Force

    $innerDir = Get-ChildItem -Path $tempExtract -Directory | Select-Object -First 1
    Move-Item -Path $innerDir.FullName -Destination $nodeCacheDir -Force
    Remove-Item $tempExtract -Recurse -Force

    if (-not (Test-Path $nodeCacheExe)) { throw "Node.js 解压后未找到 node.exe" }

    # 裁剪：corepack 与交互式 cmd 包装对运行时无用
    foreach ($junk in @("corepack", "corepack.cmd", "nodevars.bat", "install_tools.bat",
                        "node_etw_provider.man", "CHANGELOG.md", "README.md")) {
        $p = Join-Path $nodeCacheDir $junk
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
    $corepackDir = Join-Path $nodeCacheDir "node_modules\corepack"
    if (Test-Path $corepackDir) { Remove-Item $corepackDir -Recurse -Force }

    Write-Host "✓ Node.js v$BundledNodeVersion portable 已缓存并裁剪" -ForegroundColor Green
} else {
    Write-Host "✓ Node.js portable 缓存已存在，跳过下载" -ForegroundColor Green
}
$nodeRtDir = Join-Path $runtimesDir "node"
if (Test-Path $nodeRtDir) { Remove-Item $nodeRtDir -Recurse -Force }
Copy-Item -Path $nodeCacheDir -Destination $nodeRtDir -Recurse -Force
Write-Host "✓ Node.js 运行时已复制到 staging" -ForegroundColor Green

# -----------------------------------------------------------------------------
# 3c. 冒烟验证：用 staging 内的内嵌 Python 以源码模式导入 gateway 入口，
#     提前暴露依赖缺失 / 路径布局错误，而不是等安装后才在用户机器上炸。
# -----------------------------------------------------------------------------
Write-Host "正在冒烟验证 staging 内的 gateway..." -ForegroundColor Yellow
$env:PYTHONPATH = $stageGw
$env:CREW_PACKAGED = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
& (Join-Path $pythonRtDir "python.exe") -c "import crew.gateway.server; print('gateway import ok')"
$smokeResult = $LASTEXITCODE
Remove-Item Env:PYTHONPATH, Env:CREW_PACKAGED, Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
if ($smokeResult -ne 0) { throw "staging gateway 冒烟验证失败：内嵌 Python 无法导入 crew.gateway.server" }
Write-Host "✓ staging gateway 冒烟验证通过" -ForegroundColor Green

# -----------------------------------------------------------------------------
# 4. 动态生成 Inno Setup 脚本 (.iss)
# -----------------------------------------------------------------------------
Write-Host "正在生成 Inno Setup 配置文件..." -ForegroundColor Yellow
$issPath = Join-Path $ProjectRoot "dist\installer.iss"
$outputDir = Join-Path $ProjectRoot "dist"
$setupFileName = "Crew_Setup_v$Version"

# 确保存在 .ico 图标（Inno Setup SetupIconFile 必须使用 ICO 格式）
$iconPath = Join-Path $ProjectRoot "desktop\assets\icon.ico"

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
# 便于持续追踪包体变化。
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
; 兜底清理：Inno Setup 标准卸载可能因文件被锁定而跳过 runtimes
; 下的 Python/Node.js 运行时文件。显式声明确保卸载时强制尝试删除。
Type: filesandordirs; Name: "{app}\crew-gateway\runtimes"

[Run]
Filename: "{app}\crew-desktop\crew-desktop.exe"; Description: "{cm:LaunchProgram,Crew}"; Flags: nowait postinstall

"@

[System.IO.File]::WriteAllText($issPath, $issContent, [System.Text.Encoding]::UTF8)

# -----------------------------------------------------------------------------
# 5. 调用 Inno Setup 编译器 (ISCC) 进行最终打包
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
