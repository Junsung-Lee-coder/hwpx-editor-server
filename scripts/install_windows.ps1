[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$InstallRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) '.hwpx-install'),
    [ValidateSet('CheckOnly', 'InstallUserScope')]
    [string]$DependencyMode = 'CheckOnly',
    [string]$FixturePath,
    [string]$PopplerPath,
    [Nullable[int]]$ApiPort,
    [ValidateSet('Fail', 'PreserveMove')]
    [string]$ExistingInstallDisposition = 'Fail',
    [switch]$ReplaceExistingTasks,
    [string]$ReceiptPath,
    [string]$ExpectedRepository,
    [string]$ExpectedCommit,
    [string]$ExpectedTree,
    [string]$ExpectedManifestSha256
)

$ErrorActionPreference = 'Stop'
$commonPath = Join-Path $PSScriptRoot 'windows_install_common.psm1'
Import-Module $commonPath -Force

$source = $null
$install = $null
$requestedApiPort = $ApiPort
$installInputPath = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($InstallRoot))
$installRootExistedAtStart = Test-Path -LiteralPath $installInputPath -PathType Container
# Never canonicalize or write the caller's requested receipt before source,
# install-root, containment, reparse, and preimage admission. A malicious or
# accidental path inside InstallRoot must not receive an early failure record.
$requestedReceiptPath = $ReceiptPath
$receiptFile = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-' + [Guid]::NewGuid().ToString('N') + '.json')
$snapshotPath = $null
$snapshotOwnedByRun = $false
$snapshotSha256 = $null
$snapshotIdentity = $null
$candidateRoot = $null
$candidateRootOwnedByRun = $false
$candidateRootIdentity = $null
$installRootIdentity = $null
$apiPort = $null
$backupRoot = $null
$backupRootOwnedByRun = $false
$backupClaimPath = $null
$backupClaimIdentity = $null
$backupRootCanonical = $null
$backupRootIdentity = $null
$preMoveRootIdentity = $null
$preMoveTaskIdentity = @{}
$preMoveTaskAdmissionIdentity = @{}
$preMoveBackupInventory = $null
$backupInventory = $null
$rootMovedToBackup = $false
$rollbackCandidateQuarantine = $null
$rollbackCandidateQuarantineOwnedByRun = $false
$rollbackCandidateQuarantineIdentity = $null
$rollbackQuarantineClaimPath = $null
$rollbackQuarantineClaimIdentity = $null
$rollbackBackupActivated = $false
$candidateInstallCreated = $false
$configCreated = $false
$configPath = $null
$configCreatedSha256 = $null
$installRootLock = $null
$receiptPreimageBackupPath = $null
$receiptPreimageBackupIdentity = $null
$receiptPreimageBackupSha256 = $null
$receiptPreimageBackupSize = $null
$receiptPreimageOwnedByRun = $false
$transactionJournalPath = $null
$transactionJournalIdentity = $null
$transactionJournalOwnerRun = $false
$verifierHandoffLock = $null
$verifierHandoffActive = $false
$verifierHandoffRoot = $null
$verifierHandoffRootIdentity = $null
$taskNames = @()
$taskPath = '\'
$dependencyMutationAttempted = $false
$dependencyMutationRetained = $false
$runId = [Guid]::NewGuid().ToString('N')
$phase = 'preflight'
$script:receiptPersistenceFailed = $false
$receipt = [ordered]@{
    schema_version = 'hwpx/windows-install/v1'
    status = 'FAIL_PREFLIGHT'
    failure_class = 'FAIL_PREFLIGHT'
    status_code = 10
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    phase = 'preflight'
    source_root = $SourceRoot
    install_root = $InstallRoot
    dependency_mode = $DependencyMode
    existing_install_disposition = $ExistingInstallDisposition
    replace_existing_tasks = [bool]$ReplaceExistingTasks
    api_port = $null
    api_base_url = $null
    candidate_generation = $null
    checks = [ordered]@{}
    native_commands = @()
    task_identities_before = @()
    task_identities_after = @()
    rollback = [ordered]@{ attempted = $false; restored = $false; processes_released = $false; backup_root = $null; pre_swap_process_release = @(); candidate_tasks_removed = @() }
    install_root_preimage = [ordered]@{
        path = $installInputPath
        exists = [bool]$installRootExistedAtStart
        receipt_path = $receiptFile
        receipt_parent_exists = [bool](Test-Path -LiteralPath (Split-Path -Parent $receiptFile) -PathType Container)
    }
    receipt_preimage = [ordered]@{
        path = $receiptFile
        exists = [bool](Test-Path -LiteralPath $receiptFile -PathType Leaf)
        parent_exists = [bool](Test-Path -LiteralPath (Split-Path -Parent $receiptFile) -PathType Container)
    }
    snapshot_path = $null
    candidate_root = $null
    backup_identity = $null
    backup_inventory = $null
    external_mutations = @()
    errors = @()
    run_id = $runId
}

function Get-InstallerTransactionJournalPayload {
    param([Parameter(Mandatory = $true)][string]$State)
    return [ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        run_id = $runId
        owner_run_id = $runId
        owner_process_id = [int]$PID
        owner_process_start_identity = Get-ProcessGenerationIdentity -ProcessId $PID
        state = $State
        phase = [string]$phase
        updated_at_utc = [DateTime]::UtcNow.ToString('o')
        source_root = [string]$source
        install_root = [string]$install
        candidate_generation = $receipt.candidate_generation
        candidate_root = [string]$candidateRoot
        candidate_root_identity = [string]$candidateRootIdentity
        install_root_identity = [string]$installRootIdentity
        install_root_created_by_run = [bool]$candidateInstallCreated
        backup_root = [string]$backupRoot
        backup_root_identity = [string]$backupRootIdentity
        backup_root_owned_by_run = [bool]$backupRootOwnedByRun
        backup_claim_path = [string]$backupClaimPath
        backup_claim_identity = [string]$backupClaimIdentity
        root_moved_to_backup = [bool]$rootMovedToBackup
        snapshot_path = [string]$snapshotPath
        snapshot_sha256 = [string]$snapshotSha256
        snapshot_identity = [string]$snapshotIdentity
        task_names = @($taskNames)
        task_path = [string]$taskPath
        api_port = $apiPort
        receipt_path = [string]$receiptFile
    }
}

function Write-InstallTransactionJournal {
    param([string]$State = 'in_progress')
    if ([string]::IsNullOrWhiteSpace([string]$install)) { return $null }
    if ([string]::IsNullOrWhiteSpace([string]$transactionJournalPath)) {
        $transactionJournalPath = Get-InstallTransactionJournalPath -InstallRoot $install
    }
    $journalPayload = Get-InstallerTransactionJournalPayload -State $State
    $written = Write-StableTransactionJournal -Path $transactionJournalPath -Value $journalPayload
    $transactionJournalPath = $written
    $transactionJournalIdentity = Get-PathObjectIdentity -Path $written -RequireExisting
    $transactionJournalOwnerRun = $true
    $receipt.transaction_journal = [ordered]@{
        path = $written
        object_identity = $transactionJournalIdentity
        state = $State
        owner_run_id = $runId
    }
    return $written
}

function Remove-InstallTransactionJournal {
    if ([string]::IsNullOrWhiteSpace([string]$transactionJournalPath)) { return }
    if (-not $transactionJournalOwnerRun) { throw 'Refusing to remove a transaction journal without current-run ownership.' }
    if (Test-Path -LiteralPath $transactionJournalPath -PathType Leaf) {
        Assert-PathObjectIdentity -Path $transactionJournalPath -ExpectedIdentity $transactionJournalIdentity | Out-Null
        Remove-Item -LiteralPath $transactionJournalPath -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $transactionJournalPath) { throw 'Transaction journal remained after terminal cleanup.' }
    }
    $transactionJournalPath = $null
    $transactionJournalIdentity = $null
    $transactionJournalOwnerRun = $false
}

function Suspend-InstallerLifecycleLockForVerifier {
    param(
        [Parameter(Mandatory = $true)][string]$VerifierRoot
    )
    if ($null -eq $script:installRootLock) { throw 'Installer lifecycle lock is missing before verifier handoff.' }
    $canonicalRoot = Get-CanonicalPath -Path $VerifierRoot -RequireExisting
    $handoffKey = 'handoff:' + $canonicalRoot.ToLowerInvariant()
    $locks = @(Get-OptionalPropertyValue -Object $script:installRootLock -Name 'locks')
    $handoffLocks = @($locks | Where-Object { [string]$_.key -ceq $handoffKey })
    if ($handoffLocks.Count -ne 1) {
        throw "Verifier handoff lock is not held for the candidate root: $canonicalRoot"
    }
    $remainingLocks = @($locks | Where-Object { [string]$_.key -cne $handoffKey })
    if ($remainingLocks.Count -eq 0) { throw 'Verifier handoff would release every installer lifecycle lock.' }
    $script:verifierHandoffLock = $handoffLocks[0]
    $script:verifierHandoffRoot = $canonicalRoot
    $script:verifierHandoffRootIdentity = Get-PathObjectIdentity -Path $canonicalRoot -RequireExisting
    Exit-InstallLifecycleLock -Lock ([pscustomobject]@{ locks = $remainingLocks })
    $script:installRootLock = $null
    $script:verifierHandoffActive = $true
    return $true
}

function Resume-InstallerLifecycleLockAfterVerifier {
    param(
        [Parameter(Mandatory = $true)][string]$VerifierRoot,
        [Parameter(Mandatory = $true)][string[]]$TaskNames,
        [Parameter(Mandatory = $true)][int]$ApiPort
    )
    if (-not $script:verifierHandoffActive) { return $true }
    if ($null -eq $script:verifierHandoffLock) { throw 'Verifier handoff lock was lost before lifecycle reacquisition.' }
    $reacquired = Enter-InstallLifecycleLock -InstallRoot $VerifierRoot -TaskNames $TaskNames -ApiPort $ApiPort -Role 'installer' -SkipVerifierAdmissionHandoff -TimeoutSeconds 120
    $reacquired.locks = @($script:verifierHandoffLock) + @($reacquired.locks)
    $script:installRootLock = $reacquired
    $script:verifierHandoffActive = $false
    return $true
}

function Recover-StaleVerifierHandoff {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Record
    )
    $candidate = [string](Get-OptionalPropertyValue -Object $Journal -Name 'candidate_root')
    $candidateIdentity = [string](Get-OptionalPropertyValue -Object $Journal -Name 'install_root_identity')
    if ($candidate -cne [string]$install -or -not [bool](Get-OptionalPropertyValue -Object $Journal -Name 'install_root_created_by_run')) {
        throw 'Stale verifier handoff is not bound to a run-owned fresh install root.'
    }
    $backup = [string](Get-OptionalPropertyValue -Object $Journal -Name 'backup_root')
    if (-not [string]::IsNullOrWhiteSpace($backup)) {
        throw 'Stale verifier handoff with a PreserveMove backup requires explicit operator recovery.'
    }
    if (Test-Path -LiteralPath $install -PathType Container) {
        if ([string]::IsNullOrWhiteSpace($candidateIdentity)) { throw 'Stale verifier handoff install-root identity is missing.' }
        Assert-PathObjectIdentity -Path $install -ExpectedIdentity $candidateIdentity | Out-Null
        $taskNamesFromJournal = @($Journal.task_names | ForEach-Object { [string]$_ })
        if ($taskNamesFromJournal.Count -ne 2) { throw 'Stale verifier handoff task identity list is incomplete.' }
        $taskPathFromJournal = Assert-CanonicalScheduledTaskPath -TaskPath ([string]$Journal.task_path)
        $apiPortFromJournal = 0
        if (-not [int]::TryParse([string]$Journal.api_port, [ref]$apiPortFromJournal) -or $apiPortFromJournal -lt 1) { throw 'Stale verifier handoff API port is invalid.' }
        $principal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $python = Join-Path $install '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Stale verifier handoff venv executable is missing.' }
        foreach ($taskNameFromJournal in $taskNamesFromJournal) {
            $task = Get-ScheduledTaskExact -TaskName $taskNameFromJournal -TaskPath $taskPathFromJournal -AllowMissing
            if (-not $task) { continue }
            $expectedArguments = if ($taskNameFromJournal -ceq $taskNamesFromJournal[0]) { '-m app.api_server' } elseif ($taskNameFromJournal -ceq $taskNamesFromJournal[1]) { '-m app.worker' } else { throw "Stale verifier handoff task name is not one of the journaled roles: $taskNameFromJournal" }
            $handoffTaskIdentity = Get-ScheduledTaskIdentity -TaskName $taskNameFromJournal -TaskPath $taskPathFromJournal
            Assert-RunOwnedTaskIdentity -Identity $handoffTaskIdentity -ExpectedRoot $install -ExpectedPython $python -ExpectedArguments $expectedArguments -ExpectedPrincipal $principal -ExpectedApiPort $apiPortFromJournal | Out-Null
            Stop-ScheduledTaskExactAndWait -TaskName $taskNameFromJournal -TaskPath $taskPathFromJournal -ExpectedIdentity $handoffTaskIdentity | Out-Null
            Unregister-ScheduledTask -TaskName $taskNameFromJournal -TaskPath $taskPathFromJournal -Confirm:$false -ErrorAction Stop
            if (Get-ScheduledTaskExact -TaskName $taskNameFromJournal -TaskPath $taskPathFromJournal -AllowMissing) { throw "Stale verifier handoff task remained after cleanup: $taskNameFromJournal" }
        }
        $released = Stop-InstallProcesses -RootPath $install -PreserveProcessIds @()
        if (-not $released.ok -or @($released.remaining).Count -gt 0) { throw 'Stale verifier handoff processes were not fully released.' }
        Assert-PathObjectIdentity -Path $install -ExpectedIdentity $candidateIdentity | Out-Null
        Remove-Item -LiteralPath $install -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $install) { throw 'Stale verifier handoff install root remained after cleanup.' }
    }
    $snapshotPathFromJournal = [string](Get-OptionalPropertyValue -Object $Journal -Name 'snapshot_path')
    $snapshotShaFromJournal = [string](Get-OptionalPropertyValue -Object $Journal -Name 'snapshot_sha256')
    $snapshotIdentityFromJournal = [string](Get-OptionalPropertyValue -Object $Journal -Name 'snapshot_identity')
    $ownerRunFromJournal = [string](Get-OptionalPropertyValue -Object $Journal -Name 'owner_run_id')
    if ([string]::IsNullOrWhiteSpace($snapshotPathFromJournal) -or [string]::IsNullOrWhiteSpace($snapshotShaFromJournal) -or [string]::IsNullOrWhiteSpace($snapshotIdentityFromJournal) -or [string]::IsNullOrWhiteSpace($ownerRunFromJournal)) {
        throw 'Stale verifier handoff cannot recover without a sealed snapshot and owner identity.'
    }
    Restore-InstallSnapshot -SnapshotPath $snapshotPathFromJournal -ExpectedSnapshotSha256 $snapshotShaFromJournal -ExpectedSnapshotIdentity $snapshotIdentityFromJournal -ExpectedRunId $ownerRunFromJournal -RestoreTasks | Out-Null
    $recoveredPayload = [ordered]@{
        schema_version = 'hwpx/windows-install-transaction/v1'
        owner_run_id = $ownerRunFromJournal
        state = 'recovered'
        recovered_by_run_id = $runId
        recovered_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = $install
    }
    Write-StableTransactionJournal -Path $Record.path -Value $recoveredPayload | Out-Null
    Remove-Item -LiteralPath $Record.path -Force -ErrorAction Stop
    $receipt.recovery.outcome = 'recovered-stale-verifier-handoff'
}

