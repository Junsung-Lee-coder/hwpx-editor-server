[CmdletBinding()]
param(
    [string]$InstallRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ReceiptPath,
    [string]$FixturePath,
    [string]$PopplerPath,
    [Nullable[int]]$ApiPort,
    [string]$ExpectedRepository,
    [string]$ExpectedCommit,
    [string]$ExpectedTree,
    [string]$ExpectedManifestSha256,
    [string]$RunId,
    [switch]$RunFullDiscovery
)

# Compatibility contract retained for older harnesses: [switch]$FixturePath.
$ErrorActionPreference = 'Stop'
$commonPath = Join-Path $PSScriptRoot 'windows_install_common.psm1'
Import-Module $commonPath -Force
$script:ProofPngAssemblyError = $null
try {
    # Windows PowerShell 5.1 does not resolve [Drawing.Image] until the assembly is loaded.
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
}
catch {
    $script:ProofPngAssemblyError = $_.Exception.Message
}

$install = $null
$requestedApiPort = $ApiPort
$apiPort = $null
$apiBaseUri = $null
$installInitializationError = $null
$verifierLifecycleLock = $null
$verifierReceiptLock = $null
$verifierRootIdentity = $null
$requestedReceiptPath = $null
$receiptAdmission = $null
$runId = if ([string]::IsNullOrWhiteSpace($RunId)) { [Guid]::NewGuid().ToString('N') } else { $RunId }
$runIdValidationError = $null
if ([string]$runId -notmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$') {
    $runIdValidationError = 'RunId must be a bounded non-empty verifier invocation identity.'
    $runId = [Guid]::NewGuid().ToString('N')
}
try {
    $install = Get-CanonicalPath -Path $InstallRoot -RequireExisting
}
catch {
    $installInitializationError = $_.Exception.Message
    $install = [System.IO.Path]::GetFullPath($InstallRoot)
}
$receiptPathError = $null
$receiptFile = if ($ReceiptPath) {
    try {
        $requestedReceiptPath = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ReceiptPath))
        if (Test-CanonicalPathWithinRoot -Path $requestedReceiptPath -Root $install) {
            throw 'ReceiptPath must be outside the existing InstallRoot during verification.'
        }
        $receiptAdmission = Assert-ReceiptPathAdmission -ReceiptPath $requestedReceiptPath -InstallRoot $install
        if ([bool]$receiptAdmission.exists -and [string]::IsNullOrWhiteSpace($RunId)) {
            throw 'An existing verifier ReceiptPath requires an explicit RunId for safe reuse.'
        }
        [string]$receiptAdmission.path
    }
    catch {
        $receiptPathError = $_.Exception.Message
        Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-verify-' + [Guid]::NewGuid().ToString('N') + '-' + $runId + '.json')
    }
}
elseif ($installInitializationError) {
    Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-verify-' + $runId + '.json')
}
else {
    Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-verify-' + $runId + '.json')
}
$exitCode = 20
$script:receiptPersistenceFailed = $false
$receipt = [ordered]@{
    schema_version = 'hwpx/windows-verify/v1'
    status = 'VERIFIER_ERROR'
    failure_class = 'VERIFIER_ERROR'
    status_code = 20
    run_id = $runId
    invocation_id = $runId
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    opening_status = 'NOT_COMMITTED'
    ended_at_utc = $null
    run_id_validation_error = $runIdValidationError
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    install_root = $install
    receipt_path = $receiptFile
    api_port = $null
    api_base_url = $null
    commands = @()
    checks = [ordered]@{}
    publication_gate = [ordered]@{ status = 'NOT_RUN'; commands = @() }
    advisory_test_debt = [ordered]@{ status = 'NOT_RUN'; commands = @() }
    fixture = $null
    candidate_generation = $null
    receipt_path_admission = [ordered]@{
        requested = $requestedReceiptPath
        admitted = $receiptFile
        caller_run_id_supplied = -not [string]::IsNullOrWhiteSpace($RunId)
        existing_target_requires_run_id = $true
        same_path_concurrency = 'fail-closed-lock'
    }
    errors = @()
}
$hadPythonNoBytecodeSetting = Test-Path Env:PYTHONDONTWRITEBYTECODE
$previousPythonNoBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
$hadBaseUrlSetting = Test-Path Env:HWPX_BASE_URL
$previousBaseUrlSetting = $env:HWPX_BASE_URL
$env:PYTHONDONTWRITEBYTECODE = '1'

function Save-VerifierReceipt {
    try {
        Write-JsonReceipt -Path $receiptFile -Value $receipt | Out-Null
        return $true
    }
    catch {
        $primaryError = [string]$_.Exception.Message
        $script:receiptPersistenceFailed = $true
        $receipt.status = 'FAIL_RECEIPT'
        $receipt.failure_class = 'FAIL_RECEIPT'
        $receipt.status_code = 99
        $script:exitCode = 99
        $receipt.receipt_persistence_failed = $true
        $receipt.receipt_error = Limit-Text -Value $primaryError -MaxChars 4096
        $fallback = [ordered]@{
            schema_version = 'hwpx/windows-verify/v1'
            status = 'FAIL_RECEIPT'
            failure_class = 'FAIL_RECEIPT'
            status_code = 99
            install_root = [string]$install
            receipt_path = [string]$receiptFile
            api_port = $apiPort
            receipt_persistence_failed = $true
            error = Limit-Text -Value $primaryError -MaxChars 4096
        }
        try {
            Write-JsonReceipt -Path $receiptFile -Value $fallback | Out-Null
        }
        catch {
            $receipt.receipt_fallback_error = Limit-Text -Value $_.Exception.Message -MaxChars 4096
        }
        return $false
    }
}

function Write-VerifierTerminalSummary {
    $summary = "status=$($receipt.status); status_code=$($receipt.status_code); receipt=$receiptFile; api_port=$apiPort"
    $firstError = @($receipt.errors | ForEach-Object { [string]$_ } | Select-Object -First 1)
    if ($firstError.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($firstError[0])) {
        $summary += "; error=" + (Limit-Text -Value $firstError[0] -MaxChars 4096)
    }
    Write-Output $summary
}

function Invoke-VerifierCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $install
    )
    Push-Location $WorkingDirectory
    try {
        # Invoke-NativeChecked records stdout, stderr, and the direct native $LASTEXITCODE.
        $result = Invoke-NativeChecked -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory -AllowNonZero
    }
    finally {
        Pop-Location
    }
    $entry = [ordered]@{
        name = $Name
        file_path = $FilePath
        command = $result.command
        arguments = @($result.arguments)
        working_directory = $result.working_directory
        base_url = $apiBaseUri
        candidate_generation = $receipt.candidate_generation
        exit_code = [int]$result.exit_code
        accepted = [bool]$result.accepted
        stdout = [string]$result.stdout
        stderr = [string]$result.stderr
        stdout_bytes = [int64]$result.stdout_bytes
        stderr_bytes = [int64]$result.stderr_bytes
        stdout_captured_bytes = [int64]$result.stdout_captured_bytes
        stderr_captured_bytes = [int64]$result.stderr_captured_bytes
        stdout_truncated = [bool]$result.stdout_truncated
        stderr_truncated = [bool]$result.stderr_truncated
        stdout_encoding = [string]$result.stdout_encoding
        stderr_encoding = [string]$result.stderr_encoding
        stdout_decode_fallback = [bool]$result.stdout_decode_fallback
        stderr_decode_fallback = [bool]$result.stderr_decode_fallback
        stdout_decode_error = [string]$result.stdout_decode_error
        stderr_decode_error = [string]$result.stderr_decode_error
        capture_error = [string]$result.capture_error
        termination_error = [string]$result.termination_error
        invocation_error = [string]$result.invocation_error
        capture_mode = [string]$result.capture_mode
        max_output_bytes = [int]$result.max_output_bytes
        timeout_seconds = [int]$result.timeout_seconds
        timed_out = [bool]$result.timed_out
        process_killed = [bool]$result.process_killed
        exit_confirmed = [bool]$result.exit_confirmed
    }
    $receipt.commands = @($receipt.commands) + [pscustomobject]$entry
    return [pscustomobject]$entry
}

function Resolve-VerifierPython {
    $candidate = Join-Path $install '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return Get-CanonicalPath -Path $candidate -RequireExisting }
    $configured = [Environment]::GetEnvironmentVariable('HWP_PYTHON')
    if ([string]::IsNullOrWhiteSpace($configured)) {
        $configured = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_PYTHON'
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$configured)) {
        $configuredPath = Get-CanonicalPath -Path $configured
        if (Test-Path -LiteralPath $configuredPath -PathType Leaf) { return $configuredPath }
        throw "Configured HWP_PYTHON does not exist: $configuredPath"
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return Get-CanonicalPath -Path $command.Source -RequireExisting }
    throw 'Installed Python executable was not found.'
}

function Get-VerifierPythonRuntimeIdentity {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    $probeCode = 'import json,platform,struct,sys; print(json.dumps({"implementation":str(getattr(sys.implementation,"name","")),"major":int(sys.version_info[0]),"minor":int(sys.version_info[1]),"micro":int(sys.version_info[2]),"pointer_bits":int(struct.calcsize("P")*8),"machine":str(platform.machine())}))'
    $result = Invoke-VerifierCommand -Name 'python_runtime_identity' -FilePath $PythonPath -Arguments @('-c', $probeCode)
    if (-not $result.accepted -or $result.exit_code -ne 0 -or $result.stdout_truncated) { throw 'Installed Python runtime identity probe failed.' }
    try { $identity = ConvertFrom-Json -InputObject ([string]$result.stdout) }
    catch { throw 'Installed Python runtime identity probe returned invalid JSON.' }
    $machine = [string]$identity.machine
    if ([int]$identity.major -ne 3 -or [int]$identity.minor -ne 13 -or
        [int]$identity.pointer_bits -ne 64 -or [string]$identity.implementation -ne 'cpython' -or
        $machine.ToLowerInvariant() -notin @('amd64', 'x86_64', 'intel64')) {
        throw "Installed Python ABI is unsupported; required CPython 3.13 x86-64, got $([string]$identity.implementation) $([int]$identity.major).$([int]$identity.minor) $([int]$identity.pointer_bits)-bit $machine."
    }
    return [pscustomobject]@{
        ok = $true
        executable = $PythonPath
        implementation = [string]$identity.implementation
        major = [int]$identity.major
        minor = [int]$identity.minor
        micro = [int]$identity.micro
        pointer_bits = [int]$identity.pointer_bits
        machine = $machine
        command = $result
    }
}

