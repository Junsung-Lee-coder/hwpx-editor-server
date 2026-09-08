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
Import-Module $commonPath -Force

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-TestNamespaceInventory {
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

function Get-TestRollbackSnapshotSnapshot {
    return Get-TestNamespaceInventory -Directory ([System.IO.Path]::GetTempPath()) -Filter 'hwpx-install-snapshot-*.json'
}

function Get-TestTransactionJournalSnapshot {
    $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($base)) { $base = [System.IO.Path]::GetTempPath() }
    return Get-TestNamespaceInventory -Directory (Join-Path $base 'HWPX\transactions') -Filter '*.journal.json'
}

function Assert-OpeningNamespacePreserved {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Opening,
        [Parameter(Mandatory = $true)][hashtable]$Final,
        [Parameter(Mandatory = $true)][string]$Label
    )
    foreach ($key in @($Opening.Keys)) {
        Assert-True $Final.ContainsKey($key) ("$Label opening object disappeared: $($Opening[$key].path)")
        Assert-True ([string]$Final[$key].object_identity -ceq [string]$Opening[$key].object_identity) ("$Label opening object identity changed: $($Opening[$key].path)")
        Assert-True ([string]$Final[$key].sha256 -ceq [string]$Opening[$key].sha256) ("$Label opening object bytes changed: $($Opening[$key].path)")
    }
    foreach ($key in @($Final.Keys)) {
        Assert-True $Opening.ContainsKey($key) ("$Label unexpected residue remained: $($Final[$key].path)")
    }
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-terminal-cleanup-' + [Guid]::NewGuid().ToString('N'))
$unrelatedRoot = Join-Path $testRoot 'unrelated-install'
$ownedRoot = Join-Path $testRoot 'owned-install'
$staleInstallRoot = Join-Path $testRoot 'stale-install'
New-Item -ItemType Directory -Force -Path $unrelatedRoot, $ownedRoot | Out-Null
$unrelatedSnapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-unrelated-' + [Guid]::NewGuid().ToString('N') + '.json')
$mismatchSnapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-mismatch-' + [Guid]::NewGuid().ToString('N') + '.json')
$staleSnapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-stale-' + [Guid]::NewGuid().ToString('N') + '.json')
$staleReceiptPath = Join-Path $testRoot 'stale-terminal-receipt.json'
$journalRoot = Join-Path $testRoot 'journal-root'
$openingSnapshotIdentities = $null
$openingJournalIdentities = $null
$ownedRollbackSnapshots = New-Object System.Collections.ArrayList
$ownedTransactionJournals = New-Object System.Collections.ArrayList