function Invoke-StaleInstallTransactionRecovery {
    if ([string]::IsNullOrWhiteSpace([string]$install)) { return }
    if ([string]::IsNullOrWhiteSpace([string]$transactionJournalPath)) {
        $transactionJournalPath = Get-InstallTransactionJournalPath -InstallRoot $install
    }
    $record = Read-StableTransactionJournal -Path $transactionJournalPath
    if ($null -eq $record) { return }
    $journal = $record.value
    $owner = [string]$journal.owner_run_id
    $state = [string]$journal.state
    $receipt.recovery = [ordered]@{
        attempted = $true
        journal_path = [string]$record.path
        previous_run_id = $owner
        previous_state = $state
        outcome = 'pending'
    }
    if ([string]::IsNullOrWhiteSpace($owner) -or $owner -eq $runId) {
        throw 'Transaction journal owner identity is missing or collides with the current run.'
    }
    if ($state -in @('completed', 'terminal-committing', 'recovered')) {
        Assert-PathObjectIdentity -Path $record.path -ExpectedIdentity $record.object_identity | Out-Null
        Remove-Item -LiteralPath $record.path -Force -ErrorAction Stop
        $receipt.recovery.outcome = 'discarded-terminal-journal'
        return
    }
    if ($state -eq 'verifier-handoff-started') {
        $ownerProcessId = 0
        [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $journal -Name 'owner_process_id'), [ref]$ownerProcessId)
        $ownerStartIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'owner_process_start_identity')
        $ownerProcessActive = $false
        if ($ownerProcessId -gt 0 -and -not [string]::IsNullOrWhiteSpace($ownerStartIdentity)) {
            try { $ownerProcessActive = (Get-ProcessGenerationIdentity -ProcessId $ownerProcessId) -ceq $ownerStartIdentity } catch { $ownerProcessActive = $false }
        }
        if ($ownerProcessActive) { throw 'An installer is actively handing off the lifecycle lock to its verifier.' }
        Recover-StaleVerifierHandoff -Journal $journal -Record $record
        return
    }
    if ($state -in @('preflight-admitted', 'install-started', 'backup-claim-planned', 'backup-claim-created') -and
        [string]::IsNullOrWhiteSpace([string]$journal.snapshot_path) -and
        [string]::IsNullOrWhiteSpace([string]$journal.candidate_root)) {
        # No destructive transition was admitted; the previous process only
        # left an informational preflight journal behind.
        $staleClaim = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_claim_path')
        if (-not [string]::IsNullOrWhiteSpace($staleClaim) -and (Test-Path -LiteralPath $staleClaim -PathType Leaf)) {
            $staleClaimIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_claim_identity')
            if ([string]::IsNullOrWhiteSpace($staleClaimIdentity)) { throw 'Stale transaction claim has no sealed object identity.' }
            Remove-RunPathClaim -ClaimPath $staleClaim -ExpectedObjectIdentity $staleClaimIdentity
        }
        Assert-PathObjectIdentity -Path $record.path -ExpectedIdentity $record.object_identity | Out-Null
        Remove-Item -LiteralPath $record.path -Force -ErrorAction Stop
        $receipt.recovery.outcome = 'discarded-non-destructive-stale-run'
        return
    }
    $journalSnapshot = [string](Get-OptionalPropertyValue -Object $journal -Name 'snapshot_path')
    $journalSnapshotSha = [string](Get-OptionalPropertyValue -Object $journal -Name 'snapshot_sha256')
    $journalSnapshotIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'snapshot_identity')
    if ([string]::IsNullOrWhiteSpace($journalSnapshot) -or [string]::IsNullOrWhiteSpace($journalSnapshotSha) -or [string]::IsNullOrWhiteSpace($journalSnapshotIdentity)) {
        throw "Stale install transaction cannot be adjudicated without a sealed snapshot: $state"
    }
    $backup = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_root')
    $backupIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_root_identity')
    $backupExists = -not [string]::IsNullOrWhiteSpace($backup) -and (Test-Path -LiteralPath $backup -PathType Container)
    $installExists = Test-Path -LiteralPath $install -PathType Container
    $candidate = [string](Get-OptionalPropertyValue -Object $journal -Name 'candidate_root')
    $candidateIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'candidate_root_identity')
    $candidateExists = -not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)
    $installIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'install_root_identity')
    $installCreatedByRun = [bool](Get-OptionalPropertyValue -Object $journal -Name 'install_root_created_by_run')
    if ($state -eq 'activation-root-creating' -and $installExists) {
        # The root was observed during the non-atomic directory creation seam
        # before its stable identity was journaled. Never guess ownership after
        # a crash; leave the predecessor journal for explicit adjudication.
        throw 'Stale activation root was created before its stable identity was sealed.'
    }
    if ($installExists -and $installCreatedByRun -and $state -in @('activation-root-created', 'activation-copy-started')) {
        if ([string]::IsNullOrWhiteSpace($installIdentity)) { throw 'Stale activation root has no sealed object identity.' }
        Assert-PathObjectIdentity -Path $install -ExpectedIdentity $installIdentity | Out-Null
        Stop-InstallProcesses -RootPath $install -PreserveProcessIds @() | Out-Null
        Assert-PathObjectIdentity -Path $install -ExpectedIdentity $installIdentity | Out-Null
        Remove-Item -LiteralPath $install -Recurse -Force -ErrorAction Stop
        $installExists = $false
    }
    if ($backupExists) {
        if ([string]::IsNullOrWhiteSpace($backupIdentity)) { throw 'Stale backup has no sealed object identity.' }
        Assert-PathObjectIdentity -Path $backup -ExpectedIdentity $backupIdentity | Out-Null
    }
    if ($candidateExists -and -not [string]::IsNullOrWhiteSpace($candidateIdentity)) {
        Assert-PathObjectIdentity -Path $candidate -ExpectedIdentity $candidateIdentity | Out-Null
    }
    $recoveryQuarantine = "$install.recovery-$owner"
    if (-not $installExists -and $backupExists -and $candidate -eq $install -and (Test-Path -LiteralPath $recoveryQuarantine -PathType Container)) {
        if ([string]::IsNullOrWhiteSpace($candidateIdentity)) { throw 'Stale recovery quarantine has no sealed candidate identity.' }
        Assert-PathObjectIdentity -Path $recoveryQuarantine -ExpectedIdentity $candidateIdentity | Out-Null
        $candidate = $recoveryQuarantine
        $candidateExists = $true
        $candidateIdentity = Get-PathObjectIdentity -Path $recoveryQuarantine -RequireExisting
    }
    if (-not $installExists -and $backupExists) {
        [System.IO.Directory]::Move($backup, $install)
        $installExists = $true
    }
    elseif ($installExists -and $backupExists -and ($candidateExists -or $state -eq 'candidate-temp-removing') -and (($candidate -eq $install) -or $state -eq 'candidate-temp-removing')) {
        if ($state -eq 'candidate-temp-removing' -and $installCreatedByRun -and -not [string]::IsNullOrWhiteSpace($installIdentity)) {
            Assert-PathObjectIdentity -Path $install -ExpectedIdentity $installIdentity | Out-Null
        }
        Stop-InstallProcesses -RootPath $install -PreserveProcessIds @() | Out-Null
        $activeCandidateIdentity = if ($state -eq 'candidate-temp-removing') { $installIdentity } else { $candidateIdentity }
        Assert-PathObjectIdentity -Path $install -ExpectedIdentity $activeCandidateIdentity | Out-Null
        $quarantine = $recoveryQuarantine
        [System.IO.Directory]::Move($install, $quarantine)
        [System.IO.Directory]::Move($backup, $install)
        $candidate = $quarantine
        $candidateExists = $true
        $candidateIdentity = Get-PathObjectIdentity -Path $candidate -RequireExisting
    }
    $restoreCandidate = if ($candidateExists -and $candidate -ne $install) { $candidate } else { $null }
    $restoreCandidateIdentity = if ($restoreCandidate) { $candidateIdentity } else { $null }
    Restore-InstallSnapshot -SnapshotPath $journalSnapshot -ExpectedSnapshotSha256 $journalSnapshotSha -ExpectedSnapshotIdentity $journalSnapshotIdentity -ExpectedRunId $owner -CandidateRoot $restoreCandidate -ExpectedCandidateRootIdentity $restoreCandidateIdentity -CandidateRootOwnedByRun:$([bool]$restoreCandidate) -RestoreTasks | Out-Null
    if ($restoreCandidate -and (Test-Path -LiteralPath $restoreCandidate -PathType Container)) {
        Assert-PathObjectIdentity -Path $restoreCandidate -ExpectedIdentity $restoreCandidateIdentity | Out-Null
        Remove-Item -LiteralPath $restoreCandidate -Recurse -Force -ErrorAction Stop
    }
    $staleClaim = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_claim_path')
    if (-not [string]::IsNullOrWhiteSpace($staleClaim) -and (Test-Path -LiteralPath $staleClaim -PathType Leaf)) {
        $staleClaimIdentity = [string](Get-OptionalPropertyValue -Object $journal -Name 'backup_claim_identity')
        if ([string]::IsNullOrWhiteSpace($staleClaimIdentity)) { throw 'Stale transaction claim has no sealed object identity.' }
        Remove-RunPathClaim -ClaimPath $staleClaim -ExpectedObjectIdentity $staleClaimIdentity
    }
    $recoveredPayload = [ordered]@{ schema_version = 'hwpx/windows-install-transaction/v1'; owner_run_id = $owner; state = 'recovered'; recovered_by_run_id = $runId; recovered_at_utc = [DateTime]::UtcNow.ToString('o'); install_root = $install }
    Write-StableTransactionJournal -Path $record.path -Value $recoveredPayload | Out-Null
    Remove-Item -LiteralPath $record.path -Force -ErrorAction Stop
    $receipt.recovery.outcome = 'recovered-and-cleaned'
}

function Save-InstallerReceipt {
    if ($null -ne $receiptFile) {
        try {
            Write-JsonReceipt -Path $receiptFile -Value $receipt | Out-Null
            return $true
        }
        catch {
            # A receipt write must never leave the live state reporting success.
            # Preserve a bounded fail-closed terminal record and keep the same
            # state in memory so the terminal summary and exit code agree.
            $primaryError = [string]$_.Exception.Message
            $script:receiptPersistenceFailed = $true
            $receipt.status = 'FAIL_RECEIPT'
            $receipt.failure_class = 'FAIL_RECEIPT'
            $receipt.status_code = 99
            $receipt.receipt_persistence_failed = $true
            $receipt.receipt_error = Limit-Text -Value $primaryError -MaxChars 4096
            $fallback = [ordered]@{
                schema_version = 'hwpx/windows-install/v1'
                status = 'FAIL_RECEIPT'
                failure_class = 'FAIL_RECEIPT'
                status_code = 99
                phase = [string]$phase
                source_root = [string]$source
                install_root = [string]$install
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
            throw "Receipt persistence failed: $primaryError"
        }
    }
}

function Complete-InstallerTerminalReceipt {
    param([bool]$RemoveSnapshot)
    if ($snapshotPath) {
        $receipt.snapshot_cleanup = [ordered]@{
            path = $snapshotPath
            terminal_readback = $true
            removed = $false
        }
    }
    try {
        # First commit proves that the terminal receipt is durable. Current-run
        # temporary preimages are not eligible for cleanup before this readback.
        Write-InstallTransactionJournal -State 'terminal-committing' | Out-Null
        Save-InstallerReceipt | Out-Null
    }
    catch {
        return $false
    }
    $cleanupErrors = @()
    if ($RemoveSnapshot -and $receipt.snapshot_path -and (Test-Path -LiteralPath $receipt.snapshot_path -PathType Leaf)) {
        try {
            if (-not $snapshotOwnedByRun) { throw 'Refusing to remove a snapshot without current-run ownership.' }
            if ([string]::IsNullOrWhiteSpace($snapshotIdentity)) { throw 'Rollback snapshot stable object identity is missing before terminal cleanup.' }
            Assert-NoReparsePath -Path $receipt.snapshot_path | Out-Null
            Assert-PathObjectIdentity -Path $receipt.snapshot_path -ExpectedIdentity $snapshotIdentity | Out-Null
            if ([string]::IsNullOrWhiteSpace($snapshotSha256) -or (Get-Sha256Hex -Path $receipt.snapshot_path) -cne $snapshotSha256) {
                throw 'Rollback snapshot identity changed before terminal cleanup.'
            }
            Assert-PathObjectIdentity -Path $receipt.snapshot_path -ExpectedIdentity $snapshotIdentity | Out-Null
            Remove-Item -LiteralPath $receipt.snapshot_path -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $receipt.snapshot_path -PathType Leaf) {
                throw 'Rollback snapshot remained after terminal receipt readback cleanup.'
            }
            $receipt.snapshot_cleanup.removed = $true
        }
        catch {
            $cleanupErrors += Limit-Text -Value $_.Exception.Message -MaxChars 4096
            $receipt.snapshot_cleanup.error = $cleanupErrors[-1]
        }
    }
    if ($receiptPreimageBackupPath) {
        try {
            Remove-ReceiptPreimageBackup | Out-Null
            $receipt.receipt_preimage.backup_removed = $true
        }
        catch {
            $cleanupErrors += Limit-Text -Value $_.Exception.Message -MaxChars 4096
            $receipt.receipt_preimage.cleanup_error = $cleanupErrors[-1]
        }
    }
    if ($cleanupErrors.Count -gt 0) {
        $receipt.status = 'FAIL_RECEIPT'
        $receipt.failure_class = 'FAIL_RECEIPT'
        $receipt.status_code = 99
        $receipt.receipt_cleanup_failed = $true
        $receipt.errors = @($receipt.errors) + $cleanupErrors
        try {
            Save-InstallerReceipt | Out-Null
        }
        catch {
            return $false
        }
        return $false
    }
    try {
        # Persist the cleanup readback, including the explicit removed flags.
        Save-InstallerReceipt | Out-Null
        if ($RemoveSnapshot) {
            Remove-InstallTransactionJournal
        }
    }
    catch {
        return $false
    }
    return $true
}

function Write-InstallerTerminalSummary {
    param([string]$ErrorMessage)
    $summary = "status=$($receipt.status); status_code=$($receipt.status_code); receipt=$receiptFile; api_port=$apiPort"
    if (-not [string]::IsNullOrWhiteSpace($ErrorMessage)) {
        $summary += "; error=" + (Limit-Text -Value $ErrorMessage -MaxChars 4096)
        Write-Output $summary
    }
    else {
        Write-Output $summary
    }
}

function Add-InstallerError {
    param([Parameter(Mandatory = $true)][string]$Message)
    $receipt.errors = @($receipt.errors) + $Message
    Save-InstallerReceipt
}

function Invoke-InstallerNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory
    )
    # Invoke-NativeChecked captures the native $LASTEXITCODE directly before any transcript is written.
    $result = Invoke-NativeChecked -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory -AllowNonZero
    $receipt.native_commands = @($receipt.native_commands) + $result
    if (-not $result.accepted) {
        throw "Native command failed with exit code $($result.exit_code): $FilePath $($Arguments -join ' ')"
    }
    return $result
}

function Invoke-InstallerDependencyCheck {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    $checkerPath = Join-Path $WorkingDirectory 'scripts\check_windows_dependencies.py'
    if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
        throw "Hash-pinned Windows dependency lock is missing: $LockPath"
    }
    if (-not (Test-Path -LiteralPath $checkerPath -PathType Leaf)) {
        throw "Windows dependency checker is missing: $checkerPath"
    }
    $result = Invoke-NativeChecked -FilePath $PythonPath -Arguments @($checkerPath, '--lock', $LockPath, '--json') -WorkingDirectory $WorkingDirectory -AllowNonZero
    $receipt.native_commands = @($receipt.native_commands) + $result
    $report = $null
    try {
        $report = ConvertFrom-Json -InputObject ([string]$result.stdout)
    }
    catch {
        throw "Windows dependency checker returned invalid JSON: $($_.Exception.Message)"
    }
    $receipt.checks.dependency_completeness = [pscustomobject]@{
        ok = ($result.exit_code -eq 0 -and [bool]$report.ok)
        exit_code = [int]$result.exit_code
        report = $report
    }
    if ($result.exit_code -ne 0 -or -not [bool]$report.ok) {
        throw 'Final Windows virtual environment failed locked dependency/import completeness verification.'
    }
    return $report
}

function Seal-InstallerSnapshot {
    param(
        [hashtable]$TaskStateOverride = @{},
        [hashtable]$TaskIdentityOverride = @{}
    )
    if ($script:snapshotOwnedByRun) { return }
    if (-not $script:snapshotPath) {
        $script:snapshotPath = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-snapshot-' + $runId + '.json')
    }
    Save-InstallSnapshot -SnapshotPath $script:snapshotPath -InstallRoot $install -TaskName $taskNames -TaskPath $taskPath -OwnerRunId $runId -TaskStateOverride $TaskStateOverride -TaskIdentityOverride $TaskIdentityOverride | Out-Null
    $sealedSnapshotSha256 = Get-Sha256Hex -Path $script:snapshotPath
    $sealedSnapshotIdentity = Get-PathObjectIdentity -Path $script:snapshotPath -RequireExisting
    $script:snapshotSha256 = $sealedSnapshotSha256
    $script:snapshotIdentity = $sealedSnapshotIdentity
    $script:snapshotOwnedByRun = $true
    $receipt.snapshot_path = $script:snapshotPath
    $receipt.snapshot_identity = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        path = $script:snapshotPath
        sha256 = $script:snapshotSha256
        object_identity = $script:snapshotIdentity
        owner_run_id = $runId
    }
    $receipt.snapshot_cleanup = [ordered]@{ path = $script:snapshotPath; terminal_readback = $false; removed = $false }
}

function Restore-PreMoveTaskAdmission {
    param(
        [Parameter(Mandatory = $true)][hashtable]$DisabledTaskIdentity,
        [Parameter(Mandatory = $true)][hashtable]$OriginalTaskIdentity,
        [string]$TaskPath = '\'
    )
    foreach ($taskName in @($OriginalTaskIdentity.Keys)) {
        $expectedDisabled = $DisabledTaskIdentity[$taskName]
        $original = $OriginalTaskIdentity[$taskName]
        if ($null -eq $original) { continue }
        if ([string]$original.state -eq 'Queued') {
            throw "Queued scheduled task state cannot be restored exactly during admission rollback: $taskName"
        }
        $current = Get-ScheduledTaskIdentity -TaskName ([string]$taskName) -TaskPath $TaskPath
        if (-not [bool]$current.exists) {
            if ([string]::IsNullOrWhiteSpace([string]$original.xml)) {
                throw "Pre-snapshot scheduled task XML is missing during admission restore: $taskName"
            }
            Register-ScheduledTaskExactNoClobber -TaskName ([string]$taskName) -TaskPath $TaskPath -Xml ([string]$original.xml) | Out-Null
            $current = Get-ScheduledTaskIdentity -TaskName ([string]$taskName) -TaskPath $TaskPath
        }
        elseif ($null -ne $expectedDisabled) {
            if (-not (Test-ScheduledTaskIdentityExact -Actual $current -Expected $expectedDisabled)) {
                throw "Pre-snapshot scheduled task identity changed before admission restore: $taskName"
            }
        }
        else {
            # Disable/stop may have failed before the post-disable identity was
            # captured.  Prove ownership using the immutable action/principal
            # coordinates before restoring its exact original XML/state.
            if (-not (Test-ScheduledTaskIdentityExact -Actual $current -Expected $original)) {
                throw "Pre-snapshot scheduled task ownership changed before admission restore: $taskName"
            }
        }
        $current = Stop-ScheduledTaskExactAndWait -TaskName ([string]$taskName) -TaskPath $TaskPath -ExpectedIdentity $current
        # Re-register the sealed XML instead of approximating Enabled through
        # Enable/Disable cmdlets; this restores every omitted/default field too.
        $restoredBeforeState = Register-ScheduledTaskExactNoClobber -TaskName ([string]$taskName) -TaskPath $TaskPath -Xml ([string]$original.xml) -ExpectedCurrentIdentity $current
        $currentTask = Get-ScheduledTaskExact -TaskName ([string]$taskName) -TaskPath $TaskPath
        if ([string]$original.state -eq 'Running') {
            Start-ScheduledTask -TaskName ([string]$taskName) -TaskPath $TaskPath -ErrorAction Stop
            if (-not (Wait-ScheduledTaskRunning -TaskName ([string]$taskName) -TaskPath $TaskPath)) { throw "Pre-snapshot scheduled task did not return to Running: $taskName" }
        }
        elseif (-not (Wait-ScheduledTaskInactive -TaskName ([string]$taskName) -TaskPath $TaskPath)) {
            throw "Pre-snapshot scheduled task remained active during admission restore: $taskName"
        }
        $restored = Get-ScheduledTaskIdentity -TaskName ([string]$taskName) -TaskPath $TaskPath
        if ([string]$restored.xml -cne [string]$original.xml -or [bool]$restored.enabled -ne [bool]$original.enabled) {
            throw "Pre-snapshot scheduled task XML or enabled state was not restored exactly: $taskName"
        }
        $restoredRunning = [string]$restored.state -eq 'Running'
        $originalRunning = [string]$original.state -eq 'Running'
        if ($restoredRunning -ne $originalRunning) {
            throw "Pre-snapshot scheduled task running state was not restored exactly: $taskName"
        }
    }
}

function New-PythonInvocation {
    param([Parameter(Mandatory = $true)][string]$Path)
    $leaf = [IO.Path]::GetFileName($Path)
    if ($leaf -in @('py.exe', 'py')) {
        # The -3 selector belongs to the Python launcher only.  A direct
        # python.exe treats it as an invalid application flag.
        return [pscustomobject]@{ path = $Path; prefix = @('-3.13') }
    }
    return [pscustomobject]@{ path = $Path; prefix = @() }
}

function Get-InstallerConfiguredValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $environmentValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($environmentValue)) { return $environmentValue }
    if ($install) {
        $configured = Get-ConfiguredEnvValue -EnvPath (Join-Path $install '.env') -Name $Name
        if (-not [string]::IsNullOrWhiteSpace([string]$configured)) { return $configured }
    }
    return $null
}

function Resolve-PythonForInstaller {
    $configured = Get-InstallerConfiguredValue -Name 'HWP_PYTHON'
    if ($configured) {
        $candidate = Get-CanonicalPath -Path $configured
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return New-PythonInvocation -Path $candidate }
        throw "Configured HWP_PYTHON does not exist: $candidate"
    }
    foreach ($name in @('py.exe', 'py', 'python.exe', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            return New-PythonInvocation -Path $command.Source
        }
    }
    throw 'Python executable not found. Install CPython 3.13 x64 for the interactive user or set HWP_PYTHON.'
}

