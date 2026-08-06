param([Parameter(Mandatory = $true)][string]$Runtime)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$state = Join-Path ([System.IO.Path]::GetTempPath()) ("ace-security-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $state | Out-Null
try {
    & $runtimePath --windows-setup $state
    if ($LASTEXITCODE -ne 0) { throw 'WIN-INSTALL-001 initial setup failed' }
    & $runtimePath --windows-setup $state
    if ($LASTEXITCODE -ne 0) { throw 'WIN-INSTALL-002 repair was not idempotent' }
    if (-not (Test-Path (Join-Path $state 'windows-sandbox-identity.json'))) {
        throw 'WIN-INSTALL-003 identity state missing'
    }
    $allowedSids = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    [void]$allowedSids.Add([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    [void]$allowedSids.Add('S-1-5-18') # SYSTEM
    [void]$allowedSids.Add('S-1-5-32-544') # Builtin Administrators
    foreach ($protectedPath in @($state, (Join-Path $state 'windows-sandbox-identity.json'))) {
        $acl = Get-Acl -LiteralPath $protectedPath
        if (-not $acl.AreAccessRulesProtected) {
            throw "WIN-INSTALL-005 DACL inheritance is not protected: $protectedPath"
        }
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            if (($rule.AccessControlType -ne 'Allow') -or (-not $allowedSids.Contains($sid))) {
                throw "WIN-INSTALL-006 unexpected state principal ${sid}: $protectedPath"
            }
        }
    }
} finally {
    & $runtimePath --windows-uninstall $state
    if ($LASTEXITCODE -ne 0) { throw 'WIN-INSTALL-004 uninstall failed' }
    Remove-Item -LiteralPath $state -Recurse -Force -ErrorAction SilentlyContinue
}
