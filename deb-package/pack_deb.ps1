param (
    [string]$Version = "",
    # 指定构建平台：UOS / Kylin。不指定则同时构建 UOS 和 Kylin 两个包。
    [ValidateSet("", "UOS", "Kylin")]
    [string]$Platform = ""
)

# PowerShell 跑 .ps1 时会把 cwd 切到脚本所在目录（$PSScriptRoot），
# 切回项目根（脚本父目录），下面所有相对路径才解析正确
Set-Location (Split-Path -Parent $PSScriptRoot)

# 若未显式传入版本号，从 deb-package/version.txt 读取
if (-not $Version) {
    $Version = (Get-Content (Join-Path $PSScriptRoot "version.txt") -Raw).Trim()
}

# 确定待构建的平台列表
if ($Platform) {
    $platforms = @($Platform)
} else {
    $platforms = @("UOS", "Kylin")
}

$ErrorActionPreference = "Stop"

Write-Host "===========================================" -ForegroundColor Cyan
Write-Host " Crew Linux 安装包构建" -ForegroundColor Cyan
Write-Host " 版本: $Version" -ForegroundColor Cyan
Write-Host " 平台: $($platforms -join ', ')" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan

$builtPackages = @()

foreach ($plat in $platforms) {
    $platLabel = "$plat amd64"
    $platLower = $plat.ToLower()
    $debName = "crew-desktop_${Version}_${platLower}_amd64.deb"

    Write-Host "" -ForegroundColor Cyan
    Write-Host "--- 构建 $platLabel ($debName) ---" -ForegroundColor Cyan

    # 1. Build Docker image (每次传入 BUILD_PLATFORM 确保 package.json 含正确平台标识)
    $dockerArgs = @("build", "--build-arg", "BUILD_VERSION=$Version", "--build-arg", "BUILD_PLATFORM=$platLabel")
    $dockerArgs += @("-t", "crew-pack-$($plat.ToLower())", "-f", "Dockerfile.pack", ".")
    docker @dockerArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker build failed for $plat!"
        exit 1
    }

    # 2. Create temporary container
    $containerName = "crew-pack-$($plat.ToLower())-temp-$(Get-Random)"
    docker create --name $containerName "crew-pack-$($plat.ToLower())"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Create temporary container failed for $plat!"
        exit 1
    }

    # 3. Extract the package (Dockerfile 内部产物名固定为 crew-desktop_${Version}_amd64.deb，这里重命名)
    $internalDebName = "crew-desktop_${Version}_amd64.deb"
    Write-Host "Extracting $debName ..." -ForegroundColor Green
    docker cp "${containerName}:/${internalDebName}" "./${debName}"
    $cpResult = $LASTEXITCODE
    docker cp "${containerName}:/package-size-report-linux.txt" "./package-size-report-${platLower}.txt"
    $reportResult = $LASTEXITCODE

    # 4. Remove container
    docker rm $containerName | Out-Null

    if (($cpResult -eq 0) -and ($reportResult -eq 0)) {
        Write-Host "✓ $plat amd64 构建成功: ./$debName" -ForegroundColor Green
        $builtPackages += $debName
    } else {
        Write-Error "Extract package failed for $plat!"
        exit 1
    }
}

# 汇总输出
Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host " 全部构建完成！" -ForegroundColor Green
foreach ($pkg in $builtPackages) {
    Write-Host "   $pkg" -ForegroundColor Green
}
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
