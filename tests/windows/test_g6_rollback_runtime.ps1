[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force

$failures = New-Object System.Collections.ArrayList
function Record-Check {
    param([scriptblock]$Action, [string]$Description)
    try { & $Action }
    catch { [void]$failures.Add("${Description}: $($_.Exception.Message)") }
}
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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g6-rollback-red-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    $candidate = Join-Path $tempRoot 'candidate'
    New-Item -ItemType Directory -Force -Path $candidate | Out-Null
    $canonicalCandidate = Get-CanonicalPath -Path $candidate -RequireExisting

    $commonText = Get-Content -LiteralPath $commonPath -Raw
    $stopStart = $commonText.IndexOf('function Stop-InstallProcesses')
    $stopEnd = $commonText.IndexOf('function Write-JsonReceipt')
    Assert-True ($stopStart -ge 0 -and $stopEnd -gt $stopStart) 'Stop-InstallProcesses function was not found'
    $stopText = $commonText.Substring($stopStart, $stopEnd - $stopStart)
    Record-Check { Assert-True ($stopText -match 'race_released_process_ids') 'the vanished-PID release evidence field is missing' } 'race release evidence contract'
    Record-Check { Assert-True ($stopText -match '(?s)catch\s*\{.*Get-InstallProcessSnapshot\s+-RootPath\s+\$RootPath') 'the stop failure path does not re-check candidate ownership' } 'vanished-PID ownership re-check contract'

    Record-Check {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        $raceRuns = 5
        for ($raceIndex = 1; $raceIndex -le $raceRuns; $raceIndex++) {
            $raceRoot = Join-Path $tempRoot ('race-candidate-' + $raceIndex)
            $raceProcess = $null
            New-Item -ItemType Directory -Force -Path $raceRoot | Out-Null
            try {
                # Keep the process alive long enough to capture a caller-owned
                # snapshot, then make the disappearance deterministic before
                # the cleanup function is entered.
                $pythonArguments = '-c "import time; time.sleep(30)" "' + $raceRoot + '" app.worker'
                $raceProcess = Start-Process -FilePath $python -ArgumentList $pythonArguments -WindowStyle Hidden -PassThru
                $raceProcessId = [int]$raceProcess.Id
                $seen = $false
                $deadline = (Get-Date).AddSeconds(5)
                $observed = @()
                do {
                    $observed = @(
                        Get-InstallProcessSnapshot -RootPath $raceRoot -ExpectedPythonPath $python |
                            Where-Object { [int]$_.process_id -eq $raceProcessId }
                    )
                    $seen = $observed.Count -eq 1
                    if (-not $seen) { Start-Sleep -Milliseconds 50 }
                } while (-not $seen -and (Get-Date) -lt $deadline)
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
    } 'vanished-PID race handling (5 repetitions)'

    $stopResult = $null
    Record-Check {
        $script:stopResult = Stop-InstallProcesses -RootPath $candidate -PreserveProcessIds @() -TimeoutSeconds 1
        Assert-True ($script:stopResult.PSObject.Properties.Name -contains 'RootPath') 'Stop-InstallProcesses did not return RootPath'
        Assert-Equal $canonicalCandidate ([string]$script:stopResult.RootPath) 'Stop-InstallProcesses returned the wrong root binding'
        Assert-Equal 0 (@($script:stopResult.remaining).Count) 'Candidate processes remained after release'
    } 'stop result binding'

    $snapshotPath = Join-Path $tempRoot 'snapshot.json'
    $snapshotOwnerRunId = [Guid]::NewGuid().ToString('N')
    [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        owner_run_id = $snapshotOwnerRunId
        install_root = $canonicalCandidate
        tasks = @()
        processes = @()
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
    $snapshotHash = Get-Sha256Hex -Path $snapshotPath
    $snapshotIdentity = Get-PathObjectIdentity -Path $snapshotPath -RequireExisting
    Record-Check {
        $script:restoreResult = Restore-InstallSnapshot `
            -SnapshotPath $snapshotPath `
            -ExpectedSnapshotSha256 $snapshotHash `
            -ExpectedSnapshotIdentity $snapshotIdentity `
            -ExpectedRunId $snapshotOwnerRunId `
            -CandidateRoot $candidate `
            -KeepCandidateRoot `
            -RestoreTasks `
            -Confirm:$false
        Assert-True ([bool]$script:restoreResult.processes_released) 'Rollback did not report released processes'
        Assert-Equal $canonicalCandidate ([string]$script:restoreResult.process_release.before_tasks.RootPath) 'Rollback lost the before-task root binding'
        Assert-Equal $canonicalCandidate ([string]$script:restoreResult.process_release.after_tasks.RootPath) 'Rollback lost the after-task root binding'
    } 'rollback process-release binding'

    Record-Check {
        $rehearsalRoot = Join-Path $tempRoot 'preserve-move-rehearsal'
        $oldRoot = Join-Path $rehearsalRoot 'install'
        $backupRoot = Join-Path $rehearsalRoot 'install.backup'
        $candidateRoot = Join-Path $rehearsalRoot 'install.candidate'
        $snapshot = Join-Path $rehearsalRoot 'snapshot.json'
        $snapshotOwnerRunId = [Guid]::NewGuid().ToString('N')
        $snapshotHash = $null
        $snapshotIdentity = $null
        $taskNames = @(
            'HWPX_G12_PreserveMove_API_' + [Guid]::NewGuid().ToString('N'),
            'HWPX_G12_PreserveMove_WORKER_' + [Guid]::NewGuid().ToString('N')
        )
        $registeredTasks = @()
        $rehearsalStep = 'setup'
        try {
            $rehearsalStep = 'create old root'
            New-Item -ItemType Directory -Force -Path $oldRoot | Out-Null
            [IO.File]::WriteAllText((Join-Path $oldRoot 'preimage.txt'), 'old-root-bytes`r`n', (New-Object Text.UTF8Encoding($false)))
            $oldBytesHash = Get-Sha256Hex -Path (Join-Path $oldRoot 'preimage.txt')
            $envPreimagePath = Join-Path $oldRoot '.env'
            [IO.File]::WriteAllText($envPreimagePath, "HWP_API_PORT=18765`r`n", (New-Object Text.UTF8Encoding($false)))
            $oldEnvHash = Get-Sha256Hex -Path $envPreimagePath
            $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $rehearsalStep = 'register running tasks'
            $runningMarker = Join-Path $oldRoot 'running-marker.txt'
            $action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument ('/c ping 127.0.0.1 -n 47 > "{0}"' -f $runningMarker) -WorkingDirectory $oldRoot
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
            foreach ($taskName in $taskNames) {
                Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
                $registeredTasks += $taskName
                Start-ScheduledTask -TaskName $taskName
            }
            $runningDeadline = (Get-Date).AddSeconds(10)
            do {
                $runningStates = @($taskNames | ForEach-Object { (Get-ScheduledTask -TaskName $_ -ErrorAction Stop).State })
                if (@($runningStates | Where-Object { [string]$_ -eq 'Running' }).Count -eq $taskNames.Count) { break }
                Start-Sleep -Milliseconds 250
            } while ((Get-Date) -lt $runningDeadline)
            if (@($runningStates | Where-Object { [string]$_ -eq 'Running' }).Count -ne $taskNames.Count) {
                throw "Disposable PreserveMove tasks did not reach Running: $($runningStates -join ',')"
            }
            $rehearsalStep = 'save running snapshot'
            Save-InstallSnapshot -SnapshotPath $snapshot -InstallRoot $oldRoot -TaskName $taskNames -OwnerRunId $snapshotOwnerRunId | Out-Null
            $snapshotHash = Get-Sha256Hex -Path $snapshot
            $snapshotIdentity = Get-PathObjectIdentity -Path $snapshot -RequireExisting
            $snapshotData = [IO.File]::ReadAllText($snapshot) | ConvertFrom-Json
            if (@($snapshotData.tasks | Where-Object { [string]$_.state -eq 'Running' }).Count -ne $taskNames.Count) {
                throw 'PreserveMove snapshot did not capture both previously running tasks.'
            }
            foreach ($taskName in $taskNames) {
                $rehearsalStep = "stop task $taskName"
                Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
                $stopDeadline = (Get-Date).AddSeconds(10)
                do {
                    $taskState = [string](Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).State
                    if ($taskState -ne 'Running') { break }
                    Start-Sleep -Milliseconds 250
                } while ((Get-Date) -lt $stopDeadline)
                if ($taskState -eq 'Running') { throw "Disposable task remained Running after stop: $taskName" }
 $rehearsalStep = "unregister task $taskName"
 Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                    throw "Disposable candidate task remained before root swap: $taskName"
                }
                Start-Sleep -Milliseconds 250
            }
            $taskProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                [string]$_.CommandLine -and (
                    [string]$_.CommandLine -like "*$runningMarker*" -or
                    [string]$_.CommandLine -match '127\.0\.0\.1\s+-n\s+47'
                )
            })
            foreach ($taskProcess in $taskProcesses) {
                Stop-Process -Id ([int]$taskProcess.ProcessId) -Force -ErrorAction Stop
            }
            $processDeadline = (Get-Date).AddSeconds(10)
            do {
                $remainingTaskProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                    [string]$_.CommandLine -and (
                        [string]$_.CommandLine -like "*$runningMarker*" -or
                        [string]$_.CommandLine -match '127\.0\.0\.1\s+-n\s+47'
                    )
                })
                if ($remainingTaskProcesses.Count -eq 0) { break }
                Start-Sleep -Milliseconds 250
            } while ((Get-Date) -lt $processDeadline)
            if ($remainingTaskProcesses.Count -gt 0) { throw 'Disposable task child process remained before root swap.' }
            Start-Sleep -Seconds 2
            $rehearsalStep = 'move old root to backup'
            Move-Item -LiteralPath $oldRoot -Destination $backupRoot
            $rehearsalStep = 'prepare candidate root'
            New-Item -ItemType Directory -Force -Path $candidateRoot | Out-Null
            [IO.File]::WriteAllText((Join-Path $candidateRoot 'candidate.txt'), 'candidate-bytes`r`n', (New-Object Text.UTF8Encoding($false)))
            # Simulate an activation failure after the candidate was prepared;
            # restore the old root before asking Restore-InstallSnapshot to
            # re-register and start the saved task definitions.
            $rehearsalStep = 'restore old root before task registration'
            Move-Item -LiteralPath $backupRoot -Destination $oldRoot
            $rehearsalStep = 'restore tasks and running state'
            $restored = Restore-InstallSnapshot -SnapshotPath $snapshot -ExpectedSnapshotSha256 $snapshotHash -ExpectedSnapshotIdentity $snapshotIdentity -ExpectedRunId $snapshotOwnerRunId -CandidateRoot $oldRoot -KeepCandidateRoot -RestoreTasks -Confirm:$false
            if (-not $restored.restored -or -not $restored.processes_released) { throw 'PreserveMove rollback did not report a restored root/process state.' }
            $restoredBytesHash = Get-Sha256Hex -Path (Join-Path $oldRoot 'preimage.txt')
            if ($oldBytesHash -ne $restoredBytesHash) {
                throw 'PreserveMove rollback did not restore the original root bytes.'
            }
            if ($oldEnvHash -ne (Get-Sha256Hex -Path $envPreimagePath)) {
                throw 'PreserveMove rollback did not restore the existing .env bytes.'
            }
            foreach ($taskName in $taskNames) {
                $restoredTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
                $restoredIdentity = Get-ScheduledTaskIdentity -TaskName $taskName
                if ([string]$restoredTask.State -ne 'Running') { throw "PreserveMove task was not running after root restoration: $taskName" }
                $savedIdentity = @($snapshotData.tasks | Where-Object { [string]$_.task_name -eq $taskName })[0]
                if ([string]$restoredIdentity.task_identity_hash -ne [string]$savedIdentity.task_identity_hash) { throw "PreserveMove task identity hash changed: $taskName" }
                if ([string]$restoredIdentity.xml -cne [string]$savedIdentity.xml) { throw "PreserveMove task XML changed: $taskName" }
                if ([bool]$restoredIdentity.enabled -ne [bool]$savedIdentity.enabled) { throw "PreserveMove task enabled state changed: $taskName" }
            }
            if (Test-Path -LiteralPath $candidateRoot) { Remove-Item -LiteralPath $candidateRoot -Recurse -Force }
            $script:preserveMoveRehearsal = $true
        }
        catch {
            throw "PreserveMove rehearsal step '$rehearsalStep' failed: $($_.Exception.Message)"
        }
        finally {
            foreach ($taskName in $registeredTasks) {
                Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
            }
            if (Test-Path -LiteralPath $rehearsalRoot) { Remove-Item -LiteralPath $rehearsalRoot -Recurse -Force -ErrorAction SilentlyContinue }
        }
    } 'PreserveMove root/task rehearsal'

    if ($failures.Count -gt 0) {
        throw ('G6 rollback RED failures: ' + ($failures -join '; '))
    }
    [pscustomobject]@{
        status = 'PASS'
        rollback_root = $canonicalCandidate
        processes_released = [bool]$restoreResult.processes_released
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
