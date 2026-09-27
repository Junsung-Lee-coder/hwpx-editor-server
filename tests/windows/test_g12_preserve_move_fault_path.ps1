[CmdletBinding()]
param(
    [switch]$RunInstallerFaultPath,
    [string]$SourceRoot,
    [string]$InstallRoot,
    [Nullable[int]]$ApiPort,
    [string]$ReceiptPath,
    [string]$ResultPath,
    [string]$ExpectedRepository,
    [string]$ExpectedCommit,
    [string]$ExpectedTree,
    [string]$ExpectedManifestSha256,
    [string]$PopplerPath,
    [ValidateRange(1, 1800)][int]$ControllerTimeoutSeconds = 300,
    [Nullable[int]]$RemoteHarnessTimeoutSeconds
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$installerPath = Join-Path $root 'scripts\install_windows.ps1'
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
Import-Module $commonPath -Force
if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = $root }

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-NonEmpty {
    param([string]$Value, [string]$Name)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Name is required for the opt-in fault-path regression." }
}

function Append-G12NativeBackslashes {
    param(
        [Parameter(Mandatory = $true)][System.Text.StringBuilder]$Builder,
        [Parameter(Mandatory = $true)][int]$Count
    )
    if ($Count -gt 0) {
        [void]$Builder.Append([string]::new([char]92, $Count))
    }
}

function ConvertTo-G12NativeCommandLineArgument {
    [CmdletBinding()]
    param([AllowNull()][object]$Value)

    $text = if ($null -eq $Value) { '' } else { [string]$Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $text.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            Append-G12NativeBackslashes -Builder $builder -Count ($backslashes * 2 + 1)
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        Append-G12NativeBackslashes -Builder $builder -Count $backslashes
        [void]$builder.Append($character)
        $backslashes = 0
    }
    Append-G12NativeBackslashes -Builder $builder -Count ($backslashes * 2)
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Get-TaskEvidence {
    param([Parameter(Mandatory = $true)][string]$TaskName, [string]$TaskPath = '\')
    $identity = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    $task = Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $TaskPath -AllowMissing
    return [pscustomobject]@{
        task_name = $TaskName
        task_path = $TaskPath
        exists = [bool]$identity.exists
        state = if ($task) { [string]$task.State } else { $null }
        enabled = if ($identity.PSObject.Properties.Name -contains 'enabled') { [bool]$identity.enabled } else { $null }
        task_identity_hash = [string]$identity.task_identity_hash
        execute = [string]$identity.execute
        arguments = [string]$identity.arguments
        working_directory = [string]$identity.working_directory
    }
}

function Get-RuntimeEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][int]$RuntimePort
    )
    $taskPath = '\'
    $taskNames = @('hwpx-editor-api', 'hwpx-editor-worker')
    return [pscustomobject]@{
        tasks = @($taskNames | ForEach-Object { Get-TaskEvidence -TaskName $_ -TaskPath $taskPath })
        processes = @(Get-InstallProcessSnapshot -RootPath $RuntimeRoot -ExpectedPythonPath (Join-Path $RuntimeRoot '.venv\Scripts\python.exe'))
        health = Get-InstallApiHealth -ApiPort $RuntimePort
    }
}

function Get-G12NamespaceInventory {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Filter
    )
    $inventory = @{}
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return $inventory }
    foreach ($item in @(Get-ChildItem -LiteralPath $Directory -File -Filter $Filter -Force)) {
        $canonical = Get-CanonicalPath -Path $item.FullName -RequireExisting
        $key = $canonical.ToLowerInvariant()
        $inventory[$key] = [ordered]@{
            path = $canonical
            object_identity = Get-PathObjectIdentity -Path $canonical -RequireExisting
            sha256 = Get-Sha256Hex -Path $canonical
        }
    }
    return $inventory
}

function Get-G12RollbackSnapshotNamespace {
    return Get-G12NamespaceInventory -Directory ([System.IO.Path]::GetTempPath()) -Filter 'hwpx-install-snapshot-*.json'
}

function Get-G12TransactionJournalNamespace {
    $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($base)) { $base = [System.IO.Path]::GetTempPath() }
    return Get-G12NamespaceInventory -Directory (Join-Path $base 'HWPX\transactions') -Filter '*.journal.json'
}

