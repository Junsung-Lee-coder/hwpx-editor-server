param(
    [ValidateSet('install', 'api', 'worker', 'start', 'stop', 'status')]
    [string]$Action = 'status',
    [switch]$SkipInstall,
    [Nullable[int]]$ApiPort
)

$ErrorActionPreference = 'Stop'

$SourceRoot = Split-Path -Parent $PSScriptRoot
$PackagedRoot = Join-Path $SourceRoot '.hwpx-install'
$Root = if (Test-Path -LiteralPath (Join-Path $PackagedRoot '.hwpx-install.json') -PathType Leaf) { $PackagedRoot } else { $SourceRoot }
$commonPath = Join-Path $PSScriptRoot 'windows_install_common.psm1'
Import-Module $commonPath -Force

# Capture the root/marker object identities before any action admission, then
# revalidate them after the shared lifecycle lock is held. A startup race with
# PreserveMove must not select one generation and mutate another.
$writerRootIdentity = Get-PathObjectIdentity -Path $Root -RequireExisting
$writerMarkerIdentity = $null

# Bind every child runtime to the exact installed generation.  The installer
# writes this marker only after the candidate manifest has been verified; the
# Python worker then repeats the manifest readback before publishing PASS.
$candidateMarkerPath = Join-Path $Root '.hwpx-install.json'
if (Test-Path -LiteralPath $candidateMarkerPath -PathType Leaf) {
    $writerMarkerIdentity = Get-PathObjectIdentity -Path $candidateMarkerPath -RequireExisting
    $candidateMarkerCapture = Read-BoundedText -Path $candidateMarkerPath -MaxChars 262144
    if ($candidateMarkerCapture.truncated) { throw "Candidate marker is too large: $candidateMarkerPath" }
    $candidateMarker = ConvertFrom-Json -InputObject ([string]$candidateMarkerCapture.text)
    $candidateGeneration = [string]$candidateMarker.candidate_generation
    if ([string]::IsNullOrWhiteSpace($candidateGeneration)) {
        throw "Candidate marker does not contain candidate_generation: $candidateMarkerPath"
    }
    $env:HWP_CANDIDATE_GENERATION = $candidateGeneration
}

function Get-WriterEnvSetting {
    param([Parameter(Mandatory = $true)][string]$Name)
    $envPath = Join-Path $Root '.env'
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { return $null }
    $capture = Read-BoundedText -Path $envPath -MaxChars 65536
    if ($capture.truncated) { throw "Environment configuration is too large: $envPath" }
    foreach ($line in @([string]$capture.text -split "`r?`n")) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*?)\s*(?:#.*)?$')) {
            return [string]$matches[1]
        }
    }
    return $null
}

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$ConfiguredPython = if (-not [string]::IsNullOrWhiteSpace($env:HWP_PYTHON)) { $env:HWP_PYTHON } else { Get-WriterEnvSetting -Name 'HWP_PYTHON' }
$SpoolRoot = Join-Path $Root 'spool'
$LogsRoot = Join-Path $SpoolRoot 'logs'
$ReadinessPath = Join-Path $SpoolRoot 'readiness\worker_ready.json'
$ViewerSessionPath = Join-Path $SpoolRoot 'observation\viewer_session.json'
$LaunchStatusPath = Join-Path $LogsRoot 'writer_v1.launch_status.json'
$LauncherLogPath = Join-Path $LogsRoot 'writer_v1.launcher.log'
$requestedApiPort = $ApiPort
if ($null -eq $requestedApiPort -and -not [string]::IsNullOrWhiteSpace($env:HWP_API_PORT)) {
    $environmentPort = 0
    if (-not [int]::TryParse($env:HWP_API_PORT, [ref]$environmentPort)) {
        throw "HWP_API_PORT is not a valid integer: $env:HWP_API_PORT"
    }
    $requestedApiPort = $environmentPort
}
$configuredPort = Resolve-ApiPort -InstallRoot $Root -RequestedApiPort $requestedApiPort
$ApiPort = $configuredPort
$env:HWP_API_PORT = [string]$ApiPort
$ApiHealthUri = "http://127.0.0.1:$ApiPort/health"
$RuntimeReadinessUri = "http://127.0.0.1:$ApiPort/runtime-readiness"
$ViewerSessionUri = "http://127.0.0.1:$ApiPort/observation-viewer/session"
$ApiTaskName = if ([string]::IsNullOrWhiteSpace($env:HWP_API_TASK_NAME)) { $envTask = Get-WriterEnvSetting -Name 'HWP_API_TASK_NAME'; if ([string]::IsNullOrWhiteSpace($envTask)) { 'hwpx-editor-api' } else { $envTask } } else { $env:HWP_API_TASK_NAME }
$WorkerTaskName = if ([string]::IsNullOrWhiteSpace($env:HWP_WORKER_TASK_NAME)) { $envTask = Get-WriterEnvSetting -Name 'HWP_WORKER_TASK_NAME'; if ([string]::IsNullOrWhiteSpace($envTask)) { 'hwpx-editor-worker' } else { $envTask } } else { $env:HWP_WORKER_TASK_NAME }
$TaskPath = if ([string]::IsNullOrWhiteSpace($env:HWP_TASK_PATH)) { Get-WriterEnvSetting -Name 'HWP_TASK_PATH' } else { $env:HWP_TASK_PATH }
if ([string]::IsNullOrWhiteSpace($TaskPath)) { $TaskPath = '\' }
$TaskPath = Assert-CanonicalScheduledTaskPath -TaskPath $TaskPath
$InteractiveTaskNameByRole = @{
    api = $ApiTaskName
    worker = $WorkerTaskName
}

$RoleConfig = @{
    api = @{
        EntryModule = 'app.api_server'
        PidPath = (Join-Path $LogsRoot 'writer_v1.api.pid.json')
        StdoutPath = (Join-Path $LogsRoot 'writer_v1.api.stdout.log')
        StderrPath = (Join-Path $LogsRoot 'writer_v1.api.stderr.log')
        PythonLogPath = (Join-Path $LogsRoot 'api.log')
    }
    worker = @{
        EntryModule = 'app.worker'
        PidPath = (Join-Path $LogsRoot 'writer_v1.worker.pid.json')
        StdoutPath = (Join-Path $LogsRoot 'writer_v1.worker.stdout.log')
        StderrPath = (Join-Path $LogsRoot 'writer_v1.worker.stderr.log')
        PythonLogPath = (Join-Path $LogsRoot 'worker.log')
    }
}

function Get-UtcTimestamp {
    return [DateTime]::UtcNow.ToString('o')
}

function Get-CurrentWriterSessionId {
    try {
        return [int](Get-Process -Id $PID -ErrorAction Stop).SessionId
    }
    catch {
        return $null
    }
}

function Test-WriterSessionZeroLaunch {
    $sessionId = Get-CurrentWriterSessionId
    return ($null -ne $sessionId -and $sessionId -eq 0)
}

function Get-InteractiveTaskRegistration {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    $taskName = $InteractiveTaskNameByRole[$Role]
    if ([string]::IsNullOrWhiteSpace([string]$taskName)) {
        return $null
    }

    try {
        return Get-ScheduledTaskExact -TaskName $taskName -TaskPath $TaskPath
    }
    catch {
        return $null
    }
}

function Assert-WriterTaskIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [object]$Task
    )

    $identity = Get-ScheduledTaskIdentity -TaskName ([string]$Task.TaskName) -TaskPath $TaskPath
    if (-not $identity.exists) { throw "writer_v1 $Role task readback disappeared: $($Task.TaskName)" }
    $expectedRoot = Get-CanonicalPath -Path $Root -RequireExisting
    $expectedPython = Join-Path $expectedRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $expectedPython -PathType Leaf)) { $expectedPython = Resolve-PythonExe }
    $expectedArguments = if ($Role -eq 'api') { '-m app.api_server' } else { '-m app.worker' }
    $triggerUser = if ($identity.trigger_user -is [array]) { [string]$identity.trigger_user[0] } else { [string]$identity.trigger_user }
    $compatible = $false
    try {
        $compatible = (
            (Get-CanonicalPath -Path ([string]$identity.working_directory) -RequireExisting) -eq $expectedRoot -and
            (Get-CanonicalPath -Path ([string]$identity.execute) -RequireExisting) -eq (Get-CanonicalPath -Path $expectedPython -RequireExisting) -and
            [string]$identity.arguments -eq $expectedArguments -and
            [string]$identity.action_type -eq 'Exec' -and
            [int]$identity.action_count -eq 1 -and
            [int]$identity.xml_action_count -eq 1 -and
            [int]$identity.xml_exec_action_count -eq 1 -and
            [string]$identity.trigger_type -eq 'LogonTrigger' -and
            (Test-ScheduledTaskLogonTypeEquivalent -Actual $identity.logon_type -Expected 'Interactive') -and
            (Test-ScheduledTaskRunLevelEquivalent -Actual $identity.run_level -Expected 'Limited') -and
            [string]$identity.start_when_available -ieq 'true' -and
            (Test-CanonicalTaskSettings -Identity $identity) -and
            (Test-WindowsPrincipalEquivalent -Actual $identity.principal -Expected $triggerUser) -and
            -not [string]::IsNullOrWhiteSpace([string]$identity.task_identity_hash)
        )
    }
    catch { $compatible = $false }
    if (-not $compatible) { throw "writer_v1 $Role scheduled task identity does not match the installed candidate runtime." }
    return $identity
}

