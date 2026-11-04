[CmdletBinding()]
param([string]$RunnerPath = (Join-Path $PSScriptRoot 'run_windows_suite.ps1'))
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'PowerShell 5.1 required' }
$tokens = $null; $parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile($RunnerPath, [ref]$tokens, [ref]$parseErrors)
if (@($parseErrors).Count) { throw 'Runner does not parse as PowerShell 5.1' }
$text = [IO.File]::ReadAllText($RunnerPath)
$start = $text.IndexOf('    $processHealthy =')
$end = $text.IndexOf("`n}`n`$endedAt =", $start)
if ($start -lt 0 -or $end -le $start) { throw 'Actual runner row-consumer fragment not found' }
$consumer = $text.Substring($start, $end - $start)
if ($consumer.Contains('Invoke-BoundedSuiteProcess') -or $consumer.Contains('Start-Process')) { throw 'Row consumer unexpectedly launches a process' }
function Get-FileSha256 { param([string]$Path) return ('a' * 64) }
$cases = @(
    @{ name='healthy-pass'; status='PASS'; allow=$false; want=$true; healthy=$true },
    @{ name='healthy-skip-allowed'; status='SKIP'; allow=$true; want=$true; healthy=$true },
    @{ name='healthy-skip-denied'; status='SKIP'; allow=$false; want=$false; healthy=$true },
    @{ name='skip-nonzero'; status='SKIP'; allow=$true; want=$false; healthy=$false; field='exit_code'; value=7 },
    @{ name='skip-unconfirmed'; status='SKIP'; allow=$true; want=$false; healthy=$false; field='process_exit_confirmed'; value=$false },
    @{ name='skip-timeout'; status='SKIP'; allow=$true; want=$false; healthy=$false; field='timed_out'; value=$true },
    @{ name='skip-stdout-incomplete'; status='SKIP'; allow=$true; want=$false; healthy=$false; field='stdout_capture_complete'; value=$false },
    @{ name='skip-stderr-incomplete'; status='SKIP'; allow=$true; want=$false; healthy=$false; field='stderr_capture_complete'; value=$false },
    @{ name='ordinary-fail'; status='FAIL'; allow=$true; want=$false; healthy=$true },
    @{ name='pass-nonzero'; status='PASS'; allow=$true; want=$false; healthy=$false; field='exit_code'; value=9 }
)
$failures = New-Object System.Collections.Generic.List[string]
foreach ($case in $cases) {
    # Only inert data reach the actual consumer bytes, not a duplicate implementation of its predicate.
    $process = [ordered]@{ argv=@('PRIVATE_ARGUMENT_CANARY'); process_exit_confirmed=$true; exit_code=0; timed_out=$false;
        stdout_capture_complete=$true; stderr_capture_complete=$true; started_at_utc=''; ended_at_utc='';
        measured_elapsed_seconds=0; termination_requested=$false; termination_confirmed=$false;
        start_error=$null; termination_error=$null; stdout_sha256=('b'*64); stderr_sha256=('c'*64) }
    if ($case.ContainsKey('field')) { $process[$case.field] = $case.value }
    $reportedStatus = $case.status; $AllowRequiredSkipForStaticCi = $case.allow
    $rows = New-Object System.Collections.Generic.List[object]
    $suite = 'fixture-' + $case.name + '.ps1'
    $scriptPath='PRIVATE_PATH_CANARY'; $stdoutPath='PRIVATE_STDOUT_CANARY'; $stderrPath='PRIVATE_STDERR_CANARY'
    $environment=@{ SECRET_CANARY='PRIVATE_ENVIRONMENT_CANARY' }; $suiteTimeoutSeconds=1
    $console = @(Invoke-Expression $consumer)
    if ($rows.Count -ne 1) { throw 'Actual row consumer did not produce exactly one row' }
    $row = $rows[0]
    if ([bool]$row.accepted -ne [bool]$case.want) { $failures.Add($case.name + ': wrong acceptance') }
    if ([bool]$processHealthy -ne [bool]$case.healthy) { $failures.Add($case.name + ': wrong health') }
    if ($case.status -eq 'SKIP' -and [bool]$row.passed) { $failures.Add($case.name + ': skipped native row became a pass') }
    if ($case.status -eq 'SKIP' -and -not $case.healthy -and [string]$row.disposition -eq 'SKIP_ALLOWED_STATIC_CI') {
        $failures.Add($case.name + ': unhealthy skip disposition hides failure')
    }
    $public = @($console | Where-Object { [string]$_ -like 'WINDOWS_SUITE_ROW=*' })
    if ($public.Count -ne 1) { $failures.Add($case.name + ': missing safe per-row stdout') }
    else {
        $safe = ([string]$public[0]).Substring('WINDOWS_SUITE_ROW='.Length) | ConvertFrom-Json
        $wantedKeys = @('name','output_status','accepted','exit_code','process_exit_confirmed','timed_out','stdout_capture_complete','stderr_capture_complete','disposition')
        if ((@($safe.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($wantedKeys | Sort-Object) -join ',')) {
            $failures.Add($case.name + ': public row key allowlist differs')
        }
        if ([string]$public[0] -match 'PRIVATE_|SECRET_|environment|argv|_path') { $failures.Add($case.name + ': private detail in stdout') }
        foreach ($key in @('output_status','accepted','exit_code','process_exit_confirmed','timed_out','stdout_capture_complete','stderr_capture_complete','disposition')) {
            if ($safe.$key -cne $row[$key]) { $failures.Add($case.name + ': stdout differs from consumed ' + $key) }
        }
    }
    Write-Output ('CI_SKIP_HEALTH_CASE=' + ([ordered]@{name=$case.name; expected_accepted=$case.want; actual_accepted=[bool]$row.accepted;
         process_healthy=[bool]$processHealthy; output_status=$row.output_status; disposition=$row.disposition} | ConvertTo-Json -Compress))
}
# Execute the real result-aggregation block against one healthy required skip: static acceptance is not native PASS.
$start = $text.IndexOf('$failedRows =')
$end = $text.IndexOf('$resultPath =', $start)
if ($start -lt 0 -or $end -le $start) { throw 'Actual result aggregation not found' }
$rows = New-Object System.Collections.Generic.List[object]
[void]$rows.Add([ordered]@{ passed=$false; accepted=$true; output_status='SKIP'; timed_out=$false; start_error=$null })
$suites=@('fixture-skip.ps1'); $identity=@{repository='fixture';commit='commit';tree='tree';manifest_sha256='hash'}
$startedAt=[DateTime]::UtcNow; $endedAt=$startedAt; $AllowRequiredSkipForStaticCi=$true
$console = @(Invoke-Expression $text.Substring($start, $end - $start))
if ($result.overall_pass -or -not $result.process_exit_accepted -or $result.required_skip_count -ne 1) {
    $failures.Add('healthy skip aggregation: native overall pass or static acceptance is wrong')
}
$staticLine = @($text.Split("`n") | Where-Object { $_.TrimStart().StartsWith('Write-Output ("WINDOWS_SUITE_STATIC_CI_STATUS=') })
if ($staticLine.Count -ne 1) { $failures.Add('missing distinct static-CI stdout summary') }
else {
    $staticOutput = @(Invoke-Expression $staticLine[0])
    if ($staticOutput.Count -ne 1 -or [string]$staticOutput[0] -notmatch 'REQUIRED_SKIP_COUNT=1;UNACCEPTED_COUNT=0;NATIVE_OVERALL_PASS=False') {
        $failures.Add('static-CI summary missing literal counts or native NON-PASS')
    }
    if ([string]$staticOutput[0] -match 'PRIVATE_|SECRET_|RESULT=|source_root|environment|argv') { $failures.Add('unsafe static-CI summary') }
}
if ($failures.Count) {
    Write-Output ('CI_SKIP_HEALTH_FAILURES=' + (@($failures.ToArray()) | ConvertTo-Json -Compress))
    throw ('Actual runner skip-health contract failed: ' + $failures.Count + ' assertions')
}
Write-Output 'CI_SKIP_HEALTH_CONTRACT=PASS;CASES=10;NATIVE_SKIP_REMAINS_NONPASS=True'