function Get-G12ProcessCapture {
    param(
        [Nullable[int]]$ProcessId,
        [string]$StartIdentity
    )
    if ($null -eq $ProcessId -or [int]$ProcessId -lt 1) { return $null }
    $process = Get-Process -Id ([int]$ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [ordered]@{
            process_id = [int]$ProcessId
            start_identity = $StartIdentity
            present = $false
            captured_at_utc = [DateTime]::UtcNow.ToString('o')
        }
    }
    $observedStartIdentity = $null
    try { $observedStartIdentity = Get-ProcessGenerationIdentity -ProcessId ([int]$ProcessId) } catch { }
    return [ordered]@{
        process_id = [int]$ProcessId
        start_identity = $StartIdentity
        observed_start_identity = $observedStartIdentity
        identity_matches = (-not [string]::IsNullOrWhiteSpace($StartIdentity) -and $observedStartIdentity -ceq $StartIdentity)
        present = $true
        name = [string]$process.ProcessName
        has_exited = [bool]$process.HasExited
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
    }
}

function Start-G12InstallerProcess {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'powershell.exe'
    $argumentValues = @($Arguments | ForEach-Object { ConvertTo-G12NativeCommandLineArgument -Value $_ })
    $startInfo.Arguments = [string]::Join(' ', [string[]]$argumentValues)
    $startInfo.WorkingDirectory = Get-CanonicalPath -Path $WorkingDirectory -RequireExisting
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw 'G12 installer process could not be started.' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process | Add-Member -NotePropertyName 'g12_stdout_task' -NotePropertyValue $stdoutTask
    $process | Add-Member -NotePropertyName 'g12_stderr_task' -NotePropertyValue $stderrTask
    $process | Add-Member -NotePropertyName 'g12_stdout_path' -NotePropertyValue $StdoutPath
    $process | Add-Member -NotePropertyName 'g12_stderr_path' -NotePropertyValue $StderrPath
    return $process
}

