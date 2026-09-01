[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$path = Join-Path $root 'scripts\verify_windows.ps1'
$text = Get-Content -LiteralPath $path -Raw

foreach ($needle in @(
    '[string]$InstallRoot',
    '[string]$ReceiptPath',
    '[string]$FixturePath',
    '[string]$PopplerPath',
    '[Nullable[int]]$ApiPort',
    '[string]$ExpectedRepository',
    '[string]$ExpectedCommit',
    '[string]$ExpectedTree',
    '[string]$ExpectedManifestSha256',
    '$requestedApiPort = $ApiPort',
    '-RequestedApiPort $requestedApiPort',
    '-ExpectedApiPort $apiPort',
    'Push-Location',
    'Invoke-NativeChecked',
    'LASTEXITCODE',
    'stdout',
    'stderr',
    'publication_gate',
    'advisory_test_debt',
    'FAIL_PREFLIGHT',
    'FAIL_API',
    'FAIL_WORKER',
    'FAIL_RENDERER',
    'FAIL_NATIVE_E2E',
    'Write-JsonReceipt',
    'Write-VerifierTerminalSummary',
    'exit $exitCode'
)) {
    if (-not $text.Contains($needle)) { throw "Missing verifier contract '$needle'" }
}

if ($text -match 'Select-String.*PASS') { throw 'Verifier must not infer verdict from transcript text' }

foreach ($needle in @(
    'identity_binding.verified',
    '-ExpectedRepository $ExpectedRepository',
    '-ExpectedCommit $ExpectedCommit',
    '-ExpectedTree $ExpectedTree',
    '-ExpectedManifestSha256 $ExpectedManifestSha256'
)) {
    if (-not $text.Contains($needle)) { throw "Missing independent source identity contract '$needle'" }
}
Write-Output 'VERIFIER_CONTRACT=PASS'
