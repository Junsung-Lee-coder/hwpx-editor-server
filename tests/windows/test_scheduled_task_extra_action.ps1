[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Import-Module (Join-Path $root 'scripts\windows_install_common.psm1') -Force
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-extra-action-' + [Guid]::NewGuid().ToString('N'))
$taskName = 'HWPX_extra_action_' + [Guid]::NewGuid().ToString('N')
$registered = $false
try {
    New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $first = New-ScheduledTaskAction -Execute $env:ComSpec -Argument '/c exit 0' -WorkingDirectory $tempRoot
    $extra = New-ScheduledTaskAction -Execute $env:ComSpec -Argument '/c exit 0' -WorkingDirectory $tempRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RunOnlyIfNetworkAvailable:$false -Hidden:$false -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $taskName -Action @($first, $extra) -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $registered = $true

    $identity = Get-ScheduledTaskIdentity -TaskName $taskName
    if ([int]$identity.action_count -ne 2 -or [int]$identity.xml_action_count -ne 2 -or [int]$identity.xml_exec_action_count -ne 2) {
        throw "Additional action was not captured: action_count=$($identity.action_count), xml_action_count=$($identity.xml_action_count), xml_exec_action_count=$($identity.xml_exec_action_count)"
    }
    $accepted = Test-CanonicalTaskActionBinding -Identity $identity -ExpectedRoot $tempRoot -ExpectedPythonPath $env:ComSpec -ExpectedArguments '/c exit 0' -ExpectedPrincipal $user
    if ($accepted) {
        throw 'A scheduled task with an extra executable action was accepted as canonical.'
    }
    Write-Output 'SCHEDULED_TASK_EXTRA_ACTION=PASS'
}
finally {
    if ($registered) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            throw "Extra-action test task remained after cleanup: $taskName"
        }
    }
    if (Test-Path -LiteralPath $tempRoot -PathType Container) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $tempRoot) {
            throw "Extra-action test temp root remained after cleanup: $tempRoot"
        }
    }
}