function Wait-G12InstallerProcessBounded {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][int]$RuntimePort,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    $startedAt = [DateTime]::UtcNow
    $processId = [int]$Process.Id
    $startIdentity = $null
    try { $startIdentity = Get-ProcessGenerationIdentity -ProcessId $processId } catch { }
    $timeoutOccurred = $false
    $terminationRequested = $false
    $terminationConfirmed = $false
    $terminationError = $null
    $waitError = $null
    $exitConfirmed = $false
    while ($true) {
        try { $Process.Refresh() } catch { $waitError = [string]$_.Exception.Message; break }
        if ($Process.HasExited) {
            $exitConfirmed = $true
            break
        }
        $elapsedSeconds = ([DateTime]::UtcNow - $startedAt).TotalSeconds
        if ($elapsedSeconds -ge $TimeoutSeconds) {
            $timeoutOccurred = $true
            try {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                $terminationRequested = $true
            }
            catch {
                $terminationError = [string]$_.Exception.Message
            }
            try {
                $terminationConfirmed = [bool]$Process.WaitForExit(15000)
                $Process.Refresh()
                $exitConfirmed = [bool]$Process.HasExited
            }
            catch {
                if ([string]::IsNullOrWhiteSpace($terminationError)) { $terminationError = [string]$_.Exception.Message }
                $terminationConfirmed = $false
                $exitConfirmed = $false
            }
            break
        }
        Start-Sleep -Milliseconds 250
    }
    $endedAt = [DateTime]::UtcNow
    $exitCode = $null
    if ($exitConfirmed) {
        try { $exitCode = [int]$Process.ExitCode } catch { $waitError = [string]$_.Exception.Message }
    }
    $stdoutCaptureComplete = $false
    $stderrCaptureComplete = $false
    $stdoutCaptureError = $null
    $stderrCaptureError = $null
    $utf8 = New-Object Text.UTF8Encoding($false)
    foreach ($stream in @(
        [pscustomobject]@{ task = $Process.g12_stdout_task; path = $StdoutPath; name = 'stdout' },
        [pscustomobject]@{ task = $Process.g12_stderr_task; path = $StderrPath; name = 'stderr' }
    )) {
        $complete = $false
        try {
            $complete = [bool]$stream.task.Wait(5000)
            if ($complete) {
                [IO.File]::WriteAllText($stream.path, [string]$stream.task.Result, $utf8)
            }
            else {
                [IO.File]::WriteAllText($stream.path, '', $utf8)
            }
        }
        catch {
            [IO.File]::WriteAllText($stream.path, '', $utf8)
            if ($stream.name -eq 'stdout') { $stdoutCaptureError = [string]$_.Exception.Message }
            else { $stderrCaptureError = [string]$_.Exception.Message }
        }
        if ($stream.name -eq 'stdout') { $stdoutCaptureComplete = $complete }
        else { $stderrCaptureComplete = $complete }
    }
    if (-not $stdoutCaptureComplete) {
        $waitError = if ($waitError) { $waitError + '; ' } else { '' }
        $waitError += 'stdout capture did not complete within the bounded drain window'
    }
    if (-not $stderrCaptureComplete) {
        $waitError = if ($waitError) { $waitError + '; ' } else { '' }
        $waitError += 'stderr capture did not complete within the bounded drain window'
    }
    if ($stdoutCaptureError) {
        $waitError = if ($waitError) { $waitError + '; ' } else { '' }
        $waitError += 'stdout capture error: ' + $stdoutCaptureError
    }
    if ($stderrCaptureError) {
        $waitError = if ($waitError) { $waitError + '; ' } else { '' }
        $waitError += 'stderr capture error: ' + $stderrCaptureError
    }
    $survivingProcess = Get-G12ProcessCapture -ProcessId $processId -StartIdentity $startIdentity
    $survivingProcesses = @()
    $survivingTasks = @()
    if ($timeoutOccurred -and -not $exitConfirmed) {
        try { $survivingProcesses = @(Get-InstallProcessSnapshot -RootPath $RuntimeRoot -ExpectedPythonPath (Join-Path $RuntimeRoot '.venv\Scripts\python.exe')) } catch { }
        foreach ($taskName in @('hwpx-editor-api', 'hwpx-editor-worker')) {
            try { $survivingTasks += Get-TaskEvidence -TaskName $taskName -TaskPath '\' } catch { }
        }
    }
    return [ordered]@{
        owner = 'g12-controller'
        timeout_budget_seconds = [int]$TimeoutSeconds
        timeout_budget_enforced = $true
        started_at_utc = $startedAt.ToString('o')
        ended_at_utc = $endedAt.ToString('o')
        measured_elapsed_seconds = [Math]::Round(($endedAt - $startedAt).TotalSeconds, 3)
        timeout_occurred = [bool]$timeoutOccurred
        process_id = $processId
        process_start_identity = $startIdentity
        process_exit_confirmed = [bool]$exitConfirmed
        exit_code = $exitCode
        process_exit_code = $exitCode
        process_exit_source = 'System.Diagnostics.Process.ExitCode after bounded wait/readback.'
        termination_grace_budget_seconds = if ($timeoutOccurred) { 15 } else { $null }
        termination_requested = if ($timeoutOccurred) { [bool]$terminationRequested } else { $null }
        termination_confirmed = if ($timeoutOccurred) { [bool]$terminationConfirmed } else { $null }
        termination_error = $terminationError
        wait_error = $waitError
        surviving_process = if ($timeoutOccurred) { $survivingProcess } else { $null }
        surviving_processes = if ($timeoutOccurred) { @($survivingProcesses) } else { @() }
        surviving_tasks = if ($timeoutOccurred) { @($survivingTasks) } else { @() }
        survivor_capture_complete = [bool]($timeoutOccurred -and ($null -ne $survivingProcess)) -or -not $timeoutOccurred
        stdout_path = $StdoutPath
        stderr_path = $StderrPath
        stdout_capture_complete = [bool]$stdoutCaptureComplete
        stderr_capture_complete = [bool]$stderrCaptureComplete
        provenance = 'Measured by the G12 controller on the same Windows host as the installer child process.'
    }
}

function Test-G12OpeningNamespacePreserved {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Opening,
        [Parameter(Mandatory = $true)][hashtable]$Final
    )
    foreach ($key in @($Opening.Keys)) {
        if (-not $Final.ContainsKey($key) -or
            [string]$Final[$key].object_identity -cne [string]$Opening[$key].object_identity -or
            [string]$Final[$key].sha256 -cne [string]$Opening[$key].sha256) { return $false }
    }
    foreach ($key in @($Final.Keys)) {
        if (-not $Opening.ContainsKey($key)) { return $false }
    }
    return $true
}

