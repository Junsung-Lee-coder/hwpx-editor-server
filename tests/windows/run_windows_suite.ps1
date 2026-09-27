[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$ManifestPath,
    [string]$OutputRoot,
    [string]$InstallRoot,
    [Nullable[int]]$ApiPort,
    [string]$FixturePath,
    [string]$PopplerPath,
    [string]$ExpectedRepository,
    [string]$ExpectedCommit,
    [string]$ExpectedTree,
    [string]$ExpectedManifestSha256,
    [ValidateRange(1, 1800)][int]$PerTestTimeoutSeconds = 1800,
    [ValidateRange(1, 1800)][int]$ControllerTimeoutSeconds = 300,
    [Nullable[int]]$RemoteHarnessTimeoutSeconds,
    [switch]$RunInstallerFaultPath,
    [switch]$RunInstallerCrashMatrix,
    [switch]$AllowRequiredSkipForStaticCi
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
else {
    $SourceRoot = (Resolve-Path $SourceRoot).Path
}
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $SourceRoot 'tests\windows\suite-manifest.json'
}
else {
    $ManifestPath = (Resolve-Path $ManifestPath).Path
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-windows-suite-' + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$OutputRoot = (Resolve-Path $OutputRoot).Path

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function ConvertTo-NativeArgument {
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
            if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2 + 1))) }
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) { [void]$builder.Append(('\' * $backslashes)) }
        [void]$builder.Append($character)
        $backslashes = 0
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Get-ManifestIdentity {
    $manifestItem = Join-Path $SourceRoot 'source-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestItem -PathType Leaf)) {
        return [ordered]@{ repository = $ExpectedRepository; commit = $ExpectedCommit; tree = $ExpectedTree; manifest_sha256 = $ExpectedManifestSha256 }
    }
    $manifest = Get-Content -LiteralPath $manifestItem -Raw | ConvertFrom-Json
    return [ordered]@{
        repository = if ($ExpectedRepository) { $ExpectedRepository } else { [string]$manifest.repository }
        commit = if ($ExpectedCommit) { $ExpectedCommit } else { [string]$manifest.commit }
        tree = if ($ExpectedTree) { $ExpectedTree } else { [string]$manifest.tree }
        manifest_sha256 = if ($ExpectedManifestSha256) { $ExpectedManifestSha256 } else { Get-FileSha256 -Path $manifestItem }
    }
}

function Invoke-BoundedSuiteProcess {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $false)][AllowEmptyCollection()][string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][hashtable]$EnvironmentOverrides
    )
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $argumentVector = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ScriptPath) + @($Arguments)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $powershell
    $startInfo.Arguments = [string]::Join(' ', @($argumentVector | ForEach-Object { ConvertTo-NativeArgument $_ }))
    $startInfo.WorkingDirectory = (Resolve-Path $WorkingDirectory).Path
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($key in @($EnvironmentOverrides.Keys)) {
        $startInfo.EnvironmentVariables[$key] = [string]$EnvironmentOverrides[$key]
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $startedAt = [DateTime]::UtcNow
    $startError = $null
    $started = $false
    try { $started = $process.Start() } catch { $startError = [string]$_.Exception.Message }
    if (-not $started) {
        [IO.File]::WriteAllText($StdoutPath, '', (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($StderrPath, '', (New-Object Text.UTF8Encoding($false)))
        return [ordered]@{
            command = $powershell
            argv = $argumentVector
            started_at_utc = $startedAt.ToString('o')
            ended_at_utc = [DateTime]::UtcNow.ToString('o')
            measured_elapsed_seconds = 0.0
            process_id = $null
            process_exit_confirmed = $false
            exit_code = $null
            timed_out = $false
            termination_requested = $false
            termination_confirmed = $false
            termination_error = $null
            start_error = $startError
            stdout_capture_complete = $true
            stderr_capture_complete = $true
            stdout_sha256 = Get-FileSha256 -Path $StdoutPath
            stderr_sha256 = Get-FileSha256 -Path $StderrPath
            environment = $EnvironmentOverrides
        }
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timeoutOccurred = $false
    $terminationRequested = $false
    $terminationConfirmed = $false
    $terminationError = $null
    try {
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $timeoutOccurred = $true
            $terminationRequested = $true
            try { $process.Kill() } catch { $terminationError = [string]$_.Exception.Message }
            try { $terminationConfirmed = [bool]$process.WaitForExit(15000) } catch { }
        }
    }
    catch {
        $terminationError = [string]$_.Exception.Message
    }
    $stdoutComplete = $false
    $stderrComplete = $false
    $stdoutText = ''
    $stderrText = ''
    try { $stdoutComplete = [bool]$stdoutTask.Wait(15000); if ($stdoutComplete) { $stdoutText = [string]$stdoutTask.Result } } catch { $stdoutComplete = $false }
    try { $stderrComplete = [bool]$stderrTask.Wait(15000); if ($stderrComplete) { $stderrText = [string]$stderrTask.Result } } catch { $stderrComplete = $false }
    if (-not $stdoutComplete) { $stdoutText = '' }
    if (-not $stderrComplete) { $stderrText = '' }
    [IO.File]::WriteAllText($StdoutPath, $stdoutText, (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($StderrPath, $stderrText, (New-Object Text.UTF8Encoding($false)))
    $process.Refresh()
    $exitConfirmed = [bool]$process.HasExited
    $exitCode = if ($exitConfirmed) { [int]$process.ExitCode } else { $null }
    $endedAt = [DateTime]::UtcNow
    return [ordered]@{
        command = $powershell
        argv = $argumentVector
        started_at_utc = $startedAt.ToString('o')
        ended_at_utc = $endedAt.ToString('o')
        measured_elapsed_seconds = [Math]::Round(($endedAt - $startedAt).TotalSeconds, 3)
        process_id = [int]$process.Id
        process_exit_confirmed = $exitConfirmed
        exit_code = $exitCode
        timed_out = [bool]$timeoutOccurred
        termination_requested = [bool]$terminationRequested
        termination_confirmed = [bool]$terminationConfirmed
        termination_error = $terminationError
        start_error = $null
        stdout_capture_complete = [bool]$stdoutComplete
        stderr_capture_complete = [bool]$stderrComplete
        stdout_sha256 = Get-FileSha256 -Path $StdoutPath
        stderr_sha256 = Get-FileSha256 -Path $StderrPath
        environment = $EnvironmentOverrides
    }
}

function Get-OutputStatus {
    param([Parameter(Mandatory = $true)][string]$StdoutPath, [Parameter(Mandatory = $true)][string]$StderrPath)
    $text = ''
    if (Test-Path -LiteralPath $StdoutPath -PathType Leaf) { $text += [IO.File]::ReadAllText($StdoutPath) }
    if (Test-Path -LiteralPath $StderrPath -PathType Leaf) { $text += "`n" + [IO.File]::ReadAllText($StderrPath) }
    $matches = [regex]::Matches($text, '(?im)"status"\s*:\s*"(?<status>[^"\r\n]+)"')
    if ($matches.Count -gt 0) {
        $status = [string]$matches[$matches.Count - 1].Groups['status'].Value
        if ($status -ceq 'SKIP') { return 'SKIP' }
        if ($status -like 'FAIL*') { return 'FAIL' }
        if ($status -like 'PASS*') { return 'PASS' }
    }
    return $null
}

$identity = Get-ManifestIdentity
if ($RunInstallerCrashMatrix -and -not $RunInstallerFaultPath) {
    throw 'The exact crash-matrix suite requires G12 -RunInstallerFaultPath in the same invocation.'
}
if ($null -ne $RemoteHarnessTimeoutSeconds -and ($RemoteHarnessTimeoutSeconds -lt 1 -or $RemoteHarnessTimeoutSeconds -gt 3600)) {
    throw 'RemoteHarnessTimeoutSeconds must be a positive outer-harness budget no greater than 3600 seconds.'
}
$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ([string]$manifest.schema_version -ne 'hwpx/windows-powershell-suite-manifest/v1') {
    throw 'PowerShell suite manifest schema is invalid.'
}
$suites = @($manifest.suites | ForEach-Object { [string]$_ })
if ($suites.Count -ne 21) { throw "Expected exactly 21 manifest suites; found $($suites.Count)." }
if (($suites | Sort-Object | Select-Object -Unique).Count -ne $suites.Count) { throw 'PowerShell suite manifest contains duplicate entries.' }
$eligible = @(Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'tests\windows') -File -Filter 'test_*.ps1' | Sort-Object Name | ForEach-Object { $_.Name })
if ((@($eligible | Sort-Object) -join "`n") -cne (@($suites | Sort-Object) -join "`n")) {
    throw "PowerShell suite manifest does not exactly enumerate eligible suites. expected=$($suites -join ','); actual=$($eligible -join ',')"
}

$runnerSha256 = Get-FileSha256 -Path $PSCommandPath
$startedAt = [DateTime]::UtcNow
$rows = New-Object System.Collections.Generic.List[object]
$g12MinimumOuterTimeoutSeconds = $null
$g12OuterTimeoutSeconds = $null
foreach ($suite in $suites) {
    $scriptPath = Join-Path (Join-Path $SourceRoot 'tests\windows') $suite
    $stdoutPath = Join-Path $OutputRoot ($suite + '.stdout.log')
    $stderrPath = Join-Path $OutputRoot ($suite + '.stderr.log')
    $suiteArgs = @()
    $environment = [ordered]@{}
    if ($suite -ceq 'test_g12_preserve_move_fault_path.ps1') {
        if ($RunInstallerFaultPath) {
            if ([string]::IsNullOrWhiteSpace($InstallRoot) -or $null -eq $ApiPort) { throw 'G12 suite requires InstallRoot and ApiPort.' }
            $g12Result = Join-Path $OutputRoot 'g12-result.json'
            $g12Receipt = Join-Path $OutputRoot 'g12-installer-receipt.json'
            $suiteArgs = @('-RunInstallerFaultPath', '-SourceRoot', $SourceRoot, '-InstallRoot', $InstallRoot, '-ApiPort', [string]$ApiPort, '-ReceiptPath', $g12Receipt, '-ResultPath', $g12Result, '-ExpectedRepository', $identity.repository, '-ExpectedCommit', $identity.commit, '-ExpectedTree', $identity.tree, '-ExpectedManifestSha256', $identity.manifest_sha256, '-ControllerTimeoutSeconds', [string]$ControllerTimeoutSeconds)
            if ($PopplerPath) { $suiteArgs += @('-PopplerPath', $PopplerPath) }
            if ($null -ne $RemoteHarnessTimeoutSeconds) { $suiteArgs += @('-RemoteHarnessTimeoutSeconds', [string]$RemoteHarnessTimeoutSeconds) }
            $environment['HWPX_TEST_INSTALL_FAULT'] = 'after-task-registrations'
        }
        else {
            $environment['HWPX_TEST_INSTALL_FAULT'] = 'not-set; required G12 opt-in omitted'
        }
    }
    elseif ($suite -ceq 'test_terminal_cleanup_contracts.ps1' -and $RunInstallerCrashMatrix) {
        if ([string]::IsNullOrWhiteSpace($InstallRoot) -or $null -eq $ApiPort) { throw 'Terminal crash matrix requires InstallRoot and ApiPort.' }
        $crashEvidenceRoot = Join-Path $OutputRoot 'terminal-crash-matrix'
        $suiteArgs = @('-RunInstallerCrashMatrix', '-SourceRoot', $SourceRoot, '-InstallRoot', $InstallRoot, '-ApiPort', [string]$ApiPort, '-CrashEvidenceRoot', $crashEvidenceRoot, '-ExpectedRepository', $identity.repository, '-ExpectedCommit', $identity.commit, '-ExpectedTree', $identity.tree, '-ExpectedManifestSha256', $identity.manifest_sha256, '-CrashTimeoutSeconds', [string]$ControllerTimeoutSeconds)
        if ($FixturePath) { $suiteArgs += @('-FixturePath', $FixturePath) }
    }
    $suiteTimeoutSeconds = $PerTestTimeoutSeconds
    if ($RunInstallerFaultPath -and $suite -ceq 'test_g12_preserve_move_fault_path.ps1') {
        # The G12 child owns the installer controller deadline, while this
        # runner owns the enclosing process deadline. Keep a bounded drain
        # margin so the child can record its measured termination/readback
        # fields after the controller reaches its own deadline.
        $g12MinimumOuterTimeoutSeconds = [Math]::Min(3600, $ControllerTimeoutSeconds + 120)
        $g12OuterTimeoutSeconds = $g12MinimumOuterTimeoutSeconds
        if ($null -ne $RemoteHarnessTimeoutSeconds) {
            $g12OuterTimeoutSeconds = [Math]::Max($g12OuterTimeoutSeconds, [int]$RemoteHarnessTimeoutSeconds)
        }
        $suiteTimeoutSeconds = [Math]::Max($PerTestTimeoutSeconds, $g12OuterTimeoutSeconds)
    }
    if ($RunInstallerCrashMatrix -and $suite -ceq 'test_terminal_cleanup_contracts.ps1') {
        # The real matrix runs five sequential bounded installer/restart
        # cases. Keep each child bounded by CrashTimeoutSeconds while giving
        # the enclosing suite process enough time to complete all cases.
        $suiteTimeoutSeconds = [Math]::Max($PerTestTimeoutSeconds, 3600)
    }
    $process = Invoke-BoundedSuiteProcess -ScriptPath $scriptPath -Arguments $suiteArgs -WorkingDirectory $SourceRoot -StdoutPath $stdoutPath -StderrPath $stderrPath -TimeoutSeconds $suiteTimeoutSeconds -EnvironmentOverrides $environment
    $reportedStatus = Get-OutputStatus -StdoutPath $stdoutPath -StderrPath $stderrPath
    $processHealthy = [bool]($process.process_exit_confirmed -and $process.exit_code -eq 0 -and -not $process.timed_out -and $process.stdout_capture_complete -and $process.stderr_capture_complete)
    $outputStatus = if ($reportedStatus) { $reportedStatus } elseif ($processHealthy) { 'PASS' } else { 'FAIL' }
    $isRequiredSkip = [bool]($outputStatus -eq 'SKIP')
    $passed = [bool]($processHealthy -and -not $isRequiredSkip -and $outputStatus -ne 'FAIL')
    $accepted = [bool]($passed -or ($isRequiredSkip -and $AllowRequiredSkipForStaticCi))
    [void]$rows.Add([ordered]@{
        name = $suite
        path = $scriptPath
        required = $true
        skip_allowed = [bool]$AllowRequiredSkipForStaticCi
        argv = $process.argv
        environment = $environment
        script_sha256 = Get-FileSha256 -Path $scriptPath
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        started_at_utc = $process.started_at_utc
        ended_at_utc = $process.ended_at_utc
        measured_elapsed_seconds = $process.measured_elapsed_seconds
        timeout_seconds = $suiteTimeoutSeconds
        exit_code = $process.exit_code
        process_exit_confirmed = $process.process_exit_confirmed
        timed_out = $process.timed_out
        termination_requested = $process.termination_requested
        termination_confirmed = $process.termination_confirmed
        start_error = $process.start_error
        termination_error = $process.termination_error
        stdout_capture_complete = $process.stdout_capture_complete
        stderr_capture_complete = $process.stderr_capture_complete
        stdout_sha256 = $process.stdout_sha256
        stderr_sha256 = $process.stderr_sha256
        output_status = $outputStatus
        passed = $passed
        accepted = $accepted
        disposition = if ($isRequiredSkip) { if ($AllowRequiredSkipForStaticCi) { 'SKIP_ALLOWED_STATIC_CI' } else { 'REQUIRED_SKIP_REJECTED' } } elseif ($passed) { 'PASS' } else { 'FAIL' }
    })
}
$endedAt = [DateTime]::UtcNow
$failedRows = @($rows | Where-Object { -not [bool]$_.passed })
$unacceptedRows = @($rows | Where-Object { -not [bool]$_.accepted })
$skippedRows = @($rows | Where-Object { [string]$_.output_status -eq 'SKIP' })
$result = [ordered]@{
    schema = 'hwpx/windows-powershell-suite-result/v2'
    host = $env:COMPUTERNAME
    user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    powershell_version = $PSVersionTable.PSVersion.ToString()
    source_root = $SourceRoot
    manifest_path = $ManifestPath
    suite_manifest_sha256 = Get-FileSha256 -Path $ManifestPath
    repository = $identity.repository
    commit = $identity.commit
    tree = $identity.tree
    expected_manifest_sha256 = $identity.manifest_sha256
    runner_sha256 = $runnerSha256
    invocation_bound_parameters = $PSBoundParameters
    runner = [ordered]@{
        path = $PSCommandPath
        sha256 = $runnerSha256
        command = (Get-Command powershell.exe -ErrorAction Stop).Source
        argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath)
        working_directory = (Get-Location).Path
        timeout_enforced = $true
        runner_sha256 = $runnerSha256
    }
    crash_point_environment_key = 'HWPX_TEST_INSTALL_CRASH_POINT'
    manifest_declared_count = [int]$suites.Count
    eligible_count = [int]$rows.Count
    invoked_count = [int]$rows.Count
    passed_count = [int](@($rows | Where-Object { $_.passed }).Count)
    failed_count = [int]$failedRows.Count
    timed_out_count = [int](@($rows | Where-Object { $_.timed_out }).Count)
    load_error_count = [int](@($rows | Where-Object { $_.start_error }).Count)
    output_skip_count = [int]$skippedRows.Count
    required_skip_count = [int]$skippedRows.Count
    all_required_members_executed = ($skippedRows.Count -eq 0)
    overall_pass = ($failedRows.Count -eq 0 -and $skippedRows.Count -eq 0 -and $rows.Count -eq $suites.Count)
    static_ci_exit_allowed = [bool]$AllowRequiredSkipForStaticCi
    process_exit_accepted = ($unacceptedRows.Count -eq 0)
    started_at_utc = $startedAt.ToString('o')
    ended_at_utc = $endedAt.ToString('o')
    measured_elapsed_seconds = [Math]::Round(($endedAt - $startedAt).TotalSeconds, 3)
    per_test_timeout_seconds = $PerTestTimeoutSeconds
    timeout_enforced = $true
    g12_controller_timeout_seconds = if ($RunInstallerFaultPath) { $ControllerTimeoutSeconds } else { $null }
    g12_outer_timeout_seconds = $g12OuterTimeoutSeconds
    terminal_crash_matrix_timeout_seconds = if ($RunInstallerCrashMatrix) { [Math]::Max($PerTestTimeoutSeconds, 3600) } else { $null }
    output_root = $OutputRoot
    suites = @($rows.ToArray())
}
$resultPath = Join-Path $OutputRoot 'suite-result.json'
$result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $resultPath -Encoding UTF8
Write-Output ("WINDOWS_SUITE_STATUS={0};RESULT={1};RUNNER_SHA256={2}" -f $(if ($result.overall_pass) { 'PASS' } else { 'FAIL' }), $resultPath, $runnerSha256)
if (-not $result.process_exit_accepted) { exit 1 }
exit 0
