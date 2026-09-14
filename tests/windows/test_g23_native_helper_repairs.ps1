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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g23-native-helper-' + [Guid]::NewGuid().ToString('N'))
$raceProcess = $null
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    $python = (Get-Command python.exe -ErrorAction Stop).Source

    $nativeResult = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @('-c', "import sys; sys.stdout.write('G23_STDOUT'); sys.stderr.write('G23_STDERR'); sys.exit(7)") `
        -WorkingDirectory $tempRoot `
        -AllowNonZero
    Assert-Equal 7 ([int]$nativeResult.exit_code) 'Managed process identity fallback did not preserve the native exit code'
    Assert-True ([string]$nativeResult.stdout -match 'G23_STDOUT') 'Synchronous stdout pipe was not captured'
    Assert-True ([string]$nativeResult.stderr -match 'G23_STDERR') 'Synchronous stderr pipe was not captured'
    Assert-True ([bool]$nativeResult.exit_confirmed) 'Quick native process exit was not confirmed'

    $timeoutResult = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @('-c', 'import time; time.sleep(30)') `
        -WorkingDirectory $tempRoot `
        -TimeoutSeconds 1 `
        -AllowNonZero
    Assert-Equal -2 ([int]$timeoutResult.exit_code) 'Synchronous pipe timeout did not return the timeout marker'
    Assert-True ([bool]$timeoutResult.timed_out) 'Synchronous pipe timeout was not recorded'
    Assert-True ([bool]$timeoutResult.process_killed) 'Timed-out native process was not killed'
    Assert-True ($null -eq (Get-Process -Id ([int]$timeoutResult.process_id) -ErrorAction SilentlyContinue)) 'Timed-out native process survived cleanup'

    $raceRuns = 5
    for ($raceIndex = 1; $raceIndex -le $raceRuns; $raceIndex++) {
        $raceRoot = Join-Path $tempRoot ('race-candidate-' + $raceIndex)
        $raceProcess = $null
        New-Item -ItemType Directory -Force -Path $raceRoot | Out-Null
        try {
            # Keep the process alive long enough to capture a caller-owned
            # snapshot, then make the disappearance deterministic before
            # the cleanup function is entered.
            $raceArguments = '-c "import time; time.sleep(30)" "' + $raceRoot + '" app.worker'
            $raceProcess = Start-Process -FilePath $python -ArgumentList $raceArguments -WindowStyle Hidden -PassThru
            $raceProcessId = [int]$raceProcess.Id
            $seen = $false
            $seenDeadline = (Get-Date).AddSeconds(5)
            $observed = @()
            do {
                $observed = @(
                    Get-InstallProcessSnapshot -RootPath $raceRoot -ExpectedPythonPath $python |
                        Where-Object { [int]$_.process_id -eq $raceProcessId }
                )
                $seen = $observed.Count -eq 1
                if (-not $seen) { Start-Sleep -Milliseconds 50 }
            } while (-not $seen -and (Get-Date) -lt $seenDeadline)
            Assert-True $seen "Synthetic race process was not visible in candidate snapshot: $raceProcessId"

            # Simulate the real coordination gap: the caller observed the
            # candidate, but it exits before Stop-InstallProcesses starts.
            Microsoft.PowerShell.Management\Stop-Process -Id $raceProcessId -Force -ErrorAction Stop
            $exitDeadline = (Get-Date).AddSeconds(5)
            do {
                try { $raceProcess.Refresh() } catch { }
                if ($raceProcess.HasExited) { break }
                Start-Sleep -Milliseconds 50
            } while ((Get-Date) -lt $exitDeadline)
            Assert-True ([bool]$raceProcess.HasExited) "Synthetic race process did not vanish: $raceProcessId"

            $raceResult = Stop-InstallProcesses `
                -RootPath $raceRoot `
                -InitialProcessSnapshot $observed `
                -PreserveProcessIds @() `
                -ExpectedPythonPath $python `
                -TimeoutSeconds 2
            Assert-True (@($raceResult.race_released_process_ids | Where-Object { [int]$_ -eq $raceProcessId }).Count -eq 1) "The vanished PID was not recorded as released on iteration $raceIndex"
            Assert-Equal 0 (@($raceResult.remaining).Count) "A vanished PID was left in the remaining process set on iteration $raceIndex"
        }
        finally {
            if ($raceProcess -and -not $raceProcess.HasExited) {
                Microsoft.PowerShell.Management\Stop-Process -Id $raceProcess.Id -Force -ErrorAction SilentlyContinue
            }
            if ($raceProcess) { $raceProcess.Dispose() }
            if (Test-Path -LiteralPath $raceRoot) {
                Remove-Item -LiteralPath $raceRoot -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    [pscustomobject]@{
        status = 'PASS'
        quick_exit_code = [int]$nativeResult.exit_code
        timeout_process_killed = [bool]$timeoutResult.process_killed
        race_released_process_id = $raceProcessId
    } | ConvertTo-Json -Compress
}
finally {
    Remove-Item Function:\global:Stop-Process -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