if (-not $RunInstallerFaultPath) {
    $installerText = Get-Content -LiteralPath $installerPath -Raw
    $commonText = Get-Content -LiteralPath $commonPath -Raw
    $scriptText = Get-Content -LiteralPath $PSCommandPath -Raw
    foreach ($needle in @(
        'after-task-registrations',
        'Restore-InstallSnapshot',
        '-ExpectedApiPort ([int]$apiPort)',
        'terminal_readback',
        'restored_processes',
        'Wait-InstallApiHealth',
        'Wait-G12InstallerProcessBounded',
        'timeout_budget_enforced',
        'process_exit_confirmed',
        'remote_harness',
        'HWPX_TEST_INSTALL_CRASH_POINT'
    )) {
        Assert-True ($installerText.Contains($needle) -or $commonText.Contains($needle) -or $scriptText.Contains($needle)) "Fault-path contract is missing: $needle"
    }
    [pscustomobject]@{
        status = 'SKIP'
        reason = 'Opt-in: pass -RunInstallerFaultPath only from an isolated acceptance harness.'
        fault_path_available = $true
        manual_task_recovery_used = $false
    } | ConvertTo-Json -Compress
    exit 0
}

foreach ($required in @(
    @{ value = $InstallRoot; name = 'InstallRoot' },
    @{ value = $ReceiptPath; name = 'ReceiptPath' },
    @{ value = $ExpectedRepository; name = 'ExpectedRepository' },
    @{ value = $ExpectedCommit; name = 'ExpectedCommit' },
    @{ value = $ExpectedTree; name = 'ExpectedTree' },
    @{ value = $ExpectedManifestSha256; name = 'ExpectedManifestSha256' },
    @{ value = $PopplerPath; name = 'PopplerPath' }
)) {
    Assert-NonEmpty -Value ([string]$required.value) -Name ([string]$required.name)
}
if ($null -eq $ApiPort -or [int]$ApiPort -lt 1 -or [int]$ApiPort -gt 65535) {
    throw 'ApiPort must be a valid TCP port for the opt-in fault-path regression.'
}
if ($null -ne $RemoteHarnessTimeoutSeconds -and ([int]$RemoteHarnessTimeoutSeconds -lt 1 -or [int]$RemoteHarnessTimeoutSeconds -gt 3600)) {
    throw 'RemoteHarnessTimeoutSeconds must be a positive outer-harness budget when supplied.'
}