function Invoke-VerifierDependencyCheck {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    $lockPath = Join-Path $install 'requirements-windows.lock'
    $checkerPath = Join-Path $install 'scripts\check_windows_dependencies.py'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw "Hash-pinned Windows dependency lock is missing: $lockPath"
    }
    if (-not (Test-Path -LiteralPath $checkerPath -PathType Leaf)) {
        throw "Windows dependency checker is missing: $checkerPath"
    }
    $result = Invoke-VerifierCommand -Name 'dependency_completeness' -FilePath $PythonPath -Arguments @($checkerPath, '--lock', $lockPath, '--json') -WorkingDirectory $install
    $report = $null
    try {
        $report = ConvertFrom-Json -InputObject ([string]$result.stdout)
    }
    catch {
        throw "Windows dependency checker returned invalid JSON: $($_.Exception.Message)"
    }
    $reportOk = ($report.PSObject.Properties.Name -contains 'ok' -and $report.ok -is [bool] -and [bool]$report.ok)
    $commandOk = ([bool]$result.accepted -and $result.exit_code -eq 0)
    $receipt.checks.dependency_completeness = [pscustomobject]@{
        ok = ($commandOk -and $reportOk)
        exit_code = [int]$result.exit_code
        accepted = [bool]$result.accepted
        report = $report
    }
    if (-not $commandOk -or -not $reportOk) {
        throw 'Final Windows virtual environment failed locked dependency/import completeness verification.'
    }
    return $report
}

function Test-VerifierCandidateMarker {
    param(
        [Parameter(Mandatory = $true)][object]$ManifestResult,
        [Parameter(Mandatory = $true)][string]$InstallRoot
    )
    $markerPath = Join-Path $InstallRoot '.hwpx-install.json'
    $candidateGeneration = '{0}:{1}:{2}' -f $ManifestResult.manifest.commit, $ManifestResult.manifest.tree, $ManifestResult.manifest_sha256
    try {
        if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
            return [pscustomobject]@{
                ok = $false
                candidate_generation_contract_ok = $false
                marker_path = $markerPath
                reason = 'installed candidate marker is missing'
            }
        }
        $markerCapture = Read-BoundedText -Path $markerPath -MaxChars 65536
        if ($markerCapture.truncated) {
            return [pscustomobject]@{
                ok = $false
                candidate_generation_contract_ok = $false
                marker_path = $markerPath
                reason = 'installed candidate marker exceeds the bounded size limit'
            }
        }
        $markerPayload = ConvertFrom-Json -InputObject ([string]$markerCapture.text)
        $markerManifestSha256 = [string]$markerPayload.source_manifest_sha256
        $markerCandidateGeneration = [string]$markerPayload.candidate_generation
        $markerRuntimeEnvContractOk = $true
        if ([bool]$ManifestResult.runtime_env_contract_applied) {
            $markerRuntimeEnvContractOk = $false
            if ($markerPayload.PSObject.Properties.Name -contains 'runtime_env' -and $null -ne $ManifestResult.runtime_env_contract) {
                $markerRuntimeEnvContractOk = $true
                foreach ($field in @('schema_version', 'provenance', 'source', 'path', 'install_root', 'install_root_identity', 'size', 'sha256', 'source_manifest_sha256', 'candidate_generation')) {
                    if ([string]$markerPayload.runtime_env.$field -cne [string]$ManifestResult.runtime_env_contract.$field) {
                        $markerRuntimeEnvContractOk = $false
                        break
                    }
                }
            }
        }
        $markerIdentityOk = (
            [string]$markerPayload.schema_version -ceq 'hwpx/windows-install-marker/v1' -and
            [string]$markerPayload.repository -ceq [string]$ManifestResult.manifest.repository -and
            [string]$markerPayload.commit -ceq [string]$ManifestResult.manifest.commit -and
            [string]$markerPayload.tree -ceq [string]$ManifestResult.manifest.tree -and
            $markerManifestSha256 -ceq [string]$ManifestResult.manifest_sha256 -and
            $markerCandidateGeneration -ceq $candidateGeneration -and
            $markerRuntimeEnvContractOk
        )
        $markerSha256 = Get-Sha256Hex -Path $markerPath
        return [pscustomobject]@{
            ok = $markerIdentityOk
            candidate_generation_contract_ok = $markerIdentityOk
            marker_path = $markerPath
            marker_sha256 = $markerSha256
            marker_payload = $markerPayload
            repository = [string]$markerPayload.repository
            commit = [string]$markerPayload.commit
            tree = [string]$markerPayload.tree
            source_manifest_sha256 = $markerManifestSha256
            candidate_generation = $markerCandidateGeneration
            expected_candidate_generation = $candidateGeneration
            runtime_env_contract_ok = $markerRuntimeEnvContractOk
            reason = if ($markerIdentityOk) { $null } else { 'installed candidate marker does not match the verified source manifest' }
        }
    }
    catch {
        return [pscustomobject]@{
            ok = $false
            candidate_generation_contract_ok = $false
            marker_path = $markerPath
            reason = $_.Exception.Message
        }
    }
}

function New-VerifierPopplerProbePdf {
    param([Parameter(Mandatory = $true)][string]$Path)
    $newline = "`n"
    $objects = @(
        "1 0 obj${newline}<< /Type /Catalog /Pages 2 0 R >>${newline}endobj${newline}",
        "2 0 obj${newline}<< /Type /Pages /Kids [3 0 R] /Count 1 >>${newline}endobj${newline}",
        "3 0 obj${newline}<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << >> >>${newline}endobj${newline}",
        "4 0 obj${newline}<< /Length 37 >>${newline}stream${newline}BT /F1 12 Tf 72 720 Td (probe) Tj ET${newline}endstream${newline}endobj${newline}"
    )
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append("%PDF-1.4${newline}")
    $offsets = @(0)
    foreach ($object in $objects) {
        $offsets += [System.Text.Encoding]::ASCII.GetByteCount($builder.ToString())
        [void]$builder.Append($object)
    }
    $xrefOffset = [System.Text.Encoding]::ASCII.GetByteCount($builder.ToString())
    [void]$builder.Append("xref${newline}")
    [void]$builder.Append(("0 {0}{1}" -f ($objects.Count + 1), $newline))
    [void]$builder.Append(("0000000000 65535 f {0}" -f $newline))
    foreach ($offset in $offsets[1..$objects.Count]) {
        [void]$builder.Append(("{0:D10} 00000 n {1}" -f $offset, $newline))
    }
    [void]$builder.Append(("trailer{0}<< /Size {1} /Root 1 0 R >>{0}startxref{0}{2}{0}%%EOF{0}" -f $newline, ($objects.Count + 1), $xrefOffset))
    [System.IO.File]::WriteAllText($Path, $builder.ToString(), (New-Object System.Text.ASCIIEncoding))
}

