from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.poppler import PopplerResolutionError, resolve_pdftoppm


class PopplerResolutionTests(unittest.TestCase):
    def _executable(self, directory: Path, name: str) -> Path:
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_explicit_path_has_priority_over_path_and_winget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            explicit = self._executable(tmp, "explicit-pdftoppm")
            path_candidate = self._executable(tmp, "path-pdftoppm")
            result = resolve_pdftoppm(
                explicit=str(explicit),
                path_entries=[str(path_candidate.parent)],
                winget_roots=[tmp / "winget"],
                platform="win32",
            )

        self.assertEqual(result.path, explicit.resolve())
        self.assertEqual(result.source, "explicit")
        self.assertTrue(result.ok)

    def test_path_resolution_returns_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            candidate = self._executable(tmp, "pdftoppm.exe")
            with patch.dict(os.environ, {"PATH": str(tmp)}, clear=False):
                result = resolve_pdftoppm(platform="win32")

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, "path")

    def test_legacy_environment_alias_resolves_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            candidate = self._executable(tmp, "legacy-pdftoppm.exe")
            result = resolve_pdftoppm(
                env={"HWP_PDFTOPPM_PATH": str(candidate), "PATH": ""},
                platform="win32",
            )

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, "explicit")

    def test_winget_resolution_searches_dynamic_package_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            package_root = tmp / "Microsoft.Poppler_1.0.0_x64__abc" / "bin"
            package_root.mkdir(parents=True)
            candidate = self._executable(package_root, "pdftoppm.exe")
            result = resolve_pdftoppm(
                explicit=None,
                path_entries=[],
                winget_roots=[tmp],
                platform="win32",
            )

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, "winget")
        self.assertIn("winget", result.detail.lower())

    def test_missing_explicit_path_fails_closed_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            missing = Path(tmp_raw) / "missing" / "pdftoppm.exe"
            with self.assertRaises(PopplerResolutionError) as ctx:
                resolve_pdftoppm(
                    explicit=str(missing),
                    path_entries=[],
                    winget_roots=[],
                    platform="win32",
                )

        self.assertIn("explicit", str(ctx.exception).lower())
        self.assertIn("not executable", str(ctx.exception).lower())

    def test_explicit_symlink_fails_closed_without_resolving_around_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            target = self._executable(tmp, "real-pdftoppm.exe")
            link = tmp / "linked-pdftoppm.exe"
            link.symlink_to(target)
            with self.assertRaises(PopplerResolutionError):
                resolve_pdftoppm(explicit=str(link), path_entries=[], winget_roots=[], platform="win32")

    def test_check_only_result_is_structured_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            result = resolve_pdftoppm(
                explicit=None,
                path_entries=[],
                winget_roots=[Path(tmp_raw)],
                platform="win32",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.path, None)
        self.assertEqual(result.source, "unresolved")
        self.assertIsInstance(result.candidates, list)
        self.assertIn("pdftoppm", result.detail.lower())


if __name__ == "__main__":
    unittest.main()
