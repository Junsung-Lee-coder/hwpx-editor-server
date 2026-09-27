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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g10-venv-identity-' + [Guid]::NewGuid().ToString('N'))
$candidateRoot = Join-Path $tempRoot 'candidate'
$venvScripts = Join-Path $candidateRoot '.venv\Scripts'
$venvPython = Join-Path $venvScripts 'python.exe'
$venvConfig = Join-Path $candidateRoot '.venv\pyvenv.cfg'
$receiptPath = Join-Path $tempRoot 'receipts\install.json'
try {
    New-Item -ItemType Directory -Force -Path $venvScripts | Out-Null
    $basePython = (Get-Command python.exe -ErrorAction Stop).Source
    $basePythonItem = Get-Item -LiteralPath $basePython -ErrorAction Stop
    Copy-Item -LiteralPath $basePython -Destination $venvPython -Force
    $baseVersionText = [string]$basePythonItem.VersionInfo.ProductVersion
    if ([string]::IsNullOrWhiteSpace($baseVersionText)) {
        $baseVersionText = [string]$basePythonItem.VersionInfo.FileVersion
    }
    $versionMatch = [regex]::Match($baseVersionText, '\d+\.\d+')
    if (-not $versionMatch.Success) { throw "Could not derive the base interpreter version: $baseVersionText" }
    $expectedInterpreterVersion = $versionMatch.Value
    $baseHome = Split-Path -Parent $basePython
    [System.IO.File]::WriteAllText(
        $venvConfig,
        "home = $baseHome`r`nversion = $expectedInterpreterVersion`r`ninclude-system-site-packages = false`r`n",
        (New-Object System.Text.UTF8Encoding($false))
    )

    $processId = 41001
    $apiPort = 19871
    $process = [pscustomobject]@{
        ProcessId = $processId
        Name = 'python.exe'
        ExecutablePath = $basePython
        ExecutableVersion = $baseVersionText
        CommandLine = ('"{0}" -m app.api_server' -f $venvPython)
    }
    $taskIdentity = [pscustomobject]@{
        exists = $true
        contract_ok = $true
        task_name = 'HWPX_G10_identity_api'
        execute = $venvPython
        arguments = '-m app.api_server'
        working_directory = $candidateRoot
        action_type = 'Exec'
        trigger_type = 'LogonTrigger'
        logon_type = 'Interactive'
        run_level = 'Limited'
        start_when_available = 'true'
        enabled = $true
        settings = [pscustomobject]@{
            MultipleInstancesPolicy = 'IgnoreNew'
            DisallowStartIfOnBatteries = 'false'
            StopIfGoingOnBatteries = 'false'
            AllowHardTerminate = 'true'
            StartWhenAvailable = 'true'
            RunOnlyIfNetworkAvailable = 'false'
            Hidden = 'false'
            Enabled = 'true'
            ExecutionTimeLimit = 'PT0S'
            RestartCount = '3'
            RestartInterval = 'PT5M'
        }
        action_count = 1
        xml_action_count = 1
        xml_exec_action_count = 1
        task_identity_hash = 'g10-task-hash'
    }
    $listener = [pscustomobject]@{
        OwningProcess = $processId
        LocalAddress = '127.0.0.1'
        LocalPort = $apiPort
        State = 'Listen'
    }

    $legitimate = Test-CanonicalProcessIdentity `
        -Process $process `
        -RootPath $candidateRoot `
        -ExpectedPythonPath $venvPython `
        -ModuleNames @('app.api_server') `
        -ExpectedArguments '-m app.api_server' `
        -ExpectedTaskIdentity $taskIdentity `
        -ExpectedListener $listener `
        -ExpectedListenerPort $apiPort `
        -ExpectedInterpreterVersion $expectedInterpreterVersion
    if (-not $legitimate) { throw 'A legitimate venv/base-interpreter process was rejected.' }

    # Windows Store/Python installations may report the versioned base image
    # (for example python3.13.exe) rather than the venv launcher name. Keep the
    # venv home bound to a directory that contains only that versioned image so
    # this acceptance check fails unless the alternate basename is admitted.
    $alternateHome = Join-Path $tempRoot 'alternate-python-home'
    $alternatePython = Join-Path $alternateHome ('python' + $expectedInterpreterVersion + '.exe')
    New-Item -ItemType Directory -Force -Path $alternateHome | Out-Null
    Copy-Item -LiteralPath $basePython -Destination $alternatePython -Force
    [System.IO.File]::WriteAllText(
        $venvConfig,
        "home = $alternateHome`r`nversion = $expectedInterpreterVersion`r`ninclude-system-site-packages = false`r`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    $alternateProcess = [pscustomobject]@{
        ProcessId = $processId + 2
        Name = 'python.exe'
        ExecutablePath = $alternatePython
        ExecutableVersion = $baseVersionText
        CommandLine = ('"{0}" -m app.api_server' -f $venvPython)
    }
    $alternateListener = [pscustomobject]@{
        OwningProcess = $alternateProcess.ProcessId
        LocalAddress = '127.0.0.1'
        LocalPort = $apiPort
        State = 'Listen'
    }
    $alternateLegitimate = Test-CanonicalProcessIdentity `
        -Process $alternateProcess `
        -RootPath $candidateRoot `
        -ExpectedPythonPath $venvPython `
        -ModuleNames @('app.api_server') `
        -ExpectedArguments '-m app.api_server' `
        -ExpectedTaskIdentity $taskIdentity `
        -ExpectedListener $alternateListener `
        -ExpectedListenerPort $apiPort `
        -ExpectedInterpreterVersion $expectedInterpreterVersion
    if (-not $alternateLegitimate) { throw 'A versioned Windows base-interpreter process was rejected.' }

    # Python also records an explicit base executable in pyvenv.cfg. Honor
    # that binding when the configured home is not the directory containing
    # the process image (a layout used by packaged Windows distributions).
    $declaredHome = Join-Path $tempRoot 'declared-python-home'
    $configuredHome = Join-Path $tempRoot 'configured-python-image'
    $configuredPython = Join-Path $configuredHome ('python' + $expectedInterpreterVersion + '.exe')
    New-Item -ItemType Directory -Force -Path $declaredHome, $configuredHome | Out-Null
    Copy-Item -LiteralPath $basePython -Destination $configuredPython -Force
    [System.IO.File]::WriteAllText(
        $venvConfig,
        "home = $declaredHome`r`nversion = $expectedInterpreterVersion`r`nexecutable = $configuredPython`r`ninclude-system-site-packages = false`r`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    $configuredProcess = [pscustomobject]@{
        ProcessId = $processId + 3
        Name = 'python.exe'
        ExecutablePath = $configuredPython
        ExecutableVersion = $baseVersionText
        CommandLine = ('"{0}" -m app.api_server' -f $venvPython)
    }
    $configuredListener = [pscustomobject]@{
        OwningProcess = $configuredProcess.ProcessId
        LocalAddress = '127.0.0.1'
        LocalPort = $apiPort
        State = 'Listen'
    }
    $configuredLegitimate = Test-CanonicalProcessIdentity `
        -Process $configuredProcess `
        -RootPath $candidateRoot `
        -ExpectedPythonPath $venvPython `
        -ModuleNames @('app.api_server') `
        -ExpectedArguments '-m app.api_server' `
        -ExpectedTaskIdentity $taskIdentity `
        -ExpectedListener $configuredListener `
        -ExpectedListenerPort $apiPort `
        -ExpectedInterpreterVersion $expectedInterpreterVersion
    if (-not $configuredLegitimate) { throw 'An explicit pyvenv.cfg executable binding was rejected.' }

    $foreignListener = [pscustomobject]@{
        OwningProcess = $processId + 1
        LocalAddress = '127.0.0.1'
        LocalPort = $apiPort
        State = 'Listen'
    }
    if (Test-CanonicalProcessIdentity `
        -Process $process `
        -RootPath $candidateRoot `
        -ExpectedPythonPath $venvPython `
        -ModuleNames @('app.api_server') `
        -ExpectedArguments '-m app.api_server' `
        -ExpectedTaskIdentity $taskIdentity `
        -ExpectedListener $foreignListener `
        -ExpectedListenerPort $apiPort `
        -ExpectedInterpreterVersion $expectedInterpreterVersion) {
        throw 'A listener owned by another PID was accepted as the candidate API.'
    }

    $wrongRootProcess = [pscustomobject]@{
        ProcessId = $processId
        Name = 'python.exe'
        ExecutablePath = $basePython
        ExecutableVersion = $baseVersionText
        CommandLine = ('"{0}" -m app.api_server' -f (Join-Path $tempRoot 'foreign\.venv\Scripts\python.exe'))
    }
    if (Test-CanonicalProcessIdentity `
        -Process $wrongRootProcess `
        -RootPath $candidateRoot `
        -ExpectedPythonPath $venvPython `
        -ModuleNames @('app.api_server') `
        -ExpectedArguments '-m app.api_server' `
        -ExpectedTaskIdentity $taskIdentity `
        -ExpectedListener $listener `
        -ExpectedListenerPort $apiPort `
        -ExpectedInterpreterVersion $expectedInterpreterVersion) {
        throw 'A process launched from a foreign candidate root was accepted.'
    }

    $largeDiagnostic = ('diagnostic-' + ('x' * 2048)) * 3000
    $receipt = [ordered]@{
        schema_version = 'hwpx/windows-install/v1'
        status = 'ROLLED_BACK'
        failure_class = 'FAIL_ACTIVATION'
        status_code = 40
        phase = 'activation'
        source_root = $root
        install_root = $candidateRoot
        candidate_root = $candidateRoot
        candidate_generation = 'g10-test-candidate'
        source_identity = [ordered]@{
            repository = 'github:Junsung-Lee-coder/hwpx-editor-server'
            commit = 'g10-commit'
            tree = 'g10-tree'
            manifest_sha256 = ('a' * 64)
            file_count = 1
        }
        failed_predicates = @(
            [ordered]@{ predicate = 'api listener identity'; result = 'FAIL_API'; evidence = 'wrong executable path' }
        )
        cleanup = [ordered]@{
            candidate_removed = $true
            processes_released = $true
            port_stopped = $true
        }
        rollback = [ordered]@{
            attempted = $true
            restored = $true
            processes_released = $true
            candidate_cleanup = $true
        }
        errors = @('API listener identity failed')
        diagnostic = $largeDiagnostic
    }
    Write-JsonReceipt -Path $receiptPath -Value $receipt | Out-Null
    $receiptBytes = [int64](Get-Item -LiteralPath $receiptPath -ErrorAction Stop).Length
    if ($receiptBytes -ge 4194304) { throw "Bounded receipt is too large: $receiptBytes bytes" }
    $readback = [IO.File]::ReadAllText($receiptPath) | ConvertFrom-Json
    foreach ($property in @('status', 'failure_class', 'status_code', 'candidate_generation', 'source_identity', 'failed_predicates', 'cleanup', 'rollback', 'errors')) {
        if ($readback.PSObject.Properties.Name -notcontains $property) {
            throw "Bounded receipt lost required property: $property"
        }
    }
    if ([string]$readback.status -ne 'ROLLED_BACK' -or [int]$readback.status_code -ne 40) { throw 'Terminal rollback status was not preserved.' }
    if ([string]$readback.source_identity.commit -ne 'g10-commit') { throw 'Source identity was not preserved.' }
    if ([string]$readback.failed_predicates[0].predicate -ne 'api listener identity') { throw 'Failed predicates were not preserved.' }
    if (-not [bool]$readback.cleanup.processes_released -or -not [bool]$readback.rollback.restored) { throw 'Cleanup/rollback state was not preserved.' }

    Write-Output 'G10_VENV_PROCESS_IDENTITY=PASS'
    Write-Output ('G10_BOUNDED_RECEIPT=PASS;bytes={0}' -f $receiptBytes)
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
