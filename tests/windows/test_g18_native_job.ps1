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

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g18-native-job-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$childPidFile = Join-Path $tempRoot 'late-child.pid'
$childScriptPath = Join-Path $tempRoot 'late-child.py'
try {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    [IO.File]::WriteAllText(
        $childScriptPath,
        "import os,sys,time`nopen(sys.argv[1],'w').write(str(os.getpid()))`ntime.sleep(30)`n",
        (New-Object Text.UTF8Encoding($false))
    )
    $parentScript = "import subprocess,sys,time; subprocess.Popen([sys.executable,sys.argv[2],sys.argv[1]]); time.sleep(30)"
    $timeoutResult = Invoke-NativeChecked `
        -FilePath $python `
        -Arguments @('-c', $parentScript, $childPidFile, $childScriptPath) `
        -WorkingDirectory $tempRoot `
        -TimeoutSeconds 1 `
        -AllowNonZero
    Assert-True ([bool]$timeoutResult.job_object_owned) 'Timed-out native work was not assigned to an owned Job Object.'
    Assert-True ([bool]$timeoutResult.timed_out) 'Late-spawn timeout probe did not time out.'
    Assert-True ([bool]$timeoutResult.job_object_terminated) 'Owned Job Object was not terminated for timed-out work.'
    Assert-True ([bool]$timeoutResult.process_tree_released) 'Owned Job Object release proof was not established.'
    Assert-True (-not [bool]$timeoutResult.accepted) 'Timed-out native work was incorrectly accepted.'

    $lateChildPid = $null
    $deadline = (Get-Date).AddSeconds(2)
    do {
        if (Test-Path -LiteralPath $childPidFile -PathType Leaf) {
            $lateChildPid = [int](Get-Content -LiteralPath $childPidFile -Raw).Trim()
            break
        }
        Start-Sleep -Milliseconds 50
    } while ((Get-Date) -lt $deadline)
    Assert-True ([bool]$lateChildPid) 'Late-spawn probe did not record a child PID before timeout cleanup.'
    Start-Sleep -Milliseconds 200
    Assert-True ($null -eq (Get-Process -Id $lateChildPid -ErrorAction SilentlyContinue)) 'Late-spawned child survived owned Job Object termination.'

    $previousFault = $env:HWPX_TEST_NATIVE_FAULT
    try {
        $env:HWPX_TEST_NATIVE_FAULT = 'identity'
        $identityResult = Invoke-NativeChecked `
            -FilePath $python `
            -Arguments @('-c', 'import time; time.sleep(1)') `
            -WorkingDirectory $tempRoot `
            -TimeoutSeconds 2 `
            -AllowNonZero
    }
    finally {
        if ($null -eq $previousFault) { Remove-Item Env:HWPX_TEST_NATIVE_FAULT -ErrorAction SilentlyContinue }
        else { $env:HWPX_TEST_NATIVE_FAULT = $previousFault }
    }
    Assert-True ([bool]$identityResult.identity_capture_failed) 'Identity-capture fault was not recorded.'
    Assert-True (-not [bool]$identityResult.accepted) 'Identity-capture failure was incorrectly accepted.'
    Assert-True ([bool]$identityResult.job_object_owned) 'Identity-capture failure did not retain owned-job evidence.'

    [pscustomobject]@{
        status = 'PASS'
        timeout_job_owned = [bool]$timeoutResult.job_object_owned
        timeout_process_tree_released = [bool]$timeoutResult.process_tree_released
        identity_capture_failed = [bool]$identityResult.identity_capture_failed
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
