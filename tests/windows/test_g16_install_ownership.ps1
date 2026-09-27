[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force
$installer = Join-Path $root 'scripts\install_windows.ps1'
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g16-ownership-' + [Guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $tempRoot 'existing-install'
$receiptPath = Join-Path $tempRoot 'receipt.json'
$envPath = Join-Path $installRoot '.env'
$sentinelPath = Join-Path $installRoot 'pre-existing.bin'
$hadFault = Test-Path Env:HWPX_TEST_INSTALL_FAULT
$previousFault = $env:HWPX_TEST_INSTALL_FAULT
try {
    New-Item -ItemType Directory -Path $installRoot -ErrorAction Stop | Out-Null
    [IO.File]::WriteAllText($envPath, "HWP_API_PORT=19991`r`nPREIMAGE=keep-me`r`n", (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllBytes($sentinelPath, [Text.Encoding]::UTF8.GetBytes('untouched-install-preimage'))
    $envHashBefore = Get-Sha256Hex -Path $envPath
    $sentinelHashBefore = Get-Sha256Hex -Path $sentinelPath
    $envBytesBefore = [int64](Get-Item -LiteralPath $envPath).Length
    $sentinelBytesBefore = [int64](Get-Item -LiteralPath $sentinelPath).Length

    $env:HWPX_TEST_INSTALL_FAULT = 'after-existing-root-compatibility'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -SourceRoot $root -InstallRoot $installRoot -DependencyMode CheckOnly -ExistingInstallDisposition Fail -ReceiptPath $receiptPath
    $installerExitCode = [int]$LASTEXITCODE
    if ($installerExitCode -eq 0) { throw 'The injected incompatible-root install unexpectedly succeeded.' }

    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) { throw 'The pre-existing install root was deleted.' }
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf) -or -not (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) { throw 'A pre-existing install file was deleted.' }
    if ((Get-Sha256Hex -Path $envPath) -ne $envHashBefore -or (Get-Sha256Hex -Path $sentinelPath) -ne $sentinelHashBefore) { throw 'A pre-existing install file changed.' }
    if ([int64](Get-Item -LiteralPath $envPath).Length -ne $envBytesBefore -or [int64](Get-Item -LiteralPath $sentinelPath).Length -ne $sentinelBytesBefore) { throw 'A pre-existing install file size changed.' }
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw 'The external terminal receipt was not persisted.' }
    $receipt = [IO.File]::ReadAllText($receiptPath) | ConvertFrom-Json
    if ([string]$receipt.status -eq 'PASS' -or [string]$receipt.status -eq 'PASS_RUNTIME_ONLY') { throw 'A failed pre-existing-root install reported success.' }
    Write-Output ("G16_INSTALL_ROOT_PREIMAGE=PASS;exit_code={0};env_sha256={1};sentinel_sha256={2}" -f $installerExitCode, $envHashBefore, $sentinelHashBefore)
}
finally {
    if ($hadFault) { $env:HWPX_TEST_INSTALL_FAULT = $previousFault }
    else { Remove-Item Env:HWPX_TEST_INSTALL_FAULT -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $tempRoot -PathType Container) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction Stop }
}