function Get-PythonRuntimeIdentity {
    param(
        [Parameter(Mandatory = $true)][object]$Invocation,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$Role = 'python'
    )
    $probeCode = 'import json,platform,struct,sys; print(json.dumps({"implementation":str(getattr(sys.implementation,"name","")),"major":int(sys.version_info[0]),"minor":int(sys.version_info[1]),"micro":int(sys.version_info[2]),"pointer_bits":int(struct.calcsize("P")*8),"machine":str(platform.machine())}))'
    $result = Invoke-InstallerNative -FilePath ([string]$Invocation.path) -Arguments (@($Invocation.prefix) + @('-c', $probeCode)) -WorkingDirectory $WorkingDirectory
    if ($result.stdout_truncated -or $result.stderr_truncated) { throw "Python $Role identity probe exceeded the bounded output limit." }
    try {
        $identity = ConvertFrom-Json -InputObject ([string]$result.stdout)
    }
    catch {
        throw "Python $Role identity probe returned invalid JSON."
    }
    $machine = [string]$identity.machine
    if ([int]$identity.major -ne 3 -or [int]$identity.minor -ne 13 -or
        [int]$identity.pointer_bits -ne 64 -or [string]$identity.implementation -ne 'cpython' -or
        $machine.ToLowerInvariant() -notin @('amd64', 'x86_64', 'intel64')) {
        throw "Python $Role ABI is unsupported; required CPython 3.13 x86-64, got $([string]$identity.implementation) $([int]$identity.major).$([int]$identity.minor) $([int]$identity.pointer_bits)-bit $machine."
    }
    return [pscustomobject]@{
        ok = $true
        role = $Role
        executable = [string]$Invocation.path
        prefix = @($Invocation.prefix)
        implementation = [string]$identity.implementation
        major = [int]$identity.major
        minor = [int]$identity.minor
        micro = [int]$identity.micro
        pointer_bits = [int]$identity.pointer_bits
        machine = $machine
        probe = $result
    }
}

function Resolve-PopplerForInstaller {
    $configured = Get-InstallerConfiguredValue -Name 'HWP_PDFTOPPM'
    if ([string]::IsNullOrWhiteSpace([string]$configured)) { $configured = Get-InstallerConfiguredValue -Name 'HWP_PDFTOPPM_PATH' }
    $explicit = if ($PopplerPath) { $PopplerPath } elseif ($configured) { $configured } else { $null }
    if ($explicit) {
        try {
            $explicitItem = Get-Item -LiteralPath $explicit -ErrorAction Stop
            if (($explicitItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
                $canonical = Get-CanonicalPath -Path $explicit -RequireExisting
                $explicitExtension = [IO.Path]::GetExtension($canonical).ToLowerInvariant()
                if ((Test-Path -LiteralPath $canonical -PathType Leaf) -and $explicitExtension -eq '.exe') {
                    return [pscustomobject]@{ ok = $true; path = $canonical; source = 'explicit'; candidates = @($explicit) }
                }
            }
        }
        catch { }
        # An explicit path is authoritative. Never fall back when it is invalid.
        return [pscustomobject]@{ ok = $false; path = $null; source = 'explicit-invalid'; candidates = @($explicit) }
    }
    $candidates = @()
    foreach ($name in @('pdftoppm.exe', 'pdftoppm')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source) { $candidates += $command.Source }
    }
    $roots = @()
    if ($env:HWP_WINGET_ROOTS) { $roots += ($env:HWP_WINGET_ROOTS -split [IO.Path]::PathSeparator) }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'WindowsApps') }
    foreach ($root in ($roots | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            $found = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @('pdftoppm.exe', 'pdftoppm') } |
                Sort-Object FullName |
                Select-Object -First 1
            if ($found) { $candidates += $found.FullName }
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            $canonical = Get-CanonicalPath -Path ([string]$candidate) -RequireExisting
            if ((Test-Path -LiteralPath $canonical -PathType Leaf) -and ((Get-Item -LiteralPath $canonical).Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
                return [pscustomobject]@{ ok = $true; path = $canonical; source = 'path-or-winget'; candidates = @($candidates) }
            }
        }
        catch { }
    }
    return [pscustomobject]@{ ok = $false; path = $null; source = 'unresolved'; candidates = @($candidates) }
}

function New-GitSourceManifest {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { throw 'Source manifest is missing and git.exe is unavailable; install from a verified source bundle.' }
    $commit = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'rev-parse', 'HEAD') -WorkingDirectory $source
    $tree = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'rev-parse', 'HEAD^{tree}') -WorkingDirectory $source
    $commitValue = ([string]$commit.stdout).Trim()
    $treeValue = ([string]$tree.stdout).Trim()
    if ($commitValue -notmatch '^[0-9a-fA-F]{40}$' -or $treeValue -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Git source identity did not return valid commit/tree object IDs.'
    }
    $status = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'status', '--porcelain=v1', '--untracked-files=all') -WorkingDirectory $source
    if (-not [string]::IsNullOrWhiteSpace([string]$status.stdout)) {
        throw 'Refusing to derive a clean source manifest from a dirty Git worktree.'
    }
    $tracked = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'ls-files', '-z') -WorkingDirectory $source
    if ($tracked.stdout_truncated) { throw 'Tracked source file listing exceeded the bounded native output limit.' }
    $files = @()
    $runtimeDirectoryNames = @(
        '.git', '.venv', '.venv313', '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox',
        'spool', 'receipts', 'fixtures', 'uploads', 'output', 'logs', 'cache', 'backups', 'proofs', 'evidence',
        'runtime', 'queue', 'documents', 'customer', 'projects', 'sessions', 'ocr', 'renders', 'env',
        'source-bundle', 'artifacts', 'archives', 'staging', 'temp', 'tmp', 'build', 'dist'
    ) | ForEach-Object { $_.ToLowerInvariant() }
    foreach ($raw in ([string]$tracked.stdout -split [char]0)) {
        if ([string]::IsNullOrWhiteSpace($raw)) { continue }
        $relative = Assert-WindowsSafeSourceRelativePath -RelativePath $raw
        $parts = @($relative.Split([char]92))
        if (Test-ProhibitedPrivateSourceMember -RelativePath $relative) {
            throw "Prohibited private source member is tracked and cannot be packaged: $relative"
        }
        if (Test-ProhibitedSourceMember -RelativePath $relative) {
            # The canonical source-bundle policy excludes runtime, document,
            # archive, binary, and root-generated manifest members.
            continue
        }
        $ignoredRuntimePath = $false
        foreach ($part in $parts) {
            $partLower = $part.ToLowerInvariant()
            if ($runtimeDirectoryNames -contains $partLower -or $partLower.StartsWith('.hwpx-install')) {
                $ignoredRuntimePath = $true
                break
            }
        }
        if ($ignoredRuntimePath -or $parts[-1].ToLowerInvariant().StartsWith('.env') -or $relative.ToLowerInvariant().EndsWith('.pyc') -or $relative.ToLowerInvariant().EndsWith('.pyo') -or $relative.ToLowerInvariant().EndsWith('.db') -or $relative.ToLowerInvariant().EndsWith('.sqlite') -or $relative.ToLowerInvariant().EndsWith('.sqlite3') -or $relative.ToLowerInvariant().EndsWith('.log') -or $relative.ToLowerInvariant().EndsWith('.zip') -or $relative.ToLowerInvariant().EndsWith('.tar') -or $relative.ToLowerInvariant().EndsWith('.gz') -or $relative.ToLowerInvariant().EndsWith('.bz2') -or $relative.ToLowerInvariant().EndsWith('.xz') -or $relative.ToLowerInvariant().EndsWith('.7z')) { continue }
        if ([IO.Path]::IsPathRooted($relative) -or ($parts -contains '..')) { throw "Unsafe tracked source path: $raw" }
        $candidate = Assert-NoReparseSourcePath -Root $source -RelativePath $relative
        $item = Get-Item -LiteralPath $candidate -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse-point tracked source member is not allowed: $raw" }
        $gitRelative = $relative.Replace('\', '/')
        $expectedBlob = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'rev-parse', '--verify', ("{0}:{1}" -f $commitValue, $gitRelative)) -WorkingDirectory $source
        $actualBlob = Invoke-InstallerNative -FilePath $git.Source -Arguments @('-C', $source, 'hash-object', '--no-filters', '--', $candidate) -WorkingDirectory $source
        if (([string]$expectedBlob.stdout).Trim() -ne ([string]$actualBlob.stdout).Trim()) {
            throw "Tracked source bytes do not match Git HEAD: $relative"
        }
        $files += [ordered]@{ path = $relative.Replace('\', '/'); size = [int64]$item.Length; sha256 = Get-Sha256Hex -Path $candidate }
    }
    $files = @($files | Sort-Object path)
    $manifestPath = Join-Path ([IO.Path]::GetTempPath()) ('hwpx-source-manifest-' + [Guid]::NewGuid().ToString('N') + '.json')
    $manifest = [ordered]@{
        schema_version = 'hwpx/source-bundle/v1'
        repository = 'github:Junsung-Lee-coder/hwpx-editor-server'
        commit = $commitValue
        tree = $treeValue
        identity_source = 'git'
        identity_verified = $true
        file_count = $files.Count
        files = $files
    }
    Write-JsonReceipt -Path $manifestPath -Value $manifest | Out-Null
    return $manifestPath
}

function Find-InstallerManifest {
    $configured = [Environment]::GetEnvironmentVariable('HWP_SOURCE_MANIFEST')
    if ([string]::IsNullOrWhiteSpace($configured)) {
        $configured = Get-ConfiguredEnvValue -EnvPath (Join-Path $source '.env') -Name 'HWP_SOURCE_MANIFEST'
    }
    if ($configured) {
        $configuredPath = if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $source $configured }
        return Get-CanonicalPath -Path $configuredPath -RequireExisting
    }
    foreach ($candidate in @(
        (Join-Path $source 'source-manifest.json'),
        (Join-Path $source 'manifest.json'),
        (Join-Path $source 'source_bundle_manifest.json')
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return Get-CanonicalPath -Path $candidate -RequireExisting }
    }
    return New-GitSourceManifest
}

function Get-InteractiveUserIdentity {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($null -eq $identity -or [string]::IsNullOrWhiteSpace($identity.Name)) { throw 'Interactive Windows user identity is unavailable.' }
    $session = (Get-Process -Id $PID -ErrorAction Stop).SessionId
    if ([int]$session -eq 0) { throw 'Installer must run in an interactive user session, not session 0.' }
    return [pscustomobject]@{ name = $identity.Name; session_id = [int]$session }
}

function Get-HancomComIdentity {
    $progId = if ($env:HWP_HANCOM_PROGID) { $env:HWP_HANCOM_PROGID } else { 'HWPFrame.HwpObject' }
    try {
        $comType = [type]::GetTypeFromProgID($progId, $true)
        if ($null -eq $comType) { throw "COM ProgID is not registered: $progId" }
        return [pscustomobject]@{ ok = $true; prog_id = $progId; clsid = [string]$comType.GUID }
    }
    catch {
        throw "Hancom COM ProgID preflight failed for ${progId}: $($_.Exception.Message)"
    }
}

function Assert-PortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [string]$ExpectedRoot,
        [string]$ExpectedTaskName,
        [string]$ExpectedTaskPath = '\'
    )
    if ($Port -lt 1 -or $Port -gt 65535) { throw "ApiPort must be between 1 and 65535: $Port" }
    $networkCommand = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if (-not $networkCommand) { throw 'Get-NetTCPConnection is unavailable; refusing to assume the loopback port is available.' }
    $lookupErrors = @()
    $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue -ErrorVariable +lookupErrors)
    $unexpectedLookupErrors = @($lookupErrors | Where-Object { [string]$_.CategoryInfo.Category -ne 'ObjectNotFound' })
    if ($unexpectedLookupErrors.Count -gt 0) {
        throw "Unable to inspect loopback port $Port; refusing to assume it is available: $($unexpectedLookupErrors[0].Exception.Message)"
    }
    $listenerSummaries = @($listeners | ForEach-Object {
        [ordered]@{
            local_address = [string]$_.LocalAddress
            local_port = [int]$_.LocalPort
            owning_process = [int]$_.OwningProcess
            state = [string]$_.State
        }
    })
    if ($listeners.Count -gt 0) {
        $owners = @($listeners | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
        $compatible = $true
        $processes = @()
        foreach ($owner in $owners) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $owner" -ErrorAction SilentlyContinue
            $commandLine = if ($process) { Limit-Text -Value $process.CommandLine -MaxChars 4096 } else { '' }
            $listener = $listeners | Where-Object { [int]$_.OwningProcess -eq [int]$owner } | Select-Object -First 1
            $taskIdentity = if (-not [string]::IsNullOrWhiteSpace($ExpectedTaskName)) { Get-ScheduledTaskIdentity -TaskName $ExpectedTaskName -TaskPath $ExpectedTaskPath } else { $null }
            $candidateIdentity = if ($process) {
                Test-CanonicalProcessIdentity -Process $process -RootPath $ExpectedRoot -ExpectedPythonPath (Join-Path $ExpectedRoot '.venv\Scripts\python.exe') -ExpectedArguments '-m app.api_server' -ExpectedTaskIdentity $taskIdentity -ExpectedListener $listener -ExpectedListenerPort $Port -ExpectedApiPort $Port -ModuleNames @('app.api_server')
            }
            else { $false }
            $processes += [ordered]@{
                process_id = [int]$owner
                name = if ($process) { [string]$process.Name } else { $null }
                executable_path = if ($process) { [string]$process.ExecutablePath } else { $null }
                command_line = $commandLine
                candidate_identity = [bool]$candidateIdentity
            }
            if (-not $candidateIdentity) {
                $compatible = $false
            }
        }
        if (-not $compatible) {
            throw "Port $Port is already owned by incompatible listener process(es): $($owners -join ', ')"
        }
        return [pscustomobject]@{
            available = $false
            compatible_existing_install = $true
            port = $Port
            listeners = @($listenerSummaries)
            owners = @($owners)
            processes = @($processes)
        }
    }
    return [pscustomobject]@{
        available = $true
        compatible_existing_install = $false
        port = $Port
        listeners = @()
        owners = @()
        processes = @()
    }
}

function Assert-TaskCompatibility {
    param(
        [string]$TaskName,
        [string]$ExpectedRoot,
        [string]$ExpectedExecutable,
        [string]$ExpectedArguments,
        [string]$ExpectedPrincipal,
        [Nullable[int]]$ExpectedApiPort,
        [string]$TaskPath = '\',
        [switch]$AllowReplacement
    )
    $identity = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    $envPath = Join-Path $ExpectedRoot '.env'
    if ($null -ne $ExpectedApiPort -and (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        $configuredPort = Get-ConfiguredApiPort -EnvPath $envPath
        if ($configuredPort -ne [int]$ExpectedApiPort) {
            throw "Scheduled task $TaskName uses HWP_API_PORT $configuredPort, expected $ExpectedApiPort."
        }
    }
    $receipt.task_identities_before = @($receipt.task_identities_before) + $identity
    if (-not $identity.exists) { return $identity }
    $working = Get-CanonicalPath -Path ([string]$identity.working_directory)
    $expectedWorking = Get-CanonicalPath -Path $ExpectedRoot
    $executableCompatible = $true
    if (-not [string]::IsNullOrWhiteSpace($ExpectedExecutable) -and -not [string]::IsNullOrWhiteSpace([string]$identity.execute)) {
        $executableCompatible = ([IO.Path]::GetFullPath([string]$identity.execute) -ieq [IO.Path]::GetFullPath($ExpectedExecutable))
    }
    elseif (-not [string]::IsNullOrWhiteSpace($ExpectedExecutable)) {
        $executableCompatible = $false
    }
    $argumentsCompatible = [string]::IsNullOrWhiteSpace($ExpectedArguments) -or [string]$identity.arguments -eq $ExpectedArguments
    $principalCompatible = Test-WindowsPrincipalEquivalent -Actual $identity.principal -Expected $ExpectedPrincipal
    $triggerUser = if ($identity.trigger_user -is [array]) { [string]$identity.trigger_user[0] } else { [string]$identity.trigger_user }
    $triggerCompatible = (
        [string]$identity.trigger_type -eq 'LogonTrigger' -and
        (Test-WindowsPrincipalEquivalent -Actual $triggerUser -Expected $ExpectedPrincipal) -and
        (Test-ScheduledTaskLogonTypeEquivalent -Actual $identity.logon_type -Expected 'Interactive') -and
        (Test-ScheduledTaskRunLevelEquivalent -Actual $identity.run_level -Expected 'Limited') -and
        [string]$identity.action_type -eq 'Exec' -and
        [int]$identity.action_count -eq 1 -and
        [int]$identity.xml_action_count -eq 1 -and
        [int]$identity.xml_exec_action_count -eq 1 -and
        [string]$identity.start_when_available -ieq 'true' -and
        (Test-CanonicalTaskSettings -Identity $identity) -and
        -not [string]::IsNullOrWhiteSpace([string]$identity.task_identity_hash)
    )
    $compatible = ($working -eq $expectedWorking) -and $executableCompatible -and $argumentsCompatible -and $principalCompatible -and $triggerCompatible
    if (-not $compatible -and -not $AllowReplacement) {
        throw "Scheduled task $TaskName does not match the candidate action contract (working_directory=$working, execute=$($identity.execute), arguments=$($identity.arguments), principal=$($identity.principal))."
    }
    return $identity
}

function Assert-TaskIdentity {
    param([object]$Identity, [string]$ExpectedRoot, [string]$ExpectedPython, [string]$ExpectedArguments, [string]$ExpectedPrincipal, [Nullable[int]]$ExpectedApiPort, [string]$ExpectedTaskPath = '\')
    if (-not $Identity.exists) { throw "Scheduled task $($Identity.task_name) was not registered." }
    if ([string]$Identity.task_path -cne (Assert-CanonicalScheduledTaskPath -TaskPath $ExpectedTaskPath)) { throw "Scheduled task path did not match the candidate contract: $($Identity.task_name)" }
    $actualWorking = Get-CanonicalPath -Path ([string]$Identity.working_directory) -RequireExisting
    $actualExecute = Get-CanonicalPath -Path ([string]$Identity.execute) -RequireExisting
    $triggerUser = if ($Identity.trigger_user -is [array]) { [string]$Identity.trigger_user[0] } else { [string]$Identity.trigger_user }
    $triggerCompatible = (
        [string]$Identity.trigger_type -eq 'LogonTrigger' -and
        (Test-WindowsPrincipalEquivalent -Actual $triggerUser -Expected $ExpectedPrincipal) -and
        (Test-ScheduledTaskLogonTypeEquivalent -Actual $Identity.logon_type -Expected 'Interactive') -and
        (Test-ScheduledTaskRunLevelEquivalent -Actual $Identity.run_level -Expected 'Limited') -and
        [string]$Identity.action_type -eq 'Exec' -and
        [int]$Identity.action_count -eq 1 -and
        [int]$Identity.xml_action_count -eq 1 -and
        [int]$Identity.xml_exec_action_count -eq 1 -and
        [string]$Identity.start_when_available -ieq 'true' -and
        (Test-CanonicalTaskSettings -Identity $Identity) -and
        -not [string]::IsNullOrWhiteSpace([string]$Identity.task_identity_hash)
    )
    if ($actualWorking -ne (Get-CanonicalPath -Path $ExpectedRoot -RequireExisting) -or $actualExecute -ne (Get-CanonicalPath -Path $ExpectedPython -RequireExisting) -or [string]$Identity.arguments -ne $ExpectedArguments -or -not (Test-WindowsPrincipalEquivalent -Actual $Identity.principal -Expected $ExpectedPrincipal) -or -not $triggerCompatible) {
        throw "Scheduled task $($Identity.task_name) readback does not match the candidate action contract."
    }
    if ($null -ne $ExpectedApiPort) {
        $envPath = Join-Path $ExpectedRoot '.env'
        $configuredPort = Get-ConfiguredApiPort -EnvPath $envPath
        if ($configuredPort -ne [int]$ExpectedApiPort) {
            throw "Scheduled task $($Identity.task_name) readback uses HWP_API_PORT $configuredPort, expected $ExpectedApiPort."
        }
    }
    return $Identity
}

function Assert-RunOwnedTaskIdentity {
    param(
        [Parameter(Mandatory = $true)][object]$Identity,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments,
        [Parameter(Mandatory = $true)][string]$ExpectedPrincipal,
        [Parameter(Mandatory = $true)][int]$ExpectedApiPort
    )
    if (-not (Test-CanonicalTaskActionBinding -Identity $Identity -ExpectedRoot $ExpectedRoot -ExpectedPythonPath $ExpectedPython -ExpectedArguments $ExpectedArguments -ExpectedPrincipal $ExpectedPrincipal -ExpectedApiPort $ExpectedApiPort)) {
        throw "Refusing to remove a scheduled task without current-run candidate identity proof: $($Identity.task_name)"
    }
    return $true
}

function Get-InstallInventory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $root = Get-CanonicalPath -Path $Path -RequireExisting
    Assert-NoReparsePath -Path $root | Out-Null
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction Stop | Sort-Object FullName)
    if ($files.Count -gt 100000) { throw "Install inventory exceeds the bounded file limit: $root" }
    $fingerprints = @()
    foreach ($file in $files) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Install inventory encountered a reparse-point file: $($file.FullName)"
        }
        $relative = $file.FullName.Substring($root.Length).TrimStart([char]92, [char]47)
        $fingerprints += ('{0}|{1}|{2}' -f $relative.Replace('\\', '/').ToLowerInvariant(), [int64]$file.Length, (Get-Sha256Hex -Path $file.FullName))
    }
    $inventoryText = ($fingerprints -join "`n") + "`n"
    $inventorySha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $inventoryBytes = [System.Text.Encoding]::UTF8.GetBytes($inventoryText)
        $inventoryHash = ([System.BitConverter]::ToString($inventorySha.ComputeHash($inventoryBytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $inventorySha.Dispose() }
    $totalBytes = if ($files.Count -gt 0) { [int64](($files | Measure-Object -Property Length -Sum).Sum) } else { 0L }
    return [pscustomobject]@{
        root = $root
        file_count = $files.Count
        total_bytes = $totalBytes
        inventory_sha256 = $inventoryHash
    }
}

function Ensure-UserScopePoppler {
    $resolved = Resolve-PopplerForInstaller
    if ($resolved.ok) { return $resolved }
    if ($resolved.source -eq 'explicit-invalid') {
        throw 'Explicit Poppler path is invalid or not an allowed executable; refusing fallback installation.'
    }
    if ($DependencyMode -ne 'InstallUserScope') { throw 'pdftoppm is unavailable; rerun with -DependencyMode InstallUserScope or configure HWP_PDFTOPPM.' }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'DependencyMode InstallUserScope requires winget.exe.' }
    $packageId = Get-InstallerConfiguredValue -Name 'HWP_POPPLER_PACKAGE_ID'
    if ([string]::IsNullOrWhiteSpace([string]$packageId)) { $packageId = 'oschwartz10612.Poppler' }
    Invoke-InstallerNative -FilePath $winget.Source -Arguments @('install', '--id', $packageId, '--exact', '--scope', 'user', '--accept-source-agreements', '--accept-package-agreements') -WorkingDirectory $source | Out-Null
    $resolved = Resolve-PopplerForInstaller
    if (-not $resolved.ok) { throw 'Poppler installation completed without a resolvable pdftoppm executable.' }
    return $resolved
}

