[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g6-repair-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    $python = (Get-Command python.exe -ErrorAction Stop).Source

    # Readiness must bind to the exact worker process generation, not the
    # first compatible process returned by CIM (the venv launcher may appear
    # before the worker child).
    $verifierPath = Join-Path $root 'scripts\verify_windows.ps1'
    $verifierText = Get-Content -LiteralPath $verifierPath -Raw -Encoding UTF8
    $selectionStart = $verifierText.IndexOf('function Select-VerifierWorkerProcess')
    $selectionEnd = if ($selectionStart -ge 0) {
        $verifierText.IndexOf('function Test-VerifierApiListener', $selectionStart)
    }
    else { -1 }
    Assert-True ($selectionStart -ge 0 -and $selectionEnd -gt $selectionStart) 'Verifier worker selection helper was not found.'
    Invoke-Expression $verifierText.Substring($selectionStart, $selectionEnd - $selectionStart)

    $launcher = [pscustomobject]@{
        process_id = 13068
        start_identity = 'win-filetime:launcher'
    }
    $worker = [pscustomobject]@{
        process_id = 19976
        start_identity = 'win-filetime:worker-current'
    }
    $ready = [pscustomobject]@{
        worker_pid = 19976
        worker_start_identity = 'win-filetime:worker-current'
    }
    $bound = Select-VerifierWorkerProcess -Processes @($launcher, $worker) -Readiness $ready
    Assert-True ([bool]$bound.ok) 'Launcher-first enumeration did not bind the later readiness worker.'
    Assert-Equal 19976 ([int]$bound.process.process_id) 'Readiness binding selected the launcher instead of the exact worker PID.'
    Assert-Equal 'win-filetime:worker-current' ([string]$bound.process.start_identity) 'Readiness binding returned the wrong worker generation.'

    $invalidCases = @(
        [pscustomobject]@{
            name = 'missing readiness PID'
            processes = @($launcher, $worker)
            readiness = [pscustomobject]@{ worker_pid = 40123; worker_start_identity = 'win-filetime:missing' }
        },
        [pscustomobject]@{
            name = 'duplicate readiness PID'
            processes = @($launcher, $worker, [pscustomobject]@{ process_id = 19976; start_identity = 'win-filetime:worker-current' })
            readiness = $ready
        },
        [pscustomobject]@{
            name = 'stale readiness process row'
            processes = @($launcher, [pscustomobject]@{ process_id = 19976; start_identity = $null })
            readiness = $ready
        },
        [pscustomobject]@{
            name = 'mismatched readiness generation'
            processes = @($launcher, $worker)
            readiness = [pscustomobject]@{ worker_pid = 19976; worker_start_identity = 'win-filetime:worker-old' }
        }
    )
    foreach ($case in $invalidCases) {
        $rejected = Select-VerifierWorkerProcess -Processes $case.processes -Readiness $case.readiness
        Assert-True (-not [bool]$rejected.ok) ("Verifier accepted {0}." -f $case.name)
    }

    # Native stderr must not turn a real process exit into -1.
    $nativeFailure = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @('-c', "import sys; sys.stdout.write('G6_STDOUT'); sys.stderr.write('G6_STDERR'); sys.exit(7)") `
        -WorkingDirectory $tempRoot `
        -AllowNonZero
    Assert-Equal 7 ([int]$nativeFailure.exit_code) 'Native nonzero exit was not preserved'
    Assert-True ([string]$nativeFailure.stdout -match 'G6_STDOUT') 'Native stdout was not captured'
    Assert-True ([string]$nativeFailure.stderr -match 'G6_STDERR') 'Native stderr was not captured'
    Assert-True ([int64]$nativeFailure.stdout_bytes -gt 0) 'Native stdout byte count was not captured'
    Assert-True ([int64]$nativeFailure.stderr_bytes -gt 0) 'Native stderr byte count was not captured'

    # RED: unittest-style progress on stderr must retain a zero exit.
    $nativeSuccess = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @('-c', "import sys; sys.stderr.write(chr(10) + '.' * 21); sys.exit(0)") `
        -WorkingDirectory $tempRoot `
        -AllowNonZero
    Assert-Equal 0 ([int]$nativeSuccess.exit_code) 'Native zero exit with stderr was not preserved'
    Assert-True ([string]$nativeSuccess.stderr -match '\.\.' ) 'Progress stderr was not captured'
    Assert-True ([int64]$nativeSuccess.stderr_bytes -gt 0) 'Progress stderr byte count was not captured'

    # RED: rollback process-release evidence must expose its bound root.
    $candidate = Join-Path $tempRoot 'candidate'
    New-Item -ItemType Directory -Force -Path $candidate | Out-Null
    $stopResult = Stop-InstallProcesses -RootPath $candidate -PreserveProcessIds @() -TimeoutSeconds 1
    Assert-True ($stopResult.PSObject.Properties.Name -contains 'RootPath') 'Stop-InstallProcesses did not return RootPath'
    Assert-Equal ((Get-CanonicalPath -Path $candidate -RequireExisting)) ([string]$stopResult.RootPath) 'Stop-InstallProcesses returned the wrong root binding'
    Assert-Equal 0 (@($stopResult.remaining).Count) 'Candidate processes remained after release'

    $snapshotPath = Join-Path $tempRoot 'snapshot.json'
    [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        owner_run_id = [Guid]::NewGuid().ToString('N')
        install_root = (Get-CanonicalPath -Path $candidate -RequireExisting)
        tasks = @()
        processes = @()
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
    $snapshotData = [IO.File]::ReadAllText($snapshotPath) | ConvertFrom-Json
    $snapshotHash = Get-Sha256Hex -Path $snapshotPath
    $snapshotIdentity = Get-PathObjectIdentity -Path $snapshotPath -RequireExisting
    $restoreResult = Restore-InstallSnapshot `
        -SnapshotPath $snapshotPath `
        -ExpectedSnapshotSha256 $snapshotHash `
        -ExpectedSnapshotIdentity $snapshotIdentity `
        -ExpectedRunId ([string]$snapshotData.owner_run_id) `
        -CandidateRoot $candidate `
        -KeepCandidateRoot `
        -RestoreTasks `
        -Confirm:$false
    Assert-True ([bool]$restoreResult.processes_released) 'Rollback did not report released processes'
    Assert-Equal ([string]$stopResult.RootPath) ([string]$restoreResult.process_release.before_tasks.RootPath) 'Rollback lost the before-task root binding'
    Assert-Equal ([string]$stopResult.RootPath) ([string]$restoreResult.process_release.after_tasks.RootPath) 'Rollback lost the after-task root binding'

    # The race path must re-check ownership before converting a vanished PID
    # into a rollback failure; a real remaining candidate process stays fatal.
    $commonText = Get-Content -LiteralPath $commonPath -Raw
    $stopStart = $commonText.IndexOf('function Stop-InstallProcesses')
    $stopEnd = $commonText.IndexOf('function Write-JsonReceipt')
    Assert-True ($stopStart -ge 0 -and $stopEnd -gt $stopStart) 'Stop-InstallProcesses function was not found'
    $stopText = $commonText.Substring($stopStart, $stopEnd - $stopStart)
    Assert-True ($stopText -match 'Get-InstallProcessSnapshot\s+-RootPath\s+\$RootPath') 'Stop-InstallProcesses does not re-snapshot candidate ownership'
    Assert-True ($stopText -match 'race_released_process_ids') 'Stop-InstallProcesses does not record vanished-PID releases'

    [pscustomobject]@{
        status = 'PASS'
        native_nonzero_exit = [int]$nativeFailure.exit_code
        native_nonzero_stderr_bytes = [int64]$nativeFailure.stderr_bytes
        native_success_exit = [int]$nativeSuccess.exit_code
        native_success_stderr_bytes = [int64]$nativeSuccess.stderr_bytes
        rollback_root = [string]$stopResult.RootPath
        rollback_processes_released = [bool]$restoreResult.processes_released
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