function Start-InteractiveTaskLaunch {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    $sessionId = Get-CurrentWriterSessionId
    $task = Get-InteractiveTaskRegistration -Role $Role
    if ($null -eq $task) {
        throw "writer_v1 $Role launch requires an interactive desktop session. No scheduled task is registered for session-0 delegation."
    }
    $taskIdentity = Assert-WriterTaskIdentity -Role $Role -Task $task

    if ($task.State -eq 'Running') {
        $context = [ordered]@{
            role = $Role
            mode = 'interactive_task_already_running'
            task_name = $task.TaskName
            task_path = $task.TaskPath
            session_id = $sessionId
            task_identity_hash = $taskIdentity.task_identity_hash
            owned = $false
        }
        Write-LauncherEvent -Message "writer_v1 $Role reusing interactive scheduled task because the current session is non-interactive" -Context $context
        return $context
    }

    Start-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath
    $context = [ordered]@{
        role = $Role
        mode = 'interactive_task_started'
        task_name = $task.TaskName
        task_path = $task.TaskPath
        session_id = $sessionId
        task_identity_hash = $taskIdentity.task_identity_hash
        owned = $true
    }
    Write-LauncherEvent -Message "writer_v1 $Role delegated to interactive scheduled task because the current session is non-interactive" -Context $context
    return $context
}

function Ensure-WriterDirectories {
    foreach ($path in @($SpoolRoot, $LogsRoot, (Split-Path -Parent $ReadinessPath), (Split-Path -Parent $ViewerSessionPath))) {
        if (-not (Test-Path $path)) {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
        }
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [object]$Payload
    )

    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    Write-JsonReceipt -Path $Path -Value $Payload | Out-Null
}

function Read-JsonFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    try {
        $capture = Read-BoundedText -Path $Path -MaxChars 262144
        if ($capture.truncated) { return $null }
        return ConvertFrom-Json -InputObject ([string]$capture.text)
    }
    catch {
        return $null
    }
}

function Resolve-PythonExe {
    $candidates = @()
    if (Test-Path $VenvPython) {
        $candidates += $VenvPython
    }
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPython)) {
        $candidates += $ConfiguredPython
    }
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pathPython -and -not [string]::IsNullOrWhiteSpace([string]$pathPython.Source)) {
        $candidates += $pathPython.Source
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    throw 'Python executable not found. Set HWP_PYTHON, create .venv, or ensure python.exe is on PATH.'
}

function Ensure-Install {
    Push-Location $Root
    try {
        Ensure-WriterDirectories
        $pythonExe = Resolve-PythonExe
        $requirementsPath = Join-Path $Root 'requirements-windows.lock'
        if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
            throw "Hash-pinned Windows dependency lock is missing: $requirementsPath"
        }
        $nativeResult = Invoke-NativeChecked -FilePath $pythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes', '-r', $requirementsPath) -WorkingDirectory $Root -AllowNonZero
        if (-not $nativeResult.accepted) {
            throw "Hash-pinned dependency installation failed with exit code $($nativeResult.exit_code)."
        }
        Write-JsonReceipt -Path $LaunchStatusPath -Value $nativeResult | Out-Null
        if (-not (Test-Path '.env') -and (Test-Path 'config.example')) {
            $templateContent = [System.IO.File]::ReadAllText((Get-Item 'config.example').FullName)
            $templateContent = [regex]::Replace($templateContent, '(?m)^\s*HWP_API_PORT\s*=.*$', "HWP_API_PORT=$ApiPort")
            [System.IO.File]::WriteAllText((Join-Path $Root '.env'), $templateContent, (New-Object System.Text.UTF8Encoding($false)))
        }
    }
    finally {
        Pop-Location
    }
}

function Write-LauncherEvent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level = 'INFO',
        [object]$Context
    )

    Ensure-WriterDirectories
    $timestamp = Get-UtcTimestamp
    $line = "$timestamp | $Level | $Message"
    if ($null -ne $Context) {
        try {
            $contextJson = $Context | ConvertTo-Json -Depth 8 -Compress
            if (-not [string]::IsNullOrWhiteSpace($contextJson)) {
                $line = "$line | $contextJson"
            }
        }
        catch {
        }
    }
    Add-Content -Path $LauncherLogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-RoleConfig {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    return $RoleConfig[$Role]
}

function ConvertTo-WindowsProcessArgument {
    param([AllowNull()][object]$Value)
    $text = if ($null -eq $Value) { '' } else { [string]$Value }
    if ($text.Length -gt 0 -and $text -notmatch '[\s"]') { return $text }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $text.ToCharArray()) {
        if ($character -eq [char]92) {
            $slashes++
            continue
        }
        if ($character -eq [char]34) {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2 + 1)) -join '') }
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes) -join '') }
        [void]$builder.Append($character)
        $slashes = 0
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2)) -join '') }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-WindowsProcessArgumentList {
    param([AllowNull()][object[]]$Values)
    return [string]::Join(' ', @($Values | ForEach-Object { ConvertTo-WindowsProcessArgument -Value $_ }))
}

function Get-ProcessState {
    param([object]$ProcessIdValue)

    if ($null -eq $ProcessIdValue -or [string]::IsNullOrWhiteSpace([string]$ProcessIdValue)) {
        return [ordered]@{
            pid = $null
            running = $false
        }
    }

    $pidValue = $null
    try {
        $pidValue = [int]$ProcessIdValue
    }
    catch {
        return [ordered]@{
            pid = $ProcessIdValue
            running = $false
        }
    }

    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [ordered]@{
            pid = $pidValue
            running = $false
        }
    }

    $startedAt = $null
    try {
        $startedAt = $process.StartTime.ToUniversalTime().ToString('o')
    }
    catch {
    }

    return [ordered]@{
        pid = $pidValue
        running = $true
        process_name = $process.ProcessName
        started_at = $startedAt
    }
}

function Get-WriterProcessIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessIdValue)
    if ($ProcessIdValue -le 0) { return $null }
    $row = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessIdValue" -ErrorAction SilentlyContinue
    if ($null -eq $row) { return $null }
    $process = Get-Process -Id $ProcessIdValue -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $executablePath = [string]$row.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($executablePath)) { return $null }
    try { $canonicalExecutable = Get-CanonicalPath -Path $executablePath -RequireExisting } catch { return $null }
    $commandLine = Limit-Text -Value ([string]$row.CommandLine) -MaxChars 4096
    $startedAt = $null
    try { $startedAt = $process.StartTime.ToUniversalTime().ToString('o') } catch { }
    if ([string]::IsNullOrWhiteSpace($startedAt)) { $startedAt = [string]$row.CreationDate }
    return [ordered]@{
        process_id = [int]$ProcessIdValue
        pid = [int]$ProcessIdValue
        process_name = [string]$row.Name
        executable_path = $canonicalExecutable
        command_line = $commandLine
        command_line_hash = Get-TextSha256 -Value $commandLine
        tracked_start_time = $startedAt
        creation_date = [string]$row.CreationDate
        root = $Root
    }
}