function Test-VerifierPopplerExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)
    $probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-poppler-probe-' + [Guid]::NewGuid().ToString('N'))
    $probePdf = Join-Path $probeRoot 'probe.pdf'
    $probePrefix = Join-Path $probeRoot 'rendered'
    $versionProbe = $null
    $renderProbe = $null
    $versionText = ''
    $renderOutputBytes = 0L
    $renderOutputSha256 = $null
    try {
        $workingDirectory = Split-Path -Parent $Path
        New-Item -ItemType Directory -Path $probeRoot -Force -ErrorAction Stop | Out-Null
        New-VerifierPopplerProbePdf -Path $probePdf
        $versionProbe = Invoke-NativeChecked -FilePath $Path -Arguments @('-v') -WorkingDirectory $workingDirectory -AllowNonZero -TimeoutSeconds 10 -MaxOutputBytes 65536
        $versionText = ([string]$versionProbe.stdout + "`n" + [string]$versionProbe.stderr).Trim()
        $versionOk = $versionProbe.accepted -and [int]$versionProbe.exit_code -eq 0 -and $versionProbe.exit_confirmed -and
            -not $versionProbe.timed_out -and -not [string]::IsNullOrWhiteSpace($versionText) -and
            $versionText -match '(?i)(pdftoppm|poppler)'
        $renderProbe = Invoke-NativeChecked -FilePath $Path -Arguments @('-f', '1', '-l', '1', '-singlefile', '-png', $probePdf, $probePrefix) -WorkingDirectory $workingDirectory -TimeoutSeconds 10 -MaxOutputBytes 65536
        $renderOutput = $probePrefix + '.png'
        if (Test-Path -LiteralPath $renderOutput -PathType Leaf) {
            $renderItem = Get-Item -LiteralPath $renderOutput -Force -ErrorAction Stop
            $renderOutputBytes = [int64]$renderItem.Length
            if ($renderOutputBytes -gt 0) { $renderOutputSha256 = Get-Sha256Hex -Path $renderOutput }
        }
        $renderOk = $renderProbe.accepted -and [int]$renderProbe.exit_code -eq 0 -and $renderProbe.exit_confirmed -and
            -not $renderProbe.timed_out -and $renderOutputBytes -gt 0 -and -not [string]::IsNullOrWhiteSpace($renderOutputSha256)
        $ok = $versionOk -and $renderOk
        return [pscustomobject]@{
            ok = [bool]$ok
            exit_code = [int]$renderProbe.exit_code
            accepted = [bool]($versionProbe.accepted -and $renderProbe.accepted)
            exit_confirmed = [bool]($versionProbe.exit_confirmed -and $renderProbe.exit_confirmed)
            timed_out = [bool]($versionProbe.timed_out -or $renderProbe.timed_out)
            version_output = Limit-Text -Value $versionText -MaxChars 2048
            version_probe = $versionProbe
            render_probe = $renderProbe
            render_output_bytes = $renderOutputBytes
            render_output_sha256 = $renderOutputSha256
            reason = if ($ok) { 'bounded pdftoppm version and render probes passed' } else { 'bounded pdftoppm version or render probe failed' }
        }
    }
    catch {
        return [pscustomobject]@{
            ok = $false
            exit_code = -1
            accepted = $false
            exit_confirmed = $false
            timed_out = $false
            version_output = Limit-Text -Value $versionText -MaxChars 2048
            version_probe = $versionProbe
            render_probe = $renderProbe
            render_output_bytes = $renderOutputBytes
            render_output_sha256 = $renderOutputSha256
            reason = $_.Exception.Message
        }
    }
    finally {
        if (Test-Path -LiteralPath $probeRoot) {
            Remove-Item -LiteralPath $probeRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-VerifierPoppler {
    $configured = [Environment]::GetEnvironmentVariable('HWP_PDFTOPPM')
    if ([string]::IsNullOrWhiteSpace($configured)) { $configured = [Environment]::GetEnvironmentVariable('HWP_PDFTOPPM_PATH') }
    if ([string]::IsNullOrWhiteSpace($configured)) { $configured = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_PDFTOPPM' }
    if ([string]::IsNullOrWhiteSpace([string]$configured)) { $configured = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_PDFTOPPM_PATH' }
    $explicit = if ($PopplerPath) { $PopplerPath } elseif ($configured) { $configured } else { $null }
    if ($explicit) {
        try {
            $explicitItem = Get-Item -LiteralPath $explicit -ErrorAction Stop
            if (($explicitItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
                $canonical = Get-CanonicalPath -Path $explicit -RequireExisting
                $explicitExtension = [IO.Path]::GetExtension($canonical).ToLowerInvariant()
                if ((Test-Path -LiteralPath $canonical -PathType Leaf) -and $explicitExtension -eq '.exe') {
                    $probe = Test-VerifierPopplerExecutable -Path $canonical
                    return [pscustomobject]@{ ok = [bool]$probe.ok; path = if ($probe.ok) { $canonical } else { $null }; source = 'explicit'; candidates = @($explicit); probe = $probe }
                }
            }
        }
        catch { }
        return [pscustomobject]@{ ok = $false; path = $null; source = 'explicit-invalid'; candidates = @($explicit) }
    }
    $candidates = @()
    foreach ($name in @('pdftoppm.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source) { $candidates += $command.Source }
    }
    $roots = @()
    if ($env:HWP_WINGET_ROOTS) { $roots += ($env:HWP_WINGET_ROOTS -split [IO.Path]::PathSeparator) }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'WindowsApps') }
    foreach ($root in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            $found = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ieq 'pdftoppm.exe' -and ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 } |
                Sort-Object FullName |
                Select-Object -First 1
            if ($found) { $candidates += $found.FullName }
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf -and [IO.Path]::GetExtension($candidate).ToLowerInvariant() -eq '.exe') {
            $canonicalCandidate = Get-CanonicalPath -Path $candidate -RequireExisting
            $probe = Test-VerifierPopplerExecutable -Path $canonicalCandidate
            if ($probe.ok) {
                return [pscustomobject]@{ ok = $true; path = $canonicalCandidate; source = 'path-or-winget'; candidates = @($candidates); probe = $probe }
            }
        }
    }
    return [pscustomobject]@{ ok = $false; path = $null; source = 'unresolved'; candidates = @($candidates) }
}

function Test-VerifierTask {
    param(
        [string]$TaskName,
        [string]$ExpectedRoot,
        [string]$ExpectedExecutable,
        [string]$ExpectedArguments,
        [string]$ExpectedPrincipal,
        [ValidateSet('Running', 'Ready', 'Disabled')][string]$ExpectedState = 'Running',
        [bool]$ExpectedEnabled = $true,
        [Parameter(Mandatory = $true)][int]$ExpectedApiPort,
        [string]$TaskPath = '\'
    )
    $identity = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    $configuredPort = $null
    $portConfigOk = $false
    try {
        $configuredPort = Get-ConfiguredApiPort -EnvPath (Join-Path $ExpectedRoot '.env')
        $portConfigOk = ([int]$configuredPort -eq $ExpectedApiPort)
    }
    catch { $portConfigOk = $false }
    # Test-CanonicalTaskActionBinding applies the PS5.1 equivalence helpers
    # Test-ScheduledTaskLogonTypeEquivalent and Test-ScheduledTaskRunLevelEquivalent.
    # It also retains Test-WindowsPrincipalEquivalent -Actual $identity.principal -Expected $ExpectedPrincipal.
    $stateOk = [string]$identity.state -ieq $ExpectedState
    $enabledOk = ($identity.PSObject.Properties.Name -contains 'enabled' -and [bool]$identity.enabled -eq $ExpectedEnabled)
    $settingsOk = Test-CanonicalTaskSettings -Identity $identity
    $contractOk = (Test-CanonicalTaskActionBinding -Identity $identity -ExpectedRoot $ExpectedRoot -ExpectedPythonPath $ExpectedExecutable -ExpectedArguments $ExpectedArguments -ExpectedPrincipal $ExpectedPrincipal -ExpectedApiPort $ExpectedApiPort) -and $portConfigOk -and $stateOk -and $enabledOk -and $settingsOk
    $payload = [ordered]@{}
    foreach ($property in $identity.PSObject.Properties) { $payload[$property.Name] = $property.Value }
    $payload.contract_ok = $contractOk
    $payload.expected_executable = $ExpectedExecutable
    $payload.expected_arguments = $ExpectedArguments
    $payload.expected_principal = $ExpectedPrincipal
    $payload.expected_state = $ExpectedState
    $payload.expected_enabled = $ExpectedEnabled
    $payload.state_contract_ok = $stateOk
    $payload.enabled_contract_ok = $enabledOk
    $payload.settings_contract_ok = $settingsOk
    $payload.expected_api_port = $ExpectedApiPort
    $payload.configured_api_port = $configuredPort
    $payload.api_port_contract_ok = $portConfigOk
    return [pscustomobject]$payload
}

function Test-VerifierWorker {
    $expectedPython = Join-Path $install '.venv\Scripts\python.exe'
    $processes = @()
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $name = ([string]$process.Name).ToLowerInvariant()
        if ($name -notin @('python.exe', 'pythonw.exe')) { continue }
        if (-not (Test-CanonicalProcessIdentity -Process $process -RootPath $install -ExpectedPythonPath $expectedPython -ExpectedArguments '-m app.worker' -ModuleNames @('app.worker'))) { continue }
        $processes += [ordered]@{
            process_id = [int]$process.ProcessId
            name = [string]$process.Name
            executable_path = [string]$process.ExecutablePath
            identity_root = $install
            module = 'app.worker'
            command_line = Limit-Text -Value $process.CommandLine -MaxChars 4096
            command_line_sha256 = Get-TextSha256 -Value ([string]$process.CommandLine)
            creation_date = [string]$process.CreationDate
            start_identity = Get-ProcessGenerationIdentity -ProcessId ([int]$process.ProcessId)
        }
    }
    return [pscustomobject]@{ ok = ($processes.Count -gt 0); process_count = $processes.Count; processes = @($processes) }
}

function Test-VerifierApiListener {
    $networkCommand = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if (-not $networkCommand) {
        return [pscustomobject]@{ ok = $false; reason = 'Get-NetTCPConnection is unavailable'; listeners = @(); processes = @() }
    }
    try {
        $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $apiPort -State Listen -ErrorAction Stop)
    }
    catch {
        return [pscustomobject]@{ ok = $false; reason = $_.Exception.Message; listeners = @(); processes = @() }
    }
    $expectedPython = Join-Path $install '.venv\Scripts\python.exe'
    $apiTaskIdentity = Get-ScheduledTaskIdentity -TaskName $apiTaskName -TaskPath $taskPath
    $expectedPrincipal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $processes = @()
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$listener.OwningProcess)" -ErrorAction SilentlyContinue
        $candidateIdentity = if ($process) {
            Test-CanonicalProcessIdentity -Process $process -RootPath $install -ExpectedPythonPath $expectedPython -ExpectedArguments '-m app.api_server' -ExpectedTaskIdentity $apiTaskIdentity -ExpectedListener $listener -ExpectedListenerPort $apiPort -ExpectedPrincipal $expectedPrincipal -ModuleNames @('app.api_server')
        }
        else { $false }
        $processes += [ordered]@{
            process_id = [int]$listener.OwningProcess
            local_address = [string]$listener.LocalAddress
            local_port = [int]$listener.LocalPort
            executable_path = if ($process) { [string]$process.ExecutablePath } else { $null }
            command_line = if ($process) { Limit-Text -Value $process.CommandLine -MaxChars 4096 } else { $null }
            command_line_sha256 = if ($process) { Get-TextSha256 -Value ([string]$process.CommandLine) } else { $null }
            creation_date = if ($process) { [string]$process.CreationDate } else { $null }
            start_identity = if ($process) { Get-ProcessGenerationIdentity -ProcessId ([int]$process.ProcessId) } else { $null }
            candidate_identity = [bool]$candidateIdentity
            module = 'app.api_server'
            identity_root = $install
        }
    }
    $listenerOk = $listeners.Count -gt 0 -and (@($processes | Where-Object { -not $_.candidate_identity }).Count -eq 0)
    return [pscustomobject]@{
        ok = $listenerOk
        listener_count = $listeners.Count
        listeners = @($listeners | ForEach-Object { [ordered]@{ local_address = [string]$_.LocalAddress; local_port = [int]$_.LocalPort; owning_process = [int]$_.OwningProcess; state = [string]$_.State } })
        processes = @($processes)
    }
}

function Wait-VerifierWorker {
    param([int]$Attempts = 30)
    $last = $null
    foreach ($attempt in 1..$Attempts) {
        $last = Test-VerifierWorker
        if ($last.ok) { return $last }
        Start-Sleep -Seconds 1
    }
    return $last
}

function Wait-VerifierHealth {
    param([string]$Uri, [int]$Attempts = 30)
    $lastError = $null
    foreach ($attempt in 1..$Attempts) {
        try { return Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec 2 }
        catch { $lastError = $_.Exception.Message; Start-Sleep -Seconds 1 }
    }
    throw "Health endpoint did not become ready: $Uri ($lastError)"
}

