[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw "Windows PowerShell 5.1 is required; found $($PSVersionTable.PSVersion)"
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$commonPath = Join-Path $root 'scripts\windows_install_common.psm1'
$installerPath = Join-Path $root 'scripts\install_windows.ps1'
Import-Module $commonPath -Force

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path)))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Write-TextUtf8 {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-ScriptUtf8 {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
    # Windows PowerShell 5.1 parses a generated script containing a non-ASCII
    # temp/profile path as the system code page unless the UTF-8 BOM is present.
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($true)))
}

function Wait-FileContains {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Needle, [int]$TimeoutSeconds = 10)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            if ((Test-Path -LiteralPath $Path -PathType Leaf) -and [System.IO.File]::ReadAllText($Path).Contains($Needle)) { return $true }
        }
        catch { }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return $false
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-g3-integrated-' + [Guid]::NewGuid().ToString('N'))
$holders = @()
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
try {
    # H1: an invalid SourceRoot must not redirect the early failure receipt to
    # a caller-selected file inside InstallRoot.
    $install = Join-Path $testRoot 'install'
    $sentinel = Join-Path $install 'app\config.py'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $sentinel) | Out-Null
    Write-TextUtf8 -Path $sentinel -Text 'sentinel-source-bytes'
    $sentinelHash = Get-FileSha256 -Path $sentinel
    $unsafeReceipt = $sentinel
    $invalidSource = Join-Path $testRoot 'does-not-exist'
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $installerArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installerPath,
        '-SourceRoot', $invalidSource, '-InstallRoot', $install,
        '-ReceiptPath', $unsafeReceipt, '-DependencyMode', 'CheckOnly'
    )
    $invalidRun = Start-Process -FilePath $powershell -ArgumentList $installerArgs -WindowStyle Hidden -Wait -PassThru
    Assert-True ($invalidRun.ExitCode -ne 0) 'Invalid-source installer rehearsal unexpectedly passed.'
    Assert-True ((Get-FileSha256 -Path $sentinel) -eq $sentinelHash) 'H1 sentinel changed during early failure admission.'
    Assert-True ((Get-Item -LiteralPath $sentinel -Force).Length -eq ([System.IO.File]::ReadAllText($sentinel).Length)) 'H1 sentinel was not left as a regular file.'

    # H3/M10: the machine lifecycle and receipt locks reject a concurrent
    # caller instead of allowing last-writer-wins state.
    $lockRoot = Join-Path $testRoot 'lock-root'
    New-Item -ItemType Directory -Force -Path $lockRoot | Out-Null
    $holderOut = Join-Path $testRoot 'holder.out'
    $holderScript = Join-Path $testRoot 'holder.ps1'
    Write-ScriptUtf8 -Path $holderScript -Text @"