function Test-WriterProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('api', 'worker')][string]$Role,
        [Parameter(Mandatory = $true)][int]$ProcessIdValue,
        [Parameter(Mandatory = $true)][object]$ExpectedIdentity,
        [switch]$Bootstrap
    )
    try {
        $actual = Get-WriterProcessIdentity -ProcessIdValue $ProcessIdValue
        if ($null -eq $actual) { return $false }
        $expectedPid = [int](Get-OptionalPropertyValue -Object $ExpectedIdentity -Name 'process_id')
        if ($expectedPid -ne $ProcessIdValue -or [int]$actual.process_id -ne $expectedPid) { return $false }
        $expectedExecutable = [string](Get-OptionalPropertyValue -Object $ExpectedIdentity -Name 'executable_path')
        if ([string]::IsNullOrWhiteSpace($expectedExecutable) -or (Get-CanonicalPath -Path $expectedExecutable -RequireExisting) -ne [string]$actual.executable_path) { return $false }
        $expectedStart = [string](Get-OptionalPropertyValue -Object $ExpectedIdentity -Name 'tracked_start_time')
        if (-not [string]::IsNullOrWhiteSpace($expectedStart) -and $expectedStart -ne [string]$actual.tracked_start_time) { return $false }
        $expectedHash = [string](Get-OptionalPropertyValue -Object $ExpectedIdentity -Name 'command_line_hash')
        if ([string]::IsNullOrWhiteSpace($expectedHash) -or $expectedHash -ne [string]$actual.command_line_hash) { return $false }
        $line = [string]$actual.command_line
        if (-not (Test-CommandLinePathToken -CommandLine $line -Path $Root)) { return $false }
        if ($Bootstrap) {
            $processName = [string](Get-OptionalPropertyValue -Object $ExpectedIdentity -Name 'process_name')
            if ($processName -match '(?i)(?:powershell|pwsh)') {
                if ($line -notmatch '(?i)(?:powershell|pwsh)(?:\.exe)?') { return $false }
                if ($line -notmatch '(?i)(?:-File|writer_v1\.ps1)') { return $false }
            }
            elseif (-not (Test-CommandLineModuleToken -CommandLine $line -ModuleName ([string](Get-RoleConfig -Role $Role).EntryModule))) {
                return $false
            }
        }
        else {
            $entryModule = [string](Get-RoleConfig -Role $Role).EntryModule
            if (-not (Test-CommandLineModuleToken -CommandLine $line -ModuleName $entryModule)) { return $false }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Get-WriterProcessIdentityWithRetry {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessIdValue,
        [ValidateRange(1, 100)][int]$Attempts = 20
    )
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $identity = Get-WriterProcessIdentity -ProcessIdValue $ProcessIdValue
        if ($null -ne $identity) { return $identity }
        Start-Sleep -Milliseconds 50
    }
    return $null
}

function Test-WriterKillCandidate {
    param(
        [Parameter(Mandatory = $true)][int]$CandidatePid,
        [Parameter(Mandatory = $true)][int]$TrustedRootPid,
        [Parameter(Mandatory = $true)][ValidateSet('api', 'worker')][string]$Role,
        [Parameter(Mandatory = $true)][object]$PidInfo,
        [int[]]$TrustedPids = @()
    )
    $trackedPid = [int](Get-OptionalPropertyValue -Object $PidInfo -Name 'pid')
    $bootstrapPid = [int](Get-OptionalPropertyValue -Object $PidInfo -Name 'bootstrap_pid')
    if ($CandidatePid -eq $trackedPid) {
        $expected = Get-OptionalPropertyValue -Object $PidInfo -Name 'process_identity'
        return ($null -ne $expected -and (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $CandidatePid -ExpectedIdentity $expected))
    }
    if ($CandidatePid -eq $bootstrapPid) {
        $expected = Get-OptionalPropertyValue -Object $PidInfo -Name 'bootstrap_process_identity'
        return ($null -ne $expected -and (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $CandidatePid -ExpectedIdentity $expected -Bootstrap))
    }
    $rows = @(Get-ProcessTreeSnapshot -RootPid $TrustedRootPid)
    $byPid = @{}
    foreach ($row in $rows) { $byPid[[int]$row.process_id] = $row }
    if (-not $byPid.ContainsKey($CandidatePid)) { return $false }
    $line = [string]$byPid[$CandidatePid].command_line
    if ([string]::IsNullOrWhiteSpace($line) -or -not (Test-CommandLinePathToken -CommandLine $line -Path $Root)) { return $false }
    if (-not (Test-CommandLineModuleToken -CommandLine $line -ModuleName ([string](Get-RoleConfig -Role $Role).EntryModule)) -and $CandidatePid -ne $TrustedRootPid) { return $false }
    $cursor = $CandidatePid
    for ($depth = 0; $depth -le $rows.Count; $depth++) {
        if ($cursor -eq $TrustedRootPid -or $cursor -in $TrustedPids) { return $true }
        if (-not $byPid.ContainsKey($cursor)) { return $false }
        $cursor = [int]$byPid[$cursor].parent_process_id
    }
    return $false
}

function Read-RolePidInfo {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    $config = Get-RoleConfig -Role $Role
    return Read-JsonFile -Path $config.PidPath
}

function Clear-StaleRolePidFile {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    $config = Get-RoleConfig -Role $Role
    $pidInfo = Read-RolePidInfo -Role $Role
    if ($null -eq $pidInfo) {
        return
    }

    $pidValue = $null
    try {
        if ($null -ne $pidInfo.pid) {
            $pidValue = [int]$pidInfo.pid
        }
    }
    catch {
        $pidValue = $null
    }

    if ($null -eq $pidValue) {
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
        return
    }

    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
        return
    }
    $expectedIdentity = Get-OptionalPropertyValue -Object $pidInfo -Name 'process_identity'
    if ($null -eq $expectedIdentity -or -not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $pidValue -ExpectedIdentity $expectedIdentity)) {
        # A stale/tampered PID file is not authority to stop the live process.
        # Removing only the writer-owned metadata lets a future launch recover
        # without touching a PID-reused or otherwise unrelated process.
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
    }
}

function Set-RolePidFile {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [int]$TrackedPid,
        [int]$BootstrapPid,
        [int]$LauncherPid = $PID,
        [string]$TrackedProcessName,
        [string]$BootstrapProcessName
    )

    $config = Get-RoleConfig -Role $Role
    $processIdentity = Get-WriterProcessIdentity -ProcessIdValue $TrackedPid
    $bootstrapIdentity = Get-WriterProcessIdentity -ProcessIdValue $BootstrapPid
    if ($null -eq $processIdentity -or $null -eq $bootstrapIdentity) {
        throw "writer_v1 $Role could not capture stable process identity before writing the PID record."
    }
    if (-not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $TrackedPid -ExpectedIdentity $processIdentity) -or
        -not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $BootstrapPid -ExpectedIdentity $bootstrapIdentity -Bootstrap)) {
        throw "writer_v1 $Role process identity did not match the selected runtime before writing the PID record."
    }
    $payload = [ordered]@{
        schema_version = 'writer-v1-role-pid/v1'
        role = $Role
        pid = $TrackedPid
        bootstrap_pid = $BootstrapPid
        launcher_pid = $LauncherPid
        tracked_process_name = $TrackedProcessName
        bootstrap_process_name = $BootstrapProcessName
        started_at = Get-UtcTimestamp
        root = $Root
        entry_module = $config.EntryModule
        process_identity = $processIdentity
        bootstrap_process_identity = $bootstrapIdentity
        process_identity_schema = 'writer-v1-process-identity/v1'
        tracked_start_time = $processIdentity.tracked_start_time
        bootstrap_start_time = $bootstrapIdentity.tracked_start_time
        command_line_hash = $processIdentity.command_line_hash
        bootstrap_command_line_hash = $bootstrapIdentity.command_line_hash
    }
    Write-JsonFile -Path $config.PidPath -Payload $payload
}