function Copy-SourceToCandidate {
    param([string]$Destination, [object]$ManifestResult)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $verifiedCopies = @()
    foreach ($manifestEntry in @($ManifestResult.manifest.files)) {
        $relative = Assert-WindowsSafeSourceRelativePath -RelativePath ([string]$manifestEntry.path)
        $sourcePath = Assert-NoReparseSourcePath -Root $source -RelativePath $relative
        $destinationPath = Join-Path $Destination $relative
        $sourceItem = Get-Item -LiteralPath $sourcePath -ErrorAction Stop
        if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse-point source member is not allowed: $relative"
        }
        if ($sourceItem.PSIsContainer) { throw "Source manifest member is not a regular file: $relative" }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        $verifiedCopies += Copy-FileVerified -SourcePath $sourcePath -DestinationPath $destinationPath -ExpectedSize ([int64]$manifestEntry.size) -ExpectedSha256 ([string]$manifestEntry.sha256)
    }
    return @($verifiedCopies)
}

function Copy-ManifestToCandidate {
    param([string]$ManifestPath, [string]$Destination)
    $manifest = Get-CanonicalPath -Path $ManifestPath -RequireExisting
    $manifestItem = Get-Item -LiteralPath $manifest -Force -ErrorAction Stop
    # Do not use Copy-Item -LiteralPath $manifest: stream and reverify the manifest bytes instead.
    return Copy-FileVerified -SourcePath $manifest -DestinationPath (Join-Path $Destination 'source-manifest.json') -ExpectedSize ([int64]$manifestItem.Length) -ExpectedSha256 (Get-Sha256Hex -Path $manifest)
}

function Copy-CandidateToInstall {
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][object]$ManifestResult
    )
    $candidate = Get-CanonicalPath -Path $CandidateRoot -RequireExisting
    Assert-NoReparsePath -Path $candidate | Out-Null
    Assert-NoReparsePath -Path $Destination | Out-Null
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $destinationRoot = Get-CanonicalPath -Path $Destination -RequireExisting
    $manifestByKey = @{}
    foreach ($manifestEntry in @($ManifestResult.manifest.files)) {
        $relative = Assert-WindowsSafeSourceRelativePath -RelativePath ([string]$manifestEntry.path)
        $manifestByKey[$relative.ToLowerInvariant()] = $manifestEntry
    }
    $copiedKeys = @{}
    # The venv can contain thousands of files.  Array += copies the complete
    # PowerShell array on every iteration and turns activation into an
    # avoidable quadratic operation; keep the verified records in a list.
    $records = New-Object 'System.Collections.Generic.List[object]'
    foreach ($directory in @(Get-ChildItem -LiteralPath $candidate -Recurse -Directory -Force -ErrorAction Stop)) {
        Assert-NoReparsePath -Path ([string]$directory.FullName) | Out-Null
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $candidate -Recurse -File -Force -ErrorAction Stop)) {
        Assert-NoReparsePath -Path ([string]$item.FullName) | Out-Null
        $relative = [string]$item.FullName.Substring($candidate.Length).TrimStart([char]92, [char]47)
        $relative = Assert-WindowsSafeSourceRelativePath -RelativePath $relative
        if (-not (Test-CanonicalPathWithinRoot -Path ([string]$item.FullName) -Root $candidate)) {
            throw "Candidate file escaped the candidate root: $relative"
        }
        $key = $relative.ToLowerInvariant()
        if ($copiedKeys.ContainsKey($key)) { throw "Duplicate candidate file path: $relative" }
        $copiedKeys[$key] = $true
        $expected = $manifestByKey[$key]
        $expectedSize = if ($null -ne $expected) { [int64]$expected.size } else { [int64]$item.Length }
        $expectedSha256 = if ($null -ne $expected) { [string]$expected.sha256 } else { Get-Sha256Hex -Path ([string]$item.FullName) }
        $destinationPath = Join-Path $destinationRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        $records.Add((Copy-FileVerified -SourcePath ([string]$item.FullName) -DestinationPath $destinationPath -ExpectedSize $expectedSize -ExpectedSha256 $expectedSha256)) | Out-Null
    }
    foreach ($key in $manifestByKey.Keys) {
        if (-not $copiedKeys.ContainsKey($key)) { throw "Candidate is missing manifest member: $key" }
    }
    return [pscustomobject]@{
        candidate_root = $candidate
        destination_root = $destinationRoot
        file_count = $records.Count
        source_bytes = [int64](($records | Measure-Object -Property source_size -Sum).Sum)
        destination_bytes = [int64](($records | Measure-Object -Property destination_size -Sum).Sum)
        destination_sha256 = @($records | ForEach-Object { [string]$_.destination_sha256 })
        files = $records.ToArray()
    }
}

function Set-EnvSetting {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $lines = @($Content -split "`r?`n")
    $found = $false
    $updated = @(
        foreach ($line in $lines) {
            if ([string]$line -match ("^\s*" + [regex]::Escape($Name) + "\s*=")) {
                if (-not $found) {
                    $found = $true
                    "$Name=$Value"
                }
            }
            else { [string]$line }
        }
    )
    if (-not $found) { $updated += "$Name=$Value" }
    return (($updated -join [Environment]::NewLine).TrimEnd() + [Environment]::NewLine)
}

function Assert-ExistingEnvPreimage {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [bool]$Exists,
        [Nullable[int64]]$ExpectedSize,
        [string]$ExpectedSha256
    )
    if (-not $Exists) { return $true }
    $faultMode = [string]([Environment]::GetEnvironmentVariable('HWPX_TEST_ENV_FAULT'))
    if ($faultMode -eq 'mismatch') {
        throw 'injected existing .env mismatch'
    }
    if ($faultMode -eq 'race') {
        throw 'injected existing .env race detected'
    }
    if ($null -eq $ExpectedSize -or [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
        throw 'Existing .env preimage is incomplete; refusing to continue.'
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Existing .env disappeared before verified preservation: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $actualSize = [int64]$item.Length
    $actualHash = Get-Sha256Hex -Path $Path
    if ($actualSize -ne [int64]$ExpectedSize -or $actualHash -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Existing .env changed before verified preservation: $Path"
    }
    return $true
}

function Preserve-ReceiptPreimage {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    Assert-NoReparsePath -Path $Path | Out-Null
    $sourceCanonical = Get-CanonicalPath -Path $Path -RequireExisting
    $sourceItem = Get-Item -LiteralPath $sourceCanonical -Force -ErrorAction Stop
    $sourceIdentity = Get-PathObjectIdentity -Path $sourceCanonical -RequireExisting
    $sourceSize = [int64]$sourceItem.Length
    $sourceHash = Get-Sha256Hex -Path $sourceCanonical
    $destination = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-install-receipt-preimage-' + $runId + '.json')
    try {
        Copy-FileVerified -SourcePath $sourceCanonical -DestinationPath $destination -ExpectedSize $sourceSize -ExpectedSha256 $sourceHash | Out-Null
        $destinationCanonical = Get-CanonicalPath -Path $destination -RequireExisting
        $destinationItem = Get-Item -LiteralPath $destinationCanonical -Force -ErrorAction Stop
        if ([int64]$destinationItem.Length -ne $sourceSize -or (Get-Sha256Hex -Path $destinationCanonical) -cne $sourceHash) {
            throw 'Receipt preimage backup did not match the sealed source bytes.'
        }
        Assert-PathObjectIdentity -Path $sourceCanonical -ExpectedIdentity $sourceIdentity | Out-Null
        $sourceAfter = Get-Item -LiteralPath $sourceCanonical -Force -ErrorAction Stop
        if ([int64]$sourceAfter.Length -ne $sourceSize -or (Get-Sha256Hex -Path $sourceCanonical) -cne $sourceHash) {
            throw 'Receipt changed while its preimage was being preserved.'
        }
        return $destinationCanonical
    }
    catch {
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Remove-ReceiptPreimageBackup {
    if (-not $receiptPreimageBackupPath) { return $true }
    if (-not $receiptPreimageOwnedByRun) { throw 'Refusing to remove an unowned receipt preimage backup.' }
    $expectedLeaf = 'hwpx-install-receipt-preimage-' + $runId + '.json'
    if ([IO.Path]::GetFileName($receiptPreimageBackupPath) -cne $expectedLeaf) {
        throw "Receipt preimage backup path is not owned by the current run: $receiptPreimageBackupPath"
    }
    if (-not (Test-Path -LiteralPath $receiptPreimageBackupPath -PathType Leaf)) {
        throw "Receipt preimage backup disappeared before current-run cleanup: $receiptPreimageBackupPath"
    }
    Assert-NoReparsePath -Path $receiptPreimageBackupPath | Out-Null
    Assert-PathObjectIdentity -Path $receiptPreimageBackupPath -ExpectedIdentity $receiptPreimageBackupIdentity | Out-Null
    $item = Get-Item -LiteralPath $receiptPreimageBackupPath -Force -ErrorAction Stop
    if ([int64]$item.Length -ne [int64]$receiptPreimageBackupSize -or (Get-Sha256Hex -Path $receiptPreimageBackupPath) -cne [string]$receiptPreimageBackupSha256) {
        throw "Receipt preimage backup identity changed before cleanup: $receiptPreimageBackupPath"
    }
    Remove-Item -LiteralPath $receiptPreimageBackupPath -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $receiptPreimageBackupPath) {
        throw "Receipt preimage backup remained after terminal cleanup: $receiptPreimageBackupPath"
    }
    return $true
}

function New-RunPathClaim {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = Get-CanonicalPath -Path $Path
    $parent = Split-Path -Parent $fullPath
    if ([string]::IsNullOrWhiteSpace($parent)) { throw "Cannot claim a path without a parent: $Path" }
    Assert-NoReparsePath -Path $parent | Out-Null
    if (Test-Path -LiteralPath $fullPath) { throw "Run-owned path already exists: $fullPath" }
    $claimPath = $fullPath + '.claim-' + $runId
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($claimPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $claimBytes = [System.Text.Encoding]::UTF8.GetBytes($runId + [Environment]::NewLine)
        $stream.Write($claimBytes, 0, $claimBytes.Length)
        $stream.Flush($true)
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    return (Get-CanonicalPath -Path $claimPath -RequireExisting)
}

function Remove-RunPathClaim {
    param(
        [AllowNull()][string]$ClaimPath,
        [AllowNull()][AllowEmptyString()][Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity
    )
    if ([string]::IsNullOrWhiteSpace($ClaimPath)) { return }
    if (Test-Path -LiteralPath $ClaimPath -PathType Leaf) {
        if ([string]::IsNullOrWhiteSpace($ExpectedObjectIdentity)) {
            throw "Refusing to remove a run path claim without stable object identity: $ClaimPath"
        }
        Assert-NoReparsePath -Path $ClaimPath | Out-Null
        Assert-PathObjectIdentity -Path $ClaimPath -ExpectedIdentity $ExpectedObjectIdentity | Out-Null
        Remove-Item -LiteralPath $ClaimPath -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $ClaimPath) { throw "Run path claim remained after cleanup: $ClaimPath" }
    }
}

function Assert-RunOwnedRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity,
        [bool]$OwnedByRun
    )
    if (-not $OwnedByRun) { throw "Refusing to mutate a root without current-run ownership: $Path" }
    $expected = Get-CanonicalPath -Path $ExpectedPath
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    Assert-NoReparsePath -Path $Path | Out-Null
    $actual = Get-CanonicalPath -Path $Path -RequireExisting
    if ($actual -ne $expected) { throw "Run-owned root identity changed: expected $expected, got $actual" }
    if ([string]::IsNullOrWhiteSpace($ExpectedObjectIdentity)) {
        throw "Run-owned root stable object identity is missing: $Path"
    }
    Assert-PathObjectIdentity -Path $Path -ExpectedIdentity $ExpectedObjectIdentity | Out-Null
    return $true
}

function Remove-RunOwnedRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity,
        [bool]$OwnedByRun
    )
    if (-not (Assert-RunOwnedRoot -Path $Path -ExpectedPath $ExpectedPath -ExpectedObjectIdentity $ExpectedObjectIdentity -OwnedByRun $OwnedByRun)) { return $false }
    # Revalidate immediately before the recursive mutation. A replacement at
    # the checked pathname must fail closed rather than being deleted.
    Assert-PathObjectIdentity -Path $Path -ExpectedIdentity $ExpectedObjectIdentity | Out-Null
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $Path) { throw "Run-owned root remained after cleanup: $Path" }
    return $true
}

function Assert-BackupOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$BackupPath,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][object]$ExpectedInventory,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity,
        [bool]$OwnedByRun
    )
    if (-not $OwnedByRun) { throw "Refusing to use an unowned PreserveMove backup: $BackupPath" }
    if (-not (Assert-RunOwnedRoot -Path $BackupPath -ExpectedPath $ExpectedPath -ExpectedObjectIdentity $ExpectedObjectIdentity -OwnedByRun $OwnedByRun)) {
        throw "PreserveMove backup disappeared before rollback: $BackupPath"
    }
    $actual = Get-InstallInventory -Path $BackupPath
    foreach ($field in @('file_count', 'total_bytes', 'inventory_sha256')) {
        if ([string]$actual.$field -ne [string]$ExpectedInventory.$field) {
            throw "PreserveMove backup identity changed for ${field}: $BackupPath"
        }
    }
    Assert-PathObjectIdentity -Path $BackupPath -ExpectedIdentity $ExpectedObjectIdentity | Out-Null
    return $actual
}