function Test-VerifierApiHealth {
    param([object]$Health)
    if ($null -eq $Health -or ($Health.PSObject.Properties.Name -notcontains 'status') -or [string]$Health.status -ne 'ok') {
        return $false
    }
    $reportedPort = 0
    if (($Health.PSObject.Properties.Name -notcontains 'api_port') -or -not [int]::TryParse([string]$Health.api_port, [ref]$reportedPort)) {
        return $false
    }
    return $reportedPort -eq $apiPort
}

function Test-ProofPng {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            ok = $false
            width = 0
            height = 0
            nonempty = $false
            sampled_pixels = 0
            nonempty_samples = 0
            content_bbox = @()
            reason = 'file missing'
        }
    }
    $maxProofBytes = [int64](64MB)
    $maxProofPixels = [int64]50000000
    $maxSampleCount = [int]4096
    $fileBytes = [int64]0
    $width = 0
    $height = 0
    $sampledPixels = 0
    $nonemptySamples = 0
    $contentMinX = 0
    $contentMinY = 0
    $contentMaxX = 0
    $contentMaxY = 0
    $image = $null
    try {
        if ($script:ProofPngAssemblyError) {
            return [pscustomobject]@{
                ok = $false
                width = 0
                height = 0
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                reason = 'System.Drawing assembly could not be loaded: ' + $script:ProofPngAssemblyError
            }
        }
        $fileBytes = [int64](Get-Item -LiteralPath $Path -ErrorAction Stop).Length
        if ($fileBytes -le 0) {
            return [pscustomobject]@{
                ok = $false
                width = 0
                height = 0
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                file_bytes = $fileBytes
                reason = 'file is empty'
            }
        }
        if ($fileBytes -gt $maxProofBytes) {
            return [pscustomobject]@{
                ok = $false
                width = 0
                height = 0
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                file_bytes = $fileBytes
                reason = "file exceeds bounded proof size: $fileBytes bytes"
            }
        }
        $image = [Drawing.Image]::FromFile($Path)
        $width = [int]$image.Width
        $height = [int]$image.Height
        if ($width -le 0 -or $height -le 0) {
            return [pscustomobject]@{
                ok = $false
                width = $width
                height = $height
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                file_bytes = $fileBytes
                reason = 'image dimensions are not positive'
            }
        }
        if ([int64]$width * [int64]$height -gt $maxProofPixels) {
            return [pscustomobject]@{
                ok = $false
                width = $width
                height = $height
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                file_bytes = $fileBytes
                reason = "image exceeds bounded pixel count: $width x $height"
            }
        }
        $formatGuid = $null
        try { $formatGuid = $image.RawFormat.Guid } catch { }
        $pngFormatOk = $formatGuid -eq [Drawing.Imaging.ImageFormat]::Png.Guid
        if (-not $pngFormatOk) {
            return [pscustomobject]@{
                ok = $false
                width = $width
                height = $height
                nonempty = $false
                sampled_pixels = 0
                nonempty_samples = 0
                content_bbox = @()
                file_bytes = $fileBytes
                reason = 'image format is not PNG'
            }
        }

        # Sample a deterministic 64x64 grid across the whole page.  The bound
        # keeps verifier cost predictable while avoiding fixed-point misses.
        $gridColumns = [Math]::Min([int]64, $width)
        $gridRows = [Math]::Min([int]64, $height)
        for ($row = 0; $row -lt $gridRows -and $sampledPixels -lt $maxSampleCount; $row++) {
            $y = if ($gridRows -eq 1) { 0 } else { [int][Math]::Floor(($row * ($height - 1)) / [double]($gridRows - 1)) }
            for ($column = 0; $column -lt $gridColumns -and $sampledPixels -lt $maxSampleCount; $column++) {
                $x = if ($gridColumns -eq 1) { 0 } else { [int][Math]::Floor(($column * ($width - 1)) / [double]($gridColumns - 1)) }
                $pixel = $image.GetPixel($x, $y)
                $sampledPixels++
                if ($pixel.A -gt 0 -and ($pixel.R -lt 250 -or $pixel.G -lt 250 -or $pixel.B -lt 250)) {
                    $nonemptySamples++
                    if ($nonemptySamples -eq 1) {
                        $contentMinX = $x
                        $contentMaxX = $x
                        $contentMinY = $y
                        $contentMaxY = $y
                    }
                    else {
                        $contentMinX = [Math]::Min($contentMinX, $x)
                        $contentMaxX = [Math]::Max($contentMaxX, $x)
                        $contentMinY = [Math]::Min($contentMinY, $y)
                        $contentMaxY = [Math]::Max($contentMaxY, $y)
                    }
                }
            }
        }
        $minimumNonemptySamples = [Math]::Max([int]2, [int][Math]::Ceiling($sampledPixels * 0.001))
        $nonempty = $nonemptySamples -ge $minimumNonemptySamples
        $contentBbox = if ($nonemptySamples -gt 0) {
            @($contentMinX, $contentMinY, $contentMaxX, $contentMaxY)
        }
        else {
            @()
        }
        $reason = if ($nonempty) { $null } else { 'bounded image scan found no sufficient non-white content' }
        return [pscustomobject]@{
            ok = ($pngFormatOk -and $nonempty)
            width = $width
            height = $height
            nonempty = $nonempty
            sampled_pixels = $sampledPixels
            nonempty_samples = $nonemptySamples
            content_bbox = $contentBbox
            file_bytes = $fileBytes
            format = 'PNG'
            reason = $reason
        }
    }
    catch {
        return [pscustomobject]@{
            ok = $false
            width = $width
            height = $height
            nonempty = $false
            sampled_pixels = $sampledPixels
            nonempty_samples = $nonemptySamples
            content_bbox = @()
            file_bytes = $fileBytes
            reason = $_.Exception.Message
        }
    }
    finally {
        if ($image) { $image.Dispose() }
    }
}