function Clear-RolePidFile {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role,
        [switch]$Force
    )

    $config = Get-RoleConfig -Role $Role
    if (-not (Test-Path $config.PidPath)) {
        return
    }

    if ($Force) {
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
        return
    }

    $pidInfo = Read-RolePidInfo -Role $Role
    if ($null -eq $pidInfo) {
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
        return
    }

    try {
        $trackedPidMatches = ($null -ne $pidInfo.pid -and [int]$pidInfo.pid -eq $PID)
        $launcherPidMatches = ($null -ne $pidInfo.launcher_pid -and [int]$pidInfo.launcher_pid -eq $PID)
        if ($trackedPidMatches -or $launcherPidMatches) {
            Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
        }
    }
    catch {
        Remove-Item -Path $config.PidPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-RoleStatus {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    $config = Get-RoleConfig -Role $Role
    $pidInfo = Read-RolePidInfo -Role $Role
    $pidValue = $null
    if ($null -ne $pidInfo) {
        try {
            if ($null -ne $pidInfo.pid) {
                $pidValue = [int]$pidInfo.pid
            }
        }
        catch {
            $pidValue = $null
        }
    }

    $processState = Get-ProcessState -ProcessIdValue $pidValue
    return [ordered]@{
        role = $Role
        pid_file = $config.PidPath
        pid_file_exists = (Test-Path $config.PidPath)
        pid = $processState.pid
        running = $processState.running
        process_name = $processState.process_name
        started_at = $processState.started_at
        stale_pid_file = ((Test-Path $config.PidPath) -and (-not $processState.running))
        launcher_stdout_log = $config.StdoutPath
        launcher_stderr_log = $config.StderrPath
        python_log = $config.PythonLogPath
        entry_module = $config.EntryModule
    }
}

function Get-ProcessTreeSnapshot {
    param([int]$RootPid)

    if ($null -eq $RootPid -or $RootPid -le 0) {
        return @()
    }

    $processRows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Select-Object ProcessId, ParentProcessId, Name, CommandLine)
    if ($processRows.Count -eq 0) {
        $process = Get-Process -Id $RootPid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            return @()
        }

        return @([ordered]@{
            process_id = $RootPid
            parent_process_id = $null
            depth = 0
            name = $process.ProcessName
            command_line = $null
        })
    }

    $childrenByParent = @{}
    $rowsByPid = @{}
    foreach ($processRow in $processRows) {
        $pidValue = [int]$processRow.ProcessId
        $parentPid = [int]$processRow.ParentProcessId
        $rowsByPid[$pidValue] = $processRow
        if (-not $childrenByParent.ContainsKey($parentPid)) {
            $childrenByParent[$parentPid] = New-Object System.Collections.ArrayList
        }
        [void]$childrenByParent[$parentPid].Add($pidValue)
    }

    if (-not $rowsByPid.ContainsKey([int]$RootPid)) {
        return @()
    }

    $ordered = New-Object System.Collections.ArrayList
    $pending = New-Object System.Collections.ArrayList
    $seen = @{}
    [void]$pending.Add([ordered]@{ pid = [int]$RootPid; depth = 0 })

    while ($pending.Count -gt 0) {
        $index = $pending.Count - 1
        $current = $pending[$index]
        $pending.RemoveAt($index)
        $currentPid = [int]$current.pid
        if ($seen.ContainsKey($currentPid)) {
            continue
        }

        $seen[$currentPid] = $true
        $row = $rowsByPid[$currentPid]
        [void]$ordered.Add([ordered]@{
            process_id = $currentPid
            parent_process_id = [int]$row.ParentProcessId
            depth = [int]$current.depth
            name = [string]$row.Name
            command_line = [string]$row.CommandLine
        })

        if ($childrenByParent.ContainsKey($currentPid)) {
            foreach ($childPid in $childrenByParent[$currentPid]) {
                [void]$pending.Add([ordered]@{ pid = [int]$childPid; depth = ([int]$current.depth + 1) })
            }
        }
    }

    return @($ordered)
}

function Get-ProcessTreeIds {
    param([int]$RootPid)

    return @(
        Get-ProcessTreeSnapshot -RootPid $RootPid |
            ForEach-Object { [int]$_.process_id }
    )
}

function Stop-WriterOwnedLaunchTree {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('api', 'worker')][string]$Role,
        [Parameter(Mandatory = $true)][object]$Launch,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 15
    )

    $rootPid = 0
    try { $rootPid = [int](Get-OptionalPropertyValue -Object $Launch -Name 'pid') } catch { $rootPid = 0 }
    $expectedIdentity = Get-OptionalPropertyValue -Object $Launch -Name 'process_identity'
    if ($rootPid -le 0 -or $null -eq $expectedIdentity) {
        throw "writer_v1 $Role launch ledger is missing an authenticated bootstrap process identity."
    }

    $rootProcess = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
    if ($null -eq $rootProcess) {
        return [ordered]@{ role = $Role; root_pid = $rootPid; root_missing = $true; released = $true; stopped_pids = @() }
    }
    if (-not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $rootPid -ExpectedIdentity $expectedIdentity -Bootstrap)) {
        throw "writer_v1 $Role cleanup refused to terminate a replaced bootstrap PID $rootPid."
    }

    $initialTree = @(Get-ProcessTreeSnapshot -RootPid $rootPid)
    if ($initialTree.Count -eq 0) {
        throw "writer_v1 $Role cleanup could not establish the owned bootstrap process tree: $rootPid"
    }
    $moduleName = [string](Get-RoleConfig -Role $Role).EntryModule
    $stopped = New-Object System.Collections.ArrayList
    foreach ($row in @($initialTree | Sort-Object -Property depth -Descending)) {
        $candidatePid = [int]$row.process_id
        $currentRoot = Get-WriterProcessIdentityWithRetry -ProcessIdValue $rootPid -Attempts 3
        if ($null -eq $currentRoot -or -not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $rootPid -ExpectedIdentity $expectedIdentity -Bootstrap)) {
            throw "writer_v1 $Role cleanup bootstrap identity changed at the stop boundary: $rootPid"
        }
        $currentTree = @(Get-ProcessTreeSnapshot -RootPid $rootPid)
        $currentRow = $currentTree | Where-Object { [int]$_.process_id -eq $candidatePid } | Select-Object -First 1
        if ($null -eq $currentRow) { continue }
        $candidateProcess = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
        if ($null -eq $candidateProcess) { continue }
        if ($candidatePid -ne $rootPid) {
            $line = [string]$currentRow.command_line
            if ([string]::IsNullOrWhiteSpace($line) -or
                -not (Test-CommandLinePathToken -CommandLine $line -Path $Root) -or
                -not (Test-CommandLineModuleToken -CommandLine $line -ModuleName $moduleName)) {
                throw "writer_v1 $Role cleanup refused to terminate an unverified descendant PID $candidatePid."
            }
        }
        try {
            $candidateProcess.Refresh()
            if ($candidateProcess.HasExited) { continue }
            Stop-Process -InputObject $candidateProcess -Force -ErrorAction Stop
            $candidateProcess.WaitForExit(5000) | Out-Null
            [void]$stopped.Add($candidatePid)
        }
        catch {
            throw "writer_v1 $Role owned-launch cleanup failed for PID ${candidatePid}: $($_.Exception.Message)"
        }
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $remainingTree = @(Get-ProcessTreeSnapshot -RootPid $rootPid)
        if ($remainingTree.Count -eq 0) {
            return [ordered]@{ role = $Role; root_pid = $rootPid; root_missing = $false; released = $true; stopped_pids = @($stopped) }
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return [ordered]@{
        role = $Role
        root_pid = $rootPid
        root_missing = $false
        released = $false
        stopped_pids = @($stopped)
        remaining_pids = @($remainingTree | ForEach-Object { [int]$_.process_id })
    }
}

function Get-ListeningProcessIds {
    param([int]$Port)

    $owningPids = New-Object System.Collections.ArrayList

    try {
        if ($null -ne (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
            $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
            foreach ($connection in $connections) {
                if ($null -eq $connection.OwningProcess) {
                    continue
                }

                $owningPid = [int]$connection.OwningProcess
                if (-not $owningPids.Contains($owningPid)) {
                    [void]$owningPids.Add($owningPid)
                }
            }
        }
    }
    catch {
    }

    if ($owningPids.Count -gt 0) {
        return @($owningPids)
    }

    try {
        $netstatLines = @(cmd /c "netstat -aon -p TCP | findstr LISTENING | findstr :$Port")
        foreach ($line in $netstatLines) {
            $parts = @($line -split '\s+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($parts.Count -lt 5) {
                continue
            }

            $owningPid = 0
            if ([int]::TryParse($parts[-1], [ref]$owningPid)) {
                if (-not $owningPids.Contains($owningPid)) {
                    [void]$owningPids.Add($owningPid)
                }
            }
        }
    }
    catch {
    }

    return @($owningPids)
}

function Resolve-RoleTrackedPid {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [int]$BootstrapPid,
        [Parameter(Mandatory = $true)]
        [string]$EntryModule,
        [int]$TimeoutSeconds = 10
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $modulePattern = "-m $EntryModule"
    do {
        $processTree = @(Get-ProcessTreeSnapshot -RootPid $BootstrapPid)
        if ($processTree.Count -eq 0) {
            Start-Sleep -Milliseconds 500
            continue
        }

        $descendants = @($processTree | Where-Object { [int]$_.process_id -ne $BootstrapPid })
        $moduleMatches = @(
            $descendants |
                Where-Object {
                    $commandLine = [string]$_.command_line
                    (-not [string]::IsNullOrWhiteSpace($commandLine)) -and
                    ($commandLine -like "*$modulePattern*") -and
                    (Test-CommandLinePathToken -CommandLine $commandLine -Path $Root)
                } |
                Sort-Object depth, process_id -Descending
        )

        if ($Role -eq 'api') {
            $listenerPids = @(Get-ListeningProcessIds -Port $ApiPort)
            $listenerMatch = @(
                $moduleMatches |
                    Where-Object { [int]$_.process_id -in $listenerPids } |
                    Select-Object -First 1
            )
            if ($listenerMatch.Count -gt 0) {
                return [int]$listenerMatch[0].process_id
            }
        }

        if ($moduleMatches.Count -gt 0) {
            return [int]$moduleMatches[0].process_id
        }

        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    return $BootstrapPid
}

function Get-PortListeners {
    param([int]$Port)

    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        $matches = @(
            $listeners |
                Where-Object { $_.Port -eq $Port } |
                ForEach-Object {
                    [ordered]@{
                        address = $_.Address.ToString()
                        port = $_.Port
                    }
                }
        )
        return $matches
    }
    catch {
        return @(
            [ordered]@{
                address = 'lookup_failed'
                port = $Port
                error = $_.Exception.Message
            }
        )
    }
}

function Invoke-JsonEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [int]$TimeoutSec = 5
    )

    $result = [ordered]@{
        uri = $Uri
        ok = $false
    }

    try {
        $payload = Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec $TimeoutSec
        $result.ok = $true
        $result.payload = $payload
    }
    catch {
        $result.error = $_.Exception.Message
    }

    return $result
}

