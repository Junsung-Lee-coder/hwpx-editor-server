[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Assert-Contains {
    param([string]$Text, [string]$Needle, [string]$File)
    if (-not $Text.Contains($Needle)) {
        throw "Missing contract '$Needle' in $File"
    }
}

$installerPath = Join-Path $root 'scripts\install_windows.ps1'
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
$installer = Get-Content -LiteralPath $installerPath -Raw
$common = Get-Content -LiteralPath $commonPath -Raw

foreach ($needle in @(
    '[string]$SourceRoot',
    '[string]$InstallRoot',
    "[ValidateSet('CheckOnly', 'InstallUserScope')]",
    '[string]$FixturePath',
    "[ValidateSet('Fail', 'PreserveMove')]",
    '[switch]$ReplaceExistingTasks',
    '[string]$ReceiptPath',
    '[Nullable[int]]$ApiPort',
    '$requestedApiPort = $ApiPort',
    '-RequestedApiPort $requestedApiPort',
    '-ErrorVariable +lookupErrors',
    'ObjectNotFound',
    'Invoke-NativeChecked',
    'Get-HancomComIdentity',
    'Get-SourceManifest',
    'Save-InstallSnapshot',
    'Register-ScheduledTask',
    'Restore-InstallSnapshot',
    'config.example',
    'HWP_PDFTOPPM',
    '[string]$PopplerPath',
    '[string]$ExpectedRepository',
    '[string]$ExpectedCommit',
    '[string]$ExpectedTree',
    '[string]$ExpectedManifestSha256',
    'New-PythonInvocation',
    "-LogonType Interactive",
    "-PopplerPath",
    "-AllowStartIfOnBatteries",
    "-DontStopIfGoingOnBatteries",
    "-RunOnlyIfNetworkAvailable:`$false",
    "-Hidden:`$false"
)) {
    Assert-Contains -Text $installer -Needle $needle -File $installerPath
}

foreach ($needle in @(
    'function Get-CanonicalPath',
    'function Invoke-NativeChecked',
    'function Get-SourceManifest',
    'function Get-ScheduledTaskIdentity',
    'Test-ScheduledTaskLogonTypeEquivalent',
    'Test-ScheduledTaskRunLevelEquivalent',
    'function Write-JsonReceipt',
    'function Restore-InstallSnapshot'
)) {
    Assert-Contains -Text $common -Needle $needle -File $commonPath
}

foreach ($needle in @(
    '-ExpectedRepository $ExpectedRepository',
    '-ExpectedCommit $ExpectedCommit',
    '-ExpectedTree $ExpectedTree',
    '-ExpectedManifestSha256 $ExpectedManifestSha256'
)) {
    Assert-Contains -Text $installer $Needle -File $installerPath
}

foreach ($needle in @(
    "identitySource -eq 'asserted-gitless'",
    'externalIdentityCount -ne 3',
    'identityBindingVerified'
)) {
    Assert-Contains -Text $common $Needle -File $commonPath
}

if ($installer -match 'sample-config\.env') { throw 'stale sample configuration filename reference' }

# Exercise the production cleanup function itself under Windows PowerShell
# 5.1. PreserveMove clears the claim path after successful cleanup; the later
# rollback finally block must therefore tolerate an absent claim and identity,
# while still rejecting an existing claim without an identity.
$tokens = $null
$parseErrors = $null
$installerAst = [System.Management.Automation.Language.Parser]::ParseInput($installer, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {
    throw ("Could not parse installer while testing rollback claim cleanup: {0}" -f (($parseErrors | ForEach-Object Message) -join '; '))
}
$claimFunctions = @(
    $installerAst.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Remove-RunPathClaim'
        },
        $true
    )
)
if ($claimFunctions.Count -ne 1) { throw "Expected exactly one Remove-RunPathClaim definition, found $($claimFunctions.Count)." }
$claimFunctionText = [string]$claimFunctions[0].Extent.Text
if (-not $claimFunctionText.Contains('[AllowEmptyString()]')) {
    throw 'Remove-RunPathClaim does not allow the empty identity used by an already-cleared claim cleanup.'
}
Invoke-Expression $claimFunctionText
try {
    Remove-RunPathClaim -ClaimPath '' -ExpectedObjectIdentity ''
    Remove-RunPathClaim -ClaimPath $null -ExpectedObjectIdentity $null
}
catch {
    throw "Absent run-path claim cleanup raised an unexpected binding/runtime error: $($_.Exception.Message)"
}

$claimProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-claim-cleanup-' + [Guid]::NewGuid().ToString('N') + '.claim')
try {
    [System.IO.File]::WriteAllText($claimProbePath, 'claim-probe')
    $identityRejectionObserved = $false
    try {
        Remove-RunPathClaim -ClaimPath $claimProbePath -ExpectedObjectIdentity ''
    }
    catch {
        if ($_.Exception.Message -notmatch 'stable object identity') {
            throw "Existing run-path claim failed for an unexpected reason: $($_.Exception.Message)"
        }
        $identityRejectionObserved = $true
    }
    if (-not $identityRejectionObserved) {
        throw 'Existing run-path claim was removable without a stable object identity.'
    }
}
finally {
    if (Test-Path -LiteralPath $claimProbePath -PathType Leaf) {
        Remove-Item -LiteralPath $claimProbePath -Force -ErrorAction SilentlyContinue
    }
}
Write-Output 'INSTALLER_ROLLBACK_CLAIM_CLEANUP=PASS'
Write-Output 'INSTALLER_CONTRACT=PASS'
