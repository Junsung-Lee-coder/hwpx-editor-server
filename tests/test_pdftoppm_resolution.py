from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.poppler import PopplerResolutionError, resolve_pdftoppm


class _BoundedRendererProcess:
    """Test-owned process boundary for a platform-neutral .exe fixture."""

    def __init__(self, *, returncode: int = 0, writes_png: bool = True) -> None:
        self.returncode = returncode
        self.writes_png = writes_png
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        if self.writes_png:
            output_prefix = Path(args[-1])
            output_prefix.with_suffix('.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        return subprocess.CompletedProcess(args, self.returncode)


class PopplerResolutionTests(unittest.TestCase):
    def _executable(self, directory: Path, name: str) -> Path:
        path = directory / name
        path.write_bytes(b'test-owned bounded renderer fixture\n')
        return path

    def _assert_probe_call(self, probe: _BoundedRendererProcess) -> None:
        self.assertEqual(len(probe.calls), 1)
        args, kwargs = probe.calls[0]
        self.assertEqual(args[1:7], ['-f', '1', '-l', '1', '-singlefile', '-png'])
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['timeout'], 2.0)

    def test_explicit_path_has_priority_over_path_and_winget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            explicit = self._executable(tmp, 'explicit-pdftoppm.exe')
            path_candidate = self._executable(tmp, 'path-pdftoppm.exe')
            probe = _BoundedRendererProcess()
            with patch('app.poppler.subprocess.run', side_effect=probe):
                result = resolve_pdftoppm(
                    explicit=str(explicit),
                    path_entries=[str(path_candidate.parent)],
                    winget_roots=[tmp / 'winget'],
                    platform='win32',
                )

        self.assertEqual(result.path, explicit.resolve())
        self.assertEqual(result.source, 'explicit')
        self.assertTrue(result.ok)
        self._assert_probe_call(probe)

    def test_nonfunctional_explicit_renderer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            broken = self._executable(Path(tmp_raw), 'broken-pdftoppm.exe')
            probe = _BoundedRendererProcess(returncode=17, writes_png=False)
            with patch('app.poppler.subprocess.run', side_effect=probe):
                with self.assertRaisesRegex(PopplerResolutionError, 'functional|probe|executable'):
                    resolve_pdftoppm(explicit=str(broken), platform='win32')

        self._assert_probe_call(probe)

    def test_renderer_that_only_reports_version_fails_functional_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            version_only = self._executable(Path(tmp_raw), 'version-only-pdftoppm.exe')
            probe = _BoundedRendererProcess(writes_png=False)
            with patch('app.poppler.subprocess.run', side_effect=probe):
                with self.assertRaisesRegex(PopplerResolutionError, 'functional|render|probe|executable'):
                    resolve_pdftoppm(explicit=version_only, platform='win32')

        self._assert_probe_call(probe)

    def test_path_resolution_returns_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            candidate = self._executable(tmp, 'pdftoppm.exe')
            probe = _BoundedRendererProcess()
            with patch('app.poppler.subprocess.run', side_effect=probe):
                result = resolve_pdftoppm(path_entries=[str(tmp)], platform='win32')

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, 'path')
        self._assert_probe_call(probe)

    def test_legacy_environment_alias_resolves_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            candidate = self._executable(tmp, 'legacy-pdftoppm.exe')
            probe = _BoundedRendererProcess()
            with patch('app.poppler.subprocess.run', side_effect=probe):
                result = resolve_pdftoppm(
                    env={'HWP_PDFTOPPM_PATH': str(candidate), 'PATH': ''},
                    platform='win32',
                )

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, 'explicit')
        self._assert_probe_call(probe)

    def test_winget_resolution_searches_dynamic_package_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            package_root = tmp / 'Microsoft.Poppler_1.0.0_x64__abc' / 'bin'
            package_root.mkdir(parents=True)
            candidate = self._executable(package_root, 'pdftoppm.exe')
            probe = _BoundedRendererProcess()
            with patch('app.poppler.subprocess.run', side_effect=probe):
                result = resolve_pdftoppm(
                    explicit=None,
                    path_entries=[],
                    winget_roots=[tmp],
                    platform='win32',
                )

        self.assertEqual(result.path, candidate.resolve())
        self.assertEqual(result.source, 'winget')
        self.assertIn('winget', result.detail.lower())
        self._assert_probe_call(probe)

    def test_missing_explicit_path_fails_closed_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            missing = Path(tmp_raw) / 'missing' / 'pdftoppm.exe'
            with self.assertRaises(PopplerResolutionError) as ctx:
                resolve_pdftoppm(
                    explicit=str(missing),
                    path_entries=[],
                    winget_roots=[],
                    platform='win32',
                )

        self.assertIn('explicit', str(ctx.exception).lower())
        self.assertIn('not executable', str(ctx.exception).lower())

    def test_explicit_symlink_fails_closed_without_resolving_around_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            target = self._executable(tmp, 'real-pdftoppm.exe')
            link = tmp / 'linked-pdftoppm.exe'
            link.symlink_to(target)
            with self.assertRaises(PopplerResolutionError):
                resolve_pdftoppm(explicit=str(link), path_entries=[], winget_roots=[], platform='win32')

    def test_check_only_result_is_structured_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            result = resolve_pdftoppm(
                explicit=None,
                path_entries=[],
                winget_roots=[Path(tmp_raw)],
                platform='win32',
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.path, None)
        self.assertEqual(result.source, 'unresolved')
        self.assertIsInstance(result.candidates, list)
        self.assertIn('pdftoppm', result.detail.lower())


if __name__ == '__main__':
    unittest.main()