function Test-WriterHealthPayload {
    param([object]$HealthResult)

    if ($null -eq $HealthResult) {
        return $false
    }
    if (-not $HealthResult.ok) {
        return $false
    }
    if ($null -eq $HealthResult.payload) {
        return $false
    }

    try {
        $status = [string]$HealthResult.payload.status
        $apiPort = [int]$HealthResult.payload.api_port
        return ($status -eq 'ok' -and $apiPort -eq $ApiPort)
    }
    catch {
        return $false
    }
}

function Wait-ForWriterHealth {
    param([int]$TimeoutSeconds = 20)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $health = Invoke-JsonEndpoint -Uri $ApiHealthUri -TimeoutSec 2
        if (Test-WriterHealthPayload -HealthResult $health) {
            return $health
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return Invoke-JsonEndpoint -Uri $ApiHealthUri -TimeoutSec 2
}

function Get-RecentLogTail {
    param(
        [string]$Path,
        [int]$Tail = 10
    )

    if (-not (Test-Path $Path)) {
        return @()
    }

    try {
        return @(Get-Content -Path $Path -Tail $Tail -Encoding UTF8)
    }
    catch {
        return @("<failed to read log: $($_.Exception.Message)>")
    }
}

function Get-LastFailureReason {
    param(
        [string]$CurrentState,
        [string]$CurrentMessage,
        [object]$PreviousSnapshot,
        [object]$HealthState,
        [object]$RuntimeReadinessState,
        [string[]]$LauncherTail
    )

    if ($CurrentState -eq 'failed' -and -not [string]::IsNullOrWhiteSpace($CurrentMessage)) {
        return $CurrentMessage.Trim()
    }

    if ($null -ne $PreviousSnapshot) {
        $previousFailure = [string]$PreviousSnapshot.last_failure_reason
        if (-not [string]::IsNullOrWhiteSpace($previousFailure) -and $previousFailure -ne 'none recorded') {
            return $previousFailure.Trim()
        }

        $previousState = [string]$PreviousSnapshot.state
        $previousMessage = [string]$PreviousSnapshot.message
        if ($previousState -eq 'failed' -and -not [string]::IsNullOrWhiteSpace($previousMessage)) {
            return $previousMessage.Trim()
        }
    }

    if ($null -ne $RuntimeReadinessState -and $RuntimeReadinessState.ok -and $null -ne $RuntimeReadinessState.payload) {
        try {
            if (-not [bool]$RuntimeReadinessState.payload.ready) {
                $summary = [string]$RuntimeReadinessState.payload.summary
                if (-not [string]::IsNullOrWhiteSpace($summary)) {
                    return $summary.Trim()
                }
            }
        }
        catch {
        }
    }

    if ($null -ne $HealthState -and -not $HealthState.ok -and -not [string]::IsNullOrWhiteSpace([string]$HealthState.error)) {
        return ([string]$HealthState.error).Trim()
    }

    if ($null -ne $LauncherTail) {
        for ($index = $LauncherTail.Count - 1; $index -ge 0; $index--) {
            $line = [string]$LauncherTail[$index]
            if ($line -match '^[^|]+\|\sERROR\s\|\s(?<message>[^|]+)') {
                return $Matches['message'].Trim()
            }
        }
    }

    return 'none recorded'
}

function Get-HancomAttachmentState {
    param(
        [object]$RuntimeReadinessState,
        [object]$ViewerSessionState
    )

    if ($null -ne $ViewerSessionState -and $ViewerSessionState.ok -and $null -ne $ViewerSessionState.payload) {
        try {
            $lastObservation = $ViewerSessionState.payload.last_observation_status
            if ($null -ne $lastObservation -and $null -ne $lastObservation.window_visible) {
                if ([bool]$lastObservation.window_visible) {
                    return 'yes'
                }
                return 'no'
            }
        }
        catch {
        }
    }

    if ($null -ne $RuntimeReadinessState -and $RuntimeReadinessState.ok -and $null -ne $RuntimeReadinessState.payload) {
        try {
            $hancomAutomation = $RuntimeReadinessState.payload.checks.hancom_automation
            if ($null -ne $hancomAutomation -and $null -ne $hancomAutomation.ok -and -not [bool]$hancomAutomation.ok) {
                return 'no'
            }
        }
        catch {
        }
    }

    return 'unknown'
}

function Get-RuntimeSummaryState {
    param(
        [object]$ApiStatus,
        [object]$WorkerStatus,
        [bool]$ApiReady
    )

    $apiRunning = ($null -ne $ApiStatus -and [bool]$ApiStatus.running)
    $workerRunning = ($null -ne $WorkerStatus -and [bool]$WorkerStatus.running)

    if ($apiRunning -and $workerRunning -and $ApiReady) {
        return 'up'
    }
    if ($apiRunning -or $workerRunning -or $ApiReady) {
        return 'partial'
    }
    return 'down'
}

function Get-NextActionRecommendation {
    param(
        [string]$RuntimeState,
        [string]$HancomAttached,
        [string]$ApiReady
    )

    if ($RuntimeState -eq 'down') {
        return 'Run writer_v1_manual.cmd start.'
    }
    if ($ApiReady -ne 'yes') {
        return 'Check spool\logs\writer_v1.api.stderr.log.'
    }
    if ($HancomAttached -eq 'no') {
        return 'Open Hancom on this desktop, then run writer_v1_manual.cmd status.'
    }
    if ($HancomAttached -eq 'unknown') {
        return 'Open the target document in Hancom, then run writer_v1_manual.cmd status.'
    }
    return 'Continue with the current edit/export run.'
}

function Build-OperatorSummary {
    param(
        [object]$ApiStatus,
        [object]$WorkerStatus,
        [object]$HealthState,
        [object]$RuntimeReadinessState,
        [object]$ViewerSessionState,
        [string]$LastFailureReason
    )

    $apiReady = if (Test-WriterHealthPayload -HealthResult $HealthState) { 'yes' } else { 'no' }
    $hancomAttached = Get-HancomAttachmentState -RuntimeReadinessState $RuntimeReadinessState -ViewerSessionState $ViewerSessionState
    $runtimeState = Get-RuntimeSummaryState -ApiStatus $ApiStatus -WorkerStatus $WorkerStatus -ApiReady ($apiReady -eq 'yes')
    $nextAction = Get-NextActionRecommendation -RuntimeState $runtimeState -HancomAttached $hancomAttached -ApiReady $apiReady

    return [ordered]@{
        runtime = $runtimeState
        where_it_will_edit = "Hancom desktop session (attached=$hancomAttached)"
        how_it_will_edit = "Packaged writer_v1 API + worker (api_ready=$apiReady)"
        how_it_changed_after_last_edit = $LastFailureReason
        next_action = $nextAction
    }
}

function Build-LaunchSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ActionName,
        [Parameter(Mandatory = $true)]
        [string]$State,
        [string]$Message,
        [hashtable]$Extra
    )

    $previousSnapshot = Read-JsonFile -Path $LaunchStatusPath
    $health = Invoke-JsonEndpoint -Uri $ApiHealthUri -TimeoutSec 3
    $runtimeReadiness = Invoke-JsonEndpoint -Uri $RuntimeReadinessUri -TimeoutSec 3
    $viewerSession = Invoke-JsonEndpoint -Uri $ViewerSessionUri -TimeoutSec 3
    $launcherTail = @(Get-RecentLogTail -Path $LauncherLogPath -Tail 20)
    $lastFailureReason = Get-LastFailureReason -CurrentState $State -CurrentMessage $Message -PreviousSnapshot $previousSnapshot -HealthState $health -RuntimeReadinessState $runtimeReadiness -LauncherTail $launcherTail
    $apiStatus = Get-RoleStatus -Role 'api'
    $workerStatus = Get-RoleStatus -Role 'worker'
    $operatorSummary = Build-OperatorSummary -ApiStatus $apiStatus -WorkerStatus $workerStatus -HealthState $health -RuntimeReadinessState $runtimeReadiness -ViewerSessionState $viewerSession -LastFailureReason $lastFailureReason

    $snapshot = [ordered]@{
        schema_version = 'writer-v1-launch-status/v1'
        updated_at = Get-UtcTimestamp
        action = $ActionName
        state = $State
        message = $Message
        operator_summary = $operatorSummary
        last_failure_reason = $lastFailureReason
        root = $Root
        port = $ApiPort
        logs_root = $LogsRoot
        launcher_log = $LauncherLogPath
        launch_status = $LaunchStatusPath
        port_listeners = @(Get-PortListeners -Port $ApiPort)
        api = $apiStatus
        worker = $workerStatus
        health = $health
        runtime_readiness = $runtimeReadiness
        viewer_session = $viewerSession
        readiness_file_path = $ReadinessPath
        readiness_file_exists = (Test-Path $ReadinessPath)
        viewer_session_file_path = $ViewerSessionPath
        viewer_session_file_exists = (Test-Path $ViewerSessionPath)
    }

    if ($Extra) {
        foreach ($key in $Extra.Keys) {
            $snapshot[$key] = $Extra[$key]
        }
    }

    return $snapshot
}