try {
    # Seed an unrelated snapshot before the opening inventory. Product cleanup
    # must preserve it byte-for-byte even while removing a run-owned snapshot.
    $unrelatedSnapshot = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = Get-CanonicalPath -Path $unrelatedRoot
        owner_run_id = 'unrelated-terminal-cleanup-object'
        tasks = @()
        processes = @()
    }
    Write-JsonReceipt -Path $unrelatedSnapshotPath -Value $unrelatedSnapshot -NoProjection | Out-Null
    $openingSnapshotIdentities = Get-TestRollbackSnapshotSnapshot
    $openingJournalIdentities = Get-TestTransactionJournalSnapshot
    $unrelatedKey = (Get-CanonicalPath -Path $unrelatedSnapshotPath -RequireExisting).ToLowerInvariant()

    # Stale terminal recovery must authenticate the receipt against the exact
    # journal object and snapshot preimage before either object is removed.
    New-Item -ItemType Directory -Force -Path $staleInstallRoot | Out-Null
    $installerText = [System.IO.File]::ReadAllText((Join-Path $root 'scripts\install_windows.ps1'))
    $tokens = $null
    $parseErrors = $null
    $installerAst = [System.Management.Automation.Language.Parser]::ParseInput($installerText, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw 'Installer source could not be parsed for stale terminal binding coverage.' }
    $bindingAst = $installerAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-TerminalReceiptBinding' }, $true)
    if ($null -eq $bindingAst) { throw 'Assert-TerminalReceiptBinding was not found for stale recovery coverage.' }
    $recoveryStateAst = $installerAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-TerminalTransactionRecoveryState' }, $true)
    if ($null -eq $recoveryStateAst) { throw 'Test-TerminalTransactionRecoveryState was not found for stale recovery coverage.' }
    Invoke-Expression $recoveryStateAst.Extent.Text
    Invoke-Expression $bindingAst.Extent.Text
    $staleRunId = 'terminal-cleanup-stale-run'
    $runId = 'terminal-cleanup-current-run'
    $install = Get-CanonicalPath -Path $staleInstallRoot -RequireExisting
    $staleSnapshot = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = $install
        owner_run_id = $staleRunId
        tasks = @()
        processes = @()
    }
    Write-JsonReceipt -Path $staleSnapshotPath -Value $staleSnapshot -NoProjection | Out-Null
    $staleSnapshotSha256 = Get-Sha256Hex -Path $staleSnapshotPath
    $staleSnapshotIdentity = Get-PathObjectIdentity -Path $staleSnapshotPath -RequireExisting
    [void]$ownedRollbackSnapshots.Add([pscustomobject]@{ path = $staleSnapshotPath; object_identity = $staleSnapshotIdentity })
    $staleJournalPath = Get-InstallTransactionJournalPath -InstallRoot $staleInstallRoot
    $staleCandidateGeneration = ('a' * 40) + ':' + ('b' * 40) + ':' + ('c' * 64)
    Write-StableTransactionJournal -Path $staleJournalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        run_id = $staleRunId
        owner_run_id = $staleRunId
        state = 'terminal-committing'
        phase = 'activation'
        install_root = $install
        candidate_generation = $staleCandidateGeneration
        snapshot_path = $staleSnapshotPath
        snapshot_sha256 = $staleSnapshotSha256
        snapshot_identity = $staleSnapshotIdentity
        receipt_path = $staleReceiptPath
        terminal_cleanup_authorized = $true
        terminal_cleanup_state = 'authorized-pending'
        terminal_cleanup_owner_run_id = $staleRunId
        terminal_status = 'PASS_RUNTIME_ONLY'
        terminal_status_code = 0
    }) | Out-Null
    $staleJournalRecord = Read-StableTransactionJournal -Path $staleJournalPath
    [void]$ownedTransactionJournals.Add([pscustomobject]@{ path = $staleJournalRecord.path; object_identity = $staleJournalRecord.object_identity })
    $staleReceipt = [ordered]@{
        schema_version = 'hwpx/windows-install/v1'
        status = 'PASS_RUNTIME_ONLY'
        failure_class = $null
        status_code = 0
        run_id = $staleRunId
        install_root = $install
        candidate_generation = $staleCandidateGeneration
        snapshot_path = $staleSnapshotPath
        snapshot_identity = [ordered]@{
            schema_version = 'hwpx/windows-install-snapshot/v1'
            path = $staleSnapshotPath
            sha256 = $staleSnapshotSha256
            object_identity = $staleSnapshotIdentity
            owner_run_id = $staleRunId
        }
        snapshot_cleanup = [ordered]@{ path = $staleSnapshotPath; terminal_readback = $true; removed = $false }
        terminal_cleanup = [ordered]@{
            schema_version = 'hwpx/windows-terminal-cleanup/v1'
            cleanup_authorized = $true
            cleanup_state = 'authorized-pending'
            recovery_required = $false
            owner_run_id = $staleRunId
            terminal_status = 'PASS_RUNTIME_ONLY'
            terminal_status_code = 0
        }
        transaction_journal = [ordered]@{
            path = $staleJournalRecord.path
            object_identity = $staleJournalRecord.object_identity
            owner_run_id = $staleRunId
        }
    }
    $staleReceipt.snapshot_identity.sha256 = ('d' * 64)
    Write-JsonReceipt -Path $staleReceiptPath -Value $staleReceipt -NoProjection | Out-Null
    $staleMismatchRefused = $false
    try { Assert-TerminalReceiptBinding -Journal $staleJournalRecord.value -Record $staleJournalRecord -ReceiptPath $staleReceiptPath | Out-Null }
    catch { $staleMismatchRefused = $true }
    Assert-True $staleMismatchRefused 'Stale terminal receipt hash mismatch was not refused.'
    Assert-True (Test-Path -LiteralPath $staleSnapshotPath -PathType Leaf) 'Stale snapshot was deleted after receipt mismatch refusal.'
    Assert-True (Test-Path -LiteralPath $staleJournalRecord.path -PathType Leaf) 'Stale journal was deleted after receipt mismatch refusal.'
    $staleReceipt.snapshot_identity.sha256 = $staleSnapshotSha256
    $staleReceipt.snapshot_identity.object_identity = 'wrong-object-identity'
    Write-JsonReceipt -Path $staleReceiptPath -Value $staleReceipt -NoProjection | Out-Null
    $staleIdentityMismatchRefused = $false
    try { Assert-TerminalReceiptBinding -Journal $staleJournalRecord.value -Record $staleJournalRecord -ReceiptPath $staleReceiptPath | Out-Null }
    catch { $staleIdentityMismatchRefused = $true }
    Assert-True $staleIdentityMismatchRefused 'Stale terminal receipt object identity mismatch was not refused.'
    Assert-True (Test-Path -LiteralPath $staleSnapshotPath -PathType Leaf) 'Stale snapshot was deleted after object identity refusal.'
    $staleReceipt.snapshot_identity.object_identity = $staleSnapshotIdentity
    Write-JsonReceipt -Path $staleReceiptPath -Value $staleReceipt -NoProjection | Out-Null
    $staleBinding = Assert-TerminalReceiptBinding -Journal $staleJournalRecord.value -Record $staleJournalRecord -ReceiptPath $staleReceiptPath
    Assert-True ([bool]$staleBinding.cleanup_authorized) 'Authenticated successful terminal receipt did not authorize exact cleanup.'

    # A preflight failure before any recovery object exists authorizes only
    # journal cleanup. This covers the legacy authorized-pending state that can
    # remain if the controller crashes before it records the newer explicit
    # journal-only state.
    $preflightRunId = 'terminal-cleanup-preflight-run'
    $preflightReceiptPath = Join-Path $testRoot 'preflight-receipt.json'
    $preflightJournalPath = Join-Path $journalRoot 'preflight.journal.json'
    Write-StableTransactionJournal -Path $preflightJournalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        run_id = $preflightRunId
        owner_run_id = $preflightRunId
        state = 'terminal-committing'
        phase = 'preflight'
        install_root = $install
        candidate_generation = ''
        candidate_root = ''
        candidate_root_identity = ''
        install_root_created_by_run = $false
        backup_root = ''
        backup_root_identity = ''
        backup_root_owned_by_run = $false
        backup_claim_path = ''
        backup_claim_identity = ''
        root_moved_to_backup = $false
        snapshot_path = ''
        snapshot_sha256 = ''
        snapshot_identity = ''
        generated_manifest_path = ''
        generated_manifest_sha256 = ''
        generated_manifest_identity = ''
        generated_manifest_owned_by_run = $false
        dependency_mutation_attempted = $false
        dependency_mutation_retained = $false
        task_names = @()
        receipt_path = $preflightReceiptPath
        terminal_cleanup_authorized = $true
        terminal_cleanup_state = 'journal-only-authorized-pending'
        terminal_cleanup_owner_run_id = $preflightRunId
        terminal_status = 'FAIL_PREFLIGHT'
        terminal_status_code = 10
    }) | Out-Null
    $preflightJournalRecord = Read-StableTransactionJournal -Path $preflightJournalPath
    Write-JsonReceipt -Path $preflightReceiptPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install/v1'
        status = 'FAIL_PREFLIGHT'
        failure_class = 'FAIL_PREFLIGHT'
        status_code = 10
        run_id = $preflightRunId
        install_root = $install
        terminal_cleanup = [ordered]@{
            schema_version = 'hwpx/windows-terminal-cleanup/v1'
            cleanup_authorized = $true
            cleanup_state = 'journal-only-authorized-pending'
            recovery_required = $false
            owner_run_id = $preflightRunId
            terminal_status = 'FAIL_PREFLIGHT'
            terminal_status_code = 10
        }
        transaction_journal = [ordered]@{
            path = $preflightJournalRecord.path
            object_identity = $preflightJournalRecord.object_identity
            owner_run_id = $preflightRunId
        }
        rollback = [ordered]@{ attempted = $false; restored = $false }
    }) -NoProjection | Out-Null
    $preflightBinding = Assert-TerminalReceiptBinding -Journal $preflightJournalRecord.value -Record $preflightJournalRecord -ReceiptPath $preflightReceiptPath
    Assert-True ([bool]$preflightBinding.cleanup_authorized) 'Preflight failure without recovery state did not authorize journal-only cleanup.'
    Assert-True ([string]$preflightBinding.cleanup_state -ceq 'journal-only-authorized-pending') 'Preflight journal-only cleanup state was not preserved.'
    [void]$ownedTransactionJournals.Add([pscustomobject]@{ path = $preflightJournalRecord.path; object_identity = $preflightJournalRecord.object_identity })
    Assert-PathObjectIdentity -Path $preflightJournalRecord.path -ExpectedIdentity $preflightJournalRecord.object_identity | Out-Null
    Remove-PathIdentityExact -Path $preflightJournalRecord.path -ExpectedObjectIdentity $preflightJournalRecord.object_identity | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $preflightJournalRecord.path)) 'Preflight journal-only cleanup did not remove the exact journal.'

    # A failed rollback is terminal evidence, not deletion authority. The
    # validator must accept its preservation contract while returning a false
    # cleanup authorization, and both objects must remain available for safe
    # restart recovery.
    $failedRunId = 'terminal-cleanup-failed-rollback-run'
    $failedSnapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-failed-' + [Guid]::NewGuid().ToString('N') + '.json')
    $failedReceiptPath = Join-Path $testRoot 'failed-rollback-receipt.json'
    Write-JsonReceipt -Path $failedSnapshotPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = $install
        owner_run_id = $failedRunId
        tasks = @()
        processes = @()
    }) -NoProjection | Out-Null
    $failedSnapshotSha256 = Get-Sha256Hex -Path $failedSnapshotPath
    $failedSnapshotIdentity = Get-PathObjectIdentity -Path $failedSnapshotPath -RequireExisting
    $failedCandidateGeneration = ('e' * 40) + ':' + ('f' * 40) + ':' + ('1' * 64)
    $failedJournalPath = Join-Path $journalRoot 'failed.journal.json'
    Write-StableTransactionJournal -Path $failedJournalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        run_id = $failedRunId
        owner_run_id = $failedRunId
        state = 'terminal-committing'
        phase = 'activation'
        install_root = $install
        candidate_generation = $failedCandidateGeneration
        snapshot_path = $failedSnapshotPath
        snapshot_sha256 = $failedSnapshotSha256
        snapshot_identity = $failedSnapshotIdentity
        receipt_path = $failedReceiptPath
        terminal_cleanup_authorized = $false
        terminal_cleanup_state = 'preserve-for-recovery'
        terminal_cleanup_owner_run_id = $failedRunId
        terminal_status = 'FAIL_ROLLBACK_FAILED'
        terminal_status_code = 99
    }) | Out-Null
    $failedJournalRecord = Read-StableTransactionJournal -Path $failedJournalPath
    $failedReceipt = [ordered]@{
        schema_version = 'hwpx/windows-install/v1'
        status = 'FAIL_ROLLBACK_FAILED'
        failure_class = 'FAIL_ROLLBACK_FAILED'
        status_code = 99
        run_id = $failedRunId
        install_root = $install
        candidate_generation = $failedCandidateGeneration
        snapshot_path = $failedSnapshotPath
        snapshot_identity = [ordered]@{ path = $failedSnapshotPath; sha256 = $failedSnapshotSha256; object_identity = $failedSnapshotIdentity; owner_run_id = $failedRunId }
        snapshot_cleanup = [ordered]@{ path = $failedSnapshotPath; terminal_readback = $true; removed = $false }
        terminal_cleanup = [ordered]@{
            schema_version = 'hwpx/windows-terminal-cleanup/v1'
            cleanup_authorized = $false
            cleanup_state = 'preserve-for-recovery'
            recovery_required = $true
            owner_run_id = $failedRunId
            terminal_status = 'FAIL_ROLLBACK_FAILED'
            terminal_status_code = 99
        }
        transaction_journal = [ordered]@{ path = $failedJournalRecord.path; object_identity = $failedJournalRecord.object_identity; owner_run_id = $failedRunId }
        rollback = [ordered]@{ attempted = $true; restored = $false }
    }
    Write-JsonReceipt -Path $failedReceiptPath -Value $failedReceipt -NoProjection | Out-Null
    $failedBinding = Assert-TerminalReceiptBinding -Journal $failedJournalRecord.value -Record $failedJournalRecord -ReceiptPath $failedReceiptPath
    Assert-True (-not [bool]$failedBinding.cleanup_authorized) 'FAIL_ROLLBACK_FAILED was incorrectly accepted as deletion authority.'
    Assert-True ([string]$failedBinding.cleanup_state -ceq 'preserve-for-recovery') 'Failed rollback did not enter preserve-for-recovery state.'
    Assert-True (Test-Path -LiteralPath $failedSnapshotPath -PathType Leaf) 'Failed rollback snapshot was not preserved.'
    Assert-True (Test-Path -LiteralPath $failedJournalPath -PathType Leaf) 'Failed rollback journal was not preserved.'
    [void]$ownedRollbackSnapshots.Add([pscustomobject]@{ path = $failedSnapshotPath; object_identity = $failedSnapshotIdentity })
    [void]$ownedTransactionJournals.Add([pscustomobject]@{ path = $failedJournalPath; object_identity = $failedJournalRecord.object_identity })

    # The preservation assertions above must observe both recovery objects,
    # while the opening-namespace postcheck below must not treat this
    # test-owned pair as unrelated residue. Clean them through the same exact
    # identity-bound helpers before comparing the final namespace.
    $failedSnapshotCleanup = Remove-InstallSnapshotExact `
        -SnapshotPath $failedSnapshotPath `
        -ExpectedSnapshotSha256 $failedSnapshotSha256 `
        -ExpectedSnapshotIdentity $failedSnapshotIdentity `
        -ExpectedRunId $failedRunId `
        -OwnedByRun:$true
    Assert-True ([bool]$failedSnapshotCleanup.removed -and -not (Test-Path -LiteralPath $failedSnapshotPath -PathType Leaf)) 'Test-owned failed rollback snapshot cleanup did not remove the exact object.'
    Assert-PathObjectIdentity -Path $failedJournalRecord.path -ExpectedIdentity $failedJournalRecord.object_identity | Out-Null
    Remove-PathIdentityExact -Path $failedJournalRecord.path -ExpectedObjectIdentity $failedJournalRecord.object_identity | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $failedJournalRecord.path -PathType Leaf)) 'Test-owned failed rollback journal cleanup did not remove the exact object.'

    # A valid failed terminal receipt must remain in the explicit-recovery
    # branch. It must not fall through to ordinary stale snapshot recovery,
    # whose journal cleanup is reserved for non-terminal transactions.
    $recoveryText = [System.IO.File]::ReadAllText((Join-Path $root 'scripts\install_windows.ps1'))
    $failedTerminalBranchStart = $recoveryText.IndexOf('if ($terminalReceiptPresent -and -not $terminalReceiptValid)')
    $failedTerminalBranchEnd = $recoveryText.IndexOf('if ($terminalReceiptValid)', $failedTerminalBranchStart)
    Assert-True ($failedTerminalBranchStart -ge 0 -and $failedTerminalBranchEnd -gt $failedTerminalBranchStart) 'Failed terminal recovery branch was not found.'
    $failedTerminalBranch = $recoveryText.Substring($failedTerminalBranchStart, $failedTerminalBranchEnd - $failedTerminalBranchStart)
    Assert-True ($failedTerminalBranch.Contains('$script:staleTransactionRecoveryFailed = $true')) 'Failed terminal recovery did not enter the fail-closed state.'
    Assert-True ($failedTerminalBranch.Contains('preserving its recovery objects')) 'Failed terminal recovery preservation message is missing.'
    Assert-True (-not $failedTerminalBranch.Contains('Restore-InstallSnapshot')) 'Failed terminal recovery fell through to destructive snapshot reconciliation.'

    Assert-TerminalReceiptBinding -Journal $staleJournalRecord.value -Record $staleJournalRecord -ReceiptPath $staleReceiptPath | Out-Null
    $staleCleanup = Remove-InstallSnapshotExact -SnapshotPath $staleSnapshotPath -ExpectedSnapshotSha256 $staleSnapshotSha256 -ExpectedSnapshotIdentity $staleSnapshotIdentity -ExpectedRunId $staleRunId -OwnedByRun:$true
    Assert-True ([bool]$staleCleanup.removed -and -not (Test-Path -LiteralPath $staleSnapshotPath -PathType Leaf)) 'Validated stale snapshot was not removed.'
    Assert-PathObjectIdentity -Path $staleJournalRecord.path -ExpectedIdentity $staleJournalRecord.object_identity | Out-Null
    Remove-PathIdentityExact -Path $staleJournalRecord.path -ExpectedObjectIdentity $staleJournalRecord.object_identity | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $staleJournalRecord.path -PathType Leaf)) 'Validated stale journal was not removed.'

    # A same-run replacement/readback fault may not silently publish unrelated
    # bytes. The prior receipt must remain byte-equivalent or be retained as
    # explicit HOLD evidence.
    $replacementPath = Join-Path $testRoot 'replacement-receipt.json'
    Write-JsonReceipt -Path $replacementPath -Value ([ordered]@{ schema_version = 'hwpx/test-receipt/v1'; run_id = 'replacement-baseline'; value = 'known-good' }) -NoProjection | Out-Null
    $replacementSha = Get-Sha256Hex -Path $replacementPath
    $oldReceiptFault = [Environment]::GetEnvironmentVariable('HWPX_TEST_RECEIPT_FAULT', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('HWPX_TEST_RECEIPT_FAULT', 'readback', 'Process')
        $replacementFailed = $false
        try { Write-JsonReceipt -Path $replacementPath -Value ([ordered]@{ schema_version = 'hwpx/test-receipt/v1'; run_id = 'replacement-unrelated'; value = 'must-not-publish' }) -NoProjection | Out-Null }
        catch { $replacementFailed = $true }
        Assert-True $replacementFailed 'Injected same-run receipt readback fault unexpectedly passed.'
    }
    finally {
        if ($null -eq $oldReceiptFault) { [Environment]::SetEnvironmentVariable('HWPX_TEST_RECEIPT_FAULT', $null, 'Process') }
        else { [Environment]::SetEnvironmentVariable('HWPX_TEST_RECEIPT_FAULT', $oldReceiptFault, 'Process') }
    }
    $replacementTargetPreserved = (Test-Path -LiteralPath $replacementPath -PathType Leaf) -and (Get-Sha256Hex -Path $replacementPath) -ceq $replacementSha
    $replacementHoldPresent = @(
        Get-ChildItem -LiteralPath $testRoot -File -Filter 'replacement-receipt.json.HOLD' -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath (Split-Path -Parent $replacementPath) -File -Filter '.receipt-backup-*.tmp.HOLD' -Force -ErrorAction SilentlyContinue
    ).Count -gt 0
    Assert-True ($replacementTargetPreserved -or $replacementHoldPresent) 'Same-run receipt replacement fault lost both the prior receipt and HOLD evidence.'

    # Positive path: an authenticated owner can remove only the sealed object.
    $ownedRunId = 'terminal-cleanup-positive-run'
    $ownedSnapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-owned-' + [Guid]::NewGuid().ToString('N') + '.json')
    $ownedSnapshot = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = Get-CanonicalPath -Path $ownedRoot
        owner_run_id = $ownedRunId
        tasks = @()
        processes = @()
    }
    Write-JsonReceipt -Path $ownedSnapshotPath -Value $ownedSnapshot -NoProjection | Out-Null
    $ownedSnapshotSha256 = Get-Sha256Hex -Path $ownedSnapshotPath
    $ownedSnapshotIdentity = Get-PathObjectIdentity -Path $ownedSnapshotPath -RequireExisting
    [void]$ownedRollbackSnapshots.Add([pscustomobject]@{ path = $ownedSnapshotPath; object_identity = $ownedSnapshotIdentity })
    $ownedCleanup = Remove-InstallSnapshotExact -SnapshotPath $ownedSnapshotPath -ExpectedSnapshotSha256 $ownedSnapshotSha256 -ExpectedSnapshotIdentity $ownedSnapshotIdentity -ExpectedRunId $ownedRunId -OwnedByRun:$true
    Assert-True ([bool]$ownedCleanup.removed -and -not (Test-Path -LiteralPath $ownedSnapshotPath -PathType Leaf)) 'Positive owned snapshot cleanup did not remove the exact snapshot.'

    # Mismatch path: a changed preimage must remain untouched until an operator
    # can adjudicate it; this also proves unrelated objects are not a fallback.
    $mismatchRunId = 'terminal-cleanup-mismatch-run'
    $mismatchSnapshot = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = Get-CanonicalPath -Path $ownedRoot
        owner_run_id = $mismatchRunId
        tasks = @()
        processes = @()
    }
    Write-JsonReceipt -Path $mismatchSnapshotPath -Value $mismatchSnapshot -NoProjection | Out-Null
    $mismatchSha256 = Get-Sha256Hex -Path $mismatchSnapshotPath
    $mismatchIdentity = Get-PathObjectIdentity -Path $mismatchSnapshotPath -RequireExisting
    [void]$ownedRollbackSnapshots.Add([pscustomobject]@{ path = $mismatchSnapshotPath; object_identity = $mismatchIdentity })
    [System.IO.File]::AppendAllText($mismatchSnapshotPath, 'tampered', (New-Object System.Text.UTF8Encoding($false)))
    $mismatchRefused = $false
    try {
        Remove-InstallSnapshotExact -SnapshotPath $mismatchSnapshotPath -ExpectedSnapshotSha256 $mismatchSha256 -ExpectedSnapshotIdentity $mismatchIdentity -ExpectedRunId $mismatchRunId -OwnedByRun:$true | Out-Null
    }
    catch { $mismatchRefused = $true }
    Assert-True $mismatchRefused 'Snapshot hash mismatch was not refused.'
    Assert-True (Test-Path -LiteralPath $mismatchSnapshotPath -PathType Leaf) 'Mismatched snapshot was deleted after refusal.'
    Assert-True ([string](Get-PathObjectIdentity -Path $mismatchSnapshotPath -RequireExisting) -ceq $mismatchIdentity) 'Mismatched snapshot identity changed unexpectedly.'
    [System.IO.File]::WriteAllText($mismatchSnapshotPath, (($mismatchSnapshot | ConvertTo-Json -Depth 20) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    $mismatchSha256 = Get-Sha256Hex -Path $mismatchSnapshotPath
    Remove-InstallSnapshotExact -SnapshotPath $mismatchSnapshotPath -ExpectedSnapshotSha256 $mismatchSha256 -ExpectedSnapshotIdentity $mismatchIdentity -ExpectedRunId $mismatchRunId -OwnedByRun:$true | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $mismatchSnapshotPath -PathType Leaf)) 'Revalidated mismatch snapshot was not cleaned by the test-owned cleanup.'

    # Create and remove one run-owned journal, then prove the opening journal
    # namespace and unrelated snapshot namespace are unchanged.
    New-Item -ItemType Directory -Force -Path $journalRoot | Out-Null
    $journalPath = Get-InstallTransactionJournalPath -InstallRoot $journalRoot
    Write-StableTransactionJournal -Path $journalPath -Value ([ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        owner_run_id = 'terminal-cleanup-journal-run'
        run_id = 'terminal-cleanup-journal-run'
        state = 'terminal-committing'
        phase = 'install'
        install_root = Get-CanonicalPath -Path $journalRoot
    }) | Out-Null
    $journalRecord = Read-StableTransactionJournal -Path $journalPath
    [void]$ownedTransactionJournals.Add([pscustomobject]@{ path = $journalRecord.path; object_identity = $journalRecord.object_identity })
    Assert-PathObjectIdentity -Path $journalRecord.path -ExpectedIdentity $journalRecord.object_identity | Out-Null
    Remove-PathIdentityExact -Path $journalRecord.path -ExpectedObjectIdentity $journalRecord.object_identity | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $journalRecord.path -PathType Leaf)) 'Run-owned transaction journal remained after exact cleanup.'

    $finalSnapshotIdentities = Get-TestRollbackSnapshotSnapshot
    $finalJournalIdentities = Get-TestTransactionJournalSnapshot
    $unrelatedPreserved = $finalSnapshotIdentities.ContainsKey($unrelatedKey) -and
        [string]$finalSnapshotIdentities[$unrelatedKey].object_identity -ceq [string]$openingSnapshotIdentities[$unrelatedKey].object_identity -and
        [string]$finalSnapshotIdentities[$unrelatedKey].sha256 -ceq [string]$openingSnapshotIdentities[$unrelatedKey].sha256
    Assert-True $unrelatedPreserved 'Unrelated rollback snapshot was not preserved byte-for-byte.'
    Assert-OpeningNamespacePreserved -Opening $openingSnapshotIdentities -Final $finalSnapshotIdentities -Label 'rollback snapshot namespace'
    Assert-OpeningNamespacePreserved -Opening $openingJournalIdentities -Final $finalJournalIdentities -Label 'transaction journal namespace'

    # Final cleanup postcheck inventories both namespaces after all actions; it
    # is intentionally machine-readable for bounded acceptance provenance.
    $finalPostcheck = [ordered]@{
        schema = 'hwpx/windows-terminal-cleanup-postcheck/v1'
        captured_utc = [DateTime]::UtcNow.ToString('o')
        rollback_snapshot_namespace = [ordered]@{
            opening_count = [int]$openingSnapshotIdentities.Count
            final_count = [int]$finalSnapshotIdentities.Count
            entries = @($finalSnapshotIdentities.Values)
            unrelated_preserved = [bool]$unrelatedPreserved
        }
        transaction_journal_namespace = [ordered]@{
            opening_count = [int]$openingJournalIdentities.Count
            final_count = [int]$finalJournalIdentities.Count
            entries = @($finalJournalIdentities.Values)
        }
        checks = [ordered]@{
            opening_snapshots_preserved = $true
            no_new_snapshot_residue = ($finalSnapshotIdentities.Count -eq $openingSnapshotIdentities.Count)
            opening_journals_preserved = $true
            no_new_journal_residue = ($finalJournalIdentities.Count -eq $openingJournalIdentities.Count)
            unrelated_snapshot_preserved = [bool]$unrelatedPreserved
            owned_snapshot_cleanup_verified = (-not (Test-Path -LiteralPath $ownedSnapshotPath) -and -not (Test-Path -LiteralPath $mismatchSnapshotPath))
            owned_journal_cleanup_verified = (-not (Test-Path -LiteralPath $journalRecord.path))
        }
    }
    $postcheckPath = Join-Path $testRoot 'terminal-cleanup-postcheck.json'
    Write-JsonReceipt -Path $postcheckPath -Value $finalPostcheck | Out-Null
    $failedChecks = @($finalPostcheck.checks.GetEnumerator() | Where-Object { -not [bool]$_.Value })
    Assert-True ($failedChecks.Count -eq 0) ("Final cleanup postcheck failed: " + (($failedChecks | ForEach-Object { $_.Key }) -join ', '))
    Write-Output (($finalPostcheck | ConvertTo-Json -Depth 12 -Compress))
    Write-Output 'Windows terminal snapshot/journal cleanup contracts: PASS'
}
finally {
    foreach ($owned in @($ownedRollbackSnapshots)) {
        if (Test-Path -LiteralPath $owned.path -PathType Leaf) {
            try {
                $identity = Get-PathObjectIdentity -Path $owned.path -RequireExisting
                Remove-PathIdentityExact -Path $owned.path -ExpectedObjectIdentity $identity | Out-Null
            }
            catch { }
        }
    }
    foreach ($owned in @($ownedTransactionJournals)) {
        if (Test-Path -LiteralPath $owned.path -PathType Leaf) {
            try {
                Assert-PathObjectIdentity -Path $owned.path -ExpectedIdentity $owned.object_identity | Out-Null
                Remove-PathIdentityExact -Path $owned.path -ExpectedObjectIdentity $owned.object_identity | Out-Null
            }
            catch { }
        }
    }
    if (Test-Path -LiteralPath $unrelatedSnapshotPath -PathType Leaf) {
        try {
            $identity = Get-PathObjectIdentity -Path $unrelatedSnapshotPath -RequireExisting
            Remove-PathIdentityExact -Path $unrelatedSnapshotPath -ExpectedObjectIdentity $identity | Out-Null
        }
        catch { }
    }
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
