[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Equal {
    param([object]$Expected, [object]$Actual, [string]$Message)
    if ($Expected -ne $Actual) {
        throw "$Message (expected='$Expected', actual='$Actual')"
    }
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g13-receipt-unicode-' + [Guid]::NewGuid().ToString('N'))
$unicodeDirectoryName = ([char]0xD6C4).ToString() + ([char]0xBCF4).ToString() + ([char]0xC124).ToString() + ([char]0xCE58).ToString() + ([char]0xD55C).ToString() + ([char]0xAE00).ToString()
$expectedMarker = ([char]0xC124).ToString() + ([char]0xCE58).ToString() + '-' + ([char]0xACBD).ToString() + ([char]0xB85C).ToString()
$unicodeRoot = Join-Path $tempRoot $unicodeDirectoryName
$nativeScript = Join-Path $unicodeRoot 'emit-legacy-output.py'
$nativeReceipt = Join-Path $tempRoot 'native-receipt.json'
$textBoundaryScript = Join-Path $unicodeRoot 'emit-text-boundary.py'
$textBoundaryReceipt = Join-Path $tempRoot 'text-boundary-receipt.json'
$verifierStdout = Join-Path $tempRoot 'verifier.stdout.txt'
$verifierStderr = Join-Path $tempRoot 'verifier.stderr.txt'
$verifierProcess = $null

try {
    New-Item -ItemType Directory -Force -Path $unicodeRoot | Out-Null

    $installerPath = Join-Path $root 'scripts\install_windows.ps1'
    $installerText = [System.IO.File]::ReadAllText($installerPath, [System.Text.Encoding]::UTF8)
    $selfVerifyStart = $installerText.IndexOf('$verifyScript = Join-Path $candidateRoot')
    $selfVerifyEnd = $installerText.IndexOf('$receipt.status =', $selfVerifyStart)
    Assert-True ($selfVerifyStart -ge 0 -and $selfVerifyEnd -gt $selfVerifyStart) 'Installer self-verification block was not found.'
    $selfVerify = $installerText.Substring($selfVerifyStart, $selfVerifyEnd - $selfVerifyStart)
    Assert-True ($selfVerify.Contains('$verifyReceipt = Join-Path ([System.IO.Path]::GetTempPath())')) 'Installer verifier receipt is not allocated externally.'
    Assert-True (-not $selfVerify.Contains("Join-Path $candidateRoot 'receipts\verify.json'")) 'Installer still passes an in-root verifier receipt.'
    Assert-True ($installerText.Contains('Test-CanonicalPathWithinRoot -Path $verifyReceipt -Root $candidateRoot')) 'Installer does not re-check verifier receipt containment.'

    $verifierPath = Join-Path $root 'scripts\verify_windows.ps1'
    $verifierText = [System.IO.File]::ReadAllText($verifierPath, [System.Text.Encoding]::UTF8)
    Assert-True ($verifierText.Contains('Test-CanonicalPathWithinRoot')) 'Verifier containment guard was removed.'
    Assert-True ($verifierText.Contains('ReceiptPath must be outside the existing InstallRoot')) 'Verifier containment failure class is not preserved.'

    $fixtureSource = @'
import sys

marker = chr(0xC124) + chr(0xCE58) + "-" + chr(0xACBD) + chr(0xB85C)
sys.stdout.buffer.write((marker + "\n").encode("cp949"))
sys.stdout.flush()
'@
    [System.IO.File]::WriteAllText($nativeScript, $fixtureSource, (New-Object System.Text.UTF8Encoding($false)))
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $result = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($nativeScript) `
        -WorkingDirectory $unicodeRoot `
        -AllowNonZero `
        -MaxOutputBytes 1024 `
        -ReceiptPath $nativeReceipt

    Assert-Equal 0 ([int]$result.exit_code) 'Code-page native command returned an unexpected exit code.'
    Assert-True ([bool]$result.accepted) 'Code-page native output caused a false native failure.'
    Assert-True ([bool]$result.stdout_decode_fallback) 'Code-page output did not record the decoder fallback.'
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$result.stdout_decode_error)) 'Fallback did not retain the strict UTF-8 diagnostic.'
    Assert-True ([string]::IsNullOrWhiteSpace([string]$result.capture_error)) 'Decoder fallback was incorrectly reported as a capture failure.'
    Assert-True ([int64]$result.stdout_bytes -gt 0) 'Code-page output byte count was not captured.'
    Assert-True ([int64]$result.stdout_captured_bytes -le 1024) 'Code-page output exceeded the byte capture limit.'
    if ([System.Text.Encoding]::Default.CodePage -eq 949) {
        Assert-True ([string]$result.stdout -match [regex]::Escape($expectedMarker)) 'CP949 output was not decoded with the active Windows code page.'
    }

    $receiptPayload = ConvertFrom-Json -InputObject ([string](Read-BoundedText -Path $nativeReceipt).text)
    Assert-True ([bool]$receiptPayload.accepted) 'Native receipt did not preserve accepted=true.'
    Assert-True ([bool]$receiptPayload.stdout_decode_fallback) 'Native receipt lost decoder fallback evidence.'
    Assert-True ([int64]$receiptPayload.stdout_captured_bytes -le 1024) 'Native receipt exceeded the byte capture bound.'

    $textBoundarySource = @'
import sys

sys.stdout.buffer.write(b"Z" * 70000)
sys.stdout.flush()
'@
    [System.IO.File]::WriteAllText($textBoundaryScript, $textBoundarySource, (New-Object System.Text.UTF8Encoding($false)))
    $textBoundary = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @($textBoundaryScript) `
        -WorkingDirectory $unicodeRoot `
        -AllowNonZero `
        -MaxOutputBytes 131072 `
        -ReceiptPath $textBoundaryReceipt
    Assert-Equal 70000 ([int64]$textBoundary.stdout_bytes) 'Text-boundary fixture did not preserve the observed byte count.'
    Assert-Equal 70000 ([int64]$textBoundary.stdout_captured_bytes) 'Text-boundary fixture did not preserve the captured byte count.'
    Assert-True ([string]$textBoundary.stdout).Length -le 65536 'Native text capture exceeded the character bound.'
    Assert-True ([bool]$textBoundary.stdout_truncated) 'Native text capture did not report character truncation.'
    Assert-True ([bool]$textBoundary.accepted) 'Text-boundary truncation caused a false native failure.'

    $existingInstall = Join-Path $unicodeRoot 'existing-install'
    $inRootReceipt = Join-Path $existingInstall 'receipts\verify.json'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $inRootReceipt) | Out-Null
    Assert-True (Test-CanonicalPathWithinRoot -Path $inRootReceipt -Root $existingInstall) 'Containment helper accepted no in-root receipt.'
    Assert-True (-not (Test-CanonicalPathWithinRoot -Path $nativeReceipt -Root $existingInstall)) 'Containment helper rejected an external receipt.'

    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $verifierArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $verifierPath,
        '-InstallRoot',
        $existingInstall,
        '-ReceiptPath',
        $inRootReceipt
    )
    $verifierProcess = Start-Process `
        -FilePath $powershell `
        -ArgumentList $verifierArguments `
        -RedirectStandardOutput $verifierStdout `
        -RedirectStandardError $verifierStderr `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    Assert-Equal 20 ([int]$verifierProcess.ExitCode) 'Verifier did not fail closed for an in-root receipt.'
    Assert-True (-not (Test-Path -LiteralPath $inRootReceipt -PathType Leaf)) 'Verifier wrote an in-root receipt despite the containment guard.'
    $summary = [System.IO.File]::ReadAllText($verifierStdout, [System.Text.Encoding]::UTF8)
    Assert-True ($summary.Contains('ReceiptPath must be outside the existing InstallRoot')) 'Verifier summary omitted the containment failure.'

    Write-Output 'G13_RECEIPT_UNICODE=PASS'
}
finally {
    if ($null -ne $verifierProcess -and -not $verifierProcess.HasExited) {
        Stop-Process -Id $verifierProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
