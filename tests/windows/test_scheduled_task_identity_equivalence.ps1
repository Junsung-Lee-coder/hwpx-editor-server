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

$taskName = 'HWPX_G9_identity_equivalence_' + [Guid]::NewGuid().ToString('N')
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g9-task-identity-' + [Guid]::NewGuid().ToString('N'))
$taskRegistered = $false
try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($user)) { throw 'Current Windows identity is unavailable.' }

    $action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument '/c exit 0' -WorkingDirectory $tempRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable:$false `
        -Hidden:$false `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $taskRegistered = $true

    $identity = Get-ScheduledTaskIdentity -TaskName $taskName
    if (-not $identity.exists) { throw 'Registered task was not found during readback.' }
    if ([string]$identity.logon_type -ne 'Interactive') {
        throw "Readback did not normalize the interactive logon type: actual=$($identity.logon_type)"
    }
    if ([string]$identity.run_level -ne 'Limited') {
        throw "Readback did not normalize the default run level: actual=$($identity.run_level)"
    }
    if (-not (Test-ScheduledTaskLogonTypeEquivalent -Actual $identity.logon_type -Expected 'Interactive')) {
        throw "InteractiveToken/Interactive equivalence failed: actual=$($identity.logon_type)"
    }
    if (-not (Test-ScheduledTaskRunLevelEquivalent -Actual $identity.run_level -Expected 'Limited')) {
        throw "Omitted/Limited run-level equivalence failed: actual=$($identity.run_level)"
    }
    if (-not (Test-CanonicalTaskSettings -Identity $identity)) {
        throw "Canonical task settings readback failed: settings=$($identity.settings | ConvertTo-Json -Compress)"
    }
    if (Test-ScheduledTaskLogonTypeEquivalent -Actual 'Password' -Expected 'Interactive') {
        throw 'Password logon type was accepted as Interactive.'
    }
    if (Test-ScheduledTaskRunLevelEquivalent -Actual 'Highest' -Expected 'Limited') {
        throw 'Highest run level was accepted as Limited.'
    }

    Write-Output ("SCHEDULED_TASK_IDENTITY_READBACK=logon_type={0};run_level={1};persisted_logon_type={2};persisted_run_level={3}" -f $identity.logon_type, $identity.run_level, $identity.persisted_logon_type, $identity.persisted_run_level)
    Write-Output 'SCHEDULED_TASK_IDENTITY_EQUIVALENCE=PASS'
}
finally {
    if ($taskRegistered) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
