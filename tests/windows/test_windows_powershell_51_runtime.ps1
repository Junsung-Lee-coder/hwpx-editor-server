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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-ps51-runtime-' + [Guid]::NewGuid().ToString('N'))
$candidateRoot = Join-Path $tempRoot 'candidate-install'
$nonEmptyPath = Join-Path $candidateRoot 'proof.manifest.json'
$emptyPath = Join-Path $candidateRoot 'empty.manifest.json'
$pythonProcess = $null
$verifierProcess = $null
try {
    New-Item -ItemType Directory -Force -Path $candidateRoot | Out-Null
    [IO.File]::WriteAllText($nonEmptyPath, '{"ok":true}', (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($emptyPath, '')
    if (-not (Test-NonEmptyFile -Path $nonEmptyPath)) { throw 'Non-empty proof manifest was not detected.' }
    if (Test-NonEmptyFile -Path $emptyPath) { throw 'Empty proof manifest was accepted.' }
    if (Test-NonEmptyFile -Path (Join-Path $candidateRoot 'missing.manifest.json')) { throw 'Missing proof manifest was accepted.' }

    $existingCanonicalPath = Get-CanonicalPath -Path $candidateRoot -RequireExisting
    $expectedExistingCanonicalPath = [IO.Path]::GetFullPath($candidateRoot).TrimEnd([char]92, [char]47)
    if ($existingCanonicalPath -cne $expectedExistingCanonicalPath) {
        throw "Existing canonical path normalization failed: $existingCanonicalPath"
    }
    $futurePath = Join-Path $tempRoot 'future-path'
    $futureCanonicalPath = Get-CanonicalPath -Path $futurePath
    $expectedFutureCanonicalPath = [IO.Path]::GetFullPath($futurePath).TrimEnd([char]92, [char]47)
    if ($futureCanonicalPath -cne $expectedFutureCanonicalPath) {
        throw "Future canonical path normalization failed: $futureCanonicalPath"
    }

    $installerPath = Join-Path $root 'scripts\install_windows.ps1'
    $installerText = Get-Content -LiteralPath $installerPath -Raw -Encoding UTF8
    $envFunctionStart = $installerText.IndexOf('function Assert-ExistingEnvPreimage')
    $envFunctionEnd = $installerText.IndexOf('function Ensure-CandidateConfig', $envFunctionStart)
    if ($envFunctionStart -lt 0 -or $envFunctionEnd -le $envFunctionStart) { throw 'Existing .env preimage function was not found.' }
    Invoke-Expression $installerText.Substring($envFunctionStart, $envFunctionEnd - $envFunctionStart)
    $envPreimagePath = Join-Path $tempRoot 'existing.env'
    $envPreimageBytes = [Text.Encoding]::UTF8.GetBytes("HWP_API_PORT=18765`r`n")
    [IO.File]::WriteAllBytes($envPreimagePath, $envPreimageBytes)
    $envPreimageSize = [int64](Get-Item -LiteralPath $envPreimagePath).Length
    $envPreimageHash = Get-Sha256Hex -Path $envPreimagePath
    Assert-ExistingEnvPreimage -Path $envPreimagePath -Exists $true -ExpectedSize $envPreimageSize -ExpectedSha256 $envPreimageHash | Out-Null
    foreach ($envFaultMode in @('mismatch', 'race')) {
        $env:HWPX_TEST_ENV_FAULT = $envFaultMode
        $envFaultCaught = $false
        try {
            Assert-ExistingEnvPreimage -Path $envPreimagePath -Exists $true -ExpectedSize $envPreimageSize -ExpectedSha256 $envPreimageHash | Out-Null
        }
        catch { $envFaultCaught = $true }
        if (-not $envFaultCaught) { throw "Injected .env $envFaultMode fault did not fail closed." }
    }
    Remove-Item Env:HWPX_TEST_ENV_FAULT -ErrorAction SilentlyContinue
    [IO.File]::WriteAllBytes($envPreimagePath, [Text.Encoding]::UTF8.GetBytes("HWP_API_PORT=19999`r`n"))
    $envMismatchCaught = $false
    try {
        Assert-ExistingEnvPreimage -Path $envPreimagePath -Exists $true -ExpectedSize $envPreimageSize -ExpectedSha256 $envPreimageHash | Out-Null
    }
    catch { $envMismatchCaught = $true }
    if (-not $envMismatchCaught) { throw 'An actual existing .env mismatch was not rejected.' }

    $verifierPath = Join-Path $root 'scripts\verify_windows.ps1'
    $verifierText = Get-Content -LiteralPath $verifierPath -Raw -Encoding UTF8
    $fixtureRootFunctionStart = $verifierText.IndexOf('function New-VerifierFixtureTempRoot')
    $fixtureRootFunctionEnd = $verifierText.IndexOf('function Invoke-FixtureSequence', $fixtureRootFunctionStart)
    if ($fixtureRootFunctionStart -lt 0 -or $fixtureRootFunctionEnd -le $fixtureRootFunctionStart) {
        throw 'Verifier fixture temporary-root function was not found.'
    }
    Invoke-Expression $verifierText.Substring($fixtureRootFunctionStart, $fixtureRootFunctionEnd - $fixtureRootFunctionStart)
    $fixtureTempRoot = $null
    try {
        $fixtureTempRoot = New-VerifierFixtureTempRoot
        $fixtureTempRootFull = [IO.Path]::GetFullPath($fixtureTempRoot).TrimEnd([char[]]@([char]92, [char]47))
        $userTempFull = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@([char]92, [char]47))
        if (-not $fixtureTempRootFull.StartsWith($userTempFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Verifier fixture temp root escaped the current user's temp directory: $fixtureTempRoot"
        }
        $windowsTempFull = [IO.Path]::GetFullPath((Join-Path $env:windir 'Temp')).TrimEnd([char[]]@([char]92, [char]47))
        if ($fixtureTempRootFull.StartsWith($windowsTempFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Verifier fixture temp root still uses the Windows directory temp path: $fixtureTempRoot"
        }
        $markerPath = Join-Path $fixtureTempRoot 'ps51-marker.txt'
        [IO.File]::WriteAllText($markerPath, 'powershell-51-fixture-root')
        if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
            throw 'PowerShell 5.1 could not write inside the verifier fixture temp root.'
        }
        Remove-Item -LiteralPath $markerPath -Force -ErrorAction Stop
        Write-Output 'VERIFIER_FIXTURE_TEMP_ROOT=PASS'
    }
    finally {
        if ($fixtureTempRoot -and (Test-Path -LiteralPath $fixtureTempRoot)) {
            Remove-Item -LiteralPath $fixtureTempRoot -Recurse -Force -ErrorAction Stop
        }
        if ($fixtureTempRoot -and (Test-Path -LiteralPath $fixtureTempRoot)) {
            throw "PowerShell 5.1 fixture temp-root cleanup left residue: $fixtureTempRoot"
        }
    }

    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $pythonArguments = '-c "import time; time.sleep(60)" "' + $candidateRoot + '" app.worker'
    $pythonProcess = Start-Process -FilePath $python -ArgumentList $pythonArguments -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 500
    $before = @(Get-InstallProcessSnapshot -RootPath $candidateRoot -ExpectedPythonPath $python)
    if (@($before | Where-Object { [int]$_.process_id -eq $pythonProcess.Id }).Count -ne 1) {
        throw "Synthetic candidate process was not found in the snapshot: $($pythonProcess.Id)"
    }
    $stopped = Stop-InstallProcesses -RootPath $candidateRoot -PreserveProcessIds @() -ExpectedPythonPath $python
    if (@($stopped.remaining).Count -ne 0) {
        throw "Candidate process handles remain after stop: $($stopped.remaining -join ',')"
    }
    if (Get-Process -Id $pythonProcess.Id -ErrorAction SilentlyContinue) {
        throw "Candidate process remains after handle release: $($pythonProcess.Id)"
    }

    $verifierReceipt = Join-Path $tempRoot 'verifier-receipt.json'
    $missingInstall = Join-Path $tempRoot 'missing-install'
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $verifierArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $verifierPath + '" -InstallRoot "' + $missingInstall + '" -ReceiptPath "' + $verifierReceipt + '"'
    $verifierProcess = Start-Process -FilePath $powershell -ArgumentList $verifierArguments -WindowStyle Hidden -Wait -PassThru
    if ($verifierProcess.ExitCode -ne 20) {
        throw "Verifier did not fail closed under Windows PowerShell 5.1: $($verifierProcess.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $verifierReceipt -PathType Leaf)) {
        throw 'Verifier did not write a receipt under Windows PowerShell 5.1.'
    }
    $verifierPayload = Get-Content -LiteralPath $verifierReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$verifierPayload.status_code -ne 20) {
        throw "Verifier receipt has unexpected status code: $($verifierPayload.status_code)"
    }

    $hadReceiptFault = Test-Path Env:HWPX_TEST_RECEIPT_FAULT
    $previousReceiptFault = $env:HWPX_TEST_RECEIPT_FAULT
    try {
        foreach ($faultMode in @('serialization', 'write', 'readback')) {
            $env:HWPX_TEST_RECEIPT_FAULT = $faultMode
            $faultVerifierReceipt = Join-Path $tempRoot ("verifier-receipt-$faultMode.json")
            $faultVerifierArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $verifierPath + '" -InstallRoot "' + $missingInstall + '" -ReceiptPath "' + $faultVerifierReceipt + '"'
            $faultVerifier = Start-Process -FilePath $powershell -ArgumentList $faultVerifierArguments -WindowStyle Hidden -Wait -PassThru
            if ($faultVerifier.ExitCode -ne 99) {
                throw "Verifier receipt $faultMode fault did not fail closed with status 99: $($faultVerifier.ExitCode)"
            }
            if (Test-Path -LiteralPath $faultVerifierReceipt -PathType Leaf) {
                $faultPayload = Get-Content -LiteralPath $faultVerifierReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
                $truthfulFallback = [int]$faultPayload.status_code -eq 99 -and [string]$faultPayload.status -eq 'FAIL_RECEIPT'
                $nonSuccessReadback = [int]$faultPayload.status_code -ne 0 -and [string]$faultPayload.status -notin @('PASS', 'PASS_RUNTIME_ONLY', 'PASS_WITH_ADVISORY_TEST_DEBT')
                if (($faultMode -ne 'readback' -and -not $truthfulFallback) -or ($faultMode -eq 'readback' -and -not $nonSuccessReadback)) {
                    throw "Verifier receipt $faultMode fallback was not truthful."
                }
            }

            $installerRoot = Join-Path $tempRoot ("fault-install-$faultMode")
            $installerReceipt = Join-Path $tempRoot ("installer-receipt-$faultMode.json")
            $installerArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $root 'scripts\install_windows.ps1') + '" -SourceRoot "' + $root + '" -InstallRoot "' + $installerRoot + '" -DependencyMode CheckOnly -ReceiptPath "' + $installerReceipt + '"'
            $faultInstaller = Start-Process -FilePath $powershell -ArgumentList $installerArguments -WindowStyle Hidden -Wait -PassThru
            if ($faultInstaller.ExitCode -ne 99) {
                throw "Installer receipt $faultMode fault did not fail closed with status 99: $($faultInstaller.ExitCode)"
            }
            if (Test-Path -LiteralPath $installerReceipt -PathType Leaf) {
                $installerPayload = Get-Content -LiteralPath $installerReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
                $truthfulFallback = [int]$installerPayload.status_code -eq 99 -and [string]$installerPayload.status -eq 'FAIL_RECEIPT'
                $nonSuccessReadback = [int]$installerPayload.status_code -ne 0 -and [string]$installerPayload.status -notin @('PASS', 'PASS_RUNTIME_ONLY', 'PASS_WITH_ADVISORY_TEST_DEBT')
                if (($faultMode -ne 'readback' -and -not $truthfulFallback) -or ($faultMode -eq 'readback' -and -not $nonSuccessReadback)) {
                    throw "Installer receipt $faultMode fallback was not truthful."
                }
            }
            if (Test-Path -LiteralPath $installerRoot) {
                throw "Receipt fault $faultMode created an install root before failing closed."
            }
        }
    }
    finally {
        if ($hadReceiptFault) { $env:HWPX_TEST_RECEIPT_FAULT = $previousReceiptFault }
        else { Remove-Item Env:HWPX_TEST_RECEIPT_FAULT -ErrorAction SilentlyContinue }
    }
    Write-Output 'WINDOWS_POWERSHELL_51_RUNTIME=PASS'
}
finally {
    if ($null -ne $pythonProcess) {
        Stop-Process -Id $pythonProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $verifierProcess -and -not $verifierProcess.HasExited) {
        Stop-Process -Id $verifierProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}