function Convert-VerifierCommandJson {
    param([Parameter(Mandatory = $true)][object]$CommandResult)
    if ($CommandResult.PSObject.Properties.Name -notcontains 'accepted' -or -not [bool]$CommandResult.accepted) {
        $fault = @($CommandResult.invocation_error, $CommandResult.capture_error, $CommandResult.termination_error | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1)
        $faultText = if ($fault.Count -gt 0) { ": $($fault[0])" } else { '' }
        throw "Fixture command $($CommandResult.name) was not accepted (exit code $($CommandResult.exit_code))$faultText."
    }
    if ($CommandResult.PSObject.Properties.Name -notcontains 'exit_confirmed' -or -not [bool]$CommandResult.exit_confirmed) {
        throw "Fixture command $($CommandResult.name) did not confirm native process exit."
    }
    if ([int]$CommandResult.exit_code -ne 0) {
        throw "Fixture command $($CommandResult.name) failed with exit code $($CommandResult.exit_code)."
    }
    if ([bool]$CommandResult.stdout_truncated) {
        throw "Fixture command $($CommandResult.name) returned truncated JSON."
    }
    try {
        $payload = ConvertFrom-Json -InputObject ([string]$CommandResult.stdout)
    }
    catch {
        throw "Fixture command $($CommandResult.name) returned invalid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $payload) { throw "Fixture command $($CommandResult.name) returned an empty JSON payload." }
    return $payload
}

function Get-VerifierResponseField {
    param(
        [AllowNull()][object]$Payload,
        [Parameter(Mandatory = $true)][string]$Name
    )
    # Command envelopes are schemas, not arbitrary diagnostic trees. Only
    # accept fields from the response object's top level so a nested payload
    # cannot impersonate the live session or candidate identity.
    if ($null -eq $Payload -or $Payload -is [System.Array] -or $Payload -is [string]) { return $null }
    $property = $Payload.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Assert-FixtureCommandBinding {
    param(
        [Parameter(Mandatory = $true)][string]$StepName,
        [Parameter(Mandatory = $true)][object]$CommandResult,
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$ManagedFixture,
        [Parameter(Mandatory = $true)][string]$ExpectedFilename,
        [Parameter(Mandatory = $true)][string]$CandidateGeneration,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedBaseUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedWorkingDirectory,
        [AllowNull()][string]$ExpectedSessionId,
        [AllowNull()][string]$ExpectedArtifactPath,
        [Nullable[int]]$RequestedPage
    )
    $expectedResponseSchema = switch ($StepName) {
        'fixture_open' { 'local-cli/lifecycle/v1' }
        'fixture_status_open' { 'local-cli/status/v1' }
        'fixture_where' { 'local-output-parser/command-bundle/v1' }
        'fixture_page_screenshot' { 'local-cli/envelope/v1' }
        'fixture_close' { 'local-cli/lifecycle/v1' }
        'fixture_status_closed' { 'local-cli/status/v1' }
        default { throw "Unknown fixture command step: $StepName" }
    }
    $expectedResponseCommand = switch ($StepName) {
        'fixture_open' { 'open' }
        'fixture_status_open' { 'status' }
        'fixture_where' { 'where' }
        'fixture_page_screenshot' { 'page-screenshot' }
        'fixture_close' { 'close' }
        'fixture_status_closed' { 'status' }
        default { throw "Unknown fixture command step: $StepName" }
    }
    $responseSchemaVersion = [string](Get-VerifierResponseField -Payload $Payload -Name 'schema_version')
    $responseCommand = [string](Get-VerifierResponseField -Payload $Payload -Name 'command')
    if ($responseSchemaVersion -cne $expectedResponseSchema -or $responseCommand -cne $expectedResponseCommand) {
        throw "Fixture command $StepName returned an unexpected top-level schema or command."
    }
    $responseRepository = [string](Get-VerifierResponseField -Payload $Payload -Name 'repository')
    $responseCommit = [string](Get-VerifierResponseField -Payload $Payload -Name 'commit')
    $responseTree = [string](Get-VerifierResponseField -Payload $Payload -Name 'tree')
    $responseCandidateGeneration = [string](Get-VerifierResponseField -Payload $Payload -Name 'candidate_generation')
    $responseManifestSha256 = [string](Get-VerifierResponseField -Payload $Payload -Name 'manifest_sha256')
    $expectedRepository = [string]$receipt.source_identity.repository
    $expectedCommit = [string]$receipt.source_identity.commit
    $expectedTree = [string]$receipt.source_identity.tree
    if ([string]::IsNullOrWhiteSpace($responseRepository) -or [string]::IsNullOrWhiteSpace($responseCommit) -or [string]::IsNullOrWhiteSpace($responseTree) -or [string]::IsNullOrWhiteSpace($responseCandidateGeneration) -or [string]::IsNullOrWhiteSpace($responseManifestSha256)) {
        throw "Fixture command $StepName did not return top-level candidate identity fields."
    }
    $candidateIdentityOk = (
        $responseRepository -ceq $expectedRepository -and
        $responseCommit -ceq $expectedCommit -and
        $responseTree -ceq $expectedTree -and
        $responseCandidateGeneration -ceq $CandidateGeneration -and
        $responseManifestSha256 -ceq $ExpectedManifestSha256
    )
    if (-not $candidateIdentityOk) {
        throw "Fixture command $StepName returned a candidate identity different from the installed runtime."
    }
    $binding = [ordered]@{
        name = $StepName
        command = $StepName
        response_command = $responseCommand
        response_schema_version = $responseSchemaVersion
        base_url = $ExpectedBaseUrl
        managed_fixture = $ManagedFixture
        working_copy_id = Get-VerifierResponseField -Payload $Payload -Name 'working_copy_id'
        session_id = Get-VerifierResponseField -Payload $Payload -Name 'session_id'
        source_path = $ManagedFixture
        repository = $responseRepository
        commit = $responseCommit
        tree = $responseTree
        manifest_sha256 = $responseManifestSha256
        candidate_generation = $responseCandidateGeneration
        expected_manifest_sha256 = $ExpectedManifestSha256
        expected_candidate_generation = $CandidateGeneration
        candidate_identity_ok = $candidateIdentityOk
        requested_page = $RequestedPage
        working_directory = $ExpectedWorkingDirectory
        executable = $CommandResult.file_path
        arguments = @($CommandResult.arguments)
        response = $Payload
    }
    $sessionFromResponse = [string]$binding.session_id
    $workingCopyFromResponse = [string]$binding.working_copy_id
    switch ($StepName) {
        'fixture_open' {
            if ([string]$binding.response_command -ne 'open' -or [string]$binding.managed_fixture -ine [string](Get-VerifierResponseField -Payload $Payload -Name 'managed_fixture') -or [string]$binding.source_path -ine [string](Get-VerifierResponseField -Payload $Payload -Name 'source_path')) {
                throw 'Fixture open JSON was not bound to the managed fixture path.'
            }
            if ([string]::IsNullOrWhiteSpace($sessionFromResponse) -or $sessionFromResponse -ne $workingCopyFromResponse) {
                throw 'Fixture open JSON did not return a consistent session/working-copy identity.'
            }
            $ExpectedSessionId = $sessionFromResponse
        }
        'fixture_status_open' {
            $activeDocument = [string](Get-VerifierResponseField -Payload $Payload -Name 'active_document')
            if ($sessionFromResponse -ne $ExpectedSessionId -or [bool](Get-VerifierResponseField -Payload $Payload -Name 'live_session_bound') -ne $true -or ($activeDocument -and $activeDocument -ine $ExpectedFilename)) {
                throw 'Fixture open status JSON was not bound to the live expected session.'
            }
        }
        'fixture_where' {
            if ($workingCopyFromResponse -ne $ExpectedSessionId) {
                throw 'Fixture where JSON was not bound to the expected working copy.'
            }
        }
        'fixture_page_screenshot' {
            $proof = Get-VerifierResponseField -Payload $Payload -Name 'proof'
            $artifactProperty = if ($proof) { $proof.PSObject.Properties['artifact'] } else { $null }
            $artifactObject = if ($artifactProperty) { $artifactProperty.Value } else { $null }
            $artifactPathProperty = if ($artifactObject) { $artifactObject.PSObject.Properties['path'] } else { $null }
            $artifactPath = if ($artifactPathProperty) { [string]$artifactPathProperty.Value } else { $null }
            if ($RequestedPage -ne 1 -or [string]::IsNullOrWhiteSpace($artifactPath) -or ($ExpectedArtifactPath -and $artifactPath -ine $ExpectedArtifactPath)) {
                throw 'Fixture page screenshot JSON did not return a requested-page artifact.'
            }
        }
        'fixture_close' {
            if ([bool](Get-VerifierResponseField -Payload $Payload -Name 'close_confirmed') -ne $true -or [bool](Get-VerifierResponseField -Payload $Payload -Name 'live_session_bound') -ne $false) {
                throw 'Fixture close JSON did not confirm live-session cleanup.'
            }
        }
        'fixture_status_closed' {
            if ([bool](Get-VerifierResponseField -Payload $Payload -Name 'live_session_bound') -ne $false) {
                throw 'Fixture closed status JSON still reports a live session.'
            }
        }
    }
    return [pscustomobject]$binding
}

function New-VerifierFixtureTempRoot {
    $tempBase = [IO.Path]::GetTempPath()
    if ([string]::IsNullOrWhiteSpace($tempBase)) {
        throw 'The current user temporary path is unavailable.'
    }
    $tempBase = Assert-NoReparsePath -Path $tempBase
    $tempRoot = Join-Path $tempBase ('hwpx-fixture-verify-' + [Guid]::NewGuid().ToString('N'))
    $probePath = $null
    try {
        New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
        $tempRoot = Assert-NoReparsePath -Path $tempRoot
        $probePath = Join-Path $tempRoot '.write-probe'
        [IO.File]::WriteAllText($probePath, 'fixture-temp-root-probe')
        if (-not (Test-Path -LiteralPath $probePath -PathType Leaf)) {
            throw 'The temporary-root write probe was not created.'
        }
        Remove-Item -LiteralPath $probePath -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $probePath) {
            throw 'The temporary-root write probe could not be removed.'
        }
        $rootItem = Get-Item -LiteralPath $tempRoot -Force -ErrorAction Stop
        if (-not $rootItem.PSIsContainer -or (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw 'The temporary-root probe returned an invalid directory.'
        }
        return [string]$tempRoot
    }
    catch {
        $probeError = [string]$_.Exception.Message
        $cleanupError = $null
        $rootExists = $false
        $rootIdentity = $null
        try {
            $rootExists = Test-Path -LiteralPath $tempRoot -PathType Container -ErrorAction Stop
        }
        catch {
            $cleanupError = [string]$_.Exception.Message
        }
        if ($rootExists) {
            try {
                $rootIdentity = Get-PathObjectIdentity -Path $tempRoot -RequireExisting
                Assert-PathObjectIdentity -Path $tempRoot -ExpectedIdentity $rootIdentity | Out-Null
                Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction Stop
                if (Test-Path -LiteralPath $tempRoot) { throw "Verifier fixture temp-root remained after identity-bound cleanup: $tempRoot" }
            }
            catch {
                $cleanupError = [string]$_.Exception.Message
            }
        }
        if ($cleanupError) {
            throw ('Verifier fixture temp-root probe failed: {0}; cleanup failed: {1}' -f $probeError, $cleanupError)
        }
        throw ('Verifier fixture temp-root is not writable and cleanable: {0}' -f $probeError)
    }
}

function Remove-VerifierFixtureTempRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $true }
    Assert-NoReparsePath -Path $Path | Out-Null
    Assert-PathObjectIdentity -Path $Path -ExpectedIdentity $ExpectedObjectIdentity | Out-Null
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $Path) { throw "Verifier fixture temp-root remained after identity-bound cleanup: $Path" }
    return $true
}

function Get-VerifierClosedSessionStatus {
    $status = Invoke-RestMethod -Uri ($apiBaseUri + '/local-cli/status') -Method Get -TimeoutSec 5
    if ($null -eq $status -or -not [bool]$status.ok -or [bool]$status.live_session_bound -or $status.command_reconciliation) {
        throw 'Server-managed fixture session did not reach a confirmed closed/no-reconciliation state.'
    }
    return $status
}