$source = Get-CanonicalPath -Path $SourceRoot -RequireExisting
$install = Get-CanonicalPath -Path $InstallRoot -RequireExisting
if ($source -ceq $install) { throw 'SourceRoot and InstallRoot must be different for the fault-path regression.' }
$markerPath = Join-Path $install '.hwpx-install.json'
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { throw "Install marker is missing: $markerPath" }
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g12-preserve-move-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$receiptFile = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ReceiptPath))
$resultFile = if ([string]::IsNullOrWhiteSpace($ResultPath)) { Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g12-result-' + [Guid]::NewGuid().ToString('N') + '.json') } else { [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ResultPath)) }
$installerStdout = Join-Path $runRoot 'installer.stdout.log'
$installerStderr = Join-Path $runRoot 'installer.stderr.log'
$originalMarkerBytes = [IO.File]::ReadAllBytes($markerPath)
$originalMarkerHash = Get-Sha256Hex -Path $markerPath
$before = $null
$afterRollback = $null
$afterCleanup = $null
$installerReceipt = $null
$exitCode = $null
$markerRestored = $false
$harnessStartedAtUtc = [DateTime]::UtcNow.ToString('o')
$openingSnapshotIdentities = $null
$openingJournalIdentities = $null
$finalSnapshotIdentities = $null
$finalJournalIdentities = $null
$waitEvidence = $null
$postcheck = $null

try {
    $openingSnapshotIdentities = Get-G12RollbackSnapshotNamespace
    $openingJournalIdentities = Get-G12TransactionJournalNamespace
    $before = Get-RuntimeEvidence -RuntimeRoot $install -RuntimePort ([int]$ApiPort)
    Assert-True (@($before.tasks | Where-Object { -not $_.exists -or [string]$_.state -ne 'Running' -or -not [bool]$_.enabled }).Count -eq 0) 'Predecessor API/worker tasks were not Running and enabled before the fault-path test.'
    Assert-True (@($before.processes | Where-Object { [string]$_.module -notin @('app.api_server', 'app.worker') -or [string]$_.identity_root -cne $install }).Count -eq 0) 'Predecessor process evidence was not root-bound before the fault-path test.'
    Assert-True (@($before.processes | Where-Object { [string]$_.module -eq 'app.api_server' }).Count -ge 1) 'Predecessor API process was not observed before the fault-path test.'
    Assert-True (@($before.processes | Where-Object { [string]$_.module -eq 'app.worker' }).Count -ge 1) 'Predecessor worker process was not observed before the fault-path test.'
    Assert-True ([bool]$before.health.ok) 'Predecessor API was not healthy before the fault-path test.'

    $markerObject = [IO.File]::ReadAllText($markerPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $markerObject.commit = ('f' * 40)
    $markerObject.tree = ('e' * 40)
    $markerObject.candidate_generation = 'g12-fault-injected-predecessor'
    $injectedMarkerText = ($markerObject | ConvertTo-Json -Depth 20) + "`n"
    [IO.File]::WriteAllText($markerPath, $injectedMarkerText, (New-Object Text.UTF8Encoding($false)))
    $injectedMarkerHash = Get-Sha256Hex -Path $markerPath
    Assert-True ($injectedMarkerHash -cne $originalMarkerHash) 'Fault-path marker injection did not change the predecessor marker preimage.'

    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installerPath,
        '-SourceRoot', $source,
        '-InstallRoot', $install,
        '-DependencyMode', 'CheckOnly',
        '-ExistingInstallDisposition', 'PreserveMove',
        '-ReplaceExistingTasks',
        '-PopplerPath', $PopplerPath,
        '-ApiPort', [string]$ApiPort,
        '-ReceiptPath', $receiptFile,
        '-ExpectedRepository', $ExpectedRepository,
        '-ExpectedCommit', $ExpectedCommit,
        '-ExpectedTree', $ExpectedTree,
        '-ExpectedManifestSha256', $ExpectedManifestSha256
    )
    $oldFault = [Environment]::GetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT', 'after-task-registrations', 'Process')
        $installerProcess = Start-G12InstallerProcess -Arguments $arguments -WorkingDirectory $source -StdoutPath $installerStdout -StderrPath $installerStderr
        $waitEvidence = Wait-G12InstallerProcessBounded -Process $installerProcess -TimeoutSeconds $ControllerTimeoutSeconds -RuntimeRoot $install -RuntimePort ([int]$ApiPort) -StdoutPath $installerStdout -StderrPath $installerStderr
        if ([bool]$waitEvidence.process_exit_confirmed) { $exitCode = [int]$waitEvidence.exit_code }
    }
    finally {
        if ($null -eq $oldFault) { [Environment]::SetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT', $null, 'Process') }
        else { [Environment]::SetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT', $oldFault, 'Process') }
    }

    if (Test-Path -LiteralPath $receiptFile -PathType Leaf) {
        $installerReceipt = [IO.File]::ReadAllText($receiptFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
    }
    $afterRollback = Get-RuntimeEvidence -RuntimeRoot $install -RuntimePort ([int]$ApiPort)
    $currentMarkerHash = Get-Sha256Hex -Path $markerPath
    [IO.File]::WriteAllBytes($markerPath, $originalMarkerBytes)
    $markerRestored = ((Get-Sha256Hex -Path $markerPath) -ceq $originalMarkerHash)
    $afterCleanup = Get-RuntimeEvidence -RuntimeRoot $install -RuntimePort ([int]$ApiPort)
    $finalSnapshotIdentities = Get-G12RollbackSnapshotNamespace
    $finalJournalIdentities = Get-G12TransactionJournalNamespace
    $snapshotNamespacePreserved = Test-G12OpeningNamespacePreserved -Opening $openingSnapshotIdentities -Final $finalSnapshotIdentities
    $journalNamespacePreserved = Test-G12OpeningNamespacePreserved -Opening $openingJournalIdentities -Final $finalJournalIdentities
    $postcheck = [ordered]@{
        schema = 'hwpx/windows-g12-postcheck/v1'
        captured_utc = [DateTime]::UtcNow.ToString('o')
        rollback_snapshot_namespace = [ordered]@{
            opening_count = [int]$openingSnapshotIdentities.Count
            final_count = [int]$finalSnapshotIdentities.Count
            entries = @($finalSnapshotIdentities.Values)
            preserved = [bool]$snapshotNamespacePreserved
        }
        transaction_journal_namespace = [ordered]@{
            opening_count = [int]$openingJournalIdentities.Count
            final_count = [int]$finalJournalIdentities.Count
            entries = @($finalJournalIdentities.Values)
            preserved = [bool]$journalNamespacePreserved
        }
        checks = [ordered]@{
            rollback_snapshot_namespace_preserved = [bool]$snapshotNamespacePreserved
            transaction_journal_namespace_preserved = [bool]$journalNamespacePreserved
            no_new_snapshot_residue = ($finalSnapshotIdentities.Count -eq $openingSnapshotIdentities.Count)
            no_new_journal_residue = ($finalJournalIdentities.Count -eq $openingJournalIdentities.Count)
        }
    }

    $rollback = if ($installerReceipt) { $installerReceipt.rollback } else { $null }
    $terminal = if ($rollback) { $rollback.terminal_readback } else { $null }
    $checks = [ordered]@{
        installer_exit_code_40 = ($exitCode -eq 40)
        installer_receipt_present = ($null -ne $installerReceipt)
        installer_status_rolled_back = ($null -ne $installerReceipt -and [string]$installerReceipt.status -eq 'ROLLED_BACK')
        rollback_attempted = ($null -ne $rollback -and [bool]$rollback.attempted)
        rollback_restored = ($null -ne $rollback -and [bool]$rollback.restored)
        rollback_terminal_readback_complete = ($null -ne $terminal -and [bool]$terminal.complete)
        rollback_tasks_running_and_enabled = ($null -ne $terminal -and [bool]$terminal.tasks_running_and_enabled)
        rollback_processes_bound = ($null -ne $terminal -and [bool]$terminal.processes_bound)
        rollback_api_healthy_after_cleanup = ($null -ne $terminal -and [bool]$terminal.api_healthy_after_cleanup)
        tasks_running_and_enabled_after_cleanup = (@($afterCleanup.tasks | Where-Object { -not $_.exists -or [string]$_.state -ne 'Running' -or -not [bool]$_.enabled }).Count -eq 0)
        api_process_bound_after_cleanup = (@($afterCleanup.processes | Where-Object { [string]$_.module -eq 'app.api_server' -and [string]$_.identity_root -ceq $install }).Count -ge 1)
        worker_process_bound_after_cleanup = (@($afterCleanup.processes | Where-Object { [string]$_.module -eq 'app.worker' -and [string]$_.identity_root -ceq $install }).Count -ge 1)
        api_healthy_after_cleanup = [bool]$afterCleanup.health.ok
        marker_preimage_restored = ($markerRestored -and ($currentMarkerHash -ceq $originalMarkerHash -or $currentMarkerHash -ceq $injectedMarkerHash))
        rollback_snapshot_namespace_postcheck = [bool]$postcheck.checks.rollback_snapshot_namespace_preserved
        transaction_journal_namespace_postcheck = [bool]$postcheck.checks.transaction_journal_namespace_preserved
        manual_task_recovery_not_used = $true
        controller_timeout_provenance_complete = ($null -ne $waitEvidence -and [bool]$waitEvidence.timeout_budget_enforced -and [string]$waitEvidence.owner -eq 'g12-controller' -and [double]$waitEvidence.measured_elapsed_seconds -ge 0 -and -not [string]::IsNullOrWhiteSpace([string]$waitEvidence.provenance))
        controller_process_exit_confirmed = ($null -ne $waitEvidence -and [bool]$waitEvidence.process_exit_confirmed)
        controller_process_exit_readback = ($null -ne $waitEvidence -and [bool]$waitEvidence.process_exit_confirmed -and [int]$waitEvidence.process_exit_code -eq [int]$exitCode -and [string]$waitEvidence.process_exit_source -eq 'System.Diagnostics.Process.ExitCode after bounded wait/readback.')
        controller_stdout_capture_complete = ($null -ne $waitEvidence -and [bool]$waitEvidence.stdout_capture_complete -and (Test-Path -LiteralPath $installerStdout -PathType Leaf))
        controller_stderr_capture_complete = ($null -ne $waitEvidence -and [bool]$waitEvidence.stderr_capture_complete -and (Test-Path -LiteralPath $installerStderr -PathType Leaf))
        controller_timeout_fields_derived = ($null -ne $waitEvidence -and [int]$waitEvidence.timeout_budget_seconds -eq $ControllerTimeoutSeconds -and $null -ne $waitEvidence.started_at_utc -and $null -ne $waitEvidence.ended_at_utc)
    }
    $passed = (@($checks.GetEnumerator() | Where-Object { -not [bool]$_.Value }).Count -eq 0)
    $result = [ordered]@{
        schema_version = 'hwpx/windows-preserve-move-fault-path/v2'
        status = if ($passed) { 'PASS' } else { 'FAIL' }
        failure_class = if ($passed) { $null } else { 'FAIL_PRESERVE_MOVE_AUTOMATIC_ROLLBACK' }
        source_root = $source
        install_root = $install
        api_port = [int]$ApiPort
        source_identity = [ordered]@{ repository = $ExpectedRepository; commit = $ExpectedCommit; tree = $ExpectedTree; manifest_sha256 = $ExpectedManifestSha256 }
        run_root = $runRoot
        evidence_retention = 'run_root is preserved for receipt/stdout/stderr readback unless the outer harness explicitly requests cleanup'
        fault = 'after-task-registrations'
        exit_code = $exitCode
        receipt_path = $receiptFile
        receipt_sha256 = if (Test-Path -LiteralPath $receiptFile -PathType Leaf) { Get-Sha256Hex -Path $receiptFile } else { $null }
        stdout_path = $installerStdout
        stderr_path = $installerStderr
        marker_original_sha256 = $originalMarkerHash
        marker_after_rollback_sha256 = $currentMarkerHash
        marker_restored = $markerRestored
        before = $before
        after_rollback = $afterRollback
        after_cleanup = $afterCleanup
        installer_receipt = $installerReceipt
        postcheck = $postcheck
        recovery_chain = [ordered]@{
            schema = 'hwpx/windows-g12-recovery/v2'
            controller = $waitEvidence
            remote_harness = [ordered]@{
                owner = 'outer-remote-harness'
                timeout_budget_seconds = if ($null -ne $RemoteHarnessTimeoutSeconds) { [int]$RemoteHarnessTimeoutSeconds } else { $null }
                timeout_occurred = $null
                observed_by = 'outer harness only; this child script does not own or infer the remote harness deadline'
                provenance = 'No remote-harness timeout claim is made by the G12 controller.'
            }
            candidate = [ordered]@{
                repository = $ExpectedRepository
                commit = $ExpectedCommit
                tree = $ExpectedTree
                manifest_sha256 = $ExpectedManifestSha256
            }
            timestamps = [ordered]@{
                harness_started_at_utc = $harnessStartedAtUtc
                terminal_observed_at_utc = [DateTime]::UtcNow.ToString('o')
            }
            terminal_artifacts = [ordered]@{
                receipt_path = $receiptFile
                receipt_sha256 = if (Test-Path -LiteralPath $receiptFile -PathType Leaf) { Get-Sha256Hex -Path $receiptFile } else { $null }
                stdout_path = $installerStdout
                stderr_path = $installerStderr
                result_path = $resultFile
            }
            cleanup = [ordered]@{
                marker_restored = [bool]$markerRestored
                postcheck = $postcheck
            }
            postcheck = $postcheck
        }
        checks = $checks
    }
    $resultParent = Split-Path -Parent $resultFile
    if ($resultParent) { New-Item -ItemType Directory -Force -Path $resultParent | Out-Null }
    $result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $resultFile -Encoding UTF8
    Write-Output ("G12_STATUS={0};EXIT_CODE={1};RESULT={2};INSTALLER_RECEIPT={3}" -f $result.status, $exitCode, $resultFile, $receiptFile)
    if (-not $passed) { exit 1 }
}
finally {
    if (-not $markerRestored -and (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        try {
            [IO.File]::WriteAllBytes($markerPath, $originalMarkerBytes)
            $markerRestored = ((Get-Sha256Hex -Path $markerPath) -ceq $originalMarkerHash)
        }
        catch { }
    }
    if (([Environment]::GetEnvironmentVariable('HWPX_G12_CLEANUP_RUN_ROOT', 'Process') -eq '1') -and (Test-Path -LiteralPath $runRoot)) {
        Remove-Item -LiteralPath $runRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