function Write-LaunchSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ActionName,
        [Parameter(Mandatory = $true)]
        [string]$State,
        [string]$Message,
        [hashtable]$Extra
    )

    $snapshot = Build-LaunchSnapshot -ActionName $ActionName -State $State -Message $Message -Extra $Extra
    Write-JsonFile -Path $LaunchStatusPath -Payload $snapshot
    return $snapshot
}

function Show-RoleStatusLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [object]$RoleStatus
    )

    $stateLabel = if ($RoleStatus.running) { 'running' } else { 'stopped' }
    $pidLabel = if ($null -ne $RoleStatus.pid) { $RoleStatus.pid } else { 'n/a' }
    Write-Host ("{0}: {1} pid={2}" -f $Role, $stateLabel, $pidLabel)
    if ($RoleStatus.stale_pid_file) {
        Write-Host ("  stale_pid_file: {0}" -f $RoleStatus.pid_file)
    }
    Write-Host ("  pid_file: {0}" -f $RoleStatus.pid_file)
    Write-Host ("  bootstrap_stdout: {0}" -f $RoleStatus.launcher_stdout_log)
    Write-Host ("  bootstrap_stderr: {0}" -f $RoleStatus.launcher_stderr_log)
    Write-Host ("  python_log: {0}" -f $RoleStatus.python_log)
}

function Show-EndpointState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [object]$EndpointState
    )

    if ($EndpointState.ok) {
        Write-Host ("{0}:" -f $Label)
        $EndpointState.payload | ConvertTo-Json -Depth 10
    }
    else {
        Write-Host ("{0}: unavailable ({1})" -f $Label, $EndpointState.error)
    }
}

function Show-OperatorSummary {
    param([object]$Summary)

    Write-Host 'operator_summary:'
    Write-Host ("  runtime: {0}" -f $Summary.runtime)
    Write-Host ("  where_it_will_edit: {0}" -f $Summary.where_it_will_edit)
    Write-Host ("  how_it_will_edit: {0}" -f $Summary.how_it_will_edit)
    Write-Host ("  how_it_changed_after_last_edit: {0}" -f $Summary.how_it_changed_after_last_edit)
    Write-Host ("  next_action: {0}" -f $Summary.next_action)
}

function Show-Status {
    Ensure-WriterDirectories
    $snapshot = Write-LaunchSnapshot -ActionName 'status' -State 'ok' -Message 'status snapshot refreshed'

    Show-OperatorSummary -Summary $snapshot.operator_summary
    Write-Host ''

    Write-Host ("writer_root: {0}" -f $Root)
    Write-Host ("launch_status: {0}" -f $LaunchStatusPath)
    Write-Host ("launcher_log: {0}" -f $LauncherLogPath)
    Write-Host ("api_port: {0}" -f $ApiPort)
    if ($snapshot.port_listeners.Count -gt 0) {
        Write-Host 'port_listeners:'
        $snapshot.port_listeners | ConvertTo-Json -Depth 6
    }
    else {
        Write-Host 'port_listeners: none'
    }

    Show-RoleStatusLine -Role 'api' -RoleStatus $snapshot.api
    Show-RoleStatusLine -Role 'worker' -RoleStatus $snapshot.worker

    if ($snapshot.readiness_file_exists) {
        Write-Host ("runtime_readiness_file: {0}" -f $ReadinessPath)
    }
    else {
        Write-Host 'runtime_readiness_file: missing'
    }

    Show-EndpointState -Label 'health' -EndpointState $snapshot.health
    Show-EndpointState -Label 'runtime-readiness endpoint' -EndpointState $snapshot.runtime_readiness

    if ($snapshot.viewer_session_file_exists) {
        Write-Host ("observation_viewer_session_file: {0}" -f $ViewerSessionPath)
    }
    else {
        Write-Host 'observation_viewer_session_file: missing'
    }
    Show-EndpointState -Label 'observation-viewer session endpoint' -EndpointState $snapshot.viewer_session

    Write-Host 'recent_launcher_log_tail:'
    $launcherTail = Get-RecentLogTail -Path $LauncherLogPath -Tail 10
    if ($launcherTail.Count -eq 0) {
        Write-Host '  <no launcher log yet>'
    }
    else {
        $launcherTail | ForEach-Object { Write-Host ("  {0}" -f $_) }
    }

    foreach ($role in @('api', 'worker')) {
        $roleStatus = if ($role -eq 'api') { $snapshot.api } else { $snapshot.worker }
        foreach ($pair in @(
            @{ Label = "$role bootstrap stderr tail"; Path = $roleStatus.launcher_stderr_log },
            @{ Label = "$role python log tail"; Path = $roleStatus.python_log }
        )) {
            Write-Host ("{0}:" -f $pair.Label)
            $tail = Get-RecentLogTail -Path $pair.Path -Tail 8
            if ($tail.Count -eq 0) {
                Write-Host '  <no log yet>'
            }
            else {
                $tail | ForEach-Object { Write-Host ("  {0}" -f $_) }
            }
        }
    }
}

function Invoke-WriterRole {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    Push-Location $Root
    try {
        if (-not $SkipInstall) {
            Ensure-Install
        }
        Ensure-WriterDirectories
        Clear-StaleRolePidFile -Role $Role

        $existing = Get-RoleStatus -Role $Role
        if ($existing.running) {
            $message = "writer_v1 $Role already running with pid=$($existing.pid)"
            Write-LauncherEvent -Message $message -Level 'WARN' -Context $existing
            Write-LaunchSnapshot -ActionName $Role -State 'already_running' -Message $message -Extra @{ role = $Role; launcher_pid = $PID } | Out-Null
            return
        }

        $config = Get-RoleConfig -Role $Role
        $pythonExe = Resolve-PythonExe
        $message = "writer_v1 $Role launcher starting"
        Write-LauncherEvent -Message $message -Context @{ role = $Role; launcher_pid = $PID; entry_module = $config.EntryModule; python = $pythonExe; skip_install = [bool]$SkipInstall }
        Write-LaunchSnapshot -ActionName $Role -State 'starting' -Message $message -Extra @{ role = $Role; launcher_pid = $PID; entry_module = $config.EntryModule } | Out-Null

        $serviceProcess = Start-Process -FilePath $pythonExe `
            -ArgumentList @('-m', $config.EntryModule) `
            -WorkingDirectory $Root `
            -WindowStyle Hidden `
            -PassThru

        $trackedPid = Resolve-RoleTrackedPid -Role $Role -BootstrapPid $serviceProcess.Id -EntryModule $config.EntryModule
        $trackedProcessState = Get-ProcessState -ProcessIdValue $trackedPid
        $trackedProcessName = if ($trackedProcessState.running -and -not [string]::IsNullOrWhiteSpace([string]$trackedProcessState.process_name)) {
            [string]$trackedProcessState.process_name
        }
        else {
            [string]$serviceProcess.ProcessName
        }

        Set-RolePidFile -Role $Role -TrackedPid $trackedPid -BootstrapPid $serviceProcess.Id -LauncherPid $PID -TrackedProcessName $trackedProcessName -BootstrapProcessName $serviceProcess.ProcessName
        Write-LauncherEvent -Message "writer_v1 $Role service process started" -Context @{ role = $Role; launcher_pid = $PID; service_pid = $serviceProcess.Id; tracked_pid = $trackedPid; service_process_name = $serviceProcess.ProcessName; tracked_process_name = $trackedProcessName; entry_module = $config.EntryModule }
        Write-LaunchSnapshot -ActionName $Role -State 'running' -Message "writer_v1 $Role service process started" -Extra @{ role = $Role; launcher_pid = $PID; service_pid = $serviceProcess.Id; tracked_pid = $trackedPid; entry_module = $config.EntryModule } | Out-Null

        Wait-Process -Id $serviceProcess.Id
        $serviceProcess.Refresh()
        $exitCode = if ($null -ne $serviceProcess.ExitCode) { [int]$serviceProcess.ExitCode } else { 0 }
        if ($exitCode -ne 0) {
            throw "writer_v1 $Role launcher exited with code $exitCode"
        }

        $message = "writer_v1 $Role launcher stopped cleanly"
        Write-LauncherEvent -Message $message -Context @{ role = $Role; launcher_pid = $PID; service_pid = $serviceProcess.Id; exit_code = $exitCode }
        Write-LaunchSnapshot -ActionName $Role -State 'stopped' -Message $message -Extra @{ role = $Role; launcher_pid = $PID; service_pid = $serviceProcess.Id; exit_code = $exitCode } | Out-Null
    }
    catch {
        $message = "writer_v1 $Role launcher failed: $($_.Exception.Message)"
        Write-LauncherEvent -Message $message -Level 'ERROR' -Context @{ role = $Role; launcher_pid = $PID }
        Write-LaunchSnapshot -ActionName $Role -State 'failed' -Message $message -Extra @{ role = $Role; launcher_pid = $PID } | Out-Null
        throw
    }
    finally {
        Clear-RolePidFile -Role $Role
        Pop-Location
    }
}