function Invoke-FixtureSequence {
    param([string]$Fixture)
    $sourceHash = $null
    $tempRoot = $null
    $tempRootIdentity = $null
    $managedFixture = $null
    $managedFixtureCopy = $null
    $pngPath = $null
    $python = $null
    $results = @()
    $bindings = @()
    $expectedSessionId = $null
    $candidateGeneration = [string]$receipt.candidate_generation
    $expectedManifestSha256 = [string]$receipt.source_identity.manifest_sha256
    $hadLocalStateSetting = Test-Path Env:HWPX_LOCAL_STATE_PATH
    $previousLocalStateSetting = $env:HWPX_LOCAL_STATE_PATH
    $closeAttempted = $false
    $closeConfirmed = $false
    $closeCleanupResult = $null
    try {
        $sourceHash = Get-Sha256Hex -Path $Fixture
        $tempRoot = New-VerifierFixtureTempRoot
        $tempRootIdentity = Get-PathObjectIdentity -Path $tempRoot -RequireExisting
        $managedFixture = Join-Path $tempRoot ([IO.Path]::GetFileName($Fixture))
        $fixtureItem = Get-Item -LiteralPath $Fixture -Force -ErrorAction Stop
        $managedFixtureCopy = Copy-FileVerified -SourcePath $Fixture -DestinationPath $managedFixture -ExpectedSize ([int64]$fixtureItem.Length) -ExpectedSha256 $sourceHash
        $pngPath = Join-Path $tempRoot 'page-001.png'
        $python = Resolve-VerifierPython
        $sequence = @(
            @{ name = 'fixture_open'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'open', $managedFixture, '--json') },
            @{ name = 'fixture_status_open'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'status', '--json') },
            @{ name = 'fixture_where'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'where', '--json') },
            @{ name = 'fixture_page_screenshot'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'page-screenshot', '--page', '1', '--out', $pngPath, '--json') },
            @{ name = 'fixture_close'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'close', '--json') },
            @{ name = 'fixture_status_closed'; args = @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'status', '--json') }
        )
        $env:HWPX_LOCAL_STATE_PATH = Join-Path $tempRoot 'local-cli-state.json'
        foreach ($step in $sequence) {
            if ([string]$step.name -eq 'fixture_close') { $closeAttempted = $true }
            $stepResult = Invoke-VerifierCommand -Name ([string]$step.name) -FilePath $python -Arguments ([string[]]$step.args) -WorkingDirectory $install
            $results += $stepResult
            $stepPayload = Convert-VerifierCommandJson -CommandResult $stepResult
            $requestedPage = if ([string]$step.name -eq 'fixture_page_screenshot') { 1 } else { $null }
            $expectedArtifactPath = if ([string]$step.name -eq 'fixture_page_screenshot') { $pngPath } else { $null }
            $binding = Assert-FixtureCommandBinding -StepName ([string]$step.name) -CommandResult $stepResult -Payload $stepPayload -ManagedFixture $managedFixture -ExpectedFilename ([IO.Path]::GetFileName($managedFixture)) -CandidateGeneration $candidateGeneration -ExpectedManifestSha256 $expectedManifestSha256 -ExpectedBaseUrl $apiBaseUri -ExpectedWorkingDirectory $install -ExpectedSessionId $expectedSessionId -ExpectedArtifactPath $expectedArtifactPath -RequestedPage $requestedPage
            if ([string]$step.name -eq 'fixture_open') {
                $expectedSessionId = [string]$binding.session_id
            }
            $runtimeStatus = Invoke-RestMethod -Uri ($apiBaseUri + '/local-cli/status') -Method Get -TimeoutSec 5
            $binding | Add-Member -NotePropertyName runtime_status -NotePropertyValue $runtimeStatus
            if ([string]$step.name -in @('fixture_open', 'fixture_status_open', 'fixture_where', 'fixture_page_screenshot')) {
                if ([string]$runtimeStatus.session_id -ne $expectedSessionId -or -not [bool]$runtimeStatus.live_session_bound) {
                    throw "Fixture command $($step.name) was not executed against the expected live session."
                }
            }
            elseif ([string]$step.name -in @('fixture_close', 'fixture_status_closed')) {
                if ([bool]$runtimeStatus.live_session_bound) {
                    throw "Fixture command $($step.name) left the live session bound."
                }
            }
            $bindings += $binding
            if ([string]$step.name -eq 'fixture_close' -and [bool]$binding.response.close_confirmed) {
                $closeConfirmed = $true
                $closeStatus = Get-VerifierClosedSessionStatus
                $receipt.fixture_close_cleanup = [pscustomobject]@{
                    attempted = $closeAttempted
                    confirmed = $true
                    result = $binding.response
                    server_status = $closeStatus
                }
            }
        }
        $afterHash = Get-Sha256Hex -Path $Fixture
        $closeStatus = $null
        $closeStatusError = $null
        foreach ($closeAttempt in 1..10) {
            try {
                $closeStatus = Invoke-RestMethod -Uri ($apiBaseUri + '/local-cli/status') -Method Get -TimeoutSec 5
                if ($closeStatus -and -not [bool]$closeStatus.live_session_bound) { break }
            }
            catch {
                $closeStatusError = $_.Exception.Message
            }
            if ($closeAttempt -lt 10) { Start-Sleep -Milliseconds 500 }
        }
        if ($null -eq $closeStatus) {
            $closeStatus = [pscustomobject]@{ ok = $false; error = $closeStatusError }
        }
        $closedCommandOk = @($results | Where-Object { $_.name -eq 'fixture_status_closed' -and $_.accepted -and $_.exit_code -eq 0 }).Count -eq 1
        $closed = $closedCommandOk -and $closeStatus -and (-not [bool]$closeStatus.live_session_bound)
        $closeConfirmed = $closed
        $receipt.fixture_close_cleanup = [pscustomobject]@{
            attempted = $closeAttempted
            confirmed = $closeConfirmed
            result = $null
        }
        $pngCheck = Test-ProofPng -Path $pngPath
        $proofManifestPath = [IO.Path]::ChangeExtension($pngPath, '.manifest.json')
        $proofManifestValid = Test-NonEmptyFile -Path $proofManifestPath
        $proofManifest = $null
        $proofManifestError = $null
        if ($proofManifestValid) {
            try {
                $proofCapture = Read-BoundedText -Path $proofManifestPath -MaxChars 262144
                if ($proofCapture.truncated) { throw 'proof manifest exceeds the bounded read limit' }
                $proofManifest = ConvertFrom-Json -InputObject ([string]$proofCapture.text)
            }
            catch {
                $proofManifestError = $_.Exception.Message
            }
        }
        $proofPngSha256 = if ($pngCheck.ok) { Get-Sha256Hex -Path $pngPath } else { $null }
        $proofManifestSha256 = if ($proofManifestValid) { Get-Sha256Hex -Path $proofManifestPath } else { $null }
        $proofPngBytes = if (Test-Path -LiteralPath $pngPath -PathType Leaf) { [int64](Get-Item -LiteralPath $pngPath).Length } else { 0 }
        $proofManifestOutputMatches = $false
        if ($proofManifest -and $proofManifest.output_path) {
            try {
                $proofManifestOutputMatches = (Get-CanonicalPath -Path ([string]$proofManifest.output_path) -RequireExisting) -eq (Get-CanonicalPath -Path $pngPath -RequireExisting)
            }
            catch { $proofManifestOutputMatches = $false }
        }
        $proofManifestBytes = -1
        $proofManifestBytesValid = [int64]::TryParse([string]$proofManifest.output_bytes, [ref]$proofManifestBytes)
        $proofManifestOk = (
            $proofManifestValid -and $null -ne $proofManifest -and
            [string]$proofManifest.schema_version -eq 'local-cli/artifact-manifest/v1' -and
            [string]$proofManifest.kind -eq 'page-screenshot' -and
            $proofManifestOutputMatches -and
            [string]$proofManifest.source_hwp_path -ieq $managedFixture -and
            [string]$proofManifest.source_hwp_sha256 -ieq $sourceHash -and
            [string]$proofManifest.session_id -eq $expectedSessionId -and
            [string]$proofManifest.working_copy_id -eq $expectedSessionId -and
            [int]$proofManifest.requested_page -eq 1 -and
            $proofManifestBytesValid -and [int64]$proofManifestBytes -eq $proofPngBytes -and
            [string]$proofManifest.output_sha256 -ieq [string]$proofPngSha256
        )
        return [pscustomobject]@{
            status = if ($sourceHash -eq $afterHash -and $closed -and $pngCheck.ok -and $proofManifestOk -and (@($results | Where-Object { $_.exit_code -ne 0 }).Count -eq 0)) { 'PASS' } else { 'FAIL_NATIVE_E2E' }
            source_sha256_before = $sourceHash
            source_sha256_after = $afterHash
            source_unchanged = ($sourceHash -eq $afterHash)
            session_closed = $closed
            close_status = $closeStatus
            proof_png_created = $pngCheck.ok
            proof_png = $pngCheck
            proof_png_sha256 = $proofPngSha256
            proof_png_bytes = $proofPngBytes
            proof_manifest_created = $proofManifestValid
            proof_manifest_path = $proofManifestPath
            proof_manifest_sha256 = $proofManifestSha256
            proof_manifest = $proofManifest
            proof_manifest_verified = $proofManifestOk
            proof_manifest_error = $proofManifestError
            close_attempted = $closeAttempted
            close_confirmed = $closeConfirmed
            close_cleanup = $closeCleanupResult
            managed_fixture = $managedFixture
            managed_fixture_copy = $managedFixtureCopy
            working_copy_id = $expectedSessionId
            source_path = $managedFixture
            manifest_sha256 = $expectedManifestSha256
            candidate_generation = $candidateGeneration
            commands = @($results)
            command_bindings = @($bindings)
        }
    }
    finally {
        try {
            if (-not $closeConfirmed -and $python -and $managedFixture -and $tempRoot) {
                try {
                    $closeAttempted = $true
                    $cleanupClose = Invoke-VerifierCommand -Name 'fixture_close_cleanup' -FilePath $python -Arguments @('-m', 'local_cli_v1.main', '--base-url', $apiBaseUri, 'close', '--json') -WorkingDirectory $install
                    $cleanupPayload = Convert-VerifierCommandJson -CommandResult $cleanupClose
                    $closeCleanupBinding = Assert-FixtureCommandBinding -StepName 'fixture_close' -CommandResult $cleanupClose -Payload $cleanupPayload -ManagedFixture $managedFixture -ExpectedFilename ([IO.Path]::GetFileName($managedFixture)) -CandidateGeneration $candidateGeneration -ExpectedManifestSha256 $expectedManifestSha256 -ExpectedBaseUrl $apiBaseUri -ExpectedWorkingDirectory $install -ExpectedSessionId $expectedSessionId
                    $closeCleanupResult = [pscustomobject]@{ command = $cleanupClose; binding = $closeCleanupBinding }
                    $serverClosedStatus = Get-VerifierClosedSessionStatus
                    $receipt.fixture_close_cleanup = [pscustomobject]@{
                        attempted = $closeAttempted
                        confirmed = [bool]$closeCleanupBinding.response.close_confirmed
                        result = $cleanupClose
                        command_binding = $closeCleanupBinding
                        server_status = $serverClosedStatus
                    }
                    if ($cleanupClose.exit_code -ne 0 -or -not [bool]$closeCleanupBinding.response.close_confirmed) {
                        throw "Fixture close cleanup did not confirm closure (exit code $($cleanupClose.exit_code))."
                    }
                }
                catch {
                    $receipt.fixture_close_cleanup = [pscustomobject]@{
                        attempted = $closeAttempted
                        confirmed = $closeConfirmed
                        result = $closeCleanupResult
                        error = $_.Exception.Message
                    }
                    $receipt.errors = @($receipt.errors) + ("Fixture cleanup: " + $_.Exception.Message)
                }
            }
            if ($tempRoot -and (Test-Path -LiteralPath $tempRoot -PathType Container)) {
                $fixtureTempRootCleanup = [ordered]@{
                    path = $tempRoot
                    attempted = $true
                    removed = $false
                }
                Remove-VerifierFixtureTempRoot -Path $tempRoot -ExpectedObjectIdentity $tempRootIdentity | Out-Null
                $fixtureTempRootCleanup.removed = $true
                $receipt.fixture_temp_root_cleanup = [pscustomobject]$fixtureTempRootCleanup
            }
            else {
                $receipt.fixture_temp_root_cleanup = [pscustomobject]@{
                    path = $tempRoot
                    attempted = $false
                    removed = $true
                    reason = 'fixture temp root was not allocated or was already absent'
                }
            }
        }
        finally {
            if ($hadLocalStateSetting) { $env:HWPX_LOCAL_STATE_PATH = $previousLocalStateSetting }
            else { Remove-Item Env:HWPX_LOCAL_STATE_PATH -ErrorAction SilentlyContinue }
        }
    }
}

