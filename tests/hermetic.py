"""The parts of portable tests that depend on the host they run on, kept in one place.

- resolver_env: the environment a pdftoppm resolution case passes to app.poppler.resolve_pdftoppm.
- absent_modules: sys.modules entries for modules a test's contract says are not installed.
- write_exact: how a test writes the text fixtures whose bytes it later compares.

These keep the tests' current behaviour: the process environment, no module removed, Path.write_text.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def resolver_env(path: str = '') -> Mapping[str, str] | None:
    """None: resolve_pdftoppm reads the process environment."""
    return None


def absent_modules(*names: str) -> dict[str, None]:
    """No entry: the named modules import as they would on the host."""
    return {}


def write_exact(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