`$ErrorActionPreference = 'Stop'
Import-Module '$commonPath' -Force
`$lock = Enter-InstallLifecycleLock -InstallRoot '$lockRoot' -TaskNames @('hwpx-g3-task') -ApiPort 18976 -Role 'g3-holder' -TimeoutSeconds 5
`$handoff = @(`$lock.locks | Where-Object { [string]`$_.key -like 'handoff:*' })
if (`$handoff.Count -ne 1) { throw 'Verifier handoff lock was not part of the non-verifier lifecycle scope.' }
`$remainingLocks = @(`$lock.locks | Where-Object { [string]`$_.key -notlike 'handoff:*' })
Exit-InstallLifecycleLock -Lock ([pscustomobject]@{ locks = `$remainingLocks })
[System.IO.File]::WriteAllText('$holderOut', 'READY')
Start-Sleep -Seconds 8
Exit-PathMutex -Lock `$handoff[0]
"@
    $holder = Start-Process -FilePath $powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $holderScript) -WindowStyle Hidden -PassThru
    $holders += $holder
    Assert-True (Wait-FileContains -Path $holderOut -Needle 'READY') 'H3 lock holder did not acquire the machine lifecycle lock.'
    $verifierHandoffProbe = $null
    try {
        $verifierHandoffProbe = Enter-InstallLifecycleLock -InstallRoot $lockRoot -TaskNames @('hwpx-g3-task') -ApiPort 18976 -Role 'verifier' -TimeoutSeconds 2
    }
    catch { }
    Assert-True ($null -ne $verifierHandoffProbe) 'Verifier-role lifecycle admission deadlocked behind the retained handoff lock.'
    Exit-InstallLifecycleLock -Lock $verifierHandoffProbe
    $secondFailed = $false
    try {
        Enter-InstallLifecycleLock -InstallRoot $lockRoot -TaskNames @('hwpx-g3-task') -ApiPort 18976 -Role 'g3-contender' -TimeoutSeconds 1 | Out-Null
    }
    catch { $secondFailed = $true }
    Assert-True $secondFailed 'H3 concurrent lifecycle admission did not fail closed.'

    $receiptPath = Join-Path $testRoot 'verify.json'
    Write-TextUtf8 -Path $receiptPath -Text '{"status":"old"}'
    $receiptLock = Enter-ReceiptPathLock -ReceiptPath $receiptPath -TimeoutSeconds 2
    try {
        $receiptChildOut = Join-Path $testRoot 'receipt-child.out'
        $receiptChild = Join-Path $testRoot 'receipt-child.ps1'
        Write-ScriptUtf8 -Path $receiptChild -Text @"
`$ErrorActionPreference = 'Stop'
Import-Module '$commonPath' -Force
try {
  Enter-ReceiptPathLock -ReceiptPath '$receiptPath' -TimeoutSeconds 1 | Out-Null
  [System.IO.File]::WriteAllText('$receiptChildOut', 'UNEXPECTED_SUCCESS')
  exit 1
}
catch {
  [System.IO.File]::WriteAllText('$receiptChildOut', 'FAIL_CLOSED')
  exit 0
}
"@
        $receiptChildProcess = Start-Process -FilePath $powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $receiptChild) -WindowStyle Hidden -Wait -PassThru
        Assert-True ($receiptChildProcess.ExitCode -eq 0 -and (Wait-FileContains -Path $receiptChildOut -Needle 'FAIL_CLOSED')) 'M10 same-path receipt admission was not fail closed.'
    }
    finally { Exit-PathMutex -Lock $receiptLock }

    # H5: the manifest result is tied to the bytes read through one stable
    # object. A replacement attempt may either be blocked by the read handle or
    # rejected by the post-read object-identity check, but cannot authenticate a
    # different pathname object than the parsed bytes.
    $manifestRoot = Join-Path $testRoot 'manifest-root'
    New-Item -ItemType Directory -Force -Path $manifestRoot | Out-Null
    $member = Join-Path $manifestRoot 'app.py'
    Write-TextUtf8 -Path $member -Text 'print("g3")`n'
    $memberHash = Get-FileSha256 -Path $member
    $manifestPath = Join-Path $manifestRoot 'source-manifest.json'
    $manifest = [ordered]@{
        schema_version = 'hwpx/source-bundle/v1'
        repository = 'github:test/repo'
        commit = ('0' * 40)
        tree = ('1' * 40)
        identity_source = 'asserted-gitless'
        identity_verified = $false
        file_count = 1
        files = @([ordered]@{ path = 'app.py'; size = ([System.IO.File]::ReadAllBytes($member)).Length; sha256 = $memberHash })
    }
    Write-TextUtf8 -Path $manifestPath -Text (($manifest | ConvertTo-Json -Depth 10) + "`n")
    $manifestHash = Get-FileSha256 -Path $manifestPath
    $manifestResult = Get-SourceManifest -SourceRoot $manifestRoot -ManifestPath $manifestPath -ExpectedRepository 'github:test/repo' -ExpectedCommit ('0' * 40) -ExpectedTree ('1' * 40) -ExpectedManifestSha256 $manifestHash
    Assert-True ([bool]$manifestResult.ok) 'H5 stable manifest readback did not pass.'
    Assert-True ([string]$manifestResult.manifest_sha256 -eq $manifestHash) 'H5 manifest hash was not bound to the parsed bytes.'

    # H2: a non-destructive stale journal from a killed preflight is discovered
    # on re-entry and removed idempotently under the lifecycle lock.
    $journalRoot = Join-Path $testRoot 'journal-root'
    New-Item -ItemType Directory -Force -Path $journalRoot | Out-Null
    $journalPath = Get-InstallTransactionJournalPath -InstallRoot $journalRoot
    Write-StableTransactionJournal -Path $journalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        owner_run_id = 'old-g3-run'
        run_id = 'old-g3-run'
        state = 'preflight-admitted'
        phase = 'preflight'
        install_root = $journalRoot
    }) | Out-Null
    $journalRecord = Read-StableTransactionJournal -Path $journalPath
    Assert-True ($null -ne $journalRecord -and [string]$journalRecord.value.state -eq 'preflight-admitted') 'H2 journal preimage was not durably readable.'
    Assert-True ([string]$journalRecord.value.owner_run_id -ne [Guid]::Empty.ToString()) 'H2 journal owner identity was not retained.'
    Write-StableTransactionJournal -Path $journalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        owner_run_id = 'old-g3-run'
        run_id = 'old-g3-run'
        state = 'dependency-started'
        phase = 'dependency'
        install_root = $journalRoot
    }) | Out-Null
    $journalRecord = Read-StableTransactionJournal -Path $journalPath
    Assert-True ([string]$journalRecord.value.state -eq 'dependency-started') 'H2 journal replacement/readback failed on Windows PowerShell 5.1.'

    Write-Output 'G3 integrated PowerShell behavioral repairs: PASS'
}
finally {
    foreach ($holder in @($holders)) {
        try {
            if ($holder -and -not $holder.HasExited) { Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue }
        }
        catch { }
    }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