function Set-InstallerRuntimeEnvProvenance {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][object]$ManifestResult,
        [Parameter(Mandatory = $true)][object]$ConfigResult,
        [switch]$PreserveExistingMarkerBytes
    )
    $root = Get-CanonicalPath -Path $InstallRoot -RequireExisting
    $markerPath = Join-Path $root '.hwpx-install.json'
    $markerCapture = Read-BoundedJsonObject -Path $markerPath -MaxBytes 65536
    $markerPayload = $markerCapture.value
    $markerSha256Before = [string]$markerCapture.sha256
    $markerIdentityBefore = [string]$markerCapture.object_identity
    $candidateGeneration = '{0}:{1}:{2}' -f $ManifestResult.manifest.commit, $ManifestResult.manifest.tree, $ManifestResult.manifest_sha256
    if ([string]$markerPayload.schema_version -cne 'hwpx/windows-install-marker/v1' -or
        [string]$markerPayload.repository -cne [string]$ManifestResult.manifest.repository -or
        [string]$markerPayload.commit -cne [string]$ManifestResult.manifest.commit -or
        [string]$markerPayload.tree -cne [string]$ManifestResult.manifest.tree -or
        [string]$markerPayload.source_manifest_sha256 -cne [string]$ManifestResult.manifest_sha256 -or
        [string]$markerPayload.candidate_generation -cne $candidateGeneration) {
        throw 'Installer runtime .env provenance cannot extend a marker with a different source generation.'
    }
    $envPath = Get-CanonicalPath -Path ([string]$ConfigResult.env_path) -RequireExisting
    if (-not (Test-CanonicalPathWithinRoot -Path $envPath -Root $root)) {
        throw 'Installer runtime .env provenance path escaped the install root.'
    }
    $relativeEnvPath = [string]$envPath.Substring($root.Length).TrimStart([char]92, [char]47)
    $relativeEnvPath = Assert-WindowsSafeSourceRelativePath -RelativePath $relativeEnvPath
    if ($relativeEnvPath -ine '.env') {
        throw 'Installer runtime .env provenance must bind the exact root-relative .env path.'
    }
    $envItem = Get-Item -LiteralPath $envPath -Force -ErrorAction Stop
    if ($envItem.PSIsContainer -or ($envItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Installer runtime .env provenance requires a regular non-reparse file.'
    }
    $actualEnvSize = [int64]$envItem.Length
    $actualEnvSha256 = Get-Sha256Hex -Path $envPath
    if ($actualEnvSize -ne [int64]$ConfigResult.env_size_after -or
        $actualEnvSha256 -cne ([string]$ConfigResult.env_sha256_after).ToLowerInvariant()) {
        throw 'Installer runtime .env provenance did not match the config write readback.'
    }

    # A valid existing runtime-env contract is already the authoritative
    # provenance for a reused install. Re-serializing it would change the
    # source/provenance fields (candidate vs config.example) and its creation
    # time, even though the candidate and .env bytes are unchanged.
    $runtimeEnvPropertyPresent = $markerPayload.PSObject.Properties.Name -contains 'runtime_env'
    if ($runtimeEnvPropertyPresent -and $null -ne $markerPayload.runtime_env) {
        $existingRuntimeEnv = $markerPayload.runtime_env
        foreach ($field in @(
            'schema_version', 'provenance', 'source', 'path', 'install_root',
            'install_root_identity', 'size', 'sha256', 'source_manifest_sha256',
            'candidate_generation', 'created_at_utc'
        )) {
            if ($existingRuntimeEnv.PSObject.Properties.Name -notcontains $field) {
                throw "Existing runtime .env provenance contract is missing '$field'."
            }
        }
        if ([string]$existingRuntimeEnv.schema_version -cne 'hwpx/installer-runtime-env/v1' -or
            [string]$existingRuntimeEnv.path -ine '.env' -or
            [string]$existingRuntimeEnv.install_root -cne $root -or
            [string]$existingRuntimeEnv.install_root_identity -cne (Get-PathObjectIdentity -Path $root -RequireExisting) -or
            [string]$existingRuntimeEnv.size -cne [string]$actualEnvSize -or
            [string]$existingRuntimeEnv.sha256 -cne $actualEnvSha256 -or
            [string]$existingRuntimeEnv.source_manifest_sha256 -cne [string]$ManifestResult.manifest_sha256 -or
            [string]$existingRuntimeEnv.candidate_generation -cne $candidateGeneration -or
            [string]::IsNullOrWhiteSpace([string]$existingRuntimeEnv.created_at_utc)) {
            throw 'Existing runtime .env provenance contract does not match the current install preimage.'
        }
        $existingProvenance = [string]$existingRuntimeEnv.provenance
        $existingSource = [string]$existingRuntimeEnv.source
        if ($existingProvenance -notin @('installer-generated', 'installer-preserved') -or
            (($existingProvenance -eq 'installer-generated') -and $existingSource -cne 'config.example') -or
            (($existingProvenance -eq 'installer-preserved') -and $existingSource -notin @('existing-install', 'candidate'))) {
            throw 'Existing runtime .env provenance contract has an invalid source/provenance pair.'
        }

        # Read back the same bytes and object identity before returning. This
        # proves that the no-write reuse path did not merely skip a call while
        # another writer changed the marker underneath it.
        Assert-PathObjectIdentity -Path $markerPath -ExpectedIdentity $markerIdentityBefore | Out-Null
        $preservedReadback = Read-BoundedJsonObject -Path $markerPath -MaxBytes 65536
        if ([string]$preservedReadback.sha256 -cne $markerSha256Before) {
            throw 'Installer reused marker bytes changed during preserved marker readback.'
        }
        return [pscustomobject]@{
            marker_path = $markerPath
            marker_sha256 = [string]$preservedReadback.sha256
            original_marker_sha256 = $markerSha256Before
            bytes_preserved = $true
            marker_rewritten = $false
            contract = $preservedReadback.value.runtime_env
        }
    }
    if ($PreserveExistingMarkerBytes) {
        throw 'Reused install marker is missing its runtime .env provenance contract.'
    }

    $configSource = [string]$ConfigResult.source
    $provenance = $null
    if ($configSource -eq 'config.example') {
        $provenance = 'installer-generated'
    }
    elseif ($configSource -in @('existing-install', 'candidate')) {
        $provenance = 'installer-preserved'
    }
    else {
        throw "Installer runtime .env provenance source is unsupported: $configSource"
    }
    $runtimeEnv = [ordered]@{
        schema_version = 'hwpx/installer-runtime-env/v1'
        provenance = $provenance
        source = $configSource
        path = '.env'
        install_root = $root
        install_root_identity = Get-PathObjectIdentity -Path $root -RequireExisting
        size = $actualEnvSize
        sha256 = $actualEnvSha256
        source_manifest_sha256 = [string]$ManifestResult.manifest_sha256
        candidate_generation = $candidateGeneration
        created_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    $markerPayload | Add-Member -MemberType NoteProperty -Name 'runtime_env' -Value ([pscustomobject]$runtimeEnv) -Force
    Write-JsonReceipt -Path $markerPath -Value $markerPayload | Out-Null
    $readback = Read-BoundedJsonObject -Path $markerPath -MaxBytes 65536
    if ($null -eq $readback.value.runtime_env) {
        throw 'Installer runtime .env provenance marker readback was missing.'
    }
    foreach ($field in @('schema_version', 'provenance', 'source', 'path', 'install_root', 'install_root_identity', 'size', 'sha256', 'source_manifest_sha256', 'candidate_generation')) {
        if ([string]$readback.value.runtime_env.$field -cne [string]$runtimeEnv.$field) {
            throw "Installer runtime .env provenance marker readback differed for $field."
        }
    }
    return [pscustomobject]@{
        marker_path = $markerPath
        marker_sha256 = [string]$readback.sha256
        original_marker_sha256 = $markerSha256Before
        bytes_preserved = $false
        marker_rewritten = $true
        contract = $readback.value.runtime_env
    }
}

function Ensure-CandidateConfig {
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [string]$PreservedEnvPath,
        [bool]$PreimageExists,
        [string]$PreimageHash,
        [Nullable[int64]]$PreimageSize,
        [Parameter(Mandatory = $true)][string]$PopplerPath,
        [Parameter(Mandatory = $true)][int]$ApiPort,
        [Parameter(Mandatory = $true)][string]$ApiTaskName,
        [Parameter(Mandatory = $true)][string]$WorkerTaskName
    )
    $envFile = Join-Path $CandidateRoot '.env'
    $candidateEnvExistedBefore = Test-Path -LiteralPath $envFile -PathType Leaf
    $preservedFromExisting = $false
    if ($PreimageExists) {
        if ([string]::IsNullOrWhiteSpace($PreimageHash) -or $null -eq $PreimageSize) {
            throw 'Existing .env preimage is incomplete; refusing to continue.'
        }
        if ($candidateEnvExistedBefore) {
            Assert-ExistingEnvPreimage -Path $envFile -Exists $true -ExpectedSize $PreimageSize -ExpectedSha256 $PreimageHash | Out-Null
        }
    }
    if (-not $candidateEnvExistedBefore) {
        if ($PreservedEnvPath -and (Test-Path -LiteralPath $PreservedEnvPath -PathType Leaf)) {
            Assert-ExistingEnvPreimage -Path $PreservedEnvPath -Exists $PreimageExists -ExpectedSize $PreimageSize -ExpectedSha256 $PreimageHash | Out-Null
            Copy-FileVerified -SourcePath $PreservedEnvPath -DestinationPath $envFile -ExpectedSize $PreimageSize -ExpectedSha256 $PreimageHash | Out-Null
            $preservedFromExisting = $true
        }
        elseif ($PreimageExists) {
            throw 'Existing .env preimage is unavailable for the candidate.'
        }
        else {
            $template = Join-Path $CandidateRoot 'config.example'
            if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw "Installed config template is missing: $template" }
            $content = [System.IO.File]::ReadAllText((Get-CanonicalPath -Path $template -RequireExisting))
            $content = Set-EnvSetting -Content $content -Name 'HWP_API_PORT' -Value ([string]$ApiPort)
            $content = Set-EnvSetting -Content $content -Name 'HWP_PDFTOPPM' -Value $PopplerPath
            $content = Set-EnvSetting -Content $content -Name 'HWP_API_TASK_NAME' -Value $ApiTaskName
            $content = Set-EnvSetting -Content $content -Name 'HWP_WORKER_TASK_NAME' -Value $WorkerTaskName
            [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
        }
    }
    $envItemAfter = Get-Item -LiteralPath $envFile -Force -ErrorAction Stop
    $envSizeAfter = [int64]$envItemAfter.Length
    $envHashAfter = Get-Sha256Hex -Path $envFile
    if ($PreimageExists) {
        Assert-ExistingEnvPreimage -Path $envFile -Exists $true -ExpectedSize $PreimageSize -ExpectedSha256 $PreimageHash | Out-Null
    }
    return [pscustomobject]@{
        env_path = $envFile
        env_existed_before = $PreimageExists
        env_sha256_before = $PreimageHash
        env_size_before = if ($PreimageExists) { [int64]$PreimageSize } else { $null }
        candidate_env_existed_before = $candidateEnvExistedBefore
        env_size_after = $envSizeAfter
        env_sha256_after = $envHashAfter
        preserved = ($PreimageExists -and [int64]$PreimageSize -eq $envSizeAfter -and -not [string]::IsNullOrWhiteSpace($PreimageHash) -and $PreimageHash -eq $envHashAfter)
        source = if ($preservedFromExisting) { 'existing-install' } elseif ($candidateEnvExistedBefore) { 'candidate' } else { 'config.example' }
        pdftoppm_path_recorded = if ($PreimageExists -or $candidateEnvExistedBefore) { $null } else { $PopplerPath }
        api_port_recorded = if ($PreimageExists -or $candidateEnvExistedBefore) { $null } else { $ApiPort }
    }
}

