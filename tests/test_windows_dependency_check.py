from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_windows_dependencies.py"
_SPEC = importlib.util.spec_from_file_location("check_windows_dependencies", CHECKER_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - collection guard
    raise ImportError(f"Unable to load {CHECKER_PATH}")
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


class WindowsDependencyCheckTests(unittest.TestCase):
    def test_parse_locked_requirements_keeps_distribution_versions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements-windows.lock"
            lock.write_text(
                "annotated-types==0.8.0 \\\n"
                "    --hash=sha256:abc\n"
                "fastapi==0.141.1 \\\n"
                "    --hash=sha256:def\n",
                encoding="utf-8",
            )
            self.assertEqual(
                {"annotated-types": "0.8.0", "fastapi": "0.141.1"},
                checker.parse_locked_requirements(lock),
            )

    def test_verify_dependencies_reports_missing_import_even_when_metadata_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements-windows.lock"
            lock.write_text("annotated-types==0.8.0\nfastapi==0.141.1\n", encoding="utf-8")
            versions = {"annotated-types": "0.8.0", "fastapi": "0.141.1"}
            imported: list[str] = []

            def version_lookup(name: str) -> str:
                return versions[name]

            def importer(name: str) -> object:
                imported.append(name)
                if name == "annotated_types":
                    raise ModuleNotFoundError(name)
                return object()

            report = checker.verify_dependencies(lock, version_lookup=version_lookup, importer=importer)

        self.assertFalse(report["ok"])
        self.assertEqual(["annotated_types"], report["import_failures"])
        self.assertEqual(["annotated_types", "fastapi"], sorted(imported))


class com_error(Exception):
    """Stand-in for pywintypes.com_error raised by pyhwpx's import-time COM setup."""


class PyhwpxSpecOnlyImportTests(unittest.TestCase):
    LOCK = "fastapi==0.141.1\npyhwpx==1.6.3\n"
    VERSIONS = {"fastapi": "0.141.1", "pyhwpx": "1.6.3"}

    def _verify(self, *, versions=None, spec_finder=None):
        imported: list[str] = []
        found: list[str] = []
        versions = self.VERSIONS if versions is None else versions

        def importer(name: str) -> object:
            imported.append(name)
            if name == "pyhwpx":
                raise com_error(-2147221005, "Invalid class string", None, None)
            return object()

        def default_spec_finder(name: str) -> object | None:
            found.append(name)
            return object()

        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements-windows.lock"
            lock.write_text(self.LOCK, encoding="utf-8")
            report = checker.verify_dependencies(
                lock,
                version_lookup=lambda name: versions[name],
                importer=importer,
                spec_finder=spec_finder or default_spec_finder,
            )
        return report, imported, found

    def test_installed_pinned_pyhwpx_with_com_import_error_passes_via_spec(self) -> None:
        report, imported, found = self._verify()

        self.assertTrue(report["ok"], report)
        self.assertEqual([], report["import_failures"])
        self.assertEqual({}, report["import_errors"])
        self.assertEqual(["pyhwpx"], found)
        self.assertEqual(["fastapi"], imported)
        self.assertEqual(["pyhwpx"], report["spec_only_imports"])
        self.assertEqual(2, report["checked_import_count"])

    def test_missing_pyhwpx_spec_is_a_failure(self) -> None:
        report, imported, _found = self._verify(spec_finder=lambda _name: None)

        self.assertFalse(report["ok"])
        self.assertEqual(["pyhwpx"], report["import_failures"])
        self.assertEqual({"pyhwpx": "ModuleNotFoundError"}, report["import_errors"])
        self.assertNotIn("pyhwpx", imported)

    def test_pyhwpx_version_mismatch_still_fails(self) -> None:
        report, _imported, _found = self._verify(
            versions={"fastapi": "0.141.1", "pyhwpx": "1.6.2"}
        )

        self.assertFalse(report["ok"])
        self.assertEqual(
            [{"name": "pyhwpx", "expected": "1.6.3", "installed": "1.6.2"}],
            report["version_mismatches"],
        )

    def test_other_imports_remain_strict_when_pyhwpx_is_spec_only(self) -> None:
        def spec_finder(name: str) -> object:
            return object()

        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements-windows.lock"
            lock.write_text(self.LOCK, encoding="utf-8")

            def importer(name: str) -> object:
                raise com_error(name)

            report = checker.verify_dependencies(
                lock,
                version_lookup=lambda name: self.VERSIONS[name],
                importer=importer,
                spec_finder=spec_finder,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(["fastapi"], report["import_failures"])
        self.assertEqual({"fastapi": "com_error"}, report["import_errors"])


if __name__ == "__main__":
    unittest.main()