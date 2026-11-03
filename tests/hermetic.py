"""The parts of portable tests that depend on the host they run on, kept in one place so each test behaves the same on
every operating system and runner.

- resolver_env: the complete environment a pdftoppm resolution case passes to app.poppler.resolve_pdftoppm.
- absent_modules: sys.modules entries for modules a test's contract says are not installed.
- write_exact: how a test writes the text fixtures whose bytes it later compares.
"""
from __future__ import annotations

from pathlib import Path


def resolver_env(path: str = '') -> dict[str, str]:
    """Exactly the case's own PATH ('' for none) and nothing else.

    resolve_pdftoppm reads HWP_PDFTOPPM, HWP_PDFTOPPM_PATH and HWPX_PDFTOPPM as an explicit renderer, PATH, and
    LOCALAPPDATA/ProgramFiles for WinGet roots. A runner or host that sets any of them (the Windows test runner sets
    HWP_PDFTOPPM to the host's Poppler) must not answer for a case's own temporary candidate.
    """
    return {'PATH': path}


def absent_modules(*names: str) -> dict[str, None]:
    """A None entry per name: ``import name`` raises ImportError on every host, including Windows hosts where the
    real module (for example pythoncom) is installed."""
    return {name: None for name in names}


def write_exact(path: Path, text: str) -> None:
    """Write the UTF-8 bytes of ``text`` with no newline translation (Path.write_text writes CRLF on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))
