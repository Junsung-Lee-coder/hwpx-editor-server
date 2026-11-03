"""Exported tests must find their tracked synthetic fixtures in the source bundle.

The source-bundle workflow runs full discovery from the verified extraction, so
every tracked file under ``tests/fixtures/`` that those tests read has to travel
in the archive.  The runtime member list (``files``) keeps excluding every
directory named ``fixtures``: installers consume that list, and other fixture
directories, runtime output and private names stay out of the bundle.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.hermetic import write_exact


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_") + "_fixtures", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Fixture Test", "-c", "user.email=fixture@example.com",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
            *args,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def write(path: Path, text: str) -> None:
    # The manifest and archive assertions compare these exact bytes.
    write_exact(path, text)


FIXTURE_TEXT = "<hs:sec>public synthetic fixture</hs:sec>\n"

CONSUMER_TEST = '''import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample" / "table.section0.xml"


class ReadsTrackedFixture(unittest.TestCase):
    def test_reads_fixture_from_the_exported_tree(self):
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), {fixture!r})
'''.format(fixture=FIXTURE_TEXT)

PUBLIC_FIXTURES = {
    "tests/fixtures/sample/table.section0.xml": FIXTURE_TEXT,
    "tests/fixtures/sample/lifecycle.py": "VALUE = 1\n",
}

SOURCE_FILES = {
    "app/main.py": "print('ok')\n",
    "tests/test_reads_fixture.py": CONSUMER_TEST,
}

# Tracked paths that must stay out of the bundle entirely.
EXCLUDED_TRACKED = {
    "fixtures/local_cli/response.json": '{"ok": true}\n',
    "docs/fixtures/notes.txt": "other fixture directory\n",
    "app/fixtures/sample.json": "{}\n",
    "tests/fixtures/output/run.txt": "runtime output inside fixtures\n",
    "tests/fixtures/evidence/receipt.json": "{}\n",
    "tests/fixtures/sample/report.hwpx": "document\n",
    "tests/fixtures/sample/cache.zip": "archive\n",
    "tests/fixtures/sample/run.log": "log\n",
    "tests/fixtures/.env.fixture": "SECRET=do-not-bundle\n",
    "tests/fixtures/sample/api_token.json": "{}\n",
    "tests/fixtures/__pycache__/lifecycle.cpython-313.pyc": "bytecode\n",
}


class TrackedTestFixtureBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = load_script("build_source_bundle.py")
        self.verifier = load_script("verify_source_bundle.py")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git_repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        for name, text in {**SOURCE_FILES, **PUBLIC_FIXTURES, **EXCLUDED_TRACKED}.items():
            write(repo / name, text)
        git(repo, "add", "-f", "--", ".")
        git(repo, "commit", "-q", "-m", "fixture bundle sample")
        # Untracked files never enter a Git-backed bundle, under tests/fixtures too.
        write(repo / "tests/fixtures/sample/untracked.xml", "untracked\n")
        return repo

    def _build_and_verify(self, source: Path, *, commit: str, tree: str, repository: str = "r"):
        archive = self.tmp / "bundle" / "source.zip"
        manifest_path = self.tmp / "bundle" / "manifest.json"
        manifest = self.builder.build_source_bundle(
            source_root=source,
            archive_path=archive,
            manifest_path=manifest_path,
            repository=repository,
            commit=commit,
            tree=tree,
        )
        extract = self.tmp / "extract"
        result = self.verifier.verify_source_bundle(
            archive_path=archive,
            manifest_path=manifest_path,
            destination=extract,
            expected_repository=repository,
            expected_commit=commit,
            expected_tree=tree,
            expected_manifest_sha256=sha256(manifest_path),
            expected_archive_sha256=sha256(archive),
        )
        return manifest, result, archive, extract

    def test_git_bundle_carries_tracked_test_fixtures_to_the_exported_test_consumer(self) -> None:
        repo = self._git_repo()
        commit = git(repo, "rev-parse", "HEAD")
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        manifest, result, archive, extract = self._build_and_verify(repo, commit=commit, tree=tree)

        # The exported test reads its fixture from the verified extraction.
        consumer = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            cwd=extract,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(consumer.returncode, 0, consumer.stdout)
        self.assertIn("Ran 1 test", consumer.stdout)

        # Runtime members are unchanged: no fixtures directory in ``files``.
        self.assertEqual([entry["path"] for entry in manifest["files"]], sorted(SOURCE_FILES))
        self.assertEqual(manifest["file_count"], len(SOURCE_FILES))
        # Tracked public fixtures are listed once each, with their exact bytes.
        fixtures = manifest.get("test_fixture_files", [])
        self.assertEqual([entry["path"] for entry in fixtures], sorted(PUBLIC_FIXTURES))
        self.assertEqual(manifest.get("test_fixture_file_count"), len(PUBLIC_FIXTURES))
        for entry in fixtures:
            data = PUBLIC_FIXTURES[entry["path"]].encode("utf-8")
            self.assertEqual(entry["size"], len(data))
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry["git_mode"], "100644")
        with zipfile.ZipFile(archive) as handle:
            members = [info.filename for info in handle.infolist()]
        self.assertEqual(sorted(members), sorted({*SOURCE_FILES, *PUBLIC_FIXTURES}))
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(result["file_count"], len(SOURCE_FILES))
        self.assertEqual(result.get("test_fixture_file_count"), len(PUBLIC_FIXTURES))
        for name, text in PUBLIC_FIXTURES.items():
            self.assertEqual((extract / name).read_text(encoding="utf-8"), text)
        for name in [*EXCLUDED_TRACKED, "tests/fixtures/sample/untracked.xml"]:
            with self.subTest(excluded=name):
                self.assertFalse((extract / name).exists())
        self.assertFalse((extract / "fixtures").exists())
        self.assertFalse((extract / "docs" / "fixtures").exists())

    def test_gitless_bundle_carries_test_fixtures_and_keeps_other_fixture_directories_out(self) -> None:
        source = self.tmp / "source"
        write(source / "main.py", "print('ok')\n")
        write(source / "tests/fixtures/a.xml", FIXTURE_TEXT)
        write(source / "fixtures/local_cli/response.json", '{"ok": true}\n')
        write(source / "tests/fixtures/output/run.txt", "runtime output\n")
        manifest, result, _archive, extract = self._build_and_verify(source, commit="c" * 40, tree="d" * 40)
        self.assertEqual(manifest["identity_source"], "asserted-gitless")
        self.assertEqual([entry["path"] for entry in manifest["files"]], ["main.py"])
        self.assertEqual([entry["path"] for entry in manifest.get("test_fixture_files", [])], ["tests/fixtures/a.xml"])
        self.assertEqual(result.get("test_fixture_file_count"), 1)
        self.assertEqual((extract / "tests/fixtures/a.xml").read_text(encoding="utf-8"), FIXTURE_TEXT)
        self.assertFalse((extract / "fixtures").exists())
        self.assertFalse((extract / "tests/fixtures/output").exists())

    def test_builder_still_refuses_a_symlinked_test_fixture(self) -> None:
        repo = self._git_repo()
        os.symlink("table.section0.xml", repo / "tests/fixtures/sample/link.xml")
        git(repo, "add", "--", "tests/fixtures/sample/link.xml")
        git(repo, "commit", "-q", "-m", "symlink")
        with self.assertRaises(self.builder.SourceBundleError):
            self.builder.build_source_bundle(
                source_root=repo,
                archive_path=self.tmp / "bundle" / "source.zip",
                manifest_path=self.tmp / "bundle" / "manifest.json",
                repository="r",
            )

    def test_repository_tracked_test_fixtures_round_trip_through_the_bundle(self) -> None:
        if (ROOT / ".git").exists():
            listed = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z", "--", "tests/fixtures"],
                check=True, stdout=subprocess.PIPE,
            ).stdout.split(b"\0")
            names = sorted(raw.decode("utf-8") for raw in listed if raw)
        else:  # verified extraction of a source bundle
            names = sorted(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / "tests" / "fixtures").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
        self.assertTrue(names, "no tracked test fixtures found")
        source = self.tmp / "source"
        write(source / "main.py", "print('ok')\n")
        for name in names:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        manifest, result, _archive, extract = self._build_and_verify(source, commit="c" * 40, tree="d" * 40)
        listed_fixtures = {entry["path"]: entry for entry in manifest.get("test_fixture_files", [])}
        self.assertEqual(sorted(listed_fixtures), names)
        self.assertEqual(result.get("test_fixture_file_count"), len(names))
        for name in names:
            with self.subTest(fixture=name):
                self.assertEqual(listed_fixtures[name]["sha256"], sha256(ROOT / name))
                self.assertEqual(sha256(extract / name), sha256(ROOT / name))


class TestFixtureManifestVerificationTests(unittest.TestCase):
    """The receiver accepts fixture members only as declared test fixtures."""

    def setUp(self) -> None:
        self.verifier = load_script("verify_source_bundle.py")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bundle(self, *, members: dict[str, bytes], files: list[str], fixtures: list[str],
                fixture_count: int | None = None, label: str = "case"):
        root = self.tmp / label
        root.mkdir()
        archive = root / "source.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for name, data in members.items():
                info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                handle.writestr(info, data)

        def entries(names: list[str]):
            return [{"path": name, "size": len(members[name]), "sha256": hashlib.sha256(members[name]).hexdigest()} for name in names]

        manifest = {
            "schema_version": "hwpx/source-bundle/v1",
            "repository": "r",
            "commit": "c" * 40,
            "tree": "d" * 40,
            "identity_source": "asserted-gitless",
            "identity_verified": False,
            "file_count": len(files),
            "files": entries(files),
            "test_fixture_file_count": len(fixtures) if fixture_count is None else fixture_count,
            "test_fixture_files": entries(fixtures),
            "archive": archive.name,
            "archive_sha256": sha256(archive),
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return archive, manifest_path, root / "extract"

    def _verify(self, archive: Path, manifest_path: Path, extract: Path):
        return self.verifier.verify_source_bundle(
            archive_path=archive,
            manifest_path=manifest_path,
            destination=extract,
            expected_repository="r",
            expected_commit="c" * 40,
            expected_tree="d" * 40,
            expected_manifest_sha256=sha256(manifest_path),
            expected_archive_sha256=sha256(archive),
        )

    def test_declared_test_fixture_is_verified_and_extracted(self) -> None:
        members = {"main.py": b"print('ok')\n", "tests/fixtures/a.xml": FIXTURE_TEXT.encode("utf-8")}
        archive, manifest_path, extract = self._bundle(members=members, files=["main.py"], fixtures=["tests/fixtures/a.xml"])
        result = self._verify(archive, manifest_path, extract)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result.get("test_fixture_file_count"), 1)
        self.assertEqual((extract / "tests/fixtures/a.xml").read_bytes(), members["tests/fixtures/a.xml"])

    def test_receiver_refuses_misplaced_undeclared_or_unsafe_fixture_members(self) -> None:
        base = {"main.py": b"print('ok')\n", "tests/fixtures/a.xml": b"<a/>\n"}
        cases = {
            "fixture_listed_as_runtime_file": dict(members=base, files=["main.py", "tests/fixtures/a.xml"], fixtures=[]),
            "fixture_member_undeclared": dict(members=base, files=["main.py"], fixtures=[]),
            "non_fixture_path_in_fixture_list": dict(members={**base, "app/extra.py": b"x = 1\n"}, files=["main.py"],
                                                     fixtures=["tests/fixtures/a.xml", "app/extra.py"]),
            "other_fixture_directory": dict(members={**base, "fixtures/local_cli/r.json": b"{}\n"}, files=["main.py"],
                                            fixtures=["tests/fixtures/a.xml", "fixtures/local_cli/r.json"]),
            "runtime_directory_inside_fixtures": dict(members={**base, "tests/fixtures/output/run.txt": b"x\n"}, files=["main.py"],
                                                      fixtures=["tests/fixtures/a.xml", "tests/fixtures/output/run.txt"]),
            "prohibited_document_fixture": dict(members={**base, "tests/fixtures/report.hwpx": b"x\n"}, files=["main.py"],
                                                fixtures=["tests/fixtures/a.xml", "tests/fixtures/report.hwpx"]),
            "private_name_fixture": dict(members={**base, "tests/fixtures/api_token.json": b"{}\n"}, files=["main.py"],
                                         fixtures=["tests/fixtures/a.xml", "tests/fixtures/api_token.json"]),
            "fixture_count_mismatch": dict(members=base, files=["main.py"], fixtures=["tests/fixtures/a.xml"], fixture_count=2),
            "duplicate_fixture_entry": dict(members=base, files=["main.py"], fixtures=["tests/fixtures/a.xml", "tests/fixtures/a.xml"]),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                archive, manifest_path, extract = self._bundle(label=label, **kwargs)
                with self.assertRaises(self.verifier.SourceBundleVerificationError):
                    self._verify(archive, manifest_path, extract)
                self.assertFalse(extract.exists())


if __name__ == "__main__":
    unittest.main()
