from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsInstallerContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_common_module_exposes_path_native_task_and_receipt_contracts(self) -> None:
        text = self.read("windows_install_common.psm1")
        for token in (
            "function Get-CanonicalPath",
            "function Invoke-NativeChecked",
            "function Get-SourceManifest",
            "function Get-ScheduledTaskIdentity",
            "function Write-JsonReceipt",
            "function Restore-InstallSnapshot",
            "Export-ModuleMember",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_native_capture_or_termination_errors_force_rejection(self) -> None:
        text = self.read("windows_install_common.psm1")
        self.assertIn("capture_error = [string]$captureError", text)
        self.assertIn("termination_error = [string]$terminationError", text)
        self.assertIn("processExitConfirmed", text)
        self.assertIn("exit_confirmed = [bool]$processExitConfirmed", text)
        self.assertIn("New-Object System.Text.UTF8Encoding($false, $true)", text)
        self.assertRegex(
            text,
            r"(?s)accepted\s*=\s*\(\(\$AcceptExitCodes\s+-contains\s+\$exitCode\).*?\$processExitConfirmed",
        )

    def test_receipt_fallback_synchronizes_live_failure_state(self) -> None:
        for name in ("install_windows.ps1", "verify_windows.ps1"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("$receipt.status = 'FAIL_RECEIPT'", text)
                self.assertIn("$receipt.failure_class = 'FAIL_RECEIPT'", text)
                self.assertIn("$receipt.status_code = 99", text)
                self.assertIn("receiptPersistenceFailed", text)

    def test_receipt_write_is_atomic_and_cleans_temporary_state(self) -> None:
        text = self.read("windows_install_common.psm1")
        self.assertIn("[System.IO.File]::Replace", text)
        self.assertIn("[System.IO.File]::Move", text)
        self.assertIn(".receipt-", text)
        self.assertIn("temporary receipt", text)
        self.assertIn("$backupPath = Join-Path $parent ('.receipt-backup-'", text)
        self.assertIn("[System.IO.File]::Replace($temporaryPath, $target, $backupPath)", text)
        receipt_function = text[text.index("function Write-JsonReceipt") : text.index("function Save-InstallSnapshot")]
        self.assertIn("ConvertFrom-Json", receipt_function)

    def test_transaction_journal_replacement_has_explicit_backup_on_windows_ps51(self) -> None:
        text = self.read("windows_install_common.psm1")
        journal_function = text[text.index("function Write-StableTransactionJournal") : text.index("function Read-StableTransactionJournal")]
        self.assertIn("$backupPath = Join-Path $parent ('.journal-backup-'", journal_function)
        self.assertIn("[System.IO.File]::Replace($temporaryPath, $target, $backupPath)", journal_function)
        self.assertNotIn("[System.IO.File]::Replace($temporaryPath, $target, $null)", journal_function)
        self.assertIn("$readbackValidated = $false", journal_function)
        self.assertIn("retain uncertain new bytes as HOLD evidence", journal_function)

    def test_predecessor_move_journal_seals_identity_for_crash_recovery(self) -> None:
        text = self.read("install_windows.ps1")
        journal_function = text[
            text.index("function Get-InstallerTransactionJournalPayload") :
            text.index("function Write-InstallTransactionJournal")
        ]
        recovery_function = text[
            text.index("function Invoke-StaleInstallTransactionRecovery") :
            text.index("function Save-InstallerReceipt")
        ]
        self.assertIn("pre_move_root_identity = [string]$preMoveRootIdentity", journal_function)
        self.assertIn("pre_move_inventory", journal_function)
        self.assertIn("pre_move_root_identity", recovery_function)
        self.assertIn("predecessor-move-started", recovery_function)
        self.assertIn("backup identity", recovery_function.lower())

    def test_candidate_activation_record_collection_is_linear_for_large_venvs(self) -> None:
        text = self.read("install_windows.ps1")
        activation_function = text[text.index("function Copy-CandidateToInstall {") : text.index("function Set-EnvSetting")]
        self.assertIn("System.Collections.Generic.List[object]", activation_function)
        self.assertIn("$records.Add($record)", activation_function)
        self.assertIn("Copy-FileVerified", activation_function)
        self.assertIn("files = $records.ToArray()", activation_function)
        self.assertNotIn("$records +=", activation_function)

    def test_candidate_activation_uses_identity_verified_same_volume_fast_path(self) -> None:
        text = self.read("install_windows.ps1")
        helper = text[text.index("function New-CandidateHardLinkVerified") : text.index("function Copy-CandidateToInstall")]
        tree_helper = text[text.index("function Copy-CandidateToInstallHardLinkTree") : text.index("function Copy-CandidateToInstall {")]
        activation = text[text.index("function Copy-CandidateToInstall {") : text.index("function Set-EnvSetting")]
        self.assertIn("New-Item -ItemType HardLink", helper)
        self.assertIn("source_object_identity", helper)
        self.assertIn("destination_object_identity", helper)
        self.assertIn("copy_mode = 'hardlink'", helper)
        self.assertIn("Get-InstallInventory -Path $destination -IncludeFiles", tree_helper)
        self.assertIn("copy_mode = 'hardlink-tree'", tree_helper)
        self.assertIn("Hardlink activation destination content mismatch", tree_helper)
        self.assertIn("New-CandidateHardLinkVerified", activation)
        self.assertIn("Copy-CandidateToInstallHardLinkTree", activation)
        self.assertIn("Copy-FileVerified", activation)

    def test_install_inventory_accumulation_is_linear_for_large_venvs(self) -> None:
        text = self.read("install_windows.ps1")
        inventory = text[text.index("function Get-InstallInventory") : text.index("function Ensure-UserScopePoppler")]
        self.assertIn("System.Collections.Generic.List[string]", inventory)
        self.assertIn("[switch]$IncludeFiles", inventory)
        self.assertIn("file_records", inventory)
        self.assertIn("$fingerprints.Add(", inventory)
        self.assertIn("$fingerprints.ToArray()", inventory)
        self.assertNotIn("$fingerprints +=", inventory)

    def test_install_snapshot_cleanup_follows_terminal_receipt_readback(self) -> None:
        text = self.read("install_windows.ps1")
        terminal_receipt = text[text.index("function Complete-InstallerTerminalReceipt") : text.index("function Write-InstallerTerminalSummary")]
        receipt_write = terminal_receipt.index("Save-InstallerReceipt", terminal_receipt.index("terminal-committing"))
        snapshot_cleanup = terminal_receipt.index("snapshot_cleanup")
        remove_snapshot = terminal_receipt.index("Remove-InstallSnapshotExact", receipt_write)
        self.assertLess(snapshot_cleanup, receipt_write)
        self.assertLess(receipt_write, remove_snapshot)
        self.assertIn("terminal_readback", terminal_receipt[snapshot_cleanup:receipt_write])
        self.assertIn("Read-StableTransactionJournal -Path $journalCleanupPath", terminal_receipt)
        self.assertIn("Assert-TerminalReceiptBinding -Journal", terminal_receipt)
        self.assertIn("terminal_receipt_readback", terminal_receipt)

    def test_success_terminal_path_requests_exact_snapshot_and_journal_cleanup(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("Complete-InstallerTerminalReceipt -RemoveSnapshot:$true", text)
        self.assertIn("Remove-InstallTransactionJournal", text)
        self.assertIn("Remove-InstallSnapshotExact", text)

    def test_terminal_cleanup_uses_verified_snapshot_owner_identity_and_hash(self) -> None:
        text = self.read("windows_install_common.psm1")
        helper = text[text.index("function Remove-InstallSnapshotExact") : text.index("function Restore-InstallSnapshot")]
        for token in (
            "Read-VerifiedInstallSnapshot",
            "ExpectedSnapshotSha256",
            "ExpectedSnapshotIdentity",
            "ExpectedRunId",
            "OwnedByRun",
            "Assert-PathObjectIdentity",
            "Remove-PathIdentityExact",
        ):
            with self.subTest(token=token):
                self.assertIn(token, helper)

    def test_stale_terminal_reconciliation_validates_receipt_snapshot_binding(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("function Assert-TerminalReceiptBinding", text)
        binding = text[text.index("function Assert-TerminalReceiptBinding") : text.index("function Recover-StaleVerifierHandoff")]
        for token in (
            "Read-BoundedJsonObject",
            "receipt_path",
            "snapshot_path",
            "snapshot_sha256",
            "snapshot_identity",
            "terminal_readback",
            "status_code",
            "run_id",
            "Assert-PathObjectIdentity",
            "status and status code do not match",
        ):
            with self.subTest(token=token):
                self.assertIn(token, binding)
        stale = text[text.index("if ($state -eq 'terminal-committing')") : text.index("if ($state -eq 'verifier-handoff-started')")]
        self.assertIn("Assert-TerminalReceiptBinding", stale)
        self.assertIn("Remove-InstallSnapshotExact", stale)

    def test_terminal_cleanup_refuses_mismatch_without_deleting_bound_objects(self) -> None:
        text = self.read("install_windows.ps1")
        binding = text[text.index("function Assert-TerminalReceiptBinding") : text.index("function Recover-StaleVerifierHandoff")]
        for token in (
            "receipt path does not match",
            "snapshot path does not match",
            "snapshot SHA-256 does not match",
            "snapshot object identity does not match",
            "Terminal receipt readback is not trusted",
            "throw",
        ):
            with self.subTest(token=token):
                self.assertIn(token, binding)
        stale = text[text.index("if ($state -eq 'terminal-committing')") : text.index("if ($state -eq 'verifier-handoff-started')")]
        self.assertIn("$script:staleTransactionRecoveryFailed = $true", stale)
        self.assertIn("preserving authenticated transaction objects", stale)
        fail_closed = text[text.rindex("$receipt.errors = @($receipt.errors) + $message") : text.index("    if ($dependencyMutationAttempted)", text.rindex("$receipt.errors = @($receipt.errors) + $message"))]
        self.assertIn("terminal-receipt-binding-mismatch", fail_closed)
        self.assertIn("stale_receipt_preserved", fail_closed)
        self.assertNotIn("Write-InstallTransactionJournal", fail_closed)

    def test_g12_harness_records_bounded_recovery_provenance(self) -> None:
        harness = (ROOT / "tests" / "windows" / "test_g12_preserve_move_fault_path.ps1").read_text(encoding="utf-8")
        for token in (
            "recovery_chain",
            "timeout_budget_seconds",
            "measured_elapsed_seconds",
            "termination_requested",
            "termination_confirmed",
            "surviving_tasks",
            "surviving_process",
            "process_exit_confirmed",
            "remote_harness",
            "provenance",
            "terminal_artifacts",
            "cleanup",
            "postcheck",
        ):
            with self.subTest(token=token):
                self.assertIn(token, harness)
        self.assertNotIn("Start-Process -Wait", harness)
        self.assertNotIn("timeout_seconds = 900", harness)
        self.assertNotIn("timeout_duration_seconds = 0", harness)

    def test_g12_harness_uses_direct_process_exit_with_redirected_output(self) -> None:
        harness = (ROOT / "tests" / "windows" / "test_g12_preserve_move_fault_path.ps1").read_text(encoding="utf-8")
        for token in (
            "System.Diagnostics.ProcessStartInfo",
            "UseShellExecute = $false",
            "RedirectStandardOutput = $true",
            "RedirectStandardError = $true",
            "ReadToEndAsync",
            "process_exit_code",
        ):
            with self.subTest(token=token):
                self.assertIn(token, harness)
        self.assertNotIn("Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments", harness)

    def test_g12_default_contract_validates_its_own_harness_tokens(self) -> None:
        harness = (ROOT / "tests" / "windows" / "test_g12_preserve_move_fault_path.ps1").read_text(encoding="utf-8")
        self.assertIn("$scriptText = Get-Content -LiteralPath $PSCommandPath -Raw", harness)
        self.assertIn("$scriptText.Contains($needle)", harness)

    def test_g12_marker_preimage_accepts_installer_or_harness_restore(self) -> None:
        harness = (ROOT / "tests" / "windows" / "test_g12_preserve_move_fault_path.ps1").read_text(encoding="utf-8")
        self.assertIn("$injectedMarkerHash = Get-Sha256Hex -Path $markerPath", harness)
        self.assertIn("$currentMarkerHash -ceq $originalMarkerHash -or $currentMarkerHash -ceq $injectedMarkerHash", harness)

    def test_terminal_cleanup_commit_is_authenticated_before_destructive_cleanup(self) -> None:
        text = self.read("install_windows.ps1")
        terminal = text[text.index("function Complete-InstallerTerminalReceipt") : text.index("function Write-InstallerTerminalSummary")]
        for token in (
            "terminal_cleanup_authorized",
            "terminal_cleanup_state",
            "terminal_cleanup_owner_run_id",
            "Assert-TerminalReceiptBinding -Journal",
            "-AllowCurrentRun",
            "authorized-pending",
            "preserve-for-recovery",
            "recovery_required",
            "after-journal-terminal-commit",
            "after-terminal-receipt-commit",
            "after-snapshot-cleanup",
            "after-journal-cleanup",
        ):
            with self.subTest(token=token):
                self.assertIn(token, terminal if token not in {"terminal_cleanup_authorized", "terminal_cleanup_state", "terminal_cleanup_owner_run_id"} else text)
        auth = terminal.index("Assert-TerminalReceiptBinding -Journal")
        destructive = terminal.index("Remove-InstallSnapshotExact")
        self.assertLess(auth, destructive)
        self.assertIn("$RemoveSnapshot = $false", terminal)

    def test_terminal_status_cleanup_matrix_is_fail_closed(self) -> None:
        text = self.read("install_windows.ps1")
        binding = text[text.index("function Assert-TerminalReceiptBinding") : text.index("function Recover-StaleVerifierHandoff")]
        for token in (
            "PASS_RUNTIME_ONLY",
            "ROLLED_BACK",
            "status and status code do not match",
            "Successful terminal receipt lacks an authorized cleanup state",
            "Failed terminal receipt must preserve recovery state",
            "Failed terminal receipt cannot authorize snapshot removal",
            "cleanup_authorized",
            "cleanup_state",
        ):
            with self.subTest(token=token):
                self.assertIn(token, binding)

        for token in (
            "Test-TerminalTransactionRecoveryState",
            "journal-only-authorized-pending",
            "FAIL_PREFLIGHT",
            "without recovery state",
            "dependency_mutation_attempted",
            "reconciled-empty-preflight-journal",
            "snapshot_cleanup = 'not-applicable'",
            "Null is not a cleanup record",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

        self.assertIn("FAIL_ROLLBACK_FAILED", text)

    def test_stale_pre_activation_candidate_is_removed_before_task_restore(self) -> None:
        text = self.read("install_windows.ps1")
        stale = text[text.index("function Invoke-StaleInstallTransactionRecovery") : text.index("function Save-InstallerReceipt")]
        for token in (
            "dependency-install-started",
            "candidate_root_identity",
            "candidate_root_owned_by_run",
            "Stop-InstallProcesses -RootPath $candidate",
            "Remove-RunOwnedRoot -Path $candidate",
            "pre_activation_candidate_removed",
        ):
            with self.subTest(token=token):
                self.assertIn(token, stale)
        candidate_cleanup = stale.index("Remove-RunOwnedRoot -Path $candidate")
        task_restore = stale.index("Restore-InstallSnapshot", candidate_cleanup)
        self.assertLess(candidate_cleanup, task_restore)

    def test_stale_recovery_keeps_restored_predecessor_out_of_candidate_cleanup(self) -> None:
        text = self.read("install_windows.ps1")
        stale = text[text.index("function Invoke-StaleInstallTransactionRecovery") : text.index("function Save-InstallerReceipt")]
        self.assertIn("$predecessorRestored = $false", stale)
        self.assertIn("$predecessorRestored = $true", stale)
        candidate_selection = stale.index("$restoreCandidate = $candidate")
        fresh_selection = stale.index("$restoreCandidate = $install")
        self.assertLess(candidate_selection, fresh_selection)
        fresh_guard = stale[fresh_selection - 400 : fresh_selection]
        self.assertIn("-not $predecessorRestored", fresh_guard)
        candidate_cleanup_guard = stale[stale.index("$candidateRootOwnedByStaleRun") : candidate_selection]
        self.assertIn("$predecessorRestored", candidate_cleanup_guard)

    def test_existing_env_preservation_is_hash_and_size_enforced(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("[Nullable[int64]]$PreimageSize", text)
        self.assertIn("-ExpectedSize $PreimageSize", text)
        self.assertIn("env_size_before", text)
        self.assertIn("env_size_after", text)
        self.assertRegex(text, r"(?s)if \(\$PreimageExists\).*?throw .*\.env")
        for token in ("HWPX_TEST_ENV_FAULT", "mismatch", "race"):
            with self.subTest(token=token):
                self.assertTrue(token in text, f".env fault-injection token missing: {token}")

    def test_installer_is_parameterized_and_fail_closed(self) -> None:
        text = self.read("install_windows.ps1")
        for token in (
            "[string]$SourceRoot",
            "[string]$InstallRoot",
            "[ValidateSet('CheckOnly', 'InstallUserScope')]",
            "[string]$FixturePath",
            "[ValidateSet('Fail', 'PreserveMove')]",
            "[switch]$ReplaceExistingTasks",
            "[string]$ReceiptPath",
            "Invoke-NativeChecked",
            "FAIL_PREFLIGHT",
            "FAIL_INSTALL",
            "FAIL_ACTIVATION",
            "ROLLED_BACK",
            "Register-ScheduledTask",
            "-WorkingDirectory",
            "config.example",
            "Write-JsonReceipt",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertNotRegex(text, r"sample-config\.env")
        self.assertNotRegex(text, r"BOOK-[A-Z0-9]{10}")
        self.assertNotIn("C:\\Users\\", text)
        self.assertIn("LASTEXITCODE", text)

    def test_replaced_running_tasks_are_quiesced_before_candidate_start(self) -> None:
        text = self.read("install_windows.ps1")
        activation = text[text.index("$taskActivation = @()") : text.index("$receipt.checks.task_activation = @($taskActivation)")]
        self.assertIn("-in @('Running', 'Queued')", activation)
        self.assertIn("Stop-ScheduledTaskExactAndWait", activation)
        self.assertIn("-ExpectedIdentity $roleIdentity", activation)
        self.assertLess(activation.index("Stop-ScheduledTaskExactAndWait"), activation.index("Start-ScheduledTask"))

    def test_source_manifest_git_identity_is_independently_rebound(self) -> None:
        text = self.read("windows_install_common.psm1")
        manifest = text[text.index("function Get-IndependentGitIdentity") : text.index("function Resolve-WindowsPrincipalIdentity")]
        self.assertIn("identity_source", manifest)
        self.assertIn("git", manifest)
        self.assertIn("rev-parse", manifest)
        self.assertIn("HEAD^{tree}", manifest)
        self.assertIn("independent", manifest.lower())

    def test_generated_runtime_env_requires_marker_bound_post_custody_contract(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        for token in (
            "ExpectedRuntimeEnvContract",
            "runtime_env_contract_applied",
            "installer-generated",
            "source_manifest_sha256",
            "install_root_identity",
            "runtime .env",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        for token in (
            "function Set-InstallerRuntimeEnvProvenance",
            "Set-InstallerRuntimeEnvProvenance -InstallRoot",
            "runtime_env",
            "config.example",
        ):
            with self.subTest(token=token):
                self.assertIn(token, installer)
        for token in (
            "ExpectedRuntimeEnvContract",
            "runtime_env",
            "Get-SourceManifest -SourceRoot $install",
        ):
            with self.subTest(token=token):
                self.assertIn(token, verifier)
        self.assertIn("Test-ProhibitedPrivateSourceMember", common)

    def test_native_receipt_preimage_is_hash_bound_and_current_run_owned(self) -> None:
        text = self.read("install_windows.ps1")
        preimage = text[text.index("function Preserve-ReceiptPreimage") : text.index("function New-RunPathClaim")]
        terminal = text[text.index("function Complete-InstallerTerminalReceipt") : text.index("function Write-InstallerTerminalSummary")]
        for token in ("Get-PathObjectIdentity", "Copy-FileVerified", "backup_sha256", "backup_object_identity"):
            self.assertIn(token, text)
        self.assertIn("Remove-ReceiptPreimageBackup", terminal)
        self.assertLess(terminal.index("Save-InstallerReceipt"), terminal.index("Remove-ReceiptPreimageBackup"))

    def test_native_border_readback_is_part_of_frozen_proof_packet_contract(self) -> None:
        text = (ROOT / "local_cli_v1" / "proof_packet.py").read_text(encoding="utf-8")
        for token in (
            "native_border_readback",
            "local-cli/native-border-readback/v1",
            "pre_quit_readback",
            "persisted_readback",
            "candidate_generation",
            "source_manifest_sha256",
        ):
            self.assertIn(token, text)

    def test_verifier_collects_direct_exit_codes_and_separates_failure_classes(self) -> None:
        text = self.read("verify_windows.ps1")
        for token in (
            "[switch]$FixturePath",
            "Push-Location",
            "LASTEXITCODE",
            "stdout",
            "stderr",
            "publication_gate",
            "advisory_test_debt",
            "VERIFIER_ERROR",
            "FAIL_NATIVE_E2E",
            "Write-JsonReceipt",
            "exit $exitCode",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertNotIn("Select-String.*PASS", text)

    def test_verifier_native_command_schema_gates_capture_truth(self) -> None:
        text = self.read("verify_windows.ps1")
        command_function = text[text.index("function Invoke-VerifierCommand") : text.index("function Resolve-VerifierPython")]
        for token in (
            "accepted = [bool]$result.accepted",
            "capture_error = [string]$result.capture_error",
            "termination_error = [string]$result.termination_error",
            "invocation_error = [string]$result.invocation_error",
            "exit_confirmed = [bool]$result.exit_confirmed",
        ):
            with self.subTest(token=token):
                self.assertIn(token, command_function)
        self.assertRegex(
            text,
            r"publicationOk\s*=.*Where-Object\s+\{\s*-not\s+\$_.accepted\b",
        )
        self.assertRegex(
            text,
            r"advisoryPassed\s*=\s*\(\$advisory\.accepted\s+-and\s+\$advisory\.exit_code\s+-eq\s+0\)",
        )

    def test_verifier_enforces_enabled_running_task_state(self) -> None:
        common = self.read("windows_install_common.psm1")
        verifier = self.read("verify_windows.ps1")
        self.assertIn("enabled =", common)
        self.assertIn("ExpectedState", verifier)
        self.assertIn("ExpectedEnabled", verifier)
        task_function = verifier[verifier.index("function Test-VerifierTask") : verifier.index("function Test-VerifierWorker")]
        self.assertIn("state", task_function)
        self.assertIn("enabled", task_function)

    def test_verifier_binds_fixture_identity_from_top_level_response_fields(self) -> None:
        verifier = self.read("verify_windows.ps1")
        binding = verifier[verifier.index("function Assert-FixtureCommandBinding") : verifier.index("function Invoke-FixtureSequence")]
        response_helper = verifier[verifier.index("function Get-VerifierResponseField") : verifier.index("function Assert-FixtureCommandBinding")]
        self.assertNotIn("foreach ($child in @($Payload.PSObject.Properties))", response_helper)
        for token in (
            "$responseCandidateGeneration",
            "$responseManifestSha256",
            "$responseRepository",
            "$responseCommit",
            "$responseTree",
            "$expectedResponseSchema",
            "$expectedResponseCommand",
            "proof.PSObject.Properties['artifact']",
            "candidate_identity_ok",
            "candidate_generation = $responseCandidateGeneration",
        ):
            with self.subTest(token=token):
                self.assertIn(token, binding)
        self.assertIn("candidate_identity_ok", binding)

    def test_powershell_test_path_boolean_calls_are_parenthesized(self) -> None:
        unsafe: list[str] = []
        scripts = sorted((ROOT / "scripts").glob("*.ps1")) + sorted((ROOT / "scripts").glob("*.psm1"))
        for path in scripts:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                search_from = 0
                while True:
                    command_start = line.find("Test-Path", search_from)
                    if command_start < 0:
                        break
                    suffix = line[command_start + len("Test-Path") :]
                    if re.search(r"\s-(?:and|or)\b", suffix) and (
                        command_start == 0 or line[command_start - 1] != "("
                    ):
                        unsafe.append(f"{path.name}:{line_number}: {line.strip()}")
                    search_from = command_start + len("Test-Path")
        self.assertEqual([], unsafe)

    def test_verifier_uses_runtime_safe_nonempty_file_helper(self) -> None:
        verifier = self.read("verify_windows.ps1")
        common = self.read("windows_install_common.psm1")
        self.assertIn("Test-NonEmptyFile", common)
        self.assertIn("$proofManifestValid = Test-NonEmptyFile -Path $proofManifestPath", verifier)
        self.assertIn("fixture_close_cleanup", verifier)
        self.assertIn("$closeAttempted", verifier)
        self.assertIn("$closeConfirmed", verifier)

    def test_verifier_loads_image_assembly_before_proof_png_use(self) -> None:
        verifier = self.read("verify_windows.ps1")
        assembly_load = "Add-Type -AssemblyName System.Drawing"
        self.assertIn(assembly_load, verifier)
        self.assertLess(
            verifier.index(assembly_load),
            verifier.index("function Test-ProofPng"),
        )

    def test_verifier_uses_bounded_png_content_detection_instead_of_fixed_points(self) -> None:
        verifier = self.read("verify_windows.ps1")
        function_start = verifier.index("function Test-ProofPng")
        function_end = verifier.index("function Invoke-FixtureSequence", function_start)
        proof_function = verifier[function_start:function_end]
        for token in (
            "$maxProofBytes",
            "$maxProofPixels",
            "$maxSampleCount",
            "ImageFormat]::Png",
            "sampled_pixels",
            "nonempty_samples",
            "content_bbox",
        ):
            with self.subTest(token=token):
                self.assertIn(token, proof_function)
        self.assertNotIn("foreach ($x in @(0, [Math]", proof_function)

    def test_installer_and_verifier_gate_complete_locked_windows_dependencies(self) -> None:
        checker = ROOT / "scripts" / "check_windows_dependencies.py"
        self.assertTrue(checker.is_file())
        for name in ("install_windows.ps1", "verify_windows.ps1"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("check_windows_dependencies.py", text)
                self.assertIn("dependency_completeness", text)
                self.assertIn("annotated_types", checker.read_text(encoding="utf-8"))
        self.assertIn("--force-reinstall", self.read("install_windows.ps1"))

    def test_rollback_releases_candidate_process_handles_before_file_cleanup(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        for token in (
            "Get-InstallProcessSnapshot",
            "Stop-InstallProcesses",
            "PreserveProcessIds",
            "processes_released",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common if token != "processes_released" else installer)

    def test_rollback_task_restore_only_changes_state_when_needed(self) -> None:
        common = self.read("windows_install_common.psm1")
        self.assertRegex(
            common,
            r"if \(\[string\]\$task\.state -eq 'Running' -and \[string\]\$restoredTask\.State -ne 'Running'\)",
        )
        self.assertRegex(
            common,
            r"elseif \(\[string\]\$task\.state -ne 'Running' -and \[string\]\$restoredTask\.State -eq 'Running'\)",
        )
        self.assertTrue("if (-not $processReleaseBeforeTasks.ok" in common)
        self.assertTrue("Rollback task snapshot has no XML" in common)

    def test_rollback_readback_verifies_exact_task_identity_and_enabled_state(self) -> None:
        common = self.read("windows_install_common.psm1")
        restore = common[common.index("function Restore-InstallSnapshot") :]
        for token in (
            "Get-ScheduledTaskIdentity -TaskName",
            "task_identity_hash",
            "working_directory",
            "action_type",
            "settings",
            "enabled",
            "Rollback task identity",
        ):
            with self.subTest(token=token):
                self.assertIn(token, restore)

    def test_windows_json_reads_are_utf8_safe_under_powershell_51(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        self.assertIn("$manifest_bytes", common)
        self.assertIn("[System.Text.Encoding]::UTF8.GetString($manifest_bytes)", common)
        self.assertIn(
            "$snapshot = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json",
            common,
        )
        self.assertIn("$installedMarkerCapture = Read-BoundedJsonObject -Path $marker", installer)
        self.assertNotIn("$markerPayload = [IO.File]::ReadAllText($marker) | ConvertFrom-Json", installer)

    def test_legacy_launcher_uses_common_checked_native_helper_and_template(self) -> None:
        text = self.read("writer_v1.ps1")
        self.assertIn("Import-Module $commonPath", text)
        self.assertIn("Invoke-NativeChecked", text)
        self.assertIn("Write-JsonReceipt", text)
        self.assertIn("Read-BoundedText", text)
        self.assertIn("config.example", text)
        self.assertIn("-AllowNonZero", text)
        self.assertNotIn("Get-Content -Raw -Path $stdoutPath", text)
        self.assertNotIn("Get-Content -Raw -Path $stderrPath", text)
        self.assertNotRegex(text, r"sample-config\.env")

    def test_legacy_launcher_uses_the_hash_pinned_windows_lock(self) -> None:
        text = self.read("writer_v1.ps1")
        self.assertIn("requirements-windows.lock", text)
        self.assertIn("--require-hashes", text)
        self.assertNotIn("'requirements.txt'", text)

    def test_setup_batch_is_a_thin_powershell_wrapper(self) -> None:
        text = (ROOT / "scripts" / "setup_venv.bat").read_text(encoding="utf-8")
        self.assertIn("powershell", text.lower())
        self.assertIn("install_windows.ps1", text)
        self.assertNotIn("pip install", text.lower())

    def test_config_example_documents_portable_installation_settings(self) -> None:
        text = (ROOT / "config.example").read_text(encoding="utf-8")
        for token in ("HWP_PDFTOPPM", "HWP_API_TASK_NAME", "HWP_WORKER_TASK_NAME", "HWP_SOURCE_MANIFEST"):
            self.assertIn(token, text)

    def test_installer_receipt_tracks_dependency_and_existing_config_identity(self) -> None:
        text = self.read("install_windows.ps1")
        for token in (
            "source_identity",
            "requirements_sha256",
            "--require-hashes",
            "env_sha256_before",
            "env_sha256_after",
            "backupRoot",
            "preserved =",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_task_collision_check_covers_action_and_interactive_principal(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        for token in ("ExpectedExecutable", "ExpectedArguments", "ExpectedPrincipal"):
            with self.subTest(token=token):
                self.assertIn(token, installer)
        self.assertIn("principal", common)

    def test_verifier_records_source_identity_and_enforces_readiness_task_contract(self) -> None:
        text = self.read("verify_windows.ps1")
        for token in (
            "source_identity",
            "manifest_sha256",
            "ready",
            "contract_ok",
            "ExpectedArguments",
            "ExpectedPrincipal",
            "FAIL_API",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_installer_source_copy_is_manifest_driven_and_recopies_manifest(self) -> None:
        text = self.read("install_windows.ps1")
        for token in ("manifest.files", "Copy-SourceToCandidate", "Copy-Item -LiteralPath $manifest"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_git_fallback_manifest_excludes_runtime_outputs(self) -> None:
        text = self.read("install_windows.ps1")
        common = self.read("windows_install_common.psm1")
        for token in ("uploads", "logs", "backups", "proofs", ".sqlite3"):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertIn(".zip", text)
        self.assertIn(".zip", common)
        self.assertIn("StartsWith('.env')", text)
        self.assertIn("StartsWith('.env')", common)

    def test_windows_poppler_resolution_accepts_legacy_alias(self) -> None:
        for name in ("install_windows.ps1", "verify_windows.ps1"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("HWP_PDFTOPPM_PATH", text)

    def test_windows_poppler_resolution_checks_explicit_path_before_canonicalizing(self) -> None:
        for name in ("install_windows.ps1", "verify_windows.ps1"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("Get-Item -LiteralPath $explicit", text)
                self.assertIn("$explicitItem.Attributes", text)

    def test_windows_poppler_resolution_requires_an_executable_extension(self) -> None:
        for name in ("install_windows.ps1", "verify_windows.ps1"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("GetExtension($canonical)", text)
                self.assertIn("'.exe'", text)
                self.assertNotIn("'.cmd', '.bat'", text)

    def test_python_version_prefix_is_limited_to_the_py_launcher(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("function New-PythonInvocation", text)
        self.assertIn("$leaf -in @('py.exe', 'py')", text)
        self.assertNotIn("$name -like 'py*'", text)

    def test_configured_python_launcher_uses_the_same_version_selector(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("return New-PythonInvocation -Path $candidate", text)
        self.assertIn("return New-PythonInvocation -Path $command.Source", text)

    def test_windows_power_shell_51_uses_supported_interactive_principal_enum(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("-LogonType Interactive", text)
        self.assertNotIn("InteractiveToken", text)

    def test_explicit_poppler_parameter_is_forwarded_to_verifier(self) -> None:
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        self.assertIn("[string]$PopplerPath", installer)
        self.assertIn("[string]$PopplerPath", verifier)
        self.assertIn("'-PopplerPath'", installer)
        self.assertIn("if ($PopplerPath)", installer)
        self.assertIn("if ($PopplerPath)", verifier)

    def test_api_port_is_configurable_and_bound_to_installer_and_verifier(self) -> None:
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        writer = self.read("writer_v1.ps1")
        for name, text in (("installer", installer), ("writer", writer)):
            with self.subTest(script=name):
                self.assertIn("HWP_API_PORT", text)
        for token in (
            "[Nullable[int]]$ApiPort",
            "Resolve-ApiPort",
            "Assert-PortAvailable -Port $apiPort",
            "api_port = $apiPort",
            "'-ApiPort'",
        ):
            with self.subTest(token=token):
                self.assertIn(token, installer)
        for token in ("[Nullable[int]]$ApiPort", "Resolve-ApiPort", "$apiBaseUri", "api_port"):
            with self.subTest(token=token):
                self.assertIn(token, verifier)
        self.assertNotIn("http://127.0.0.1:8765/health", verifier)
        self.assertNotIn("http://127.0.0.1:8765/runtime-readiness", verifier)

    def test_native_receipts_bound_command_output_before_json_serialization(self) -> None:
        common = self.read("windows_install_common.psm1")
        for token in (
            "function Read-BoundedText",
            "function Convert-NativeBytesToText",
            "System.Diagnostics.ProcessStartInfo",
            "OpenStdoutStream",
            "OpenStderrStream",
            "ReadAsync",
            "MaxOutputBytes",
            "TimeoutSeconds",
            "stdout_bytes",
            "stderr_bytes",
            "stdout_captured_bytes",
            "stderr_captured_bytes",
            "stdout_truncated",
            "stderr_truncated",
            "capture_mode = 'raw-byte-pipes'",
            "MaxReceiptBytes",
            "Get-ScheduledTaskIdentity",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        self.assertNotIn("Get-Content -LiteralPath $stdoutPath -Raw", common)
        self.assertNotIn("Get-Content -LiteralPath $stderrPath -Raw", common)

    def test_native_helper_preserves_direct_exit_when_stderr_is_redirected(self) -> None:
        common = self.read("windows_install_common.psm1")
        self.assertIn("SuspendedProcess]::Create", common)
        self.assertIn("$process.ExitCode", common)
        self.assertIn("$process.Kill()", common)
        self.assertIn("process_killed", common)
        self.assertNotIn("& $FilePath @Arguments", common)
        self.assertNotIn("$ErrorActionPreference = 'Continue'", common)

    def test_api_port_contract_is_checked_on_both_verifier_task_calls(self) -> None:
        text = self.read("verify_windows.ps1")
        self.assertEqual(text.count("-ExpectedApiPort $apiPort"), 2)
        self.assertIn("(Test-VerifierTask -TaskName $apiTaskName", text)
        self.assertIn("(Test-VerifierTask -TaskName $workerTaskName", text)
        self.assertIn("$configuredPort = Get-ConfiguredApiPort", text)
        self.assertIn("api_port_contract_ok", text)

    def test_port_and_environment_diagnostics_are_bounded_and_fail_closed(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        for token in (
            "Environment configuration exceeds the bounded size limit",
            "Requested ApiPort",
            "ApiPort must be between 1 and 65535",
            "Get-NetTCPConnection",
            "ExpectedRoot",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common + installer)

    def test_free_loopback_port_is_not_misclassified_as_inspection_failure(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("-ErrorVariable +lookupErrors", text)
        self.assertIn("CategoryInfo.Category", text)
        self.assertIn("ObjectNotFound", text)

    def test_process_identity_binds_venv_base_interpreter_task_and_listener(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        for token in (
            "function Get-VenvInterpreterMetadata",
            "base_executable",
            "ExpectedInterpreterVersion",
            "ExpectedArguments",
            "ExpectedTaskIdentity",
            "ExpectedListener",
            "ExpectedListenerPort",
            "function Test-CanonicalTaskActionBinding",
            "Test-CommandLinePathToken -CommandLine $line -Path $root",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        self.assertIn("-ExpectedTaskName $apiTaskName", installer)
        self.assertIn("-ExpectedListenerPort $Port", installer)
        export_line = next(line for line in common.splitlines() if line.startswith("Export-ModuleMember"))
        self.assertIn("Test-CanonicalTaskActionBinding", export_line)
        for token in ("$apiTaskIdentity", "-ExpectedListener $listener", "-ExpectedArguments '-m app.api_server'"):
            with self.subTest(token=token):
                self.assertIn(token, verifier)

    def test_receipt_writer_projects_large_diagnostics_without_dropping_terminal_identity(self) -> None:
        common = self.read("windows_install_common.psm1")
        focused_test = ROOT / "tests" / "windows" / "test_venv_process_identity_and_receipt_bound.ps1"
        self.assertTrue(focused_test.is_file())
        for token in (
            "function ConvertTo-BoundedReceiptValue",
            "function New-BoundedReceiptProjection",
            "function New-MinimalReceiptProjection",
            "diagnostic-value-bounded",
            "file-list-bounded",
            "failed_predicates",
            "candidate_generation",
            "source_identity",
            "receipt_bounds",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        for token in (
            "G10_VENV_PROCESS_IDENTITY=PASS",
            "G10_BOUNDED_RECEIPT=PASS",
            "candidate_generation = 'g10-test-candidate'",
            "failed_predicates = @(",
            "processes_released = $true",
        ):
            with self.subTest(token=token):
                self.assertIn(token, focused_test.read_text(encoding="utf-8"))

    def test_verifier_fixture_temp_root_is_user_writable_and_cleanable(self) -> None:
        verifier = self.read("verify_windows.ps1")
        for token in (
            "function New-VerifierFixtureTempRoot",
            "[System.IO.Path]::GetTempPath()",
            "Assert-NoReparsePath -Path $tempBase",
            "$probePath = Join-Path $tempRoot '.write-probe'",
            "Remove-Item -LiteralPath $probePath -Force -ErrorAction Stop",
            "not writable and cleanable",
            "$tempRoot = New-VerifierFixtureTempRoot",
        ):
            with self.subTest(token=token):
                self.assertIn(token, verifier)
        self.assertNotIn("$tempBase = Join-Path $env:windir 'Temp'", verifier)

    def test_task_identity_arrays_preserve_switch_and_scalar_argument_binding(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("(Assert-TaskCompatibility -TaskName $apiTaskName", text)
        self.assertIn("(Assert-TaskCompatibility -TaskName $workerTaskName", text)
        self.assertIn("(Assert-TaskIdentity -Identity (Get-ScheduledTaskIdentity -TaskName $apiTaskName -TaskPath $taskPath)", text)
        self.assertIn("(Assert-TaskIdentity -Identity (Get-ScheduledTaskIdentity -TaskName $workerTaskName -TaskPath $taskPath)", text)

    def test_task_principal_readback_uses_sid_or_account_equivalence(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        for token in (
            "function Resolve-WindowsPrincipalIdentity",
            "function Test-WindowsPrincipalEquivalent",
            "SecurityIdentifier",
            "NTAccount",
            "COMPUTERNAME",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        comparison = "Test-WindowsPrincipalEquivalent -Actual $identity.principal -Expected $ExpectedPrincipal"
        self.assertIn(comparison, installer)
        self.assertIn(comparison, verifier)
        self.assertNotIn("[string]$identity.principal -ieq $ExpectedPrincipal", installer)
        self.assertNotIn("[string]$identity.principal -ieq $ExpectedPrincipal", verifier)

    def test_task_identity_readback_normalizes_only_ps51_default_equivalents(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        writer = self.read("writer_v1.ps1")
        for token in (
            "function Test-ScheduledTaskLogonTypeEquivalent",
            "function Test-ScheduledTaskRunLevelEquivalent",
            "Test-ScheduledTaskLogonTypeEquivalent",
            "Test-ScheduledTaskRunLevelEquivalent",
        ):
            with self.subTest(token=token):
                self.assertIn(token, common)
        for text in (installer, verifier, writer):
            with self.subTest(script="scheduled-task caller"):
                self.assertIn("Test-ScheduledTaskLogonTypeEquivalent", text)
                self.assertIn("Test-ScheduledTaskRunLevelEquivalent", text)
        self.assertIn("InteractiveToken", common)
        self.assertIn("IsNullOrWhiteSpace($Actual)", common)
        self.assertIn("persisted_logon_type", common)
        self.assertIn("persisted_run_level", common)
        self.assertNotIn("[string]$identity.logon_type -eq 'Interactive'", installer + verifier + writer)
        self.assertNotIn("[string]$identity.run_level -eq 'Limited'", installer + verifier + writer)

    def test_writer_builds_every_loopback_endpoint_from_selected_port(self) -> None:
        text = self.read("writer_v1.ps1")
        self.assertIn("Resolve-ApiPort", text)
        self.assertIn("http://127.0.0.1:$ApiPort/health", text)
        self.assertIn("http://127.0.0.1:$ApiPort/runtime-readiness", text)
        self.assertIn("http://127.0.0.1:$ApiPort/observation-viewer/session", text)
        self.assertNotIn("http://127.0.0.1:8765/health", text)

    def test_explicit_invalid_poppler_is_authoritative_even_in_install_user_scope(self) -> None:
        text = self.read("install_windows.ps1")
        guard = "if ($resolved.source -eq 'explicit-invalid')"
        self.assertIn(guard, text)
        self.assertIn("Explicit Poppler path is invalid", text)
        self.assertLess(text.index(guard), text.index("Get-Command winget.exe"))

    def test_poppler_user_scope_and_custom_port_are_documented_for_portable_qa(self) -> None:
        text = (ROOT / "docs" / "WINDOWS_INSTALL.md").read_text(encoding="utf-8")
        for token in ("-ApiPort", "InstallUserScope", "HWPX_POPPLER_PACKAGE_ID", "HWP_API_PORT"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_verifier_classifies_http_readiness_failure_as_api_failure(self) -> None:
        text = self.read("verify_windows.ps1")
        for token in ("readiness_error", "exitCode = 14", "PYTHONDONTWRITEBYTECODE"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_installer_capacity_uses_verified_source_members(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("manifestResult.manifest.files", text)
        self.assertNotIn("Get-ChildItem -LiteralPath $source -Recurse -File", text)

    def test_windows_manifest_validation_rejects_ads_and_reserved_names(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        for token in ("IndexOfAny", "CON", "ReparsePoint"):
            with self.subTest(token=token):
                self.assertIn(token, common)
        self.assertIn("Assert-WindowsSafeSourceRelativePath", installer)

    def test_windows_manifest_walk_is_case_insensitive_and_rejects_reparse_directories(self) -> None:
        installer = self.read("install_windows.ps1")
        common = self.read("windows_install_common.psm1")
        self.assertIn("$part.ToLowerInvariant()", installer)
        self.assertIn("-Directory -Force", common)
        self.assertIn("Reparse-point source directory", common)

    def test_windows_manifest_validates_declared_count_and_entry_digests(self) -> None:
        common = self.read("windows_install_common.psm1")
        self.assertIn("file_count", common)
        self.assertIn("sha256", common)
        self.assertIn("[int64]::TryParse", common)
        self.assertIn("file-count-mismatch", common)
        self.assertIn("@($lowerSegments | Where-Object", common)

    def test_windows_manifest_excludes_all_fixture_members(self) -> None:
        installer = self.read("install_windows.ps1")
        common = self.read("windows_install_common.psm1")
        for text in (installer, common):
            self.assertNotIn("command_bundle_where_response.json", text)
            self.assertNotIn("$isSyntheticFixture", text)

    def test_installer_rolls_back_partial_install_root_after_candidate_copy_failure(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$candidateInstallCreated", text)
        self.assertIn("Remove-Item -LiteralPath (Get-CanonicalPath -Path $install", text)
        self.assertIn("Copy the candidate only", text)

    def test_installer_removes_new_config_when_reused_install_activation_fails(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$configCreated", text)
        self.assertIn("rollback.config_removed", text)
        self.assertIn("$reused -and $configCreated", text)

    def test_installer_attempts_backup_restore_even_when_task_restore_fails(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("task_restore_error", text)
        self.assertIn("backup_restore_error", text)
        self.assertIn("$rollbackErrors", text)
        self.assertIn("Move-Item -LiteralPath $backupRoot -Destination $install", text)

    def test_preserve_move_rehomes_backup_before_restoring_task_state(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$rollbackCandidateQuarantine", text)
        self.assertIn("$rollbackBackupActivated", text)
        rollback = text[text.index("# PreserveMove must put") :]
        swap = rollback.index("Move-Item -LiteralPath $backupRoot -Destination $install")
        restore = rollback.index("Restore-InstallSnapshot")
        self.assertLess(swap, restore)
        self.assertIn("Stop-InstallProcesses", text[text.index("catch {", text.index("$receipt.status")):])
        self.assertIn("-not $rollbackBackupActivated", text)

    def test_preserve_move_removes_candidate_tasks_before_root_swap(self) -> None:
        text = self.read("install_windows.ps1")
        rollback = text[text.index("# PreserveMove must put") : text.index("if ($receipt.snapshot_path", text.index("# PreserveMove must put"))]
        self.assertIn("Unregister-ScheduledTask", rollback)
        self.assertLess(rollback.index("Unregister-ScheduledTask"), rollback.index("Move-Item -LiteralPath $backupRoot -Destination $install"))

    def test_preserve_move_waits_for_task_inactive_readback_before_root_swap(self) -> None:
        installer = self.read("install_windows.ps1")
        common = self.read("windows_install_common.psm1")
        self.assertTrue("function Wait-ScheduledTaskInactive" in common)
        self.assertTrue("Stop-ScheduledTaskExactAndWait -TaskName $taskName" in installer)
        preserve = installer[installer.index("$reused = $false") : installer.index("$requirements = $null")]
        self.assertLess(
            preserve.index("Stop-ScheduledTaskExactAndWait -TaskName $taskName"),
            preserve.index("Move-Item -LiteralPath $install -Destination $backupRoot"),
        )

    def test_preserve_move_captures_and_releases_existing_processes_before_backup_move(self) -> None:
        text = self.read("install_windows.ps1")
        preserve = text[text.index("$reused = $false") : text.index("$requirements = $null")]
        snapshot = preserve.index("Seal-InstallerSnapshot -TaskStateOverride")
        backup_move = preserve.index("Move-Item -LiteralPath $install -Destination $backupRoot")
        self.assertLess(snapshot, backup_move)
        self.assertIn("Stop-InstallProcesses -RootPath $install -PreserveProcessIds @()", preserve)
        self.assertLess(
            preserve.index("Stop-InstallProcesses -RootPath $install -PreserveProcessIds @()"),
            backup_move,
        )

    def test_verifier_binds_runtime_marker_to_manifest_candidate_generation(self) -> None:
        text = self.read("verify_windows.ps1")
        for token in (
            ".hwpx-install.json",
            "source_manifest_sha256",
            "candidate_generation_contract_ok",
            "Get-Sha256Hex -Path $markerPath",
            "markerPayload",
        ):
            with self.subTest(token=token):
                self.assertTrue(token in text, f"verifier marker-binding token missing: {token}")
        self.assertIn("candidate_generation = '{0}:{1}:{2}'", text)
        includes_install_root = bool(re.search(r"candidate_generation\s*=\s*'.*\$install", text))
        self.assertFalse(
            includes_install_root,
            "verifier candidate_generation must not include the machine-specific install path",
        )

    def test_task_contract_enforces_canonical_settings_shape(self) -> None:
        common = self.read("windows_install_common.psm1")
        verifier = self.read("verify_windows.ps1")
        self.assertIn("function Test-CanonicalTaskSettings", common)
        self.assertIn("Test-CanonicalTaskSettings -Identity $Identity", common)
        self.assertIn("Test-CanonicalTaskSettings -Identity $identity", verifier)
        settings_function = common[common.index("function Test-CanonicalTaskSettings") : common.index("function Get-ScheduledTaskIdentity")]
        for token in ("StartWhenAvailable", "Hidden", "RunOnlyIfNetworkAvailable", "settings"):
            with self.subTest(token=token):
                self.assertIn(token, settings_function)

    def test_native_and_receipt_fault_injection_contracts_are_deterministic(self) -> None:
        common = self.read("windows_install_common.psm1")
        native_test = (ROOT / "tests" / "windows" / "test_g11_native_capture.ps1").read_text(encoding="utf-8")
        receipt_test = (ROOT / "tests" / "windows" / "test_venv_process_identity_and_receipt_bound.ps1").read_text(encoding="utf-8")
        for token in (
            "HWPX_TEST_NATIVE_FAULT",
            "capture",
            "decode",
            "drain",
            "termination",
            "HWPX_TEST_RECEIPT_FAULT",
            "serialization",
            "write",
            "readback",
        ):
            with self.subTest(token=token):
                self.assertTrue(
                    token in common or token in native_test or token in receipt_test,
                    f"fault-injection token missing: {token}",
                )

    def test_preserve_move_releases_all_install_processes_before_swap(self) -> None:
        text = self.read("install_windows.ps1")
        rollback = text[text.index("# PreserveMove must put") : text.index("if ($receipt.snapshot_path", text.index("# PreserveMove must put"))]
        self.assertIn("-PreserveProcessIds @()", rollback)
        self.assertIn("@($candidateRoot, $install)", rollback)

    def test_run_path_claim_cleanup_allows_empty_identity_only_when_claim_is_absent(self) -> None:
        text = self.read("install_windows.ps1")
        start = text.index("function Remove-RunPathClaim")
        function = text[start : text.index("function Assert-RunOwnedRoot", start)]
        self.assertIn("[AllowEmptyString()]", function)
        absent_claim_guard = function.index("if ([string]::IsNullOrWhiteSpace($ClaimPath)) { return }")
        identity_guard = function.index("if ([string]::IsNullOrWhiteSpace($ExpectedObjectIdentity))")
        self.assertLess(absent_claim_guard, identity_guard)

    def test_reused_install_requires_installed_source_manifest_revalidation(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$installedManifest = Get-SourceManifest -SourceRoot $install", text)
        self.assertIn("$installManifestCompatible", text)
        self.assertIn("$installManifestCompatible", text[text.index("$markerCompatible"):])

    def test_installer_does_not_persist_preflight_as_a_successful_install(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertNotIn("$receipt.status = 'PREFLIGHT_PASS'", text)
        self.assertIn("$receipt.status = 'IN_PROGRESS'", text)
        self.assertIn("$receipt.phase = 'install'", text)

    def test_installer_terminal_exit_dispatch_is_powershell_51_compatible(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertFalse("exit (if" in text, "installer uses syntax unsupported by Windows PowerShell 5.1")
        self.assertIn("if ($terminalReceiptOk)", text)

    def test_existing_install_default_receipt_is_outside_preserved_root(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$receiptFile = Join-Path ([System.IO.Path]::GetTempPath())", text)
        self.assertNotIn("Join-Path $installInputPath 'receipts\\install.json'", text)
        self.assertNotIn("Join-Path $install 'receipts\\install.json'", text)
        self.assertIn("Assert-ReceiptPathAdmission", text)

    def test_ci_runs_all_windows_suites_and_immutable_actions(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        expected_suites = {
            "test_windows_powershell_51_runtime.ps1",
            "test_verify_windows_proof_png.ps1",
            "test_verify_windows.ps1",
            "test_venv_process_identity_and_receipt_bound.ps1",
            "test_scheduled_task_identity_equivalence.ps1",
            "test_scheduled_task_extra_action.ps1",
            "test_g16_install_ownership.ps1",
            "test_install_windows.ps1",
            "test_g6_rollback_runtime.ps1",
            "test_g6_repairs.ps1",
            "test_g7_marker_idempotency.ps1",
            "test_g11_native_capture.ps1",
            "test_g23_native_helper_repairs.ps1",
            "test_g5_runtime_env_contract.ps1",
        }
        suite_manifest = (ROOT / "tests" / "windows" / "suite-manifest.json").read_text(encoding="utf-8")
        for suite in expected_suites:
            with self.subTest(suite=suite):
                self.assertIn(suite, suite_manifest)
        self.assertIn("suite-manifest.json", workflow)
        self.assertIn("run_windows_suite.ps1", workflow)
        self.assertRegex(workflow, r"- name: Run PowerShell 5\.1 contract tests\s+shell: powershell")
        self.assertIn("Run function-style tests", workflow)
        self.assertIn("PowerShell contract suite failed", workflow)
        self.assertIn("test_g23_native_helper_repairs.ps1", workflow)
        for action in ("actions/checkout@", "actions/setup-python@"):
            with self.subTest(action=action):
                self.assertRegex(workflow, rf"{re.escape(action)}[0-9a-f]{{40}}")

    def test_ci_publication_scan_uses_neutral_full_tree_patterns(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("'BOOK-' +", workflow)
        self.assertNotIn("'sample-' +", workflow)
        self.assertRegex(workflow, r"BOOK-\[A-Z0-9\]\{10\}")
        self.assertIn("git ls-files", workflow)
        self.assertIn("foreach ($file in $sourceFiles)", workflow)

    def test_fresh_installer_requires_hash_pinned_windows_lock(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$requirements = $lockFile", text)
        self.assertIn("$pipArguments += '--require-hashes'", text)
        self.assertNotIn("$requirements = if ($dependencyLocked)", text)

    def test_verifier_publication_gate_runs_documented_checks_and_classifies_failure(self) -> None:
        text = self.read("verify_windows.ps1")
        for script in (
            "smoke_cli_workflow_status_static.py",
            "smoke_local_cli_health_static.py",
            "smoke_output_parser_static.py",
            "smoke_command_bundle_static.py",
            "smoke_command_packages_static.py",
            "smoke_native_table_command_static.py",
            "smoke_export_proof_range_clamp_static.py",
            "smoke_find_context_static.py",
            "smoke_cli_json_envelope_parity_static.py",
            "smoke_readback_diff_static.py",
            "smoke_readback_schema_static.py",
            "smoke_selection_proof_static.py",
            "smoke_text_table_cleanup_static.py",
        ):
            with self.subTest(script=script):
                self.assertIn(script, text)
        self.assertIn("FAIL_PUBLICATION", text)

    def test_verifier_does_not_create_missing_install_root_for_default_receipt(self) -> None:
        text = self.read("verify_windows.ps1")
        self.assertIn("$installInitializationError", text)
        self.assertIn("[System.IO.Path]::GetTempPath()", text)
        self.assertIn("elseif ($installInitializationError)", text)

    def test_verifier_default_receipt_is_external_to_installed_root(self) -> None:
        text = self.read("verify_windows.ps1")
        receipt_branch = text[text.index("$receiptFile = if ($ReceiptPath)") : text.index("$exitCode = 20")]
        self.assertIn("[System.IO.Path]::GetTempPath()", receipt_branch)
        self.assertNotIn("Join-Path $install 'receipts\\verify.json'", receipt_branch)
        self.assertIn("ReceiptPath must be outside the existing InstallRoot", text)

    def test_verifier_fixture_close_uses_structured_api_status(self) -> None:
        text = self.read("verify_windows.ps1")
        self.assertIn("live_session_bound", text)
        self.assertIn("close_status", text)
        self.assertIn("$apiBaseUri + '/local-cli/status'", text)
        self.assertIn("$closedCommandOk = @($results | Where-Object", text)

    def test_verifier_rejects_nonzero_python_identity_and_classifies_manifest_errors(self) -> None:
        text = self.read("verify_windows.ps1")
        self.assertIn("if ($identity.exit_code -ne 0)", text)
        self.assertIn("$exitCode = 11", text)
        self.assertIn("$exitCode = 11\n        $manifest = Get-SourceManifest", text)

    def test_verifier_distinguishes_advisory_discovery_debt_from_runtime_pass(self) -> None:
        text = self.read("verify_windows.ps1")
        self.assertIn("PASS_WITH_ADVISORY_TEST_DEBT", text)
        self.assertIn("$advisoryPassed", text)
        self.assertIn("$receipt.status = if ($advisoryPassed)", text)

    def test_explicit_api_port_survives_case_insensitive_powershell_variables(self) -> None:
        text = self.read("install_windows.ps1")
        self.assertIn("$requestedApiPort = $ApiPort", text)
        self.assertIn("-RequestedApiPort $requestedApiPort", text)

    def test_installer_and_verifier_do_not_dump_full_receipts_to_terminal_streams(self) -> None:
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        for text in (installer, verifier):
            self.assertNotIn("Write-Output (($receipt | ConvertTo-Json -Depth 30))", text)
        self.assertNotIn("Write-Error (($receipt | ConvertTo-Json -Depth 30))", installer)
        self.assertNotIn("Write-Error $summary", installer)
        self.assertIn("Write-Output $summary", installer)
        self.assertIn("Limit-Text", installer)

    def test_terminal_journal_cleanup_state_is_persisted_after_delete(self) -> None:
        text = self.read("install_windows.ps1")
        terminal = text[text.index("function Complete-InstallerTerminalReceipt") : text.index("function Write-InstallerTerminalSummary")]
        delete_index = terminal.index("Remove-InstallTransactionJournal")
        state_index = terminal.index("$receipt.transaction_journal_cleanup.cleanup_state = 'journal-cleaned'", delete_index)
        save_index = terminal.index("Save-InstallerReceipt | Out-Null", state_index)
        crash_index = terminal.index("Invoke-InstallerCrashPoint -Name 'after-journal-cleanup'", state_index)
        self.assertLess(delete_index, state_index)
        self.assertLess(state_index, save_index)
        self.assertLess(save_index, crash_index)
        self.assertIn(
            "$receipt.terminal_cleanup.cleanup_state = $receipt.transaction_journal_cleanup.cleanup_state",
            terminal,
        )

    def test_windows_suite_runner_executes_required_g12_and_rejects_skip(self) -> None:
        runner = (ROOT / "tests" / "windows" / "run_windows_suite.ps1").read_text(encoding="utf-8")
        cleanup = (ROOT / "tests" / "windows" / "test_terminal_cleanup_contracts.ps1").read_text(encoding="utf-8")
        for token in (
            "HWPX_TEST_INSTALL_CRASH_POINT",
            "WaitForExit(",
            "process_exit_confirmed",
            "required",
            "output_status",
            "SKIP",
            "-RunInstallerFaultPath",
            "runner_sha256",
            "argv",
        ):
            with self.subTest(token=token):
                self.assertIn(token, runner)
        for crash_point in (
            "after-journal-terminal-commit",
            "after-terminal-receipt-commit",
            "after-snapshot-cleanup",
            "after-snapshot-receipt-commit",
            "after-journal-cleanup",
        ):
            with self.subTest(crash_point=crash_point):
                self.assertIn(crash_point, cleanup)
        self.assertIn("$RunInstallerCrashMatrix", cleanup)
        self.assertIn("cases = @($cases.ToArray())", cleanup)
        self.assertIn("suites = @($rows.ToArray())", runner)
        self.assertIn("$g12MinimumOuterTimeoutSeconds = [Math]::Min(3600, $ControllerTimeoutSeconds + 120)", runner)
        self.assertIn("$g12OuterTimeoutSeconds", runner)
        self.assertIn("RemoteHarnessTimeoutSeconds", runner)
        self.assertIn("g12_outer_timeout_seconds", runner)
        self.assertIn("terminal_crash_matrix_timeout_seconds", runner)

    def test_windows_suite_runner_accepts_zero_argument_suites(self) -> None:
        runner = (ROOT / "tests" / "windows" / "run_windows_suite.ps1").read_text(encoding="utf-8")
        invoke = runner[runner.index("function Invoke-BoundedSuiteProcess") : runner.index("function Get-OutputStatus")]
        self.assertIn("[AllowEmptyCollection()]", invoke)
        self.assertIn("[string[]]$Arguments = @()", invoke)

    def test_windows_suite_runner_forwards_only_supported_suite_parameters(self) -> None:
        runner = (ROOT / "tests" / "windows" / "run_windows_suite.ps1").read_text(encoding="utf-8")
        g12_start = runner.index("if ($suite -ceq 'test_g12_preserve_move_fault_path.ps1')")
        terminal_start = runner.index("elseif ($suite -ceq 'test_terminal_cleanup_contracts.ps1'", g12_start)
        g12 = runner[g12_start:terminal_start]
        terminal_end = runner.index("    $process = Invoke-BoundedSuiteProcess", terminal_start)
        terminal = runner[terminal_start:terminal_end]
        self.assertIn("'-PopplerPath'", g12)
        self.assertNotIn("'-FixturePath'", g12)
        self.assertIn("'-FixturePath'", terminal)
        self.assertNotIn("'-PopplerPath'", terminal)


if __name__ == "__main__":
    unittest.main()
