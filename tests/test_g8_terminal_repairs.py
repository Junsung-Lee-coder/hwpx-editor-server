from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import readiness
from app.interactive_session_manager import InteractiveSessionManager
from app.local_cli_runtime import LocalCliLiveSession
from app.poppler import resolve_pdftoppm
from local_cli_v1.state import StatePersistenceError, update_state


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class G8TerminalSourceCustodyRedTests(unittest.TestCase):
    def test_gitless_bundle_rejects_malformed_object_ids(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("portable\n", encoding="utf-8")
            with self.assertRaisesRegex(builder.SourceBundleError, "commit|tree|object|identity"):
                builder.build_source_bundle(
                    source_root=source,
                    archive_path=root / "source.zip",
                    manifest_path=root / "manifest.json",
                    repository="r",
                    commit="abc123",
                    tree="tree123",
                )

    def test_bundle_rejects_symlinked_output_leaf(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("portable\n", encoding="utf-8")
            target = root / "outside.zip"
            target.write_bytes(b"do-not-overwrite")
            archive = root / "source.zip"
            archive.symlink_to(target)
            with self.assertRaisesRegex(builder.SourceBundleError, "symlink|reparse|output"):
                builder.build_source_bundle(
                    source_root=source,
                    archive_path=archive,
                    manifest_path=root / "manifest.json",
                    repository="r",
                    commit="a" * 40,
                    tree="b" * 40,
                )
            self.assertEqual(target.read_bytes(), b"do-not-overwrite")


class G8TerminalReadinessRedTests(unittest.TestCase):
    def test_environment_generation_is_an_expected_assertion_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = root / "source-manifest.json"
            manifest = {
                "schema_version": "hwpx/source-bundle/v1",
                "repository": "r",
                "commit": "a" * 40,
                "tree": "b" * 40,
                "identity_source": "asserted-gitless",
                "identity_verified": False,
                "file_count": 0,
                "files": [],
            }
            manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            fake_settings = SimpleNamespace(source_manifest=manifest_path, spool_root=root, worker_name="worker")
            with patch.object(readiness, "settings", fake_settings), patch.dict(
                os.environ, {"HWP_CANDIDATE_GENERATION": "forged-generation"}, clear=False
            ):
                self.assertIsNone(readiness.resolve_candidate_generation())


class G8TerminalInteractiveStateRedTests(unittest.TestCase):
    def _settings(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            spool_root=root / "spool",
            logs_root=root / "logs",
            log_level="INFO",
            retention_days=7,
            api_host="127.0.0.1",
            api_port=8765,
            worker_name="test-worker",
            security_module_name="test-security",
            security_module_dll="test.dll",
        )

    def test_concurrent_updates_preserve_every_command_history_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = InteractiveSessionManager(self._settings(root))
            session = manager.open_session(
                source_path=root / "document.hwpx",
                source_filename="document.hwpx",
                file_size_bytes=1,
                content_type="application/octet-stream",
            )
            session_id = str(session["session_id"])
            barrier = threading.Barrier(12)
            errors: list[BaseException] = []

            def record(index: int) -> None:
                try:
                    barrier.wait()
                    manager.record_command(
                        f"command-{index}",
                        session_id=session_id,
                        summary=f"summary-{index}",
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=record, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            saved = json.loads(manager.state_path(session_id).read_text(encoding="utf-8"))
            commands = {item["command"] for item in saved["command_history"]}
            self.assertTrue({f"command-{index}" for index in range(12)}.issubset(commands))

    def test_same_second_verify_captures_have_distinct_step_bound_artifacts_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = InteractiveSessionManager(self._settings(root))
            session = {
                "session_id": "session-evidence",
                "state": "open",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            source_frame = root / "frame.png"
            source_frame.write_bytes(b"frame-a")
            source_metadata = root / "frame.json"
            source_metadata.write_text('{"captured_at":"2026-01-01T00:00:00+00:00"}\n', encoding="utf-8")
            verify = {
                "gui": {
                    "frame": {"captured_at": "2026-01-01T00:00:00+00:00", "ok": True},
                    "artifacts": {
                        "latest_frame_path": str(source_frame),
                        "latest_frame_metadata_path": str(source_metadata),
                    },
                }
            }
            first, _ = manager._bind_verify_step_gui_evidence(
                session,
                step_name="verify-pre",
                verify_result=verify,
                recorded_at="2026-01-01T00:00:01+00:00",
            )
            second, _ = manager._bind_verify_step_gui_evidence(
                session,
                step_name="verify-pre",
                verify_result=verify,
                recorded_at="2026-01-01T00:00:01+00:00",
            )
            first_path = Path(first["gui"]["primary_image"]["path"])
            second_path = Path(second["gui"]["primary_image"]["path"])
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first_path.read_bytes(), source_frame.read_bytes())
            self.assertEqual(second_path.read_bytes(), source_frame.read_bytes())
            first_slug = first["gui"]["artifacts"]["step_bound_evidence_url"].rsplit("/", 1)[-1]
            self.assertEqual(
                manager.load_verify_evidence_artifact(
                    session_id="session-evidence",
                    step_name="verify-pre",
                    recorded_at=first_slug,
                )["frame_path"].read_bytes(),
                b"frame-a",
            )


class G8TerminalRuntimeFinalizerRedTests(unittest.TestCase):
    def test_cleanup_exception_cannot_prevent_closed_signal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = LocalCliLiveSession(
                session_id="finalizer-test",
                session_root=Path(raw),
                working_copy_path=Path(raw) / "document.hwpx",
                source_filename="document.hwpx",
            )
            fake_pyhwpx = types.ModuleType("pyhwpx")
            setattr(fake_pyhwpx, "Hwp", object)
            runtime = __import__("app.local_cli_runtime", fromlist=["close_hwp_instance"])
            with patch.dict(sys.modules, {"pyhwpx": fake_pyhwpx}), patch.object(
                runtime,
                "create_hwp_instance_with_recovery",
                side_effect=RuntimeError("startup failed"),
            ), patch.object(
                runtime,
                "close_hwp_instance",
                side_effect=RuntimeError("close failed"),
            ):
                session._run()
            self.assertTrue(session._closed.is_set())

    def test_pending_probe_cannot_become_ready_by_flipping_top_level_fields(self) -> None:
        identity = readiness.current_worker_identity()
        snapshot = readiness.build_current_run_not_ready_snapshot(
            run_id="pending-run",
            candidate_generation="a" * 40 + ":" + "b" * 40 + ":" + "c" * 64,
            worker_identity=identity,
        )
        snapshot["ready"] = True
        snapshot["status"] = "ready"
        self.assertFalse(
            readiness.readiness_matches_current_worker(
                snapshot,
                candidate_generation=snapshot["candidate_generation"],
                run_id="pending-run",
            )
        )


class G8TerminalPopplerRedTests(unittest.TestCase):
    def test_windows_resolution_rejects_command_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wrapper = root / "pdftoppm.cmd"
            wrapper.write_text("@echo off\r\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "executable|pdftoppm|allowed|not"):
                resolve_pdftoppm(
                    explicit=wrapper,
                    path_entries=[],
                    winget_roots=[],
                    platform="win32",
                )

    def test_winget_budget_exhaustion_is_explicit_not_nondeterministic_truncation(self) -> None:
        poppler = __import__("app.poppler", fromlist=["_iter_winget_candidates"])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "a").mkdir()
            (root / "b").mkdir()
            with self.assertRaisesRegex(Exception, "budget|bounded|deterministic|entry"):
                list(
                    poppler._iter_winget_candidates(
                        [root],
                        platform="win32",
                        max_entries=1,
                    )
                )


class G8TerminalInstallerContractRedTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_snapshot_restore_uses_exact_no_clobber_registration(self) -> None:
        common = self._read("windows_install_common.psm1")
        restore = common[common.index("function Restore-InstallSnapshot") :]
        self.assertIn("Register-ScheduledTaskExactNoClobber", restore)
        self.assertNotIn("Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Xml ([string]$task.xml) -Force", restore)

    def test_preserve_move_stops_queued_tasks_before_unregister(self) -> None:
        installer = self._read("install_windows.ps1")
        admission = installer[installer.index("function Restore-PreMoveTaskAdmission") : installer.index("function New-PythonInvocation")]
        preserve = installer[installer.index("Write-InstallTransactionJournal -State 'task-disable-started'") : installer.index("Write-InstallTransactionJournal -State 'tasks-disabled'")]
        common = self._read("windows_install_common.psm1")
        self.assertIn("@('Running', 'Queued')", admission)
        self.assertIn("@('Running', 'Queued')", preserve)
        self.assertIn("Stop-ScheduledTaskExactAndWait", preserve)
        self.assertIn("if ($state -notin @('Running', 'Queued'))", common)

    def test_fresh_install_recovery_passes_run_owned_install_root_to_cleanup(self) -> None:
        installer = self._read("install_windows.ps1")
        recovery = installer[installer.index("function Invoke-StaleInstallTransactionRecovery") : installer.index("function Save-InstallerReceipt")]
        self.assertIn("if ($installExists -and $installCreatedByRun -and -not $backupExists)", recovery)
        self.assertIn("$restoreCandidate = $install", recovery)

    def test_port_preflight_enumerates_all_listener_addresses(self) -> None:
        installer = self._read("install_windows.ps1")
        port = installer[installer.index("function Assert-PortAvailable") : installer.index("function Assert-TaskCompatibility")]
        self.assertIn("Get-NetTCPConnection -LocalPort $Port -State Listen", port)
        self.assertNotIn("-LocalAddress '127.0.0.1'", port)
        self.assertIn("local_address", port)

    def test_generated_fallback_manifest_is_run_owned_and_cleanup_bound(self) -> None:
        installer = self._read("install_windows.ps1")
        self.assertIn("generatedSourceManifestPath", installer)
        self.assertIn("generatedSourceManifestSha256", installer)
        self.assertIn("generatedSourceManifestIdentity", installer)
        self.assertIn("Remove-GeneratedSourceManifest", installer)
        self.assertIn("generated_manifest_path", installer)

    def test_module_bootstrap_failure_has_a_minimal_receipt_path(self) -> None:
        installer = self._read("install_windows.ps1")
        bootstrap_end = installer.index("$source = $null")
        bootstrap = installer[:bootstrap_end]
        self.assertIn("bootstrapReceiptPath", bootstrap)
        self.assertIn("Import-Module", bootstrap)
        self.assertIn("catch", bootstrap)
        self.assertIn("FAIL_BOOTSTRAP", bootstrap)

    def test_destructive_path_mutations_use_handle_bound_identity_operations(self) -> None:
        common = self._read("windows_install_common.psm1")
        installer = self._read("install_windows.ps1")
        self.assertIn("DeletePathIfIdentity", common)
        self.assertIn("MovePathIfIdentity", common)
        self.assertIn("function Remove-PathIdentityExact", common)
        self.assertIn("function Move-PathIdentityExact", common)
        self.assertIn("Remove-PathIdentityExact", installer)
        self.assertIn("Move-PathIdentityExact", installer)


class G8TerminalCliCasRedTests(unittest.TestCase):
    def test_expected_generation_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            with self.assertRaises(StatePersistenceError):
                update_state(lambda state: {**state, "session_id": "s1"}, path, expected_generation=True)

    def test_persisted_boolean_generation_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            path.write_text(json.dumps({"state_generation": True}), encoding="utf-8")
            with self.assertRaises(StatePersistenceError):
                update_state(lambda state: state, path)


if __name__ == "__main__":
    unittest.main()
