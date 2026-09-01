from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.source_bundle_policy import is_prohibited_member


ROOT = Path(__file__).resolve().parents[1]


class G3IntegratedRepairTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_source_policy_matches_gitignore_local_credentials_and_overrides(self) -> None:
        for name in (
            "service-account.json",
            "config.local.json",
            "config.override.toml",
            "local.settings.json",
            ".DS_Store",
            "Thumbs.db",
            ".vscode/settings.json",
            "htmlcov/index.html",
            "worker.pid",
            "native.pyd",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_prohibited_member(name))
                self.assertTrue(is_prohibited_member(f"nested/{name}"))
        self.assertTrue(is_prohibited_member("generated.manifest.json"))
        self.assertFalse(is_prohibited_member("nested/generated.manifest.json"))
        self.assertFalse(is_prohibited_member("app/command_packages/foo/manifest.json"))

    def test_gitignore_covers_canonical_private_policy_names_and_suffixes(self) -> None:
        gitignore = self.read(".gitignore")
        for pattern in (
            "id_rsa",
            "authorized_keys",
            "secrets.json",
            "tokens.json",
            "cookies.json",
            "*.p12",
            "*.pfx",
            "*.crt",
            "*.cer",
            "*.der",
            "*.kdbx",
            "*credential*",
            "*secret*",
            "*password*",
            "*passwd*",
            "*token*",
            "*cookie*",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, gitignore)

    def test_function_style_gate_binds_nonempty_exact_test_ids(self) -> None:
        text = self.read("scripts/run_function_style_tests.py")
        self.assertIn("EXPECTED_TEST_IDS", text)
        self.assertIn("EXPECTED_TEST_COUNT", text)
        self.assertIn("discovered != EXPECTED_TEST_COUNT", text)
        self.assertIn("tuple(qualified_names) != EXPECTED_TEST_IDS", text)
        self.assertIn("if discovered == 0", text)

    def test_common_uses_secured_machine_lifecycle_locks(self) -> None:
        text = self.read("scripts/windows_install_common.psm1")
        writer = self.read("scripts/writer_v1.ps1")
        self.assertIn("MutexSecurity", text)
        self.assertIn("Enter-MachineLifecycleLock", text)
        for kind in ("root", "task", "port", "role", "receipt"):
            self.assertIn(kind, text)
        self.assertIn("return ('Global' + [char]92 + 'HWPX-Install-'", text)
        stop = text[text.index("function Stop-InstallProcesses") : text.index("function Wait-ScheduledTaskInactive")]
        self.assertIn("ProcessAuthority]::Terminate($processHandle)", stop)
        self.assertNotIn("Stop-Process -Id $processId", stop)
        self.assertNotIn("Select-Object -Reverse", text)
        self.assertIn("Assert-PathObjectIdentity", writer)
        self.assertIn("writerLifecycleLock", writer)
        self.assertIn("writerRootIdentity", writer)

    def test_manifest_reader_hashes_the_same_opened_bytes(self) -> None:
        text = self.read("scripts/windows_install_common.psm1")
        function = text[text.index("function Get-SourceManifest") : text.index("function Get-ScheduledTaskIdentity")]
        self.assertIn("File]::Open", function)
        self.assertIn("manifest_bytes", function)
        self.assertIn("manifestHash", function)
        self.assertIn("Assert-PathObjectIdentity", function)
        self.assertNotIn("ReadAllText($manifestFile) | ConvertFrom-Json", function)

    def test_manifest_closure_ignores_runtime_paths_before_private_markers(self) -> None:
        text = self.read("scripts/windows_install_common.psm1")
        function = text[text.index("function Get-SourceManifest") : text.index("function Get-ScheduledTaskIdentity")]
        actual_files = function[function.index("foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -File") :]
        runtime_skip = actual_files.index("if ($ignoredRuntimePath) { continue }")
        private_marker = actual_files.index("Test-ProhibitedPrivateSourceMember -RelativePath $relativeActual")
        self.assertLess(runtime_skip, private_marker)
        self.assertIn(r"requests\cookies.py", function)

    def test_installer_admits_requested_receipt_after_safe_external_bootstrap(self) -> None:
        text = self.read("scripts/install_windows.ps1")
        try_start = text.index("try {", text.index("$receipt = [ordered]@{"))
        preamble = text[:try_start]
        self.assertNotIn("Get-CanonicalPath -Path $ReceiptPath", preamble)
        self.assertIn("requestedReceiptPath", text)
        self.assertIn("Assert-ReceiptPathAdmission", text)
        self.assertIn("Write-InstallTransactionJournal", text)
        self.assertIn("-RemoveSnapshot:$false", text)
        self.assertLess(text.index("$phase = 'dependency'"), text.index("$poppler = Ensure-UserScopePoppler"))

    def test_verifier_binds_invocation_and_closing_generation(self) -> None:
        text = self.read("scripts/verify_windows.ps1")
        self.assertIn("[string]$RunId", text)
        self.assertIn("Enter-InstallLifecycleLock", text)
        self.assertIn("opening_status", text)
        self.assertIn("closing_generation_readback", text)
        self.assertIn("ended_at_utc", text)
        self.assertIn("Assert-PathObjectIdentity", text)

    def test_readiness_has_current_worker_freshness_and_atomic_persistence(self) -> None:
        readiness = self.read("app/readiness.py")
        worker = self.read("app/worker.py")
        api = self.read("app/api_server.py")
        service = self.read("app/local_cli_service.py")
        self.assertIn("atomic_write_json", readiness)
        for field in ("worker_pid", "worker_start_identity", "candidate_generation", "run_id", "expires_at", "heartbeat"):
            self.assertIn(field, readiness)
        self.assertIn("not_ready", worker)
        self.assertIn("readiness_matches_current_worker", worker)
        self.assertIn("readiness_heartbeat", worker)
        self.assertIn("readiness_matches_current_worker", api)
        self.assertIn("readiness_matches_current_worker", service)
        self.assertIn("cleanup_errors", readiness)

    def test_ci_enumerates_authoritative_ps51_manifest_and_g23(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")
        self.assertIn("suite-manifest.json", workflow)
        self.assertIn("test_g23_native_helper_repairs.ps1", workflow)
        self.assertIn("eligible", workflow)
        source_workflow = self.read(".github/workflows/source-bundle.yml")
        self.assertNotIn("actions/upload-artifact@", source_workflow)
        self.assertIn("verified-source", source_workflow)
        self.assertIn("compileall", source_workflow)
        self.assertIn("run_function_style_tests.py", source_workflow)

    def test_python_admission_binds_lock_abi(self) -> None:
        installer = self.read("scripts/install_windows.ps1")
        self.assertIn("Get-PythonRuntimeIdentity", installer)
        self.assertIn("'-3.13'", installer)
        self.assertNotIn("prefix = @('-3')", installer)
        for field in ("major", "minor", "pointer_bits", "implementation"):
            self.assertIn(field, installer)
        self.assertIn("FAIL_DEPENDENCY", installer)

    def test_process_snapshot_preservation_is_generation_bound(self) -> None:
        common = self.read("scripts/windows_install_common.psm1")
        self.assertIn("PreserveProcessIdentities", common)
        self.assertIn("Test-InstallProcessIdentityMatch", common)
        self.assertIn("creation_date", common)
        self.assertIn("command_line_sha256", common)
        self.assertIn("start_identity", common)
        self.assertIn("nativeStartIdentity", common)
        self.assertIn("start_identity = Get-ProcessGenerationIdentity", common)
        installer = self.read("scripts/install_windows.ps1")
        self.assertNotIn("$preservedProcessIds = @($snapshotData.processes", installer)

    def test_receipt_recovery_copy_survives_failed_validation(self) -> None:
        common = self.read("scripts/windows_install_common.psm1")
        self.assertIn("$readbackValidated", common)
        self.assertIn(".HOLD", common)
        self.assertIn("last known-good receipt", common)

    def test_dependency_mutation_is_not_reported_as_preflight(self) -> None:
        installer = self.read("scripts/install_windows.ps1")
        self.assertIn("dependency-started", installer)
        self.assertIn("retained_external_mutation", installer)
        self.assertIn("$dependencyMutationAttempted", installer)
        self.assertIn("if ($dependencyFailure)", installer)

    def test_powershell_source_manifest_uses_canonical_private_names(self) -> None:
        installer = self.read("scripts/install_windows.ps1")
        common = self.read("scripts/windows_install_common.psm1")
        for name in ("service-account.json", "config.local.", "config.override.", "local.settings."):
            self.assertIn(name, common)
        self.assertIn("Test-ProhibitedPrivateSourceMember", installer)

    def test_source_publication_executes_extracted_archive_gates(self) -> None:
        workflow = self.read(".github/workflows/source-bundle.yml")
        self.assertIn("python -m compileall", workflow)
        self.assertIn("verified-source", workflow)
        self.assertIn("python scripts/run_function_style_tests.py", workflow)
        self.assertIn("unittest discover", workflow)
        self.assertIn("source-bundle-gates", workflow)

    def test_verifier_receipt_is_unique_and_nonpass_before_checks(self) -> None:
        verifier = self.read("scripts/verify_windows.ps1")
        self.assertIn("[string]$RunId", verifier)
        self.assertIn("opening_status", verifier)
        self.assertIn("receipt_path_admission", verifier)
        self.assertIn("same-path", verifier.lower())

    def test_installer_binds_verifier_receipt_bytes_and_schema(self) -> None:
        installer = self.read("scripts/install_windows.ps1")
        common = self.read("scripts/windows_install_common.psm1")
        self.assertIn("Read-BoundedJsonObject", common)
        self.assertIn("verification_receipt", installer)
        self.assertIn("readback_validated", installer)
        self.assertIn("candidate_generation", installer)

    def test_transaction_journal_covers_activation_ownership_and_reentry(self) -> None:
        installer = self.read("scripts/install_windows.ps1")
        common = self.read("scripts/windows_install_common.psm1")
        for state in ("backup-claim-planned", "backup-claim-created", "predecessor-move-started", "activation-root-created", "activation-copy-started", "tasks-registering"):
            self.assertIn(state, installer)
        self.assertIn("backup_claim_path", installer)
        self.assertIn("install_root_created_by_run", installer)
        self.assertIn("install_root_identity", installer)
        self.assertIn("Remove-RunPathClaim", installer)
        self.assertIn("stableStream", common)


if __name__ == "__main__":
    unittest.main()