function Start-RoleProcess {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role
    )

    if (Test-WriterSessionZeroLaunch) {
        return Start-InteractiveTaskLaunch -Role $Role
    }

    $config = Get-RoleConfig -Role $Role
    $writerScript = Join-Path $PSScriptRoot 'writer_v1.ps1'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $writerScript,
        '-Action',
        $Role,
        '-SkipInstall'
    )
    $serializedArguments = ConvertTo-WindowsProcessArgumentList -Values $arguments

    $process = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList $serializedArguments `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $config.StdoutPath `
        -RedirectStandardError $config.StderrPath `
        -PassThru

    $processIdentity = Get-WriterProcessIdentityWithRetry -ProcessIdValue ([int]$process.Id)
    if ($null -eq $processIdentity) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) { $process.Kill() }
            $process.WaitForExit(5000) | Out-Null
        }
        catch { }
        throw "writer_v1 $Role bootstrap process identity could not be captured: $($process.Id)"
    }

    return [ordered]@{
        role = $Role
        owned = $true
        pid = $process.Id
        process_identity = $processIdentity
        stdout_log = $config.StdoutPath
        stderr_log = $config.StderrPath
        python_log = $config.PythonLogPath
    }
}

function Invoke-PackagedStart {
    Ensure-Install
    Ensure-WriterDirectories
    Clear-StaleRolePidFile -Role 'api'
    Clear-StaleRolePidFile -Role 'worker'

    $started = New-Object System.Collections.ArrayList
    $skipped = New-Object System.Collections.ArrayList
    $healthBefore = Invoke-JsonEndpoint -Uri $ApiHealthUri -TimeoutSec 3
    $apiStatusBefore = Get-RoleStatus -Role 'api'
    $workerStatusBefore = Get-RoleStatus -Role 'worker'
    $portListenersBefore = @(Get-PortListeners -Port $ApiPort)

    if ($apiStatusBefore.running -or (Test-WriterHealthPayload -HealthResult $healthBefore)) {
        [void]$skipped.Add([ordered]@{ role = 'api'; reason = 'already_running_or_healthy'; pid = $apiStatusBefore.pid })
        Write-LauncherEvent -Message 'writer_v1 start skipped api launch because the service already looks active' -Context @{ api = $apiStatusBefore; health = $healthBefore }
    }
    else {
        if ($portListenersBefore.Count -gt 0) {
            $message = "writer_v1 start refused: port $ApiPort is already listening but the expected health endpoint is unavailable"
            Write-LauncherEvent -Message $message -Level 'ERROR' -Context @{ port_listeners = $portListenersBefore; health = $healthBefore }
            Write-LaunchSnapshot -ActionName 'start' -State 'failed' -Message $message -Extra @{ started = @($started); skipped = @($skipped) } | Out-Null
            throw $message
        }

        $apiStart = Start-RoleProcess -Role 'api'
        [void]$started.Add($apiStart)
        Write-LauncherEvent -Message 'writer_v1 start launched api in hidden background mode' -Context $apiStart
        Start-Sleep -Seconds 2
    }

    if ($workerStatusBefore.running) {
        [void]$skipped.Add([ordered]@{ role = 'worker'; reason = 'already_running'; pid = $workerStatusBefore.pid })
        Write-LauncherEvent -Message 'writer_v1 start skipped worker launch because the worker pid file is already active' -Context $workerStatusBefore
    }
    else {
        $workerStart = Start-RoleProcess -Role 'worker'
        [void]$started.Add($workerStart)
        Write-LauncherEvent -Message 'writer_v1 start launched worker in hidden background mode' -Context $workerStart
    }

    $healthAfter = Wait-ForWriterHealth -TimeoutSeconds 20
    $state = 'ok'
    $message = 'writer_v1 start completed; API health is reachable'
    if (-not (Test-WriterHealthPayload -HealthResult $healthAfter)) {
        $cleanupErrors = New-Object System.Collections.ArrayList
        foreach ($launch in @($started)) {
            if ($launch.PSObject.Properties.Name -contains 'owned' -and -not [bool]$launch.owned) {
                continue
            }
            $launchedRole = [string]$launch.role
            try {
                # The role pid file is written by the child bootstrap and may
                # not exist yet when readiness fails.  Always clean the
                # invocation-owned bootstrap tree first; Stop-RoleProcess then
                # handles a separately tracked service process if it survived.
                $ownedLaunchResult = Stop-WriterOwnedLaunchTree -Role $launchedRole -Launch $launch -TimeoutSeconds 15
                if (-not [bool]$ownedLaunchResult.released) {
                    throw "owned bootstrap tree remained: $($ownedLaunchResult.remaining_pids -join ',')"
                }
                Stop-RoleProcess -Role $launchedRole -TimeoutSeconds 15 | Out-Null
            }
            catch {
                [void]$cleanupErrors.Add("$launchedRole cleanup failed: $($_.Exception.Message)")
            }
        }
        $cleanupSuffix = if ($cleanupErrors.Count -gt 0) { '; ' + ($cleanupErrors -join '; ') } else { '' }
        $message = 'writer_v1 start failed: API health did not become ready within 20 seconds' + $cleanupSuffix
        Write-LauncherEvent -Message $message -Level 'ERROR' -Context @{ started = @($started); skipped = @($skipped); health = $healthAfter; cleanup_errors = @($cleanupErrors) }
        Write-LaunchSnapshot -ActionName 'start' -State 'failed' -Message $message -Extra @{ started = @($started); skipped = @($skipped); cleanup_errors = @($cleanupErrors) } | Out-Null
        throw $message
    }
    else {
        Write-LauncherEvent -Message $message -Context @{ started = @($started); skipped = @($skipped); health = $healthAfter }
    }

    Write-LaunchSnapshot -ActionName 'start' -State $state -Message $message -Extra @{ started = @($started); skipped = @($skipped) } | Out-Null
    Show-Status
}

function Stop-RoleProcess {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('api', 'worker')]
        [string]$Role,
        [int]$TimeoutSeconds = 15
    )

    Clear-StaleRolePidFile -Role $Role
    $pidInfo = Read-RolePidInfo -Role $Role
    $statusBefore = Get-RoleStatus -Role $Role

    $trackedPid = $null
    $bootstrapPid = $null
    try {
        if ($null -ne $pidInfo -and $null -ne $pidInfo.pid) {
            $trackedPid = [int]$pidInfo.pid
        }
    }
    catch {
        $trackedPid = $null
    }
    try {
        if ($null -ne $pidInfo -and $null -ne $pidInfo.bootstrap_pid) {
            $bootstrapPid = [int]$pidInfo.bootstrap_pid
        }
    }
    catch {
        $bootstrapPid = $null
    }

    $trackedState = Get-ProcessState -ProcessIdValue $trackedPid
    $bootstrapState = Get-ProcessState -ProcessIdValue $bootstrapPid
    if ($trackedState.running) {
        $trackedIdentity = Get-OptionalPropertyValue -Object $pidInfo -Name 'process_identity'
        if ($null -eq $trackedIdentity -or -not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $trackedPid -ExpectedIdentity $trackedIdentity)) {
            throw "writer_v1 stop refused to terminate $Role tracked_pid=$trackedPid because its recorded process identity no longer matches."
        }
    }
    if ($bootstrapState.running) {
        $bootstrapIdentity = Get-OptionalPropertyValue -Object $pidInfo -Name 'bootstrap_process_identity'
        if ($null -eq $bootstrapIdentity -or -not (Test-WriterProcessIdentity -Role $Role -ProcessIdValue $bootstrapPid -ExpectedIdentity $bootstrapIdentity -Bootstrap)) {
            throw "writer_v1 stop refused to terminate $Role bootstrap_pid=$bootstrapPid because its recorded process identity no longer matches."
        }
    }
    if (-not $trackedState.running -and -not $bootstrapState.running) {
        if ($statusBefore.pid_file_exists) {
            Clear-RolePidFile -Role $Role -Force
        }
        return [ordered]@{
            role = $Role
            state = 'not_running'
            pid = $trackedPid
            bootstrap_pid = $bootstrapPid
            pid_file = $statusBefore.pid_file
        }
    }

    $message = "writer_v1 stop stopping $Role pid=$($trackedPid) bootstrap_pid=$($bootstrapPid)"
    Write-LauncherEvent -Message $message -Context @{ role = $Role; pid = $trackedPid; bootstrap_pid = $bootstrapPid; pid_file = $statusBefore.pid_file }

    $trackedTreePids = if ($trackedState.running) { @(Get-ProcessTreeIds -RootPid $trackedPid) } else { @() }
    if ($trackedTreePids.Count -eq 0 -and $trackedState.running) {
        $trackedTreePids = @([int]$trackedPid)
    }

    $bootstrapTreePids = if ($bootstrapState.running) { @(Get-ProcessTreeIds -RootPid $bootstrapPid) } else { @() }
    if ($bootstrapTreePids.Count -eq 0 -and $bootstrapState.running) {
        $bootstrapTreePids = @([int]$bootstrapPid)
    }

    $stopOrder = @(
        @($bootstrapTreePids + $trackedTreePids) |
            Select-Object -Unique
    )
    [array]::Reverse($stopOrder)
    $stoppedProcesses = New-Object System.Collections.ArrayList

    foreach ($candidatePid in $stopOrder) {
        $candidateProcess = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
        if ($null -eq $candidateProcess) {
            continue
        }
        $trustedRootPid = if ($candidatePid -in $bootstrapTreePids) { [int]$bootstrapPid } else { [int]$trackedPid }
        if (-not (Test-WriterKillCandidate -CandidatePid ([int]$candidatePid) -TrustedRootPid $trustedRootPid -Role $Role -PidInfo $pidInfo -TrustedPids @($bootstrapPid, $trackedPid))) {
            throw "writer_v1 stop refused to terminate an unverified $Role process PID $candidatePid."
        }

        try {
            $candidateProcess.Refresh()
            if ($candidateProcess.HasExited) { continue }
            # Bind termination to this refreshed Process object, not a
            # reusable PID lookup performed earlier in the loop.
            if (-not (Test-WriterKillCandidate -CandidatePid ([int]$candidateProcess.Id) -TrustedRootPid $trustedRootPid -Role $Role -PidInfo $pidInfo -TrustedPids @($bootstrapPid, $trackedPid))) {
                throw "writer_v1 process identity changed at the stop boundary: $candidatePid"
            }
            Stop-Process -InputObject $candidateProcess -Force -ErrorAction Stop
            $candidateProcess.WaitForExit(5000) | Out-Null
            [void]$stoppedProcesses.Add([ordered]@{ pid = $candidatePid; process_name = $candidateProcess.ProcessName })
        }
        catch {
            throw "writer_v1 stop failed to stop $Role pid=$($candidatePid): $($_.Exception.Message)"
        }
    }

    $allTrackedPids = @(
        @($bootstrapTreePids + $trackedTreePids) |
            Select-Object -Unique
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $alivePids = @()
    do {
        Start-Sleep -Milliseconds 500
        $alivePids = @(
            $allTrackedPids |
                Where-Object {
                    $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
                }
        )
        if ($alivePids.Count -eq 0) {
            Clear-RolePidFile -Role $Role -Force
            return [ordered]@{
                role = $Role
                state = 'stopped'
                pid = $trackedPid
                bootstrap_pid = $bootstrapPid
                pid_file = $statusBefore.pid_file
                tracked_tree_pids = @($trackedTreePids)
                bootstrap_tree_pids = @($bootstrapTreePids)
                stopped_processes = @($stoppedProcesses)
            }
        }
    } while ((Get-Date) -lt $deadline)

    throw "writer_v1 stop timed out waiting for $Role tracked processes to exit. tracked_pid=$($trackedPid) bootstrap_pid=$($bootstrapPid) alive_pids=$($alivePids -join ',')"
}

function Invoke-PackagedStop {
    Ensure-WriterDirectories
    $results = New-Object System.Collections.ArrayList

    foreach ($role in @('worker', 'api')) {
        $result = Stop-RoleProcess -Role $role
        [void]$results.Add($result)
        Write-LauncherEvent -Message "writer_v1 stop processed $role with state=$($result.state)" -Context $result
    }

    $healthAfter = Invoke-JsonEndpoint -Uri $ApiHealthUri -TimeoutSec 3
    $state = 'ok'
    $message = 'writer_v1 stop completed; packaged writer processes were stopped or already absent'
    if (Test-WriterHealthPayload -HealthResult $healthAfter) {
        $state = 'degraded'
        $message = 'writer_v1 stop completed but API health is still reachable; inspect whether another service is bound to the writer port'
        Write-LauncherEvent -Message $message -Level 'WARN' -Context @{ results = @($results); health = $healthAfter }
    }
    else {
        Write-LauncherEvent -Message $message -Context @{ results = @($results); health = $healthAfter }
    }

    Write-LaunchSnapshot -ActionName 'stop' -State $state -Message $message -Extra @{ results = @($results) } | Out-Null
    Show-Status
}

$writerLifecycleLock = $null
try {
    if ($Action -in @('install', 'api', 'worker', 'start', 'stop', 'status')) {
        $writerLifecycleLock = Enter-InstallLifecycleLock `
            -InstallRoot $Root `
            -TaskNames @($ApiTaskName, $WorkerTaskName) `
            -ApiPort $ApiPort `
            -Role 'writer' `
            -TimeoutSeconds 120
    }
    Assert-PathObjectIdentity -Path $Root -ExpectedIdentity $writerRootIdentity | Out-Null
    if ($writerMarkerIdentity) {
        Assert-PathObjectIdentity -Path $candidateMarkerPath -ExpectedIdentity $writerMarkerIdentity | Out-Null
    }
    switch ($Action) {
        'install' {
            Ensure-Install
            Ensure-WriterDirectories
            Write-LauncherEvent -Message 'writer_v1 install complete' -Context @{ root = $Root }
            Write-LaunchSnapshot -ActionName 'install' -State 'ok' -Message 'writer_v1 install complete' | Out-Null
            Write-Host "writer_v1 install complete: $Root"
        }
        'api' {
            if (Test-WriterSessionZeroLaunch) {
                $delegated = Start-InteractiveTaskLaunch -Role 'api'
                $message = 'writer_v1 api delegated to interactive scheduled task because the current session is non-interactive'
                Write-LaunchSnapshot -ActionName 'api' -State 'delegated' -Message $message -Extra @{ delegated = $delegated } | Out-Null
                Write-Host $message
                break
            }
            Invoke-WriterRole -Role 'api'
        }
        'worker' {
            if (Test-WriterSessionZeroLaunch) {
                $delegated = Start-InteractiveTaskLaunch -Role 'worker'
                $message = 'writer_v1 worker delegated to interactive scheduled task because the current session is non-interactive'
                Write-LaunchSnapshot -ActionName 'worker' -State 'delegated' -Message $message -Extra @{ delegated = $delegated } | Out-Null
                Write-Host $message
                break
            }
            Invoke-WriterRole -Role 'worker'
        }
        'start' {
            Invoke-PackagedStart
        }
        'stop' {
            Invoke-PackagedStop
        }
        'status' {
            Show-Status
        }
    }
}
finally {
    Exit-InstallLifecycleLock -Lock $writerLifecycleLock
}
