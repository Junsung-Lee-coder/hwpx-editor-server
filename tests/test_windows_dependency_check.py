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

    def test_verify_dependencies_discovers_pyhwpx_without_importing_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements-windows.lock"
            lock.write_text("pyhwpx==1.6.6\n", encoding="utf-8")
            imported: list[str] = []
            discovered: list[str] = []

            def importer(name: str) -> object:
                imported.append(name)
                raise AssertionError("pyhwpx must not execute during hosted dependency checks")

            def module_finder(name: str) -> object:
                discovered.append(name)
                return object()

            report = checker.verify_dependencies(
                lock,
                version_lookup=lambda _name: "1.6.6",
                importer=importer,
                module_finder=module_finder,
            )

        self.assertTrue(report["ok"])
        self.assertEqual([], imported)
        self.assertEqual(["pyhwpx"], discovered)
        self.assertEqual(["pyhwpx"], report["discovery_only_imports"])

    def test_windows_ci_runs_only_windows_specific_python_contracts(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        windows_job = workflow.split("  windows-contracts:", maxsplit=1)[1]

        self.assertNotIn("unittest discover -s tests", windows_job)
        self.assertIn("tests.test_windows_dependency_check", windows_job)
        self.assertIn("tests.test_windows_installer_contracts", windows_job)
        self.assertIn("tests.test_native_acceptance_predicates", windows_job)


if __name__ == "__main__":
    unittest.main()