try {
    Assert-NoReparsePath -Path $SourceRoot | Out-Null
    Assert-NoReparsePath -Path $InstallRoot | Out-Null
    $source = Get-CanonicalPath -Path $SourceRoot -RequireExisting
    $install = Get-CanonicalPath -Path $InstallRoot
    $installRootLock = Enter-InstallRootLock -InstallRoot $install
    $receipt.install_root_preimage.lock = [ordered]@{
        purpose = $installRootLock.purpose
        mutex_name = $installRootLock.mutex_name
        acquired_at_utc = $installRootLock.acquired_at_utc
        canonical_root = $installRootLock.canonical_root
    }
    # A hard kill/reboot can leave the predecessor moved and its tasks
    # disabled after the last ordinary receipt. Adjudicate the stable journal
    # while the machine lifecycle lock is held before admitting this run.
    Invoke-StaleInstallTransactionRecovery
    $installRootExistedAtStart = Test-Path -LiteralPath $installInputPath -PathType Container
    $receipt.install_root_preimage.exists = [bool]$installRootExistedAtStart
    if ($source -eq $install) { throw 'SourceRoot and InstallRoot must be different paths; in-place installation is not supported.' }
    $rootExistsNow = Test-Path -LiteralPath $install -PathType Container
    if ([bool]$rootExistsNow -ne [bool]$installRootExistedAtStart) {
        throw 'InstallRoot changed between preimage capture and preflight; refusing to continue.'
    }
    $rootExisted = [bool]$installRootExistedAtStart
    $existingEnvPath = Join-Path $install '.env'
    $existingEnvExisted = $rootExisted -and (Test-Path -LiteralPath $existingEnvPath -PathType Leaf)
    $existingEnvItem = if ($existingEnvExisted) { Get-Item -LiteralPath $existingEnvPath -Force -ErrorAction Stop } else { $null }
    $existingEnvHash = if ($existingEnvExisted) { Get-Sha256Hex -Path $existingEnvPath } else { $null }
    $existingEnvSize = if ($existingEnvExisted) { [int64]$existingEnvItem.Length } else { $null }
    $receiptAdmission = if (-not [string]::IsNullOrWhiteSpace([string]$requestedReceiptPath)) {
        Assert-ReceiptPathAdmission -ReceiptPath $requestedReceiptPath -InstallRoot $install
    }
    else {
        Assert-ReceiptPathAdmission -ReceiptPath $receiptFile -InstallRoot $install
    }
    $admittedReceiptFile = [string]$receiptAdmission.path
    if ([string]$admittedReceiptFile -eq [string]$install) {
        throw 'ReceiptPath must identify a file outside InstallRoot.'
    }
    # Capture and verify the caller preimage while the admission identity is
    # still authoritative. Only after this completes may receiptFile switch
    # away from the safe external bootstrap path.
    if ([bool]$receiptAdmission.exists) {
        $receiptPreimageSourceCanonical = [string]$receiptAdmission.path
        Assert-PathObjectIdentity -Path $receiptPreimageSourceCanonical -ExpectedIdentity ([string]$receiptAdmission.object_identity) | Out-Null
        $receiptPreimageBackupPath = Preserve-ReceiptPreimage -Path $receiptPreimageSourceCanonical
        $receiptPreimageOwnedByRun = $true
        $receiptPreimageBackupIdentity = Get-PathObjectIdentity -Path $receiptPreimageBackupPath -RequireExisting
        $receiptPreimageBackupItem = Get-Item -LiteralPath $receiptPreimageBackupPath -Force -ErrorAction Stop
        $receiptPreimageBackupSize = [int64]$receiptPreimageBackupItem.Length
        $receiptPreimageBackupSha256 = Get-Sha256Hex -Path $receiptPreimageBackupPath
    }
    $receiptFile = $admittedReceiptFile
    $receipt.install_root_preimage.current_exists = [bool]$rootExistsNow
    $receipt.install_root_preimage.receipt_path = $receiptFile
    $receipt.install_root_preimage.receipt_parent_exists = [bool](Test-Path -LiteralPath (Split-Path -Parent $receiptFile) -PathType Container)
    $receipt.receipt_preimage.path = $receiptFile
    $receipt.receipt_preimage.exists = [bool]$receiptAdmission.exists
    $receipt.receipt_preimage.parent_exists = [bool](Test-Path -LiteralPath (Split-Path -Parent $receiptFile) -PathType Container)
    if ($receipt.receipt_preimage.exists) {
        $receipt.receipt_preimage.size = [int64]$receiptAdmission.size
        $receipt.receipt_preimage.sha256 = [string]$receiptAdmission.sha256
        $receipt.receipt_preimage.object_identity = [string]$receiptAdmission.object_identity
        $receipt.receipt_preimage.backup_path = $receiptPreimageBackupPath
        $receipt.receipt_preimage.backup_size = $receiptPreimageBackupSize
        $receipt.receipt_preimage.backup_sha256 = $receiptPreimageBackupSha256
        $receipt.receipt_preimage.backup_object_identity = $receiptPreimageBackupIdentity
        $receipt.receipt_preimage.backup_owned_by_run = $true
        $receipt.receipt_preimage.backup_removed = $false
    }
    $receipt.source_root = $source
    $receipt.install_root = $install
    $receipt.checks.config_preimage = [pscustomobject]@{
        env_path = $existingEnvPath
        exists = $existingEnvExisted
        sha256 = $existingEnvHash
        env_size_before = $existingEnvSize
    }
    if ($FixturePath) {
        $fixture = Get-CanonicalPath -Path $FixturePath -RequireExisting
        $fixtureItem = Get-Item -LiteralPath $fixture -ErrorAction Stop
        if ($fixtureItem.Extension.ToLowerInvariant() -notin @('.hwp', '.hwpx')) { throw 'FixturePath must be an .hwp or .hwpx file.' }
        $receipt.fixture = [ordered]@{
            path = $fixture
            bytes = [int64]$fixtureItem.Length
            sha256_before = Get-Sha256Hex -Path $fixture
            sha256_after = $null
        }
    }
    Save-InstallerReceipt
    Write-InstallTransactionJournal -State 'preflight-admitted' | Out-Null

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows is required for the native installer.' }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'PowerShell 5.1 or newer is required.' }
    $receipt.checks.os = [pscustomobject]@{ ok = $true; platform = [Environment]::OSVersion.VersionString; powershell = [string]$PSVersionTable.PSVersion }
    $interactive = Get-InteractiveUserIdentity
    $receipt.checks.interactive_user = $interactive
    $receipt.checks.hancom = Get-HancomComIdentity

    $python = Resolve-PythonForInstaller
    $pythonProbe = Invoke-InstallerNative -FilePath $python.path -Arguments (@($python.prefix) + @('--version')) -WorkingDirectory $source
    $pythonRuntime = Get-PythonRuntimeIdentity -Invocation $python -WorkingDirectory $source -Role 'bootstrap'
    $receipt.checks.python = [pscustomobject]@{
        ok = $true
        executable = $python.path
        prefix = @($python.prefix)
        version = ($pythonProbe.stdout + $pythonProbe.stderr).Trim()
        runtime_identity = $pythonRuntime
    }

    $manifestPath = Find-InstallerManifest
    $manifestResult = Get-SourceManifest -SourceRoot $source -ManifestPath $manifestPath -ExpectedRepository $ExpectedRepository -ExpectedCommit $ExpectedCommit -ExpectedTree $ExpectedTree -ExpectedManifestSha256 $ExpectedManifestSha256
    if (-not $manifestResult.ok) { throw "Source manifest verification failed with $($manifestResult.mismatch_count) mismatch(es)." }
    if (-not [bool]$manifestResult.identity_binding.verified) {
        throw 'Source manifest identity is not independently bound.'
    }
    $receipt.checks.source_manifest = $manifestResult
    $receipt.source_identity = [ordered]@{
        repository = [string]$manifestResult.manifest.repository
        commit = [string]$manifestResult.manifest.commit
        tree = [string]$manifestResult.manifest.tree
        identity_source = [string]$manifestResult.identity_source
        identity_verified = [bool]$manifestResult.identity_verified
        identity_binding = $manifestResult.identity_binding
        manifest_sha256 = [string]$manifestResult.manifest_sha256
        file_count = [int]$manifestResult.file_count
    }
    $receipt.candidate_generation = '{0}:{1}:{2}' -f $manifestResult.manifest.commit, $manifestResult.manifest.tree, $manifestResult.manifest_sha256

    $apiPort = Resolve-ApiPort -InstallRoot $install -RequestedApiPort $requestedApiPort
    $receipt.api_port = $apiPort
    $receipt.api_base_url = "http://127.0.0.1:$apiPort"
    $apiTaskName = Get-InstallerConfiguredValue -Name 'HWP_API_TASK_NAME'
    if ([string]::IsNullOrWhiteSpace([string]$apiTaskName)) { $apiTaskName = 'hwpx-editor-api' }
    $workerTaskName = Get-InstallerConfiguredValue -Name 'HWP_WORKER_TASK_NAME'
    if ([string]::IsNullOrWhiteSpace([string]$workerTaskName)) { $workerTaskName = 'hwpx-editor-worker' }
    $taskPath = Get-InstallerConfiguredValue -Name 'HWP_TASK_PATH'
    if ([string]::IsNullOrWhiteSpace([string]$taskPath)) { $taskPath = '\' }
    $taskPath = Assert-CanonicalScheduledTaskPath -TaskPath $taskPath
    $receipt.task_path = $taskPath
    # The machine lifecycle mutex was acquired before reading mutable install
    # configuration. Extend that same secured scope with the exact task and
    # port identities before any task, process, or candidate mutation.
    $installRootLock = Add-MachineLifecycleLockScope -Lock $installRootLock -TaskNames @($apiTaskName, $workerTaskName) -ApiPort $apiPort
    $receipt.install_root_preimage.lock_scope = [ordered]@{
        lock_kind = 'machine-lifecycle'
        task_names = @($apiTaskName, $workerTaskName)
        api_port = $apiPort
        mutex_names = @($installRootLock.locks | ForEach-Object { $_.mutex_name })
    }
    $expectedPython = Join-Path $install '.venv\Scripts\python.exe'
    $receipt.checks.port = Assert-PortAvailable -Port $apiPort -ExpectedRoot $install -ExpectedTaskName $apiTaskName -ExpectedTaskPath $taskPath
    $receipt.checks.task_preflight = @(
        (Assert-TaskCompatibility -TaskName $apiTaskName -TaskPath $taskPath -ExpectedRoot $install -ExpectedExecutable $expectedPython -ExpectedArguments '-m app.api_server' -ExpectedPrincipal $interactive.name -ExpectedApiPort $apiPort -AllowReplacement:$ReplaceExistingTasks),
        (Assert-TaskCompatibility -TaskName $workerTaskName -TaskPath $taskPath -ExpectedRoot $install -ExpectedExecutable $expectedPython -ExpectedArguments '-m app.worker' -ExpectedPrincipal $interactive.name -ExpectedApiPort $apiPort -AllowReplacement:$ReplaceExistingTasks)
    )
    $poppler = Resolve-PopplerForInstaller
    if (-not $poppler.ok -and $DependencyMode -eq 'InstallUserScope') {
        if ($poppler.source -eq 'explicit-invalid') {
            throw 'Explicit Poppler path is invalid or not an allowed executable; refusing fallback installation.'
        }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'DependencyMode InstallUserScope requires winget.exe.' }
        # winget is an external mutation, not a preflight probe. Publish its
        # own dependency phase and retained-mutation evidence before invoking
        # it so a later failure cannot be mislabeled FAIL_PREFLIGHT.
        $phase = 'dependency'
        $receipt.phase = 'dependency'
        $receipt.status = 'IN_PROGRESS'
        $receipt.failure_class = $null
        $receipt.status_code = $null
        $dependencyMutationAttempted = $true
        $receipt.external_mutations = @($receipt.external_mutations) + [ordered]@{
            kind = 'dependency-install'
            manager = 'winget'
            scope = 'user'
            status = 'retained-until-operator-review'
            preexisting_resolvable = $false
        }
        $receipt.checks.dependency_phase = [ordered]@{
            status = 'IN_PROGRESS'
            manager = 'winget'
            scope = 'user'
            preexisting_resolvable = $false
            retained_external_mutation = $true
        }
        Save-InstallerReceipt
        Write-InstallTransactionJournal -State 'dependency-started' | Out-Null
        $dependencyMutationRetained = $true
        $poppler = Ensure-UserScopePoppler
        $receipt.checks.dependency_phase.status = 'PASS'
        $receipt.checks.dependency_phase.postcondition = 'pdftoppm-resolvable'
        $receipt.checks.dependency_phase.completed_at_utc = [DateTime]::UtcNow.ToString('o')
        $phase = 'preflight'
        $receipt.phase = 'preflight'
        Write-InstallTransactionJournal -State 'dependency-complete' | Out-Null
    }
    elseif (-not $poppler.ok) {
        throw 'pdftoppm is unavailable; rerun with -DependencyMode InstallUserScope or configure HWP_PDFTOPPM.'
    }
    $receipt.checks.pdf_renderer = $poppler

    $sourceFiles = @($manifestResult.manifest.files)
    $sourceBytes = [int64](($sourceFiles | Measure-Object -Property size -Sum).Sum)
    $fixtureReservation = if ($receipt.fixture) { [int64]$receipt.fixture.bytes } else { 0 }
    $spoolReservation = [int64]$fixtureReservation + 536870912
    $requiredBytes = [int64]$sourceBytes + $spoolReservation
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($install).TrimEnd([char]92).TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($drive -and [int64]$drive.Free -lt $requiredBytes) { throw "Insufficient disk capacity: need $requiredBytes bytes, have $($drive.Free)." }
    $receipt.checks.capacity = [pscustomobject]@{ required_bytes = $requiredBytes; available_bytes = if ($drive) { [int64]$drive.Free } else { $null }; spool_reservation_bytes = $spoolReservation; fixture_reservation_bytes = $fixtureReservation }
    $receipt.phase = 'install'
    $receipt.status = 'IN_PROGRESS'
    $receipt.failure_class = $null
    $receipt.status_code = $null
    Save-InstallerReceipt

    $phase = 'install'
    $inPlace = ($source -eq $install)
    $marker = if ($rootExisted) { Join-Path $install '.hwpx-install.json' } else { $null }
    $reused = $false
    $backupRoot = $null
    $candidateRoot = $null
    $taskNames = @($apiTaskName, $workerTaskName)
    foreach ($configuredTaskName in $taskNames) {
        Assert-SafeScheduledTaskName -TaskName ([string]$configuredTaskName) | Out-Null
    }
    if (@($taskNames | Select-Object -Unique).Count -ne $taskNames.Count) {
        throw 'API and worker scheduled task names must be distinct.'
    }
    Write-InstallTransactionJournal -State 'install-started' | Out-Null
    # The snapshot is sealed at the last safe preimage boundary below: for a
    # PreserveMove it is captured after process quiescence but before task
    # unregister/move; for a new root it is captured before candidate creation.
    $snapshotPath = $null
    if ($rootExisted -and -not $inPlace) {
        $markerCompatible = $false
        $installManifestCompatible = $false
        if ($marker) {
            $installedMarkerPayload = $null
            try {
                $installedMarkerCapture = Read-BoundedJsonObject -Path $marker -MaxBytes 65536
                $installedMarkerPayload = $installedMarkerCapture.value
                $installedRuntimeEnvContract = $null
                if ($installedMarkerPayload.PSObject.Properties.Name -contains 'runtime_env') {
                    $installedRuntimeEnvContract = $installedMarkerPayload.runtime_env
                }
                $installedManifestPath = Join-Path $install 'source-manifest.json'
                if (Test-Path -LiteralPath $installedManifestPath -PathType Leaf) {
                    $installedManifest = Get-SourceManifest -SourceRoot $install -ManifestPath $installedManifestPath -ExpectedRepository ([string]$manifestResult.manifest.repository) -ExpectedCommit ([string]$manifestResult.manifest.commit) -ExpectedTree ([string]$manifestResult.manifest.tree) -ExpectedManifestSha256 ([string]$manifestResult.manifest_sha256) -ExpectedRuntimeEnvContract $installedRuntimeEnvContract
                    $installManifestCompatible = $installedManifest.ok -and [string]$installedManifest.manifest_sha256 -eq (Get-Sha256Hex -Path $manifestPath)
                }
                $markerPayload = $installedMarkerPayload
                $expectedMarkerGeneration = '{0}:{1}:{2}' -f $manifestResult.manifest.commit, $manifestResult.manifest.tree, (Get-Sha256Hex -Path $manifestPath)
                $markerRuntimeEnvPresent = $markerPayload.PSObject.Properties.Name -contains 'runtime_env' -and $null -ne $markerPayload.runtime_env
                $markerCompatible = (
                    [string]$markerPayload.schema_version -ceq 'hwpx/windows-install-marker/v1' -and
                    [string]$markerPayload.repository -eq [string]$manifestResult.manifest.repository -and
                    [string]$markerPayload.commit -eq [string]$manifestResult.manifest.commit -and
                    [string]$markerPayload.tree -eq [string]$manifestResult.manifest.tree -and
                    [string]$markerPayload.source_manifest_sha256 -eq [string]$manifestResult.manifest_sha256 -and
                    [string]$markerPayload.candidate_generation -ceq $expectedMarkerGeneration -and
                    $markerRuntimeEnvPresent -and
                    $installManifestCompatible -and
                    (Test-Path -LiteralPath (Join-Path $install '.venv\Scripts\python.exe') -PathType Leaf) -and
                    (Test-Path -LiteralPath (Join-Path $install 'requirements-windows.lock') -PathType Leaf)
                )
            }
            catch { $markerCompatible = $false }
        }
        if ($markerCompatible) {
            $reused = $true
            $candidateRoot = $install
            $candidateRootOwnedByRun = $false
            Seal-InstallerSnapshot
        }
        elseif ($ExistingInstallDisposition -eq 'Fail') {
            if ([Environment]::GetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT') -eq 'after-existing-root-compatibility') {
                throw 'injected installer fault after existing-root compatibility preflight'
            }
            throw 'InstallRoot contains an incompatible existing installation; use -ExistingInstallDisposition PreserveMove.'
        }
        else {
            $backupRoot = "$install.backup-$runId"
            # Resolve the destination before moving the root so the rollback
            # receipt can identify the claimed path without a post-move lookup.
            $backupRootCanonical = Get-CanonicalPath -Path $backupRoot
            $backupClaimPath = $backupRootCanonical + '.claim-' + $runId
            Write-InstallTransactionJournal -State 'backup-claim-planned' | Out-Null
            $backupClaimPath = New-RunPathClaim -Path $backupRoot
            $backupClaimIdentity = Get-PathObjectIdentity -Path $backupClaimPath -RequireExisting
            $receipt.rollback.backup_claim_path = $backupClaimPath
            Write-InstallTransactionJournal -State 'backup-claim-created' | Out-Null
            $receipt.rollback.pre_move_tasks_removed = @()
            $preMoveTaskStates = @{}
            # Capture the complete predecessor task/process preimage before
            # the first disable call. The journal then always points to a
            # sealed recovery snapshot if the host is killed mid-transition.
            foreach ($taskName in $taskNames) {
                $originalTaskIdentity = Get-ScheduledTaskIdentity -TaskName $taskName -TaskPath $taskPath
                if (-not [bool]$originalTaskIdentity.exists) { continue }
                $preMoveTaskIdentity[$taskName] = $originalTaskIdentity
                $preMoveTaskStates[$taskName] = [string]$originalTaskIdentity.state
            }
            Seal-InstallerSnapshot -TaskStateOverride $preMoveTaskStates -TaskIdentityOverride $preMoveTaskIdentity
            Write-InstallTransactionJournal -State 'snapshot-sealed' | Out-Null
            # Task admission, process quiescence, snapshot sealing, and both
            # unregister operations share one recovery boundary.  A partial
            # first/second-task failure must restore the exact XML, enabled
            # state, and running state before the predecessor is considered
            # preserved.
            try {
                foreach ($taskName in $taskNames) {
                    $existingTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing
                    if (-not $existingTask) {
                        throw "Existing scheduled task disappeared during PreserveMove admission: $taskName"
                    }
                    Write-InstallTransactionJournal -State 'task-disable-started' | Out-Null
                    Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop | Out-Null
                    $preMoveTaskAdmissionIdentity[$taskName] = Get-ScheduledTaskIdentity -TaskName $taskName -TaskPath $taskPath
                    if ([string]$existingTask.State -eq 'Running') {
                        Write-InstallTransactionJournal -State 'task-stop-started' | Out-Null
                        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                        if (-not (Wait-ScheduledTaskInactive -TaskName $taskName -TaskPath $taskPath)) {
                            throw "Existing scheduled task remained active before PreserveMove: $taskName"
                        }
                    }
                }
                Write-InstallTransactionJournal -State 'tasks-disabled' | Out-Null
                # Seal caller-owned process evidence immediately before the
                # release boundary so a process that vanishes while the stop
                # helper is coordinating is still accounted for truthfully.
                $preMoveInitialProcessSnapshot = @(Get-InstallProcessSnapshot -RootPath $install -ExpectedPythonPath (Join-Path $install '.venv\Scripts\python.exe'))
                $preMoveProcessRelease = Stop-InstallProcesses -RootPath $install -PreserveProcessIds @() -InitialProcessSnapshot $preMoveInitialProcessSnapshot
                $receipt.rollback.pre_move_process_release = $preMoveProcessRelease
                if (-not $preMoveProcessRelease.ok -or @($preMoveProcessRelease.remaining).Count -gt 0) {
                    throw 'Existing-install process handles were not fully released before PreserveMove.'
                }
                Write-InstallTransactionJournal -State 'processes-quiesced' | Out-Null
                $removedTaskIndex = 0
                foreach ($taskName in $taskNames) {
                    $existingTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing
                    if (-not $existingTask) {
                        if ($preMoveTaskIdentity.ContainsKey($taskName)) {
                            throw "Existing scheduled task disappeared before PreserveMove root move: $taskName"
                        }
                        continue
                    }
                    Write-InstallTransactionJournal -State 'task-unregister-started' | Out-Null
                    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
                    if (Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing) {
                        throw "Existing scheduled task remained before PreserveMove: $taskName"
                    }
                    $receipt.rollback.pre_move_tasks_removed += $taskName
                    $removedTaskIndex++
                    if ([Environment]::GetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT') -eq "after-preserve-task-$removedTaskIndex") {
                        throw "injected PreserveMove fault after task $removedTaskIndex"
                    }
                }
            }
            catch {
                $preMoveError = $_.Exception.Message
                if ($preMoveTaskIdentity.Count -gt 0) {
                    try {
                        Restore-PreMoveTaskAdmission -DisabledTaskIdentity $preMoveTaskAdmissionIdentity -OriginalTaskIdentity $preMoveTaskIdentity -TaskPath $taskPath
                    }
                    catch {
                        throw "PreserveMove task admission restore failed: $($_.Exception.Message); original error: $preMoveError"
                    }
                }
                throw $preMoveError
            }
            Assert-ExistingEnvPreimage -Path $existingEnvPath -Exists $existingEnvExisted -ExpectedSize $existingEnvSize -ExpectedSha256 $existingEnvHash
            $preMoveBackupInventory = Get-InstallInventory -Path $install
            $preMoveRootIdentity = Get-PathObjectIdentity -Path $install -RequireExisting
            $receipt.rollback.pre_move_root = [ordered]@{
                path = $install
                object_identity = $preMoveRootIdentity
                inventory = $preMoveBackupInventory
            }
            $receipt.rollback.pre_move_root_identity = $preMoveRootIdentity
            Write-InstallTransactionJournal -State 'predecessor-sealed' | Out-Null
            # Use the .NET directory move so a concurrently-created destination
            # cannot turn the backup into a nested, unowned directory.
            # Legacy contract: Move-Item -LiteralPath $install -Destination $backupRoot
            Write-InstallTransactionJournal -State 'predecessor-move-started' | Out-Null
            [System.IO.Directory]::Move($install, $backupRoot)
            $rootMovedToBackup = $true
            $backupRootOwnedByRun = $true
            $backupRootIdentity = Get-PathObjectIdentity -Path $backupRoot -RequireExisting
            if ([string]$backupRootIdentity -cne [string]$preMoveRootIdentity) {
                throw 'PreserveMove changed the filesystem object identity of the moved install root.'
            }
            # Capture the rollback identity immediately after PreserveMove,
            # before path-boundary or post-move inventory checks can fail.
            $backupInventory = [pscustomobject]@{
                root = $backupRootCanonical
                file_count = $preMoveBackupInventory.file_count
                total_bytes = $preMoveBackupInventory.total_bytes
                inventory_sha256 = $preMoveBackupInventory.inventory_sha256
            }
            $receipt.rollback.backup_root = $backupRoot
            $receipt.backup_inventory = $backupInventory
            $receipt.backup_identity = [ordered]@{
                schema_version = 'hwpx/windows-install-backup/v1'
                run_id = $runId
                original_root = $install
                backup_root = $backupRootCanonical
                object_identity = $backupRootIdentity
                inventory_sha256 = $backupInventory.inventory_sha256
                file_count = $backupInventory.file_count
                total_bytes = $backupInventory.total_bytes
                pre_move_inventory = $true
                verified_after_move = $false
            }
            if (-not (Test-Path -LiteralPath $backupRoot -PathType Container) -or (Test-Path -LiteralPath $install -PathType Container)) {
                throw 'PreserveMove did not produce the exact claimed backup root.'
            }
            Assert-PathObjectIdentity -Path $backupRoot -ExpectedIdentity $backupRootIdentity | Out-Null
            $movedBackupInventory = Get-InstallInventory -Path $backupRoot
            foreach ($field in @('file_count', 'total_bytes', 'inventory_sha256')) {
                if ([string]$movedBackupInventory.$field -ne [string]$backupInventory.$field) {
                    throw "PreserveMove backup inventory changed for ${field}: $backupRoot"
                }
            }
            $backupInventory = $movedBackupInventory
            $receipt.backup_inventory = $backupInventory
            $receipt.backup_identity.backup_root = $backupInventory.root
            $receipt.backup_identity.inventory_sha256 = $backupInventory.inventory_sha256
            $receipt.backup_identity.file_count = $backupInventory.file_count
            $receipt.backup_identity.total_bytes = $backupInventory.total_bytes
            $receipt.backup_identity.verified_after_move = $true
            Write-InstallTransactionJournal -State 'root-moved-to-backup' | Out-Null
            Write-InstallTransactionJournal -State 'backup-claim-removing' | Out-Null
            Remove-RunPathClaim -ClaimPath $backupClaimPath -ExpectedObjectIdentity $backupClaimIdentity
            $backupClaimPath = $null
        }
    }
    if (-not $snapshotOwnedByRun) {
        Seal-InstallerSnapshot
        Write-InstallTransactionJournal -State 'snapshot-sealed' | Out-Null
    }
    if ($rootExisted -and [Environment]::GetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT') -eq 'after-existing-root-compatibility') {
        throw 'injected installer fault after existing-root compatibility preflight'
    }
    $requirements = $null
    $dependencyLocked = $false
    if (-not $reused) {
        if (-not $inPlace) {
            $candidateRoot = "$install.candidate-$([Guid]::NewGuid().ToString('N'))"
            if (Test-Path -LiteralPath $candidateRoot) { throw "Candidate root already exists: $candidateRoot" }
            Write-InstallTransactionJournal -State 'candidate-creating' | Out-Null
            New-Item -ItemType Directory -Path $candidateRoot -ErrorAction Stop | Out-Null
            $candidateRootOwnedByRun = $true
            $candidateRootIdentity = Get-PathObjectIdentity -Path $candidateRoot -RequireExisting
            Write-InstallTransactionJournal -State 'candidate-created' | Out-Null
            Write-InstallTransactionJournal -State 'candidate-copy-started' | Out-Null
            $verifiedCopies = @(Copy-SourceToCandidate -Destination $candidateRoot -ManifestResult $manifestResult)
            $verifiedManifestCopy = Copy-ManifestToCandidate -ManifestPath $manifestPath -Destination $candidateRoot
            $candidateManifestPath = Join-Path $candidateRoot 'source-manifest.json'
            $candidateManifestResult = Get-SourceManifest -SourceRoot $candidateRoot -ManifestPath $candidateManifestPath -ExpectedRepository ([string]$manifestResult.manifest.repository) -ExpectedCommit ([string]$manifestResult.manifest.commit) -ExpectedTree ([string]$manifestResult.manifest.tree) -ExpectedManifestSha256 ([string]$manifestResult.manifest_sha256)
            if (-not $candidateManifestResult.ok -or [string]$candidateManifestResult.manifest_sha256 -ne [string]$manifestResult.manifest_sha256) {
                throw 'Candidate source copy failed manifest/hash re-verification before activation.'
            }
            $receipt.checks.candidate_copy = [pscustomobject]@{
                file_count = $verifiedCopies.Count
                source_bytes = [int64](($verifiedCopies | Measure-Object -Property source_size -Sum).Sum)
                destination_bytes = [int64](($verifiedCopies | Measure-Object -Property destination_size -Sum).Sum)
                manifest_copy = $verifiedManifestCopy
                manifest_reverification = $candidateManifestResult
            }
        }
        else {
            New-Item -ItemType Directory -Path $candidateRoot -ErrorAction Stop | Out-Null
            $candidateRootOwnedByRun = $true
            $candidateRootIdentity = Get-PathObjectIdentity -Path $candidateRoot -RequireExisting
        }
        $receipt.candidate_root = $candidateRoot
        $venvPython = Join-Path $candidateRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            Write-InstallTransactionJournal -State 'venv-creating' | Out-Null
            Invoke-InstallerNative -FilePath $python.path -Arguments (@($python.prefix) + @('-m', 'venv', '.venv')) -WorkingDirectory $candidateRoot | Out-Null
            Write-InstallTransactionJournal -State 'venv-created' | Out-Null
        }
        $lockFile = Join-Path $candidateRoot 'requirements-windows.lock'
        if (-not (Test-Path -LiteralPath $lockFile -PathType Leaf)) { throw "Hash-pinned Windows dependency lock is missing: $lockFile" }
        $dependencyLocked = $true
        $requirements = $lockFile
        $pipArguments = @('-m', 'pip', 'install', '--disable-pip-version-check')
        $pipArguments += '--require-hashes'
        $pipArguments += @('-r', $requirements)
        $receipt.checks.dependencies = [pscustomobject]@{
            mode = 'install'
            requirements_path = $requirements
            requirements_sha256 = Get-Sha256Hex -Path $requirements
            locked = $dependencyLocked
            require_hashes = $dependencyLocked
        }
        Write-InstallTransactionJournal -State 'dependency-install-started' | Out-Null
        Invoke-InstallerNative -FilePath $venvPython -Arguments $pipArguments -WorkingDirectory $candidateRoot | Out-Null
        Write-InstallTransactionJournal -State 'dependency-install-complete' | Out-Null
        $markerPayload = [ordered]@{
            schema_version = 'hwpx/windows-install-marker/v1'
            repository = $manifestResult.manifest.repository
            commit = $manifestResult.manifest.commit
            tree = $manifestResult.manifest.tree
            source_manifest_sha256 = Get-Sha256Hex -Path $manifestPath
            candidate_generation = '{0}:{1}:{2}' -f $manifestResult.manifest.commit, $manifestResult.manifest.tree, (Get-Sha256Hex -Path $manifestPath)
            installed_at_utc = [DateTime]::UtcNow.ToString('o')
        }
        Write-InstallTransactionJournal -State 'marker-writing' | Out-Null
        Write-JsonReceipt -Path (Join-Path $candidateRoot '.hwpx-install.json') -Value $markerPayload | Out-Null
        if (-not $inPlace) {
            if (Test-Path -LiteralPath $install) { throw "InstallRoot appeared after preflight; refusing unowned activation: $install" }
            Write-InstallTransactionJournal -State 'activation-root-creating' | Out-Null
            New-Item -ItemType Directory -Path $install -ErrorAction Stop | Out-Null
            $candidateInstallCreated = $true
            $installRootIdentity = Get-PathObjectIdentity -Path $install -RequireExisting
            Write-InstallTransactionJournal -State 'activation-root-created' | Out-Null
            $candidateRootOwnedByRun = $true
            # Copy the candidate only through the verified activation helper after
            # dependency checks have passed; no recursive Copy-Item activation.
            # The helper streams every file and records destination_sha256.
            Write-InstallTransactionJournal -State 'activation-copy-started' | Out-Null
            $activationCopy = Copy-CandidateToInstall -CandidateRoot $candidateRoot -Destination $install -ManifestResult $manifestResult
            $candidateManifestPath = Join-Path $install 'source-manifest.json'
            $candidate_manifest = Get-SourceManifest -SourceRoot $install -ManifestPath $candidateManifestPath -ExpectedRepository ([string]$manifestResult.manifest.repository) -ExpectedCommit ([string]$manifestResult.manifest.commit) -ExpectedTree ([string]$manifestResult.manifest.tree) -ExpectedManifestSha256 ([string]$manifestResult.manifest_sha256)
            if (-not $candidate_manifest.ok -or [string]$candidate_manifest.manifest_sha256 -ne [string]$manifestResult.manifest_sha256) {
                throw 'Activated install failed source manifest/hash re-verification.'
            }
            $receipt.checks.activation_copy = [ordered]@{
                candidate_root = [string]$activationCopy.candidate_root
                destination_root = [string]$activationCopy.destination_root
                file_count = [int]$activationCopy.file_count
                source_bytes = [int64]$activationCopy.source_bytes
                destination_bytes = [int64]$activationCopy.destination_bytes
                candidate_manifest = [ordered]@{
                    ok = [bool]$candidate_manifest.ok
                    manifest_sha256 = [string]$candidate_manifest.manifest_sha256
                    file_count = [int]$candidate_manifest.file_count
                    mismatch_count = [int]$candidate_manifest.mismatch_count
                }
            }
            $candidateInstallCreated = $true
            Write-InstallTransactionJournal -State 'candidate-temp-removing' | Out-Null
            Remove-RunOwnedRoot -Path $candidateRoot -ExpectedPath $candidateRoot -ExpectedObjectIdentity $candidateRootIdentity -OwnedByRun $true | Out-Null
            $candidateRoot = $install
            $candidateRootOwnedByRun = $true
            $candidateRootIdentity = $installRootIdentity
            Write-InstallTransactionJournal -State 'candidate-activated' | Out-Null
        }
    }
    else {
        $candidateRoot = $install
        $receipt.candidate_root = $candidateRoot
        $receipt.reused_existing_install = $true
        $lockFile = Join-Path $candidateRoot 'requirements-windows.lock'
        if (-not (Test-Path -LiteralPath $lockFile -PathType Leaf)) { throw "Hash-pinned Windows dependency lock is missing: $lockFile" }
        $dependencyLocked = $true
        $requirements = $lockFile
        $receipt.checks.dependencies = [pscustomobject]@{
            mode = 'reused'
            requirements_path = $requirements
            requirements_sha256 = Get-Sha256Hex -Path $requirements
            locked = $dependencyLocked
            require_hashes = $dependencyLocked
        }
    }

    $venvPython = Join-Path $candidateRoot '.venv\Scripts\python.exe'
    $requirements = Join-Path $candidateRoot 'requirements-windows.lock'
    $venvInvocation = New-PythonInvocation -Path $venvPython
    $receipt.checks.venv_python = Get-PythonRuntimeIdentity -Invocation $venvInvocation -WorkingDirectory $candidateRoot -Role 'venv'
    $receipt.checks.dependency_repair = [ordered]@{ attempted = $false; ok = $true; reason = 'not-needed' }
    try {
        Invoke-InstallerDependencyCheck -PythonPath $venvPython -LockPath $requirements -WorkingDirectory $candidateRoot | Out-Null
    }
    catch {
        if ($reused) { throw }
        # A candidate copy can preserve the lock and venv metadata while
        # omitting a package payload. Reinstall the exact hash-pinned lock in
        # the final install root before activation, then prove the imports.
        $repairArguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes', '--force-reinstall', '-r', $requirements)
        $receipt.checks.dependency_repair = [ordered]@{
            attempted = $true
            ok = $false
            reason = 'initial-final-runtime-completeness-check-failed'
            requirements_path = $requirements
            requirements_sha256 = Get-Sha256Hex -Path $requirements
        }
        Invoke-InstallerNative -FilePath $venvPython -Arguments $repairArguments -WorkingDirectory $candidateRoot | Out-Null
        Invoke-InstallerDependencyCheck -PythonPath $venvPython -LockPath $requirements -WorkingDirectory $candidateRoot | Out-Null
        $receipt.checks.dependency_repair.ok = $true
    }
    Save-InstallerReceipt

    $preservedEnvPath = if ($backupRoot) { Join-Path $backupRoot '.env' } else { $null }
    Write-InstallTransactionJournal -State 'config-writing' | Out-Null
    $configResult = Ensure-CandidateConfig -CandidateRoot $candidateRoot -PreservedEnvPath $preservedEnvPath -PreimageExists $existingEnvExisted -PreimageHash $existingEnvHash -PreimageSize $existingEnvSize -PopplerPath ([string]$poppler.path) -ApiPort $apiPort -ApiTaskName $apiTaskName -WorkerTaskName $workerTaskName
    $receipt.checks.config = $configResult
    Write-InstallTransactionJournal -State 'config-created' | Out-Null
    $configCreated = -not [bool]$configResult.candidate_env_existed_before
    $configPath = [string]$configResult.env_path
    $configCreatedSha256 = if ($configCreated -and (Test-Path -LiteralPath $configPath -PathType Leaf)) { Get-Sha256Hex -Path $configPath } else { $null }
    $runtimeEnvProvenance = Set-InstallerRuntimeEnvProvenance -InstallRoot $candidateRoot -ManifestResult $manifestResult -ConfigResult $configResult -PreserveExistingMarkerBytes:$reused
    $receipt.checks.runtime_env_provenance = $runtimeEnvProvenance

    $phase = 'activation'
    $venvPython = Join-Path $candidateRoot '.venv\Scripts\python.exe'
    $postMovePort = Assert-PortAvailable -Port $apiPort -ExpectedRoot $candidateRoot -ExpectedTaskName $apiTaskName -ExpectedTaskPath $taskPath
    $receipt.checks.post_move_port = $postMovePort
    if (-not $reused -and -not [bool]$postMovePort.available) {
        throw "Candidate activation port $apiPort remained occupied after predecessor move."
    }
    $taskActionApi = New-ScheduledTaskAction -Execute $venvPython -Argument '-m app.api_server' -WorkingDirectory $candidateRoot
    $taskActionWorker = New-ScheduledTaskAction -Execute $venvPython -Argument '-m app.worker' -WorkingDirectory $candidateRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $interactive.name
    $principal = New-ScheduledTaskPrincipal -UserId $interactive.name -LogonType Interactive -RunLevel Limited
    # Keep the persisted settings equivalent to Test-CanonicalTaskSettings.
    # PowerShell's defaults otherwise serialize battery restrictions as true.
    $taskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable:$false `
        -Hidden:$false `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5)
    Write-InstallTransactionJournal -State 'tasks-registering' | Out-Null
    Register-ScheduledTask -TaskName $apiTaskName -TaskPath $taskPath -Action $taskActionApi -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
    Write-InstallTransactionJournal -State 'api-task-registered' | Out-Null
    Register-ScheduledTask -TaskName $workerTaskName -TaskPath $taskPath -Action $taskActionWorker -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
    Write-InstallTransactionJournal -State 'tasks-registered' | Out-Null
    if ([Environment]::GetEnvironmentVariable('HWPX_TEST_INSTALL_FAULT') -eq 'after-task-registrations') {
        throw 'injected installer fault after both scheduled task registrations'
    }
    $receipt.task_identities_after = @(
        (Assert-TaskIdentity -Identity (Get-ScheduledTaskIdentity -TaskName $apiTaskName -TaskPath $taskPath) -ExpectedRoot $candidateRoot -ExpectedPython $venvPython -ExpectedArguments '-m app.api_server' -ExpectedPrincipal $interactive.name -ExpectedApiPort $apiPort -ExpectedTaskPath $taskPath),
        (Assert-TaskIdentity -Identity (Get-ScheduledTaskIdentity -TaskName $workerTaskName -TaskPath $taskPath) -ExpectedRoot $candidateRoot -ExpectedPython $venvPython -ExpectedArguments '-m app.worker' -ExpectedPrincipal $interactive.name -ExpectedApiPort $apiPort -ExpectedTaskPath $taskPath)
    )
    # Do not reuse the pre-move listener result here.  PreserveMove may have
    # stopped/moved the predecessor while the old port receipt still says it
    # was compatible.  Each exact candidate role gets an independent
    # post-registration state check and start decision.
    $taskActivation = @()
    foreach ($roleTask in @(
        [pscustomobject]@{ role = 'api'; name = $apiTaskName },
        [pscustomobject]@{ role = 'worker'; name = $workerTaskName }
    )) {
        $roleIdentity = Get-ScheduledTaskIdentity -TaskName $roleTask.name -TaskPath $taskPath
        if (-not [bool]$roleIdentity.exists) { throw "Post-registration scheduled task is missing: $($roleTask.name)" }
        $roleTaskObject = Get-ScheduledTaskExact -TaskName $roleTask.name -TaskPath $taskPath
        $startedRole = $false
        if ([string]$roleTaskObject.State -ne 'Running') {
            Start-ScheduledTask -TaskName $roleTask.name -TaskPath $taskPath -ErrorAction Stop
            $startedRole = $true
            if (-not (Wait-ScheduledTaskRunning -TaskName $roleTask.name -TaskPath $taskPath)) {
                throw "Post-registration scheduled task did not reach Running: $($roleTask.name)"
            }
        }
        $postIdentity = Get-ScheduledTaskIdentity -TaskName $roleTask.name -TaskPath $taskPath
        if ([string]$postIdentity.state -ne 'Running' -or -not [bool]$postIdentity.enabled) {
            throw "Post-registration scheduled task state is not active: $($roleTask.name)"
        }
        $taskActivation += [ordered]@{
            role = $roleTask.role
            task_name = $roleTask.name
            task_path = $taskPath
            started = $startedRole
            state = [string]$postIdentity.state
            enabled = [bool]$postIdentity.enabled
            task_identity_hash = [string]$postIdentity.task_identity_hash
        }
    }
    $receipt.checks.task_activation = @($taskActivation)
    $verifyScript = Join-Path $candidateRoot 'scripts\verify_windows.ps1'
    if (-not (Test-Path -LiteralPath $verifyScript -PathType Leaf)) { throw "Installed verifier is missing: $verifyScript" }
    # The independent verifier rejects receipts under an existing InstallRoot
    # so that verification cannot mutate the installed tree. Keep this
    # caller-owned receipt in the OS temp directory and retain the path in the
    # installer receipt for bounded operator diagnostics.
    $verifyReceipt = Join-Path ([System.IO.Path]::GetTempPath()) ('hwpx-verify-' + [Guid]::NewGuid().ToString('N') + '.json')
    if (Test-CanonicalPathWithinRoot -Path $verifyReceipt -Root $candidateRoot) {
        throw 'Could not allocate an external verifier receipt outside the candidate InstallRoot.'
    }
    $receipt.verification_receipt_path = $verifyReceipt
    $verifierRunId = $runId + ':verify'
    $verifyArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $verifyScript, '-InstallRoot', $candidateRoot, '-ReceiptPath', $verifyReceipt, '-RunId', $verifierRunId, '-ApiPort', $apiPort, '-ExpectedRepository', [string]$manifestResult.manifest.repository, '-ExpectedCommit', [string]$manifestResult.manifest.commit, '-ExpectedTree', [string]$manifestResult.manifest.tree, '-ExpectedManifestSha256', [string]$manifestResult.manifest_sha256)
    if ($FixturePath) { $verifyArgs += @('-FixturePath', (Get-CanonicalPath -Path $FixturePath -RequireExisting)) }
    if ($PopplerPath) { $verifyArgs += @('-PopplerPath', (Get-CanonicalPath -Path $PopplerPath -RequireExisting)) }
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $verifierRoot = Get-CanonicalPath -Path $candidateRoot -RequireExisting
    $verifierRootIdentity = Get-PathObjectIdentity -Path $verifierRoot -RequireExisting
    $receipt.verifier_handoff = [ordered]@{
        active = $true
        root = $verifierRoot
        root_identity = $verifierRootIdentity
        owner_process_id = [int]$PID
        owner_process_start_identity = Get-ProcessGenerationIdentity -ProcessId $PID
        verifier_run_id = $verifierRunId
    }
    Write-InstallTransactionJournal -State 'verifier-handoff-started' | Out-Null
    Save-InstallerReceipt | Out-Null
    Suspend-InstallerLifecycleLockForVerifier -VerifierRoot $verifierRoot | Out-Null
    $verification = $null
    $verificationFailure = $null
    try {
        $verification = Invoke-InstallerNative -FilePath $powershell -Arguments $verifyArgs -WorkingDirectory $verifierRoot
    }
    catch {
        $verificationFailure = $_.Exception.Message
    }
    finally {
        Resume-InstallerLifecycleLockAfterVerifier -VerifierRoot $verifierRoot -TaskNames @($apiTaskName, $workerTaskName) -ApiPort ([int]$apiPort) | Out-Null
    }
    if ($verificationFailure) { throw $verificationFailure }
    $receipt.verifier_handoff.active = $false
    $receipt.verification = $verification
    if (-not $verification.accepted) { throw "Installed verifier failed with exit code $($verification.exit_code)." }
    if (-not (Test-Path -LiteralPath $verifyReceipt -PathType Leaf)) {
        throw 'Installed verifier did not leave its declared terminal receipt.'
    }
    Assert-PathObjectIdentity -Path $verifierRoot -ExpectedIdentity $verifierRootIdentity | Out-Null
    $closingMarkerPath = Join-Path $verifierRoot '.hwpx-install.json'
    $closingMarkerCapture = Read-BoundedJsonObject -Path $closingMarkerPath -MaxBytes 4194304
    $closingMarker = $closingMarkerCapture.value
    $closingRuntimeEnvContract = $null
    if ($closingMarker.PSObject.Properties.Name -contains 'runtime_env') {
        $closingRuntimeEnvContract = $closingMarker.runtime_env
    }
    $closingManifest = Get-SourceManifest -SourceRoot $verifierRoot -ManifestPath (Join-Path $verifierRoot 'source-manifest.json') -ExpectedRepository ([string]$manifestResult.manifest.repository) -ExpectedCommit ([string]$manifestResult.manifest.commit) -ExpectedTree ([string]$manifestResult.manifest.tree) -ExpectedManifestSha256 ([string]$manifestResult.manifest_sha256) -ExpectedRuntimeEnvContract $closingRuntimeEnvContract
    if (-not $closingManifest.ok -or [string]$closingManifest.manifest_sha256 -ne [string]$receipt.source_identity.manifest_sha256) {
        throw 'Installer closing source-manifest readback did not match the verifier handoff generation.'
    }
    if ([string]$closingMarker.commit -cne [string]$receipt.source_identity.commit -or
        [string]$closingMarker.tree -cne [string]$receipt.source_identity.tree -or
        [string]$closingMarker.source_manifest_sha256 -cne [string]$receipt.source_identity.manifest_sha256 -or
        [string]$closingMarker.candidate_generation -cne [string]$receipt.candidate_generation) {
        throw 'Installer closing candidate marker readback did not match the verifier handoff generation.'
    }
    $receipt.verifier_handoff.closing_root_identity = Get-PathObjectIdentity -Path $verifierRoot -RequireExisting
    $receipt.verifier_handoff.closing_candidate_generation = [string]$closingMarker.candidate_generation
    $receipt.verifier_handoff.closing_manifest_sha256 = [string]$closingManifest.manifest_sha256
    if ([string]$receipt.verifier_handoff.closing_root_identity -cne [string]$verifierRootIdentity) {
        throw 'Installer closing root identity changed after verifier lock handoff.'
    }
    $verifyReceiptCapture = Read-BoundedJsonObject -Path $verifyReceipt -MaxBytes 4194304
    $verifyReceiptObject = $verifyReceiptCapture.value
    if ([string]$verifyReceiptObject.schema_version -cne 'hwpx/windows-verify/v1' -or
        [string]$verifyReceiptObject.run_id -cne $verifierRunId -or
        [string]$verifyReceiptObject.candidate_generation -cne [string]$receipt.candidate_generation -or
        [string]$verifyReceiptObject.status -notlike 'PASS*' -or
        [int]$verifyReceiptObject.status_code -ne 0) {
        throw 'Installed verifier terminal receipt did not match the current candidate, run, and PASS contract.'
    }
    $receipt.verification_receipt = [ordered]@{
        path = (Get-CanonicalPath -Path $verifyReceipt -RequireExisting)
        bytes = [int64]$verifyReceiptCapture.bytes
        sha256 = [string]$verifyReceiptCapture.sha256
        object_identity = [string]$verifyReceiptCapture.object_identity
        run_id = [string]$verifyReceiptObject.run_id
        status = [string]$verifyReceiptObject.status
        status_code = [int]$verifyReceiptObject.status_code
        candidate_generation = [string]$verifyReceiptObject.candidate_generation
        readback_validated = $true
    }
    if ($FixturePath) {
        $receipt.fixture.sha256_after = Get-Sha256Hex -Path ([string]$receipt.fixture.path)
        if ($receipt.fixture.sha256_after -ne $receipt.fixture.sha256_before) { throw 'Fixture changed during installer verification.' }
    }
    $receipt.status = if ($FixturePath) { 'PASS' } else { 'PASS_RUNTIME_ONLY' }
    $receipt.status_code = 0
    $receipt.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $terminalReceiptOk = Complete-InstallerTerminalReceipt -RemoveSnapshot:$false
    Exit-InstallLifecycleLock -Lock $installRootLock
    Write-InstallerTerminalSummary
    if ($terminalReceiptOk) { exit 0 }
    exit 99
}
catch {
    $message = $_.Exception.Message
    if ($script:verifierHandoffActive) {
        try {
            Resume-InstallerLifecycleLockAfterVerifier -VerifierRoot $script:verifierHandoffRoot -TaskNames @($taskNames) -ApiPort ([int]$apiPort) | Out-Null
            if ($receipt.verifier_handoff) { $receipt.verifier_handoff.active = $false }
        }
        catch {
            $message += '; verifier lifecycle lock reacquisition failed: ' + $_.Exception.Message
        }
    }
    $receipt.errors = @($receipt.errors) + $message
    if ($dependencyMutationAttempted) {
        $dependencyMutationRetained = $true
        if ($receipt.checks.Contains('dependency_phase')) {
            $receipt.checks.dependency_phase.status = 'FAILED_EXTERNAL_MUTATION_RETAINED'
            $receipt.checks.dependency_phase.error = Limit-Text -Value $message -MaxChars 4096
            $receipt.checks.dependency_phase.retained_external_mutation = $true
        }
    }
    if (-not $script:receiptPersistenceFailed) {
        $dependencyFailure = $dependencyMutationAttempted -and ($phase -in @('preflight', 'dependency'))
        $failureStatus = if ($dependencyFailure) { 'FAIL_DEPENDENCY' } elseif ($phase -eq 'preflight') { 'FAIL_PREFLIGHT' } elseif ($phase -eq 'install') { 'FAIL_INSTALL' } else { 'FAIL_ACTIVATION' }
        $failureCode = if ($dependencyFailure) { 15 } elseif ($phase -eq 'preflight') { 10 } elseif ($phase -eq 'install') { 20 } else { 30 }
        $receipt.failure_class = $failureStatus
        $receipt.status = $failureStatus
        $receipt.status_code = $failureCode
    }
    else {
        $receipt.status = 'FAIL_RECEIPT'
        $receipt.failure_class = 'FAIL_RECEIPT'
        $receipt.status_code = 99
    }
    if ($install -and ($snapshotPath -or $backupRoot -or $candidateInstallCreated)) {
        try { Write-InstallTransactionJournal -State 'rollback-started' | Out-Null } catch { $receipt.errors = @($receipt.errors) + $_.Exception.Message }
    }
    $receipt.rollback.attempted = ($null -ne $receipt.snapshot_path -or $null -ne $backupRoot -or $candidateInstallCreated)
    $rollbackErrors = @()
    $rollbackActions = 0
    $rollbackTaskRestoreReady = $true
    $snapshotData = $null
    $preservedProcessIdentities = @()
    if ($receipt.snapshot_path -and (Test-Path -LiteralPath $receipt.snapshot_path)) {
        try {
            $snapshotData = Read-VerifiedInstallSnapshot -SnapshotPath $receipt.snapshot_path -ExpectedSnapshotSha256 $snapshotSha256 -ExpectedSnapshotIdentity $snapshotIdentity -ExpectedRunId $runId
            if ($snapshotData.PSObject.Properties.Name -contains 'processes') {
                $preservedProcessIdentities = @($snapshotData.processes)
            }
        }
        catch {
            $rollbackErrors += "Rollback snapshot could not be read: $($_.Exception.Message)"
            $rollbackTaskRestoreReady = $false
        }
    }
    # PreserveMove must put the old root back at its final path before task
    # definitions or running state are restored. A quarantine keeps the new
    # candidate recoverable if any step of the swap fails.
    if ($backupRoot -and (Test-Path -LiteralPath $backupRoot) -and -not $backupRootOwnedByRun) {
        $rollbackErrors += "PreserveMove backup exists without current-run ownership and was retained: $backupRoot"
        $receipt.rollback.backup_unowned_retained = $backupRoot
        $rollbackTaskRestoreReady = $false
    }
    if ($backupRootOwnedByRun -and $backupRoot -and (Test-Path -LiteralPath $backupRoot)) {
        try {
            if ($null -eq $backupInventory) {
                throw 'PreserveMove backup inventory is missing; refusing rollback activation.'
            }
            Assert-BackupOwnership -BackupPath $backupRoot -ExpectedPath $backupRoot -ExpectedInventory $backupInventory -ExpectedObjectIdentity $backupRootIdentity -OwnedByRun $backupRootOwnedByRun | Out-Null
            Assert-NoReparsePath -Path (Split-Path -Parent $install) | Out-Null
            $releasedRoots = @()
            foreach ($rootToRelease in @($candidateRoot, $install)) {
                if (-not $rootToRelease -or -not (Test-Path -LiteralPath $rootToRelease -PathType Container)) { continue }
                if ([string]$rootToRelease -eq [string]$install -and -not $candidateInstallCreated) {
                    throw 'InstallRoot appeared without current-run ownership during PreserveMove rollback; refusing to inspect or stop its processes.'
                }
                $canonicalRootToRelease = Get-CanonicalPath -Path $rootToRelease -RequireExisting
                if ($releasedRoots -contains $canonicalRootToRelease) { continue }
                # Preserve the caller's ownership observation across the
                # rollback coordination gap; the helper revalidates identity
                # before any process mutation.
                $preSwapInitialProcessSnapshot = @(Get-InstallProcessSnapshot -RootPath $canonicalRootToRelease -ExpectedPythonPath (Join-Path $canonicalRootToRelease '.venv\Scripts\python.exe'))
                $preSwapRelease = Stop-InstallProcesses -RootPath $canonicalRootToRelease -PreserveProcessIds @() -InitialProcessSnapshot $preSwapInitialProcessSnapshot
                $releasedRoots += $canonicalRootToRelease
                $receipt.rollback.pre_swap_process_release = @($receipt.rollback.pre_swap_process_release) + $preSwapRelease
                if (-not $preSwapRelease.ok -or @($preSwapRelease.remaining).Count -gt 0) {
                    throw 'Candidate and previous-install process handles were not fully released before PreserveMove rollback.'
                }
            }
            if ($snapshotData -and $snapshotData.PSObject.Properties.Name -contains 'tasks') {
                $receipt.rollback.candidate_tasks_removed = @()
                foreach ($task in @($snapshotData.tasks)) {
                    if (-not $task.task_name) { continue }
                    $taskName = Assert-SafeScheduledTaskName -TaskName ([string]$task.task_name)
                    $taskPathForRollback = Assert-CanonicalScheduledTaskPath -TaskPath ([string]$task.task_path)
                    $currentTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPathForRollback -AllowMissing
                    if ($currentTask) {
                        $taskExpectedArguments = if ($taskName -eq [string]$apiTaskName) { '-m app.api_server' } elseif ($taskName -eq [string]$workerTaskName) { '-m app.worker' } else { $null }
                        $taskExpectedRoot = if ($candidateRoot -and (Test-Path -LiteralPath $candidateRoot -PathType Container)) { $candidateRoot } else { $install }
                        $taskExpectedPython = Join-Path $taskExpectedRoot '.venv\Scripts\python.exe'
                        if ([string]::IsNullOrWhiteSpace($taskExpectedArguments) -or -not (Test-Path -LiteralPath $taskExpectedPython -PathType Leaf)) {
                            throw "Cannot establish current-run task identity for removal: $taskName"
                        }
                        Assert-RunOwnedTaskIdentity -Identity (Get-ScheduledTaskIdentity -TaskName $taskName -TaskPath $taskPathForRollback) -ExpectedRoot $taskExpectedRoot -ExpectedPython $taskExpectedPython -ExpectedArguments $taskExpectedArguments -ExpectedPrincipal $interactive.name -ExpectedApiPort $apiPort | Out-Null
                    }
                    if ($currentTask -and [string]$currentTask.State -eq 'Running') {
                        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPathForRollback -ErrorAction Stop
                        if (-not (Wait-ScheduledTaskInactive -TaskName $taskName -TaskPath $taskPathForRollback)) {
                            throw "Candidate scheduled task remained active before rollback root swap: $taskName"
                        }
                    }
                    if ($currentTask) {
                        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPathForRollback -Confirm:$false -ErrorAction Stop
                        if (Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPathForRollback -AllowMissing) {
                            throw "Candidate scheduled task remained after removal: $taskName"
                        }
                        $receipt.rollback.candidate_tasks_removed += $taskName
                    }
                }
            }
            if (Test-Path -LiteralPath $install -PathType Container) {
                if (-not $candidateInstallCreated -or -not $candidateRootOwnedByRun) {
                    throw 'Refusing to quarantine an install root that was not created and owned by this run.'
                }
                Assert-PathObjectIdentity -Path $install -ExpectedIdentity $installRootIdentity | Out-Null
                $quarantineBase = "$install.failed-$([Guid]::NewGuid().ToString('N'))"
                $rollbackCandidateQuarantine = $quarantineBase
                $rollbackQuarantineClaimPath = New-RunPathClaim -Path $rollbackCandidateQuarantine
                $rollbackQuarantineClaimIdentity = Get-PathObjectIdentity -Path $rollbackQuarantineClaimPath -RequireExisting
                # Legacy contract: Move-Item -LiteralPath $install -Destination $rollbackCandidateQuarantine
                [System.IO.Directory]::Move($install, $rollbackCandidateQuarantine)
                $rollbackCandidateQuarantineOwnedByRun = $true
                $rollbackCandidateQuarantineIdentity = Get-PathObjectIdentity -Path $rollbackCandidateQuarantine -RequireExisting
                if (-not (Test-Path -LiteralPath $rollbackCandidateQuarantine -PathType Container) -or (Test-Path -LiteralPath $install -PathType Container)) {
                    throw 'Rollback candidate quarantine did not produce the exact claimed root.'
                }
                Assert-PathObjectIdentity -Path $rollbackCandidateQuarantine -ExpectedIdentity $rollbackCandidateQuarantineIdentity | Out-Null
                Remove-RunPathClaim -ClaimPath $rollbackQuarantineClaimPath -ExpectedObjectIdentity $rollbackQuarantineClaimIdentity
                $rollbackQuarantineClaimPath = $null
            }
            try {
                # Legacy contract: Move-Item -LiteralPath $backupRoot -Destination $install
                Assert-BackupOwnership -BackupPath $backupRoot -ExpectedPath $backupRoot -ExpectedInventory $backupInventory -ExpectedObjectIdentity $backupRootIdentity -OwnedByRun $backupRootOwnedByRun | Out-Null
                [System.IO.Directory]::Move($backupRoot, $install)
            }
            catch {
                if ($rollbackCandidateQuarantineOwnedByRun -and $rollbackCandidateQuarantine -and (Test-Path -LiteralPath $rollbackCandidateQuarantine) -and -not (Test-Path -LiteralPath $install)) {
                    [System.IO.Directory]::Move($rollbackCandidateQuarantine, $install)
                }
                throw
            }
            Assert-PathObjectIdentity -Path $install -ExpectedIdentity $backupRootIdentity | Out-Null
            $rollbackBackupActivated = $true
            $candidateRoot = $install
            $candidateRootIdentity = $backupRootIdentity
            $installRootIdentity = $backupRootIdentity
            $receipt.rollback.backup_restored = $true
            $receipt.rollback.active_root = Get-CanonicalPath -Path $install -RequireExisting
            $candidateRootOwnedByRun = $false
            $rollbackActions++
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.backup_restore_error = $_.Exception.Message
            $rollbackTaskRestoreReady = $false
        }
    }
    if ($receipt.snapshot_path -and (Test-Path -LiteralPath $receipt.snapshot_path) -and $rollbackTaskRestoreReady -and ($rootMovedToBackup -or $candidateInstallCreated -or $reused -or $backupRoot)) {
        $rollbackRoot = if ($rootMovedToBackup -or $candidateInstallCreated -or $reused) { $candidateRoot } else { $null }
        try {
            $keepRollbackRoot = $reused -or $rollbackBackupActivated
            $restoreResult = Restore-InstallSnapshot -SnapshotPath $receipt.snapshot_path -ExpectedSnapshotSha256 $snapshotSha256 -ExpectedSnapshotIdentity $snapshotIdentity -ExpectedRunId $runId -CandidateRoot $rollbackRoot -ExpectedCandidateRootIdentity $candidateRootIdentity -KeepCandidateRoot:$keepRollbackRoot -CandidateRootOwnedByRun:($candidateRootOwnedByRun -and -not $reused) -RestoreTasks -Confirm:$false
            $receipt.rollback.processes_released = [bool]$restoreResult.processes_released
            $receipt.rollback.process_release = $restoreResult.process_release
            if (-not $restoreResult.processes_released) {
                throw 'Candidate process handles were not fully released during rollback.'
            }
            $rollbackActions++
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.task_restore_error = $_.Exception.Message
        }
    }
    if ($rollbackCandidateQuarantine -and (Test-Path -LiteralPath $rollbackCandidateQuarantine) -and -not $rollbackCandidateQuarantineOwnedByRun) {
        $rollbackErrors += "Rollback candidate quarantine exists without current-run ownership and was retained: $rollbackCandidateQuarantine"
        $receipt.rollback.candidate_quarantine_unowned_retained = $rollbackCandidateQuarantine
    }
    if ($rollbackCandidateQuarantineOwnedByRun -and $rollbackCandidateQuarantine -and (Test-Path -LiteralPath $rollbackCandidateQuarantine)) {
        if ($rollbackBackupActivated -and $rollbackErrors.Count -eq 0) {
            try {
                Remove-RunOwnedRoot -Path $rollbackCandidateQuarantine -ExpectedPath $rollbackCandidateQuarantine -ExpectedObjectIdentity $rollbackCandidateQuarantineIdentity -OwnedByRun $rollbackCandidateQuarantineOwnedByRun | Out-Null
                $receipt.rollback.candidate_quarantine_removed = $true
                $rollbackActions++
            }
            catch {
                $rollbackErrors += $_.Exception.Message
                $receipt.rollback.candidate_quarantine_error = $_.Exception.Message
            }
        }
        else {
            $receipt.rollback.candidate_quarantine_retained = $rollbackCandidateQuarantine
        }
    }
    if (-not $reused -and -not $rollbackBackupActivated -and $candidateRootOwnedByRun -and $candidateRoot -and (Test-Path -LiteralPath $candidateRoot)) {
        $rollbackActions++
        try {
            Remove-RunOwnedRoot -Path $candidateRoot -ExpectedPath $candidateRoot -ExpectedObjectIdentity $candidateRootIdentity -OwnedByRun $candidateRootOwnedByRun | Out-Null
            $receipt.rollback.candidate_cleanup = $true
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.candidate_cleanup_error = $_.Exception.Message
        }
    }
    if ($candidateInstallCreated -and $candidateRootOwnedByRun -and -not $rollbackBackupActivated -and (Test-Path -LiteralPath $install)) {
        $rollbackActions++
        try {
            # Legacy contract: Remove-Item -LiteralPath (Get-CanonicalPath -Path $install ...)
            Remove-RunOwnedRoot -Path $install -ExpectedPath $install -ExpectedObjectIdentity $installRootIdentity -OwnedByRun $candidateRootOwnedByRun | Out-Null
            $receipt.rollback.partial_install_removed = $true
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.partial_install_error = $_.Exception.Message
        }
    }
    if ($reused -and $configCreated -and $configPath -and (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        $rollbackActions++
        try {
            Assert-NoReparsePath -Path $configPath | Out-Null
            if ([string]::IsNullOrWhiteSpace($configCreatedSha256) -or (Get-Sha256Hex -Path $configPath) -ne $configCreatedSha256) {
                throw "Created config preimage changed before rollback cleanup: $configPath"
            }
            Remove-Item -LiteralPath $configPath -Force
            if (Test-Path -LiteralPath $configPath) { throw "Created config remained after rollback cleanup: $configPath" }
            $receipt.rollback.config_removed = $true
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.config_remove_error = $_.Exception.Message
        }
    }
    if ($backupRootOwnedByRun -and $backupRoot -and -not $rollbackBackupActivated -and (Test-Path -LiteralPath $backupRoot) -and -not (Test-Path -LiteralPath $install)) {
        $rollbackActions++
        try {
            Assert-BackupOwnership -BackupPath $backupRoot -ExpectedPath $backupRoot -ExpectedInventory $backupInventory -ExpectedObjectIdentity $backupRootIdentity -OwnedByRun $backupRootOwnedByRun | Out-Null
            # Legacy contract: Move-Item -LiteralPath $backupRoot -Destination $install
            [System.IO.Directory]::Move($backupRoot, $install)
            if (-not (Test-Path -LiteralPath $install -PathType Container) -or (Test-Path -LiteralPath $backupRoot -PathType Container)) {
                throw 'Fallback backup restoration did not preserve the exact root boundary.'
            }
            $backupRootOwnedByRun = $false
            $receipt.rollback.backup_restored = $true
        }
        catch {
            $rollbackErrors += $_.Exception.Message
            $receipt.rollback.backup_restore_error = $_.Exception.Message
        }
    }
    try { Remove-RunPathClaim -ClaimPath $backupClaimPath -ExpectedObjectIdentity $backupClaimIdentity } catch { $rollbackErrors += $_.Exception.Message }
    try { Remove-RunPathClaim -ClaimPath $rollbackQuarantineClaimPath -ExpectedObjectIdentity $rollbackQuarantineClaimIdentity } catch { $rollbackErrors += $_.Exception.Message }
    if ($rollbackActions -gt 0 -and $rollbackErrors.Count -eq 0) {
        $receipt.rollback.restored = $true
        $receipt.status = 'ROLLED_BACK'
        $receipt.status_code = 40
    }
    elseif ($rollbackErrors.Count -gt 0) {
        $receipt.rollback.restore_error = $rollbackErrors -join '; '
    }
    $receipt.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $terminalReceiptOk = Complete-InstallerTerminalReceipt -RemoveSnapshot:($rollbackErrors.Count -eq 0 -and -not $script:receiptPersistenceFailed)
    Exit-InstallLifecycleLock -Lock $installRootLock
    Write-InstallerTerminalSummary -ErrorMessage $message
    if ($terminalReceiptOk) { exit [int]$receipt.status_code }
    exit 99
}
