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


if __name__ == "__main__":
    unittest.main()