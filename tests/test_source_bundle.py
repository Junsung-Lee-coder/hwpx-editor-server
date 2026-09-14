from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceBundleTests(unittest.TestCase):
    def test_parser_smoke_uses_runtime_neutral_samples(self) -> None:
        smoke = (ROOT / "scripts" / "smoke_output_parser_static.py").read_text(encoding="utf-8")
        self.assertNotIn("fixtures/local_cli", smoke)
        self.assertIn("def _sample_selected_response", smoke)
        self.assertIn("def _sample_where_response", smoke)
        self.assertIn("def _sample_context_response", smoke)
        self.assertIn("def _sample_selection_proof_response", smoke)

    def test_ci_runs_function_style_tests_with_a_portable_runner(self) -> None:
        runner = ROOT / "scripts" / "run_function_style_tests.py"
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertTrue(runner.is_file())
        self.assertIn("run_function_style_tests.py", workflow)
        self.assertIn("tmp_path", runner.read_text(encoding="utf-8"))

    def test_testing_docs_require_independent_source_bundle_identity_binding(self) -> None:
        testing = (ROOT / "TESTING.md").read_text(encoding="utf-8")
        for token in (
            "--expected-repository",
            "--expected-commit",
            "--expected-tree",
            "--expected-manifest-sha256",
        ):
            with self.subTest(token=token):
                self.assertTrue(token in testing, f"source-bundle documentation omits {token}")
        self.assertIn("smoke_native_table_command_static.py", testing)
        self.assertIn("smoke_cli_json_envelope_parity_static.py", testing)

    def test_build_and_verify_reproduce_tracked_files_without_git(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "README.md").write_text("portable\n", encoding="utf-8")
            (source / "config.example").write_text("HWP_API_PORT=8765\n", encoding="utf-8")
            (source / "nested").mkdir()
            (source / "nested" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"

            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="github:Junsung-Lee-coder/hwpx-editor-server",
                commit="a" * 40,
                tree="b" * 40,
            )
            verified = verifier.verify_source_bundle(
                archive_path=archive,
                manifest_path=manifest_path,
                destination=tmp / "extract",
                expected_repository="github:Junsung-Lee-coder/hwpx-editor-server",
                expected_commit="a" * 40,
                expected_tree="b" * 40,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                expected_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

            self.assertEqual(manifest["file_count"], 3)
            self.assertEqual(manifest["repository"], "github:Junsung-Lee-coder/hwpx-editor-server")
            self.assertEqual(manifest["commit"], "a" * 40)
            self.assertEqual(manifest["tree"], "b" * 40)
            self.assertRegex(manifest["archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(verified["mismatch_count"], 0)
            self.assertEqual(verified["unsafe_member_count"], 0)
            self.assertEqual((tmp / "extract" / "nested" / "main.py").read_text(encoding="utf-8"), "print('ok')\n")

    def test_archive_order_and_bytes_are_deterministic(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "b.txt").write_text("b", encoding="utf-8")
            (source / "a.txt").write_text("a", encoding="utf-8")
            first_archive = tmp / "first.zip"
            first_manifest = tmp / "first.json"
            second_archive = tmp / "second.zip"
            second_manifest = tmp / "second.json"
            first = builder.build_source_bundle(source_root=source, archive_path=first_archive, manifest_path=first_manifest, repository="r", commit="c" * 40, tree="d" * 40)
            second = builder.build_source_bundle(source_root=source, archive_path=second_archive, manifest_path=second_manifest, repository="r", commit="c" * 40, tree="d" * 40)

            self.assertEqual(first["files"], second["files"])
            self.assertEqual(first["archive_sha256"], second["archive_sha256"])
            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
            with zipfile.ZipFile(first_archive) as archive:
                self.assertEqual(archive.namelist(), ["a.txt", "b.txt"])

    def test_builder_rejects_symlinked_source_member(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            target = tmp / "target.txt"
            target.write_text("secret", encoding="utf-8")
            (source / "link.txt").symlink_to(target)
            with self.assertRaises(builder.SourceBundleError):
                builder.build_source_bundle(source_root=source, archive_path=tmp / "source.zip", manifest_path=tmp / "manifest.json", repository="r", commit="c" * 40, tree="d" * 40)

    def test_builder_rejects_symlinked_source_directory(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            target = tmp / "target"
            target.mkdir()
            (target / "hidden.py").write_text("print('hidden')\n", encoding="utf-8")
            (source / "linked").symlink_to(target, target_is_directory=True)
            with self.assertRaises(builder.SourceBundleError):
                builder.build_source_bundle(source_root=source, archive_path=tmp / "source.zip", manifest_path=tmp / "manifest.json", repository="r", commit="c" * 40, tree="d" * 40)

    def test_builder_fails_closed_when_git_metadata_exists_but_git_listing_fails(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / ".git").mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            with mock.patch.object(builder.subprocess, "run", side_effect=OSError("git unavailable")):
                with self.assertRaises(builder.SourceBundleError):
                    builder.build_source_bundle(source_root=source, archive_path=tmp / "source.zip", manifest_path=tmp / "manifest.json", repository="r", commit="c" * 40, tree="d" * 40)

    def test_builder_excludes_runtime_paths_case_insensitively(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "SpOoL").mkdir()
            (source / "SpOoL" / "runtime.db").write_bytes(b"runtime")
            (source / ".ENV").write_text("secret=not-for-bundle\n", encoding="utf-8")
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=tmp / "source.zip",
                manifest_path=tmp / "manifest.json",
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            self.assertEqual([entry["path"] for entry in manifest["files"]], ["main.py"])

    def test_builder_excludes_environment_override_files(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (source / ".env.local").write_text("SECRET=do-not-bundle\n", encoding="utf-8")
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=tmp / "source.zip",
                manifest_path=tmp / "manifest.json",
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            self.assertEqual([entry["path"] for entry in manifest["files"]], ["main.py"])

    def test_builder_excludes_all_fixture_members(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            fixture_dir = source / "fixtures" / "local_cli"
            fixture_dir.mkdir(parents=True)
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            fixture = fixture_dir / "command_bundle_where_response.json"
            fixture.write_text('{"ok": true}\n', encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            self.assertEqual([entry["path"] for entry in manifest["files"]], ["main.py"])
            verifier.verify_source_bundle(
                archive_path=archive,
                manifest_path=manifest_path,
                destination=tmp / "extract",
                expected_repository="r",
                expected_commit="c" * 40,
                expected_tree="d" * 40,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                expected_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertFalse((tmp / "extract" / "fixtures").exists())

    def test_builder_uses_stored_compression_for_highly_compressible_members(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "adversarial.bin").write_bytes(b"A" * (1024 * 1024))
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            verified = verifier.verify_source_bundle(
                archive_path=archive,
                manifest_path=manifest_path,
                destination=tmp / "extract",
                expected_repository="r",
                expected_commit="c" * 40,
                expected_tree="d" * 40,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                expected_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertEqual(verified["mismatch_count"], 0)
            self.assertEqual((tmp / "extract" / "adversarial.bin").stat().st_size, 1024 * 1024)
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(handle.getinfo("adversarial.bin").compress_type, zipfile.ZIP_STORED)

    def test_gitless_verification_requires_independent_identity(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(
                    archive_path=archive,
                    manifest_path=manifest_path,
                    destination=tmp / "without-independent-identity",
                )
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(
                    archive_path=archive,
                    manifest_path=manifest_path,
                    destination=tmp / "mismatched-identity",
                    expected_repository="r",
                    expected_commit="wrong",
                    expected_tree="d" * 40,
                )

    def test_gitless_manifest_rejects_malformed_commit_and_tree_ids(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        manifest = {
            "schema_version": "hwpx/source-bundle/v1",
            "repository": "r",
            "commit": "abc123",
            "tree": "tree123",
            "identity_source": "asserted-gitless",
            "identity_verified": False,
            "file_count": 0,
            "files": [],
            "archive_sha256": "a" * 64,
        }
        with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "object ids"):
            verifier._parse_manifest_bytes(json.dumps(manifest).encode("utf-8"), Path("manifest.json"))

    def test_builder_rejects_output_symlink_created_before_manifest_write(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            outside = tmp / "outside.json"
            outside.write_text("sentinel\n", encoding="utf-8")
            original = builder._write_deterministic_archive

            def replace_manifest_with_link(*args, **kwargs):
                original(*args, **kwargs)
                manifest_path.symlink_to(outside)

            with mock.patch.object(builder, "_write_deterministic_archive", replace_manifest_with_link):
                with self.assertRaises(builder.SourceBundleError):
                    builder.build_source_bundle(
                        source_root=source,
                        archive_path=archive,
                        manifest_path=manifest_path,
                        repository="r",
                        commit="c" * 40,
                        tree="d" * 40,
                    )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")

    def test_builder_rejects_archive_output_swap_before_hashing(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            outside = tmp / "outside.zip"
            outside.write_bytes(b"sentinel")
            original = builder._write_deterministic_archive

            def replace_archive_with_link(*args, **kwargs):
                original(*args, **kwargs)
                archive.unlink()
                archive.symlink_to(outside)

            with mock.patch.object(builder, "_write_deterministic_archive", replace_archive_with_link):
                with self.assertRaises(builder.SourceBundleError):
                    builder.build_source_bundle(
                        source_root=source,
                        archive_path=archive,
                        manifest_path=manifest_path,
                        repository="r",
                        commit="c" * 40,
                        tree="d" * 40,
                    )
            self.assertEqual(outside.read_bytes(), b"sentinel")

    def test_verifier_rejects_manifest_path_replacement_after_parse(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            original_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            original_archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            replacement = tmp / "replacement.json"
            replacement.write_text('{"forged":true}\n', encoding="utf-8")
            original_parse = verifier._parse_manifest_bytes

            def parse_then_replace(payload, path):
                parsed = original_parse(payload, path)
                manifest_path.unlink()
                manifest_path.symlink_to(replacement)
                return parsed

            with mock.patch.object(verifier, "_parse_manifest_bytes", parse_then_replace):
                with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "manifest.*(identity|changed)"):
                    verifier.verify_source_bundle(
                        archive_path=archive,
                        manifest_path=manifest_path,
                        destination=tmp / "extract",
                        expected_repository="r",
                        expected_commit="c" * 40,
                        expected_tree="d" * 40,
                        expected_manifest_sha256=original_manifest_sha256,
                        expected_archive_sha256=original_archive_sha256,
                    )

    def test_verifier_rejects_same_inode_manifest_mutation_after_parse(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            original_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            original_archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            original_stat = manifest_path.stat()
            original_parse = verifier._parse_manifest_bytes

            def parse_then_mutate(payload, path):
                parsed = original_parse(payload, path)
                mutated = manifest_path.read_bytes().replace(b'"repository": "r"', b'"repository": "x"', 1)
                self.assertNotEqual(mutated, manifest_path.read_bytes())
                manifest_path.write_bytes(mutated)
                os.utime(manifest_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                return parsed

            with mock.patch.object(verifier, "_parse_manifest_bytes", parse_then_mutate):
                with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "manifest.*changed"):
                    verifier.verify_source_bundle(
                        archive_path=archive,
                        manifest_path=manifest_path,
                        destination=tmp / "extract",
                        expected_repository="r",
                        expected_commit="c" * 40,
                        expected_tree="d" * 40,
                        expected_manifest_sha256=original_manifest_sha256,
                        expected_archive_sha256=original_archive_sha256,
                    )

    @unittest.skipUnless(os.name == "posix", "Git executable-mode probe requires POSIX chmod semantics")
    def test_builder_binds_git_file_mode_when_core_filemode_is_disabled(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            commands = [
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "Source Bundle Test"],
            ]
            for command in commands:
                subprocess.run(command, cwd=source, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            member = source / "main.py"
            member.write_text("print('ok')\n", encoding="utf-8")
            member.chmod(0o755)
            subprocess.run(["git", "add", "main.py"], cwd=source, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                ["git", "commit", "-qm", "mode"],
                cwd=source,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "config", "core.filemode", "false"], cwd=source, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            member.chmod(0o644)
            with self.assertRaisesRegex(builder.SourceBundleError, "mode"):
                builder.build_source_bundle(
                    source_root=source,
                    archive_path=tmp / "source.zip",
                    manifest_path=tmp / "manifest.json",
                    repository="r",
                )

    def test_verifier_rejects_archive_path_replacement_during_extraction(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            original_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            original_archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            replacement = tmp / "replacement.zip"
            replacement.write_bytes(b"not-the-authenticated-archive")
            original_resources = verifier._validate_archive_resources
            swapped = False

            def check_then_replace(infos):
                nonlocal swapped
                result = original_resources(infos)
                if not swapped:
                    swapped = True
                    archive.unlink()
                    archive.symlink_to(replacement)
                return result

            with mock.patch.object(verifier, "_validate_archive_resources", check_then_replace):
                with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "archive.*(identity|changed)"):
                    verifier.verify_source_bundle(
                        archive_path=archive,
                        manifest_path=manifest_path,
                        destination=tmp / "extract",
                        expected_repository="r",
                        expected_commit="c" * 40,
                        expected_tree="d" * 40,
                        expected_manifest_sha256=original_manifest_sha256,
                        expected_archive_sha256=original_archive_sha256,
                    )

    def test_verifier_has_a_native_windows_no_follow_open_path(self) -> None:
        verifier_source = (ROOT / "scripts" / "verify_source_bundle.py").read_text(encoding="utf-8")
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", verifier_source)
        self.assertIn("CreateFileW", verifier_source)
        self.assertIn("O_NOFOLLOW", verifier_source)

    def test_verified_git_manifest_requires_independent_manifest_binding(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        manifest = {
            "schema_version": "hwpx/source-bundle/v1",
            "repository": "github:example/project",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "identity_source": "git",
            "identity_verified": True,
        }
        with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "independent.*manifest"):
            verifier._validate_identity_binding(
                manifest,
                expected_repository="github:example/project",
                expected_commit="a" * 40,
                expected_tree="b" * 40,
                expected_manifest_sha256=None,
                actual_manifest_sha256="c" * 64,
            )

    def test_verified_git_manifest_rejects_unbound_identity_even_when_manifest_hash_is_omitted(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        manifest = {
            "schema_version": "hwpx/source-bundle/v1",
            "repository": "github:example/project",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "identity_source": "git",
            "identity_verified": True,
        }
        with self.assertRaises(verifier.SourceBundleVerificationError):
            verifier._validate_identity_binding(
                manifest,
                expected_repository=None,
                expected_commit=None,
                expected_tree=None,
                expected_manifest_sha256=None,
                actual_manifest_sha256="c" * 64,
            )

    def test_builder_rejects_empty_source_identity(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            for field in ("repository", "commit", "tree"):
                kwargs = {"repository": "r", "commit": "c", "tree": "t"}
                kwargs[field] = ""
                with self.subTest(field=field):
                    with self.assertRaises(builder.SourceBundleError):
                        builder.build_source_bundle(
                            source_root=source,
                            archive_path=tmp / f"{field}.zip",
                            manifest_path=tmp / f"{field}.json",
                            **kwargs,
                        )

    def test_builder_excludes_stale_root_manifests_and_archives(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "source-manifest.json").write_text("stale\n", encoding="utf-8")
            (source / "old.zip").write_bytes(b"stale archive")
            manifest = builder.build_source_bundle(
                source_root=source,
                archive_path=tmp / "new.zip",
                manifest_path=tmp / "new-manifest.json",
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            self.assertEqual([entry["path"] for entry in manifest["files"]], ["main.py"])

    def test_builder_rejects_symlink_even_when_it_targets_output(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            archive = source / "source.zip"
            archive.write_bytes(b"placeholder")
            (source / "alias.zip").symlink_to(archive)
            with self.assertRaises(builder.SourceBundleError):
                builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=tmp / "manifest.json", repository="r", commit="c" * 40, tree="d" * 40)

    def test_verifier_rejects_windows_ads_member_path(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            archive = tmp / "unsafe.zip"
            manifest = tmp / "manifest.json"
            data = b"not an alternate data stream\n"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("nested/file.txt:secret", data)
            manifest.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "nested/file.txt:secret",
                                "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=tmp / "extract")
            self.assertFalse((tmp / "extract").exists())

    def test_verifier_rejects_manifest_file_count_mismatch(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest = tmp / "manifest.json"
            builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=manifest, repository="r", commit="c" * 40, tree="d" * 40)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["file_count"] += 1
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=tmp / "extract")
            self.assertFalse((tmp / "extract").exists())

    def test_verifier_rejects_manifest_without_source_identity(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=manifest_path, repository="r", commit="c" * 40, tree="d" * 40)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("commit")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest_path, destination=tmp / "extract")
            self.assertFalse((tmp / "extract").exists())

    def test_verifier_rejects_manifest_without_archive_hash(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["archive_sha256"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest_path, destination=tmp / "extract")

    def test_verifier_rejects_symlinked_archive_input(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest = tmp / "manifest.json"
            builder.build_source_bundle(
                source_root=source,
                archive_path=archive,
                manifest_path=manifest,
                repository="r",
                commit="c" * 40,
                tree="d" * 40,
            )
            linked_archive = tmp / "linked-source.zip"
            linked_archive.symlink_to(archive)
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=linked_archive, manifest_path=manifest, destination=tmp / "extract")

    def test_verifier_rejects_runtime_member_paths(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            archive = tmp / "source.zip"
            manifest_path = tmp / "manifest.json"
            data = b"secret=do-not-extract\n"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(".env", data)
            manifest = {
                "schema_version": "hwpx/source-bundle/v1",
                "repository": "r",
                "commit": "c",
                "tree": "t",
                "file_count": 1,
                "files": [{"path": ".env", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest_path, destination=tmp / "extract")
            self.assertFalse((tmp / "extract").exists())

    def test_verifier_rejects_symlink_destination(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest = tmp / "manifest.json"
            builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=manifest, repository="r", commit="c" * 40, tree="d" * 40)
            target = tmp / "target"
            target.mkdir()
            destination = tmp / "extract"
            destination.symlink_to(target, target_is_directory=True)
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=destination)
            self.assertTrue(destination.is_symlink())
            self.assertFalse((target / "main.py").exists())

    def test_verifier_rejects_symlinked_destination_parent(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest = tmp / "manifest.json"
            builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=manifest, repository="r", commit="c" * 40, tree="d" * 40)
            target = tmp / "target"
            target.mkdir()
            linked_parent = tmp / "linked-parent"
            linked_parent.symlink_to(target, target_is_directory=True)
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=linked_parent / "extract")
            self.assertFalse((target / "extract").exists())

    def test_source_bundle_rejects_windows_invalid_member_names(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "CON.txt").write_text("device name\n", encoding="utf-8")
            with self.assertRaises(builder.SourceBundleError):
                builder.build_source_bundle(source_root=source, archive_path=tmp / "source.zip", manifest_path=tmp / "manifest.json", repository="r", commit="c" * 40, tree="d" * 40)

    def test_verifier_rejects_unsafe_archive_member_without_extracting(self) -> None:
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            archive = tmp / "unsafe.zip"
            manifest = tmp / "manifest.json"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escape.txt", "bad")
            manifest.write_text(json.dumps({"files": []}), encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=tmp / "extract")
            self.assertFalse((tmp / "escape.txt").exists())

    def test_verifier_does_not_delete_nonempty_destination(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source = tmp / "source"
            source.mkdir()
            (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = tmp / "source.zip"
            manifest = tmp / "manifest.json"
            builder.build_source_bundle(source_root=source, archive_path=archive, manifest_path=manifest, repository="r", commit="c" * 40, tree="d" * 40)
            destination = tmp / "extract"
            destination.mkdir()
            sentinel = destination / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(verifier.SourceBundleVerificationError):
                verifier.verify_source_bundle(archive_path=archive, manifest_path=manifest, destination=destination)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
