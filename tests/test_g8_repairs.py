from __future__ import annotations

import hashlib
import importlib.util
import os
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "G8 test",
            "GIT_AUTHOR_EMAIL": "g8@example.invalid",
            "GIT_COMMITTER_NAME": "G8 test",
            "GIT_COMMITTER_EMAIL": "g8@example.invalid",
        },
    )
    return result.stdout.strip()


class G8SourceBundleRepairTests(unittest.TestCase):
    def test_builder_rejects_dirty_tracked_bytes_in_git_worktree(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "tracked.txt").write_text("committed\n", encoding="utf-8")
            run_git(source, "init", "-q")
            run_git(source, "add", "tracked.txt")
            run_git(source, "commit", "-qm", "initial")
            (source / "tracked.txt").write_text("modified-but-uncommitted\n", encoding="utf-8")

            with self.assertRaisesRegex(builder.SourceBundleError, "dirty|clean|worktree|tree"):
                builder.build_source_bundle(
                    source_root=source,
                    archive_path=tmp / "source.zip",
                    manifest_path=tmp / "manifest.json",
                    repository="github:Junsung-Lee-coder/hwpx-editor-server",
                    commit="unused",
                    tree="unused",
                )

    def test_builder_rejects_symlinked_ancestor_of_tracked_member(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            linked = source / "linked"
            linked.mkdir()
            (linked / "payload.txt").write_text("committed\n", encoding="utf-8")
            run_git(source, "init", "-q")
            run_git(source, "add", "linked/payload.txt")
            run_git(source, "commit", "-qm", "initial")
            external = tmp / "external"
            external.mkdir()
            (external / "payload.txt").write_text("EXTERNAL-SECRET\n", encoding="utf-8")
            (linked / "payload.txt").unlink()
            linked.rmdir()
            linked.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(builder.SourceBundleError, "symlink|reparse|ancestor"):
                builder.build_source_bundle(
                    source_root=source,
                    archive_path=tmp / "source.zip",
                    manifest_path=tmp / "manifest.json",
                    repository="r",
                    commit="unused",
                    tree="unused",
                )

    def test_gitless_manifest_is_explicitly_unverified(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "README.md").write_text("gitless\n", encoding="utf-8")
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=tmp / "source.zip",
                manifest_path=tmp / "manifest.json",
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            self.assertEqual(manifest["identity_source"], "asserted-gitless")
            self.assertFalse(manifest["identity_verified"])

    def test_verifier_rejects_archive_with_excessive_compression_ratio(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            payload = b"0" * (2 * 1024 * 1024)
            archive = tmp / "source.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("payload.bin", payload)
            manifest = {
                "schema_version": "hwpx/source-bundle/v1",
                "repository": "r",
                "commit": "e" * 40,
                "tree": "f" * 40,
                "identity_source": "asserted-gitless",
                "identity_verified": False,
                "file_count": 1,
                "files": [
                    {
                        "path": "payload.bin",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
                "archive": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            manifest_path = tmp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "compression|resource|uncompressed|ratio"):
                verifier.verify_source_bundle(
                    archive_path=archive,
                    manifest_path=manifest_path,
                    destination=tmp / "extract",
                    expected_repository="r",
                    expected_commit="e" * 40,
                    expected_tree="f" * 40,
                )

    def test_verifier_requires_explicit_identity_provenance(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            payload = b"payload"
            archive = tmp / "source.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr("payload.txt", payload)
            manifest = {
                "schema_version": "hwpx/source-bundle/v1",
                "repository": "r",
                "commit": "asserted-commit",
                "tree": "asserted-tree",
                "file_count": 1,
                "files": [{"path": "payload.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}],
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            manifest_path = tmp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "identity|provenance"):
                verifier.verify_source_bundle(
                    archive_path=archive,
                    manifest_path=manifest_path,
                    destination=tmp / "extract",
                )


class G8WindowsContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_fresh_root_preimage_is_captured_before_default_receipt_write(self) -> None:
        text = self.read("install_windows.ps1")
        try_body = text[text.index("try {\n    Assert-NoReparsePath -Path $SourceRoot"):]
        root_probe = try_body.index("$rootExisted")
        setup_start = try_body.index("$source =")
        first_receipt = try_body.index("Save-InstallerReceipt", setup_start)
        self.assertLess(root_probe, first_receipt)
        self.assertIn("receipt_preimage", text)
        self.assertIn("[System.IO.Path]::GetTempPath()", text)

    def test_process_matching_is_boundary_safe_and_listener_is_candidate_bound(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        self.assertNotIn("IndexOf($canonicalRoot", common)
        self.assertIn("Test-CanonicalProcessIdentity", common)
        self.assertIn("boundary", common)
        self.assertIn("listener", installer)
        self.assertIn("process_id", verifier)
        self.assertIn("candidate_generation", verifier)

    def test_manifest_copy_streams_expected_source_and_reverifies_candidate(self) -> None:
        installer = self.read("install_windows.ps1")
        common = self.read("windows_install_common.psm1")
        self.assertIn("Copy-FileVerified", common)
        self.assertIn("ExpectedSha256", common)
        self.assertIn("ExpectedSize", common)
        self.assertIn("candidate_manifest", installer)
        self.assertIn("Get-SourceManifest -SourceRoot $candidateRoot", installer)
        self.assertIn("destination_sha256", installer)

    def test_native_fixture_acceptance_parses_and_binds_every_command(self) -> None:
        verifier = self.read("verify_windows.ps1")
        for token in (
            "ConvertFrom-Json",
            "managed_fixture",
            "live_session_bound",
            "working_copy_id",
            "source_path",
            "manifest_sha256",
            "requested_page",
            "proof_manifest",
            "close_confirmed",
            "candidate_generation",
        ):
            with self.subTest(token=token):
                self.assertTrue(token in verifier, token)

    def test_task_readback_contains_trigger_settings_and_logon_identity(self) -> None:
        common = self.read("windows_install_common.psm1")
        for token in (
            "trigger_type",
            "trigger_user",
            "AtLogOn",
            "logon_type",
            "run_level",
            "settings",
            "StartWhenAvailable",
            "task_identity_hash",
        ):
            with self.subTest(token=token):
                self.assertTrue(token in common, token)

    def test_writer_uses_canonical_hwp_task_namespace_and_installed_runtime(self) -> None:
        writer = self.read("writer_v1.ps1")
        self.assertIn("HWP_API_TASK_NAME", writer)
        self.assertIn("HWP_WORKER_TASK_NAME", writer)
        self.assertIn(".hwpx-install", writer)
        self.assertIn(".venv\\Scripts\\python.exe", writer)
        self.assertNotIn("$env:HWPX_API_TASK_NAME", writer)
        self.assertNotIn("$env:HWPX_WORKER_TASK_NAME", writer)

    def test_installer_and_verifier_read_canonical_settings_from_env_file(self) -> None:
        common = self.read("windows_install_common.psm1")
        installer = self.read("install_windows.ps1")
        verifier = self.read("verify_windows.ps1")
        self.assertIn("function Get-ConfiguredEnvValue", common)
        for text in (installer, verifier):
            for token in ("Get-ConfiguredEnvValue", "HWP_API_TASK_NAME", "HWP_WORKER_TASK_NAME", "HWP_SOURCE_MANIFEST"):
                with self.subTest(token=token):
                    self.assertIn(token, text)

    def test_installed_runtime_prefers_candidate_venv_over_bootstrap_python_override(self) -> None:
        writer = self.read("writer_v1.ps1")
        verifier = self.read("verify_windows.ps1")
        self.assertLess(writer.index("$VenvPython"), writer.index("$ConfiguredPython"))
        self.assertLess(verifier.index("$candidate = Join-Path $install '.venv\\Scripts\\python.exe'"), verifier.index("$configured = [Environment]::GetEnvironmentVariable('HWP_PYTHON')"))

    def test_cli_lifecycle_json_contract_exposes_binding_identity(self) -> None:
        cli = (ROOT / "local_cli_v1" / "main.py").read_text(encoding="utf-8")
        for token in (
            "open_parser.add_argument('--json'",
            "status_parser.add_argument('--json'",
            "close_parser.add_argument('--json'",
            "working_copy_id",
            "live_session_bound",
            "source_path",
        ):
            with self.subTest(token=token):
                self.assertIn(token, cli)

    def test_cli_lifecycle_json_flags_parse_for_native_caller(self) -> None:
        from local_cli_v1.main import build_parser

        parser = build_parser()
        for argv in (
            ["open", "fixture.hwpx", "--json"],
            ["status", "--json"],
            ["close", "--json"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(bool(parser.parse_args(argv).json))

    def test_cli_proof_manifest_records_output_and_source_identity(self) -> None:
        cli = (ROOT / "local_cli_v1" / "main.py").read_text(encoding="utf-8")
        for token in ("output_sha256", "output_bytes", "source_hwp_sha256", "working_copy_id", "requested_page"):
            with self.subTest(token=token):
                self.assertIn(token, cli)

    def test_verifier_fixture_uses_isolated_cli_state_path(self) -> None:
        verifier = self.read("verify_windows.ps1")
        for token in ("HWPX_LOCAL_STATE_PATH", "previousLocalStateSetting", "local-cli-state.json"):
            with self.subTest(token=token):
                self.assertIn(token, verifier)

    def test_cli_proof_manifest_hashes_exact_output_and_source(self) -> None:
        from local_cli_v1 import main as cli_main

        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "fixture.hwpx"
            output = tmp / "page-001.png"
            source.write_bytes(b"fixture-bytes")
            output.write_bytes(b"rendered-proof")
            with patch.object(cli_main, "load_state", return_value={"source_path": str(source), "session_id": "sid-1"}):
                manifest_path = cli_main._write_artifact_manifest(
                    "page-screenshot",
                    output,
                    extra={"page": 1, "requested_page": 1},
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["output_bytes"], output.stat().st_size)
            self.assertEqual(manifest["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(manifest["source_hwp_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest["working_copy_id"], "sid-1")
            self.assertEqual(manifest["requested_page"], 1)


if __name__ == "__main__":
    unittest.main()
