$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $python) {
    $python = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $python) {
    throw 'Python 3 is required to build the security runtime'
}

& $python.Source (Join-Path $scriptDir 'build-security-runtime.py') @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
