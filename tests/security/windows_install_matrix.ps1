param([Parameter(Mandatory = $true)][string]$Runtime)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path

function Invoke-Setup([string]$State, [string]$FailureCode) {
    & $runtimePath --windows-setup $State
    if ($LASTEXITCODE -ne 0) { throw $FailureCode }
}

function Invoke-Uninstall([string]$State, [string]$FailureCode) {
    & $runtimePath --windows-uninstall $State
    if ($LASTEXITCODE -ne 0) { throw $FailureCode }
}

function Assert-Ready([string]$State, [string]$FailureCode) {
    & $runtimePath --windows-check-setup $State
    if ($LASTEXITCODE -ne 0) { throw $FailureCode }
}

function Assert-NotReady([string]$State, [string]$FailureCode) {
    & $runtimePath --windows-check-setup $State
    if ($LASTEXITCODE -eq 0) { throw $FailureCode }
}

function Assert-ProtectedState([string]$State, [string]$FailurePrefix) {
    $identityPath = Join-Path $State 'windows-sandbox-identity.json'
    if (-not (Test-Path -LiteralPath $identityPath)) {
        throw "$FailurePrefix identity state missing"
    }
    $allowedSids = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    [void]$allowedSids.Add([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    [void]$allowedSids.Add('S-1-5-18') # SYSTEM
    [void]$allowedSids.Add('S-1-5-32-544') # Builtin Administrators
    foreach ($protectedPath in @($State, $identityPath)) {
        $acl = Get-Acl -LiteralPath $protectedPath
        if (-not $acl.AreAccessRulesProtected) {
            throw "$FailurePrefix DACL inheritance is not protected: $protectedPath"
        }
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            if (($rule.AccessControlType -ne 'Allow') -or (-not $allowedSids.Contains($sid))) {
                throw "$FailurePrefix unexpected state principal ${sid}: $protectedPath"
            }
        }
    }
}

function Invoke-IdentityCycle([bool]$removeAFirst, [string]$FailurePrefix) {
    $root = Join-Path ([System.IO.Path]::GetTempPath()) (
        'ace-security-' + [guid]::NewGuid().ToString('N')
    )
    $stateA = Join-Path $root 'profile-a'
    $stateB = Join-Path $root 'profile-b'
    New-Item -ItemType Directory -Path $stateA -Force | Out-Null
    New-Item -ItemType Directory -Path $stateB -Force | Out-Null
    try {
        Invoke-Setup $stateA "$FailurePrefix profile A initial setup failed"
        Invoke-Setup $stateB "$FailurePrefix profile B setup replaced profile A policy"
        Assert-Ready $stateA "$FailurePrefix profile A stopped being ready after B setup"
        Assert-Ready $stateB "$FailurePrefix profile B is not ready after setup"

        Invoke-Setup $stateA "$FailurePrefix profile A repair failed"
        Assert-Ready $stateB "$FailurePrefix profile A repair replaced profile B policy"
        Invoke-Setup $stateB "$FailurePrefix profile B repair failed"
        Assert-Ready $stateA "$FailurePrefix profile B repair replaced profile A policy"
        Assert-ProtectedState $stateA "$FailurePrefix profile A"
        Assert-ProtectedState $stateB "$FailurePrefix profile B"

        $identityA = Get-Content -LiteralPath (Join-Path $stateA 'windows-sandbox-identity.json') -Raw |
            ConvertFrom-Json
        $identityB = Get-Content -LiteralPath (Join-Path $stateB 'windows-sandbox-identity.json') -Raw |
            ConvertFrom-Json
        if ($identityA.username -eq $identityB.username -or
            $identityA.online_username -eq $identityB.online_username) {
            throw "$FailurePrefix profile identities were unexpectedly shared"
        }

        if ($removeAFirst) {
            Invoke-Uninstall $stateA "$FailurePrefix profile A uninstall failed"
            Assert-NotReady $stateA "$FailurePrefix profile A remained ready after uninstall"
            Assert-Ready $stateB "$FailurePrefix uninstalling A removed profile B policy"
            Invoke-Uninstall $stateB "$FailurePrefix profile B uninstall failed"
        } else {
            Invoke-Uninstall $stateB "$FailurePrefix profile B uninstall failed"
            Assert-NotReady $stateB "$FailurePrefix profile B remained ready after uninstall"
            Assert-Ready $stateA "$FailurePrefix uninstalling B removed profile A policy"
            Invoke-Uninstall $stateA "$FailurePrefix profile A uninstall failed"
        }
        Assert-NotReady $stateA "$FailurePrefix profile A remained ready after cleanup"
        Assert-NotReady $stateB "$FailurePrefix profile B remained ready after cleanup"
    } finally {
        Invoke-Uninstall $stateA "$FailurePrefix cleanup of profile A failed"
        Invoke-Uninstall $stateB "$FailurePrefix cleanup of profile B failed"
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Invoke-IdentityCycle $true 'WIN-INSTALL-010'
Invoke-IdentityCycle $false 'WIN-INSTALL-020'