try {
    if ($installInitializationError) { throw "Verifier install-root initialization failed: $installInitializationError" }
    if ($runIdValidationError) { throw $runIdValidationError }
    if ($receiptPathError) { throw $receiptPathError }
    # Verification participates in the same machine/root lifecycle namespace
    # as installation and writer start/stop.  Bind the root object before any
    # manifest, task, process, API, or receipt observation.
    $verifierLifecycleLock = Enter-InstallLifecycleLock -InstallRoot $install -Role 'verifier' -TimeoutSeconds 120
    $verifierRootIdentity = Get-PathObjectIdentity -Path $install -RequireExisting
    $receiptPathLockSeconds = 1
    try {
        $verifierReceiptLock = Enter-ReceiptPathLock -ReceiptPath $receiptFile -TimeoutSeconds $receiptPathLockSeconds
    }
    catch {
        $receipt.receipt_path_admission.lock_error = Limit-Text -Value $_.Exception.Message -MaxChars 4096
        $receiptFile = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-verify-admission-failed-' + $runId + '.json')
        $receipt.receipt_path = $receiptFile
        throw 'Same-path verifier receipt admission was already held or unavailable; refusing concurrent reuse.'
    }
    $receipt.receipt_path_admission.lock_acquired = $true
    $receipt.receipt_path_admission.root_identity = $verifierRootIdentity
    $receipt.status = 'IN_PROGRESS'
    $receipt.failure_class = 'IN_PROGRESS'
    $receipt.status_code = 20
    $receipt.opening_status = 'IN_PROGRESS'
    if (-not (Save-VerifierReceipt)) {
        throw 'Verifier opening receipt could not be committed; refusing to run checks.'
    }
    $python = Resolve-VerifierPython
    $identity = Invoke-VerifierCommand -Name 'python_identity' -FilePath $python -Arguments @('--version')
    $receipt.checks.python = [pscustomobject]@{
        ok = ($identity.accepted -and $identity.exit_code -eq 0)
        result = $identity
        runtime_identity = $null
    }
    if ($identity.exit_code -ne 0) { $exitCode = 11; throw 'Installed Python identity verification failed.' }
    if (-not $identity.accepted) { $exitCode = 11; throw 'Installed Python identity command capture was not accepted.' }
    $receipt.checks.python.runtime_identity = Get-VerifierPythonRuntimeIdentity -PythonPath $python

    $exitCode = 11
    Invoke-VerifierDependencyCheck -PythonPath $python | Out-Null
    $runtimeEnvContract = $null
    $runtimeEnvMarkerPath = Join-Path $install '.hwpx-install.json'
    if (Test-Path -LiteralPath $runtimeEnvMarkerPath -PathType Leaf) {
        $runtimeEnvMarkerCapture = Read-BoundedJsonObject -Path $runtimeEnvMarkerPath -MaxBytes 65536
        if ($runtimeEnvMarkerCapture.value.PSObject.Properties.Name -contains 'runtime_env') {
            $runtimeEnvContract = $runtimeEnvMarkerCapture.value.runtime_env
        }
    }

    $configuredManifest = [Environment]::GetEnvironmentVariable('HWP_SOURCE_MANIFEST')
    if ([string]::IsNullOrWhiteSpace($configuredManifest)) { $configuredManifest = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_SOURCE_MANIFEST' }
    $manifestPath = if ($configuredManifest) {
        if ([IO.Path]::IsPathRooted($configuredManifest)) { $configuredManifest } else { Join-Path $install $configuredManifest }
    }
    elseif (Test-Path -LiteralPath (Join-Path $install 'source-manifest.json')) { Join-Path $install 'source-manifest.json' }
    else { $null }
    if ($manifestPath) {
        $exitCode = 11
        $manifest = Get-SourceManifest -SourceRoot $install -ManifestPath $manifestPath -ExpectedRepository $ExpectedRepository -ExpectedCommit $ExpectedCommit -ExpectedTree $ExpectedTree -ExpectedManifestSha256 $ExpectedManifestSha256 -ExpectedRuntimeEnvContract $runtimeEnvContract
        $receipt.checks.source_manifest = $manifest
        if (-not $manifest.ok) { $exitCode = 11; throw 'Source manifest verification failed.' }
        if (-not [bool]$manifest.identity_binding.verified) {
            $exitCode = 11
            throw 'Installed source manifest identity is not independently bound.'
        }
        $receipt.source_identity = [ordered]@{
            repository = [string]$manifest.manifest.repository
            commit = [string]$manifest.manifest.commit
            tree = [string]$manifest.manifest.tree
            identity_source = [string]$manifest.identity_source
            identity_verified = [bool]$manifest.identity_verified
            identity_binding = $manifest.identity_binding
            manifest_sha256 = [string]$manifest.manifest_sha256
            file_count = [int]$manifest.file_count
        }
        $receipt.candidate_generation = '{0}:{1}:{2}' -f $manifest.manifest.commit, $manifest.manifest.tree, $manifest.manifest_sha256
        $markerPath = Join-Path $install '.hwpx-install.json'
        $markerCheck = Test-VerifierCandidateMarker -ManifestResult $manifest -InstallRoot $install
        $receipt.checks.candidate_marker = $markerCheck
        if (-not $markerCheck.ok) {
            $exitCode = 11
            throw "Installed candidate marker verification failed: $($markerCheck.reason)"
        }
    }
    else {
        $receipt.checks.source_manifest = [pscustomobject]@{ ok = $false; reason = 'manifest not found' }
        $exitCode = 11
        throw 'Source manifest is required for verification.'
    }

    $apiPort = Resolve-ApiPort -InstallRoot $install -RequestedApiPort $requestedApiPort
    $apiBaseUri = "http://127.0.0.1:$apiPort"
    $receipt.api_port = $apiPort
    $receipt.api_base_url = $apiBaseUri
    # The CLI gives an explicit URL precedence over its cached session URL.
    # Keep the environment aligned for any verifier-side helper invocation.
    $env:HWPX_BASE_URL = $apiBaseUri

    $apiTaskName = [Environment]::GetEnvironmentVariable('HWP_API_TASK_NAME')
    if ([string]::IsNullOrWhiteSpace($apiTaskName)) { $apiTaskName = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_API_TASK_NAME' }
    if ([string]::IsNullOrWhiteSpace($apiTaskName)) { $apiTaskName = 'hwpx-editor-api' }
    $workerTaskName = [Environment]::GetEnvironmentVariable('HWP_WORKER_TASK_NAME')
    if ([string]::IsNullOrWhiteSpace($workerTaskName)) { $workerTaskName = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_WORKER_TASK_NAME' }
    if ([string]::IsNullOrWhiteSpace($workerTaskName)) { $workerTaskName = 'hwpx-editor-worker' }
    $taskPath = [Environment]::GetEnvironmentVariable('HWP_TASK_PATH')
    if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name 'HWP_TASK_PATH' }
    if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = '\' }
    $taskPath = Assert-CanonicalScheduledTaskPath -TaskPath $taskPath
    $verifierLifecycleLock = Add-MachineLifecycleLockScope -Lock $verifierLifecycleLock -TaskNames @($apiTaskName, $workerTaskName) -ApiPort $apiPort
    $expectedPython = Join-Path $install '.venv\Scripts\python.exe'
    $expectedPrincipal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $tasks = @(
        (Test-VerifierTask -TaskName $apiTaskName -TaskPath $taskPath -ExpectedRoot $install -ExpectedExecutable $expectedPython -ExpectedArguments '-m app.api_server' -ExpectedPrincipal $expectedPrincipal -ExpectedState 'Running' -ExpectedEnabled $true -ExpectedApiPort $apiPort),
        (Test-VerifierTask -TaskName $workerTaskName -TaskPath $taskPath -ExpectedRoot $install -ExpectedExecutable $expectedPython -ExpectedArguments '-m app.worker' -ExpectedPrincipal $expectedPrincipal -ExpectedState 'Running' -ExpectedEnabled $true -ExpectedApiPort $apiPort)
    )
    $receipt.checks.tasks = $tasks
    $badTask = @($tasks | Where-Object { -not $_.contract_ok }).Count
    if ($badTask -gt 0) { $exitCode = 12; throw 'Scheduled task action or working directory verification failed.' }

    $worker = Wait-VerifierWorker
    $receipt.checks.worker = $worker
    if (-not $worker.ok) { $exitCode = 15; throw 'Worker process verification failed.' }

    $renderer = Test-VerifierPoppler
    $receipt.checks.pdf_renderer = $renderer
    if (-not $renderer.ok) { $exitCode = 16; throw 'pdftoppm renderer verification failed.' }

    try {
        $health = Wait-VerifierHealth -Uri ($apiBaseUri + '/health')
        $healthOk = Test-VerifierApiHealth -Health $health
        $receipt.checks.api = [pscustomobject]@{ ok = $healthOk; expected_api_port = $apiPort; health = $health }
        if (-not $healthOk) { throw "API health reported an unexpected status or port; expected $apiPort." }
        $listener = Test-VerifierApiListener
        $receipt.checks.listener = $listener
        if (-not $listener.ok) { throw 'API listener is not owned by the exact candidate runtime process.' }
    }
    catch {
        $exitCode = 13
        $receipt.checks.api = [pscustomobject]@{ ok = $false; error = $_.Exception.Message }
        throw 'API health verification failed.'
    }
    try {
        $readiness = Wait-VerifierHealth -Uri ($apiBaseUri + '/runtime-readiness')
        $receipt.checks.readiness = $readiness
    }
    catch {
        $exitCode = 14
        $receipt.checks.readiness = [pscustomobject]@{ ok = $false; ready = $false; error = $_.Exception.Message; readiness_error = $_.Exception.Message }
        throw 'Runtime readiness endpoint verification failed.'
    }
    $readinessReady = $false
    if ($readiness -and ($readiness.PSObject.Properties.Name -contains 'ready')) { $readinessReady = [bool]$readiness.ready }
    if (-not $readinessReady) { $exitCode = 14; throw 'Runtime readiness verification failed: ready was not true.' }
    if ([string]$readiness.candidate_generation -cne [string]$receipt.candidate_generation) {
        $exitCode = 14
        throw 'Runtime readiness candidate generation did not match the verified install generation.'
    }
    $workerIdentityRows = @($worker.processes)
    if ($workerIdentityRows.Count -eq 0 -or [int]$readiness.worker_pid -ne [int]$workerIdentityRows[0].process_id -or
        [string]$readiness.worker_start_identity -cne [string]$workerIdentityRows[0].start_identity) {
        $exitCode = 15
        throw 'Runtime readiness worker process generation did not match the current candidate worker.'
    }
    $receipt.checks.readiness_binding = [pscustomobject]@{
        ok = $true
        candidate_generation = [string]$readiness.candidate_generation
        worker_pid = [int]$readiness.worker_pid
        worker_start_identity = [string]$readiness.worker_start_identity
        run_id = [string]$readiness.run_id
        expires_at = [string]$readiness.expires_at
        heartbeat = $readiness.heartbeat
    }
    if ($FixturePath) {
        if (-not (Test-Path -LiteralPath $FixturePath -PathType Leaf)) { $exitCode = 17; throw 'FixturePath does not exist.' }
        $fixture = Invoke-FixtureSequence -Fixture (Get-CanonicalPath -Path $FixturePath -RequireExisting)
        $receipt.fixture = $fixture
        if ($fixture.status -ne 'PASS') { $exitCode = 17; throw 'Native fixture E2E failed.' }
    }

    $publicationScripts = @(
        'smoke_cli_workflow_status_static.py',
        'smoke_local_cli_health_static.py',
        'smoke_output_parser_static.py',
        'smoke_command_bundle_static.py',
        'smoke_command_packages_static.py',
        'smoke_native_table_command_static.py',
        'smoke_export_proof_range_clamp_static.py',
        'smoke_find_context_static.py',
        'smoke_cli_json_envelope_parity_static.py',
        'smoke_readback_diff_static.py',
        'smoke_readback_schema_static.py',
        'smoke_selection_proof_static.py',
        'smoke_text_table_cleanup_static.py'
    )
    $publicationResults = @()
    foreach ($scriptName in $publicationScripts) {
        $scriptPath = Join-Path $install ('scripts\' + $scriptName)
        $publicationResults += Invoke-VerifierCommand -Name ('publication:' + $scriptName) -FilePath $python -Arguments @($scriptPath)
    }
    $publicationUnitArguments = @(
        '-m', 'unittest',
        'tests.test_local_cli_status_payload',
        'tests.test_raw_target_readback',
        'tests.test_readback_command_status',
        'tests.test_readback_diff',
        'tests.test_table_scoped_cell_and_list_primitives',
        'tests.test_table4_anchor_range_replace_bundle'
    )
    $publicationResults += Invoke-VerifierCommand -Name 'publication:unit-gate' -FilePath $python -Arguments $publicationUnitArguments
    $publicationOk = (@($publicationResults | Where-Object { -not $_.accepted -or $_.exit_code -ne 0 }).Count -eq 0)
    $receipt.publication_gate = [pscustomobject]@{
        status = if ($publicationOk) { 'PASS' } else { 'FAIL' }
        commands = @($publicationResults)
    }
    if (-not $publicationOk) { $exitCode = 18; throw 'Publication gate failed.' }
    $advisoryPassed = $true
    if ($RunFullDiscovery) {
        $advisory = Invoke-VerifierCommand -Name 'advisory_full_discovery' -FilePath $python -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py')
        $advisoryPassed = ($advisory.accepted -and $advisory.exit_code -eq 0)
        $receipt.advisory_test_debt = [pscustomobject]@{ status = if ($advisoryPassed) { 'PASS' } else { 'ADVISORY_FAIL' }; result = $advisory }
    }
    else {
        $receipt.advisory_test_debt = [pscustomobject]@{ status = 'NOT_RUN'; reason = 'pass -RunFullDiscovery to run advisory repository discovery' }
    }
    # Closing readback is the final generation admission.  It runs while the
    # machine/root/task/port lifecycle locks are still held, so a concurrent
    # installer cannot replace the bytes described by this receipt.
    $closingRootIdentity = Get-PathObjectIdentity -Path $install -RequireExisting
    if ([string]$closingRootIdentity -cne [string]$verifierRootIdentity) {
        throw 'InstallRoot object identity changed before verifier terminal receipt publication.'
    }
    $closingManifest = Get-SourceManifest -SourceRoot $install -ManifestPath $manifestPath -ExpectedRepository ([string]$receipt.source_identity.repository) -ExpectedCommit ([string]$receipt.source_identity.commit) -ExpectedTree ([string]$receipt.source_identity.tree) -ExpectedManifestSha256 ([string]$receipt.source_identity.manifest_sha256) -ExpectedRuntimeEnvContract $runtimeEnvContract
    $closingGeneration = '{0}:{1}:{2}' -f $closingManifest.manifest.commit, $closingManifest.manifest.tree, $closingManifest.manifest_sha256
    if (-not $closingManifest.ok -or $closingGeneration -cne [string]$receipt.candidate_generation) {
        throw 'Verified install generation changed before verifier terminal receipt publication.'
    }
    $closingMarker = Test-VerifierCandidateMarker -ManifestResult $closingManifest -InstallRoot $install
    if (-not $closingMarker.ok) { throw 'Candidate marker changed before verifier terminal receipt publication.' }
    $closingReadiness = Wait-VerifierHealth -Uri ($apiBaseUri + '/runtime-readiness')
    $closingWorker = Test-VerifierWorker
    $closingReadinessReady = $closingReadiness -and [bool]$closingReadiness.ready
    $closingWorkerRows = @($closingWorker.processes)
    if (-not $closingReadinessReady -or [string]$closingReadiness.candidate_generation -cne [string]$receipt.candidate_generation -or
        $closingWorkerRows.Count -eq 0 -or [int]$closingReadiness.worker_pid -ne [int]$closingWorkerRows[0].process_id -or
        [string]$closingReadiness.worker_start_identity -cne [string]$closingWorkerRows[0].start_identity) {
        throw 'Final runtime readiness readback did not match the current candidate worker generation.'
    }
    # Repeat the root and manifest readback after the last API/worker probe and
    # immediately before setting the terminal PASS state.
    $terminalRootIdentity = Get-PathObjectIdentity -Path $install -RequireExisting
    if ([string]$terminalRootIdentity -cne [string]$verifierRootIdentity) {
        throw 'InstallRoot object identity changed at terminal verifier admission.'
    }
    $terminalManifest = Get-SourceManifest -SourceRoot $install -ManifestPath $manifestPath -ExpectedRepository ([string]$receipt.source_identity.repository) -ExpectedCommit ([string]$receipt.source_identity.commit) -ExpectedTree ([string]$receipt.source_identity.tree) -ExpectedManifestSha256 ([string]$receipt.source_identity.manifest_sha256) -ExpectedRuntimeEnvContract $runtimeEnvContract
    $terminalGeneration = '{0}:{1}:{2}' -f $terminalManifest.manifest.commit, $terminalManifest.manifest.tree, $terminalManifest.manifest_sha256
    if (-not $terminalManifest.ok -or $terminalGeneration -cne [string]$receipt.candidate_generation) {
        throw 'Verified install generation changed at terminal verifier admission.'
    }
    $terminalMarker = Test-VerifierCandidateMarker -ManifestResult $terminalManifest -InstallRoot $install
    if (-not $terminalMarker.ok) { throw 'Candidate marker changed at terminal verifier admission.' }
    $receipt.closing_generation_readback = [pscustomobject]@{
        root_identity = $terminalRootIdentity
        candidate_generation = $terminalGeneration
        manifest_sha256 = [string]$terminalManifest.manifest_sha256
        marker = $terminalMarker
        readiness = $closingReadiness
        worker = $closingWorker
    }
    $receipt.status = if ($advisoryPassed) { 'PASS' } else { 'PASS_WITH_ADVISORY_TEST_DEBT' }
    $receipt.failure_class = $null
    $receipt.status_code = 0
    $exitCode = 0
}
catch {
    $receipt.errors = @($receipt.errors) + $_.Exception.Message
    $receipt.status = if ($exitCode -eq 20) { 'VERIFIER_ERROR' } elseif ($exitCode -eq 18) { 'FAIL_PUBLICATION' } elseif ($exitCode -eq 17) { 'FAIL_NATIVE_E2E' } elseif ($exitCode -eq 16) { 'FAIL_RENDERER' } elseif ($exitCode -eq 15) { 'FAIL_WORKER' } elseif ($exitCode -eq 14 -or $exitCode -eq 13) { 'FAIL_API' } elseif ($exitCode -eq 12) { 'FAIL_TASK' } else { 'FAIL_PREFLIGHT' }
    $receipt.failure_class = $receipt.status
    $receipt.status_code = $exitCode
}
finally {
    $receipt.ended_at_utc = [DateTime]::UtcNow.ToString('o')
    if ($receipt.opening_status -eq 'IN_PROGRESS' -and $receipt.status -like 'PASS*') { $receipt.opening_status = 'PASS' }
    $receipt.checked_at_utc = [DateTime]::UtcNow.ToString('o')
    try {
        Save-VerifierReceipt
    }
    finally {
        Exit-PathMutex -Lock $verifierReceiptLock
        Exit-InstallLifecycleLock -Lock $verifierLifecycleLock
        if ($hadPythonNoBytecodeSetting) { $env:PYTHONDONTWRITEBYTECODE = $previousPythonNoBytecodeSetting }
        else { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue }
        if ($hadBaseUrlSetting) { $env:HWPX_BASE_URL = $previousBaseUrlSetting }
        else { Remove-Item Env:HWPX_BASE_URL -ErrorAction SilentlyContinue }
    }
}

Write-VerifierTerminalSummary
exit $exitCode
