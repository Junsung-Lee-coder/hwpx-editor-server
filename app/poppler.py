from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


class PopplerResolutionError(RuntimeError):
    """Raised when a configured or discovered pdftoppm is not usable."""


MAX_WINGET_TRAVERSAL_DEPTH = 8
MAX_WINGET_TRAVERSAL_ENTRIES = 4096
MAX_WINGET_TRAVERSAL_SECONDS = 2.0
MAX_RENDERER_PROBE_SECONDS = 2.0
_MINIMAL_RENDER_PROBE_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>
endobj
trailer
<< /Root 1 0 R >>
%%EOF
"""


@dataclass(frozen=True)
class PopplerResolution:
    """Deterministic, JSON-friendly pdftoppm resolution result."""

    path: Path | None
    source: str
    detail: str
    candidates: list[str] = field(default_factory=list)
    ok: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "path": str(self.path) if self.path is not None else None,
            "source": self.source,
            "detail": self.detail,
            "candidates": list(self.candidates),
        }


def _is_executable_file(path: Path, *, platform: str) -> bool:
    try:
        if _has_reparse_component(path) or not path.is_file():
            return False
        if platform == "win32":
            # Windows command wrappers are not an acceptable renderer binary:
            # they add another mutable interpreter boundary and can run an
            # unrelated program.  Only a real PE executable is admitted.
            if path.suffix.lower() != ".exe":
                return False
        elif not os.access(path, os.X_OK):
            return False
        return _functional_renderer_probe(path)
    except OSError:
        return False


def _functional_renderer_probe(path: Path) -> bool:
    """Verify that the selected binary renders a bounded minimal PDF."""

    try:
        before = path.stat()
        before_identity = (
            int(getattr(before, "st_dev", 0)),
            int(getattr(before, "st_ino", 0)),
            int(getattr(before, "st_size", 0)),
            int(getattr(before, "st_mtime_ns", 0)),
        )
        with tempfile.TemporaryDirectory(prefix="hwpx-pdftoppm-probe-") as raw_dir:
            probe_dir = Path(raw_dir)
            pdf_path = probe_dir / "input.pdf"
            output_prefix = probe_dir / "render"
            pdf_path.write_bytes(_MINIMAL_RENDER_PROBE_PDF)
            completed = subprocess.run(
                [
                    str(path),
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    "-png",
                    str(pdf_path),
                    str(output_prefix),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=MAX_RENDERER_PROBE_SECONDS,
            )
            rendered_path = output_prefix.with_suffix(".png")
            if completed.returncode != 0 or not rendered_path.is_file() or rendered_path.stat().st_size <= 0:
                return False
            with rendered_path.open("rb") as rendered:
                if rendered.read(8) != b"\x89PNG\r\n\x1a\n":
                    return False
        after = path.stat()
        after_identity = (
            int(getattr(after, "st_dev", 0)),
            int(getattr(after, "st_ino", 0)),
            int(getattr(after, "st_size", 0)),
            int(getattr(after, "st_mtime_ns", 0)),
        )
        return after_identity == before_identity
    except (OSError, subprocess.SubprocessError):
        return False


def _normalise_path(value: str | os.PathLike[str]) -> Path:
    # Keep symlinks/reparse points visible to the safety checks.  ``resolve``
    # would silently turn an unsafe configured path into its target.
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
            if current.exists():
                attributes = int(getattr(current.lstat(), "st_file_attributes", 0))
                if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _candidate_names(platform: str) -> tuple[str, ...]:
    return ("pdftoppm.exe", "pdftoppm") if platform == "win32" else ("pdftoppm", "pdftoppm.exe")


def _iter_path_candidates(path_entries: Iterable[str | os.PathLike[str]], *, platform: str) -> Iterable[Path]:
    names = _candidate_names(platform)
    for raw_entry in path_entries:
        if not str(raw_entry).strip():
            continue
        directory = _normalise_path(raw_entry)
        for name in names:
            yield directory / name


def _default_winget_roots(env: Mapping[str, str], *, platform: str) -> list[Path]:
    if platform != "win32":
        return []
    configured = env.get("HWP_WINGET_ROOTS") or env.get("HWPX_WINGET_ROOTS", "")
    roots: list[Path] = []
    for raw in configured.split(os.pathsep):
        if raw.strip():
            roots.append(_normalise_path(raw))
    local_app_data = env.get("LOCALAPPDATA")
    program_files = env.get("ProgramFiles") or env.get("ProgramW6432")
    if local_app_data:
        roots.append(_normalise_path(Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"))
    if program_files:
        roots.append(_normalise_path(Path(program_files) / "WindowsApps"))
    # Keep discovery deterministic and bounded to known package roots.  The
    # caller may pass an explicit root in tests or for managed installations.
    unique: dict[str, Path] = {}
    for root in roots:
        unique[str(root).casefold()] = root
    return [unique[key] for key in sorted(unique)]


def _iter_winget_candidates(
    roots: Iterable[Path],
    *,
    platform: str,
    max_depth: int = MAX_WINGET_TRAVERSAL_DEPTH,
    max_entries: int = MAX_WINGET_TRAVERSAL_ENTRIES,
    max_seconds: float = MAX_WINGET_TRAVERSAL_SECONDS,
) -> Iterable[Path]:
    """Yield supported binaries from bounded, non-reparse traversal."""

    if max_depth < 0 or max_entries <= 0 or max_seconds <= 0:
        return
    names = {name.casefold() for name in _candidate_names(platform)}
    deadline = time.monotonic() + float(max_seconds)
    visited_entries = 0
    for root in sorted((_normalise_path(item) for item in roots), key=lambda p: str(p).casefold()):
        if time.monotonic() >= deadline:
            return
        if not root.exists() or not root.is_dir() or _has_reparse_component(root):
            continue
        pending: list[tuple[Path, int]] = [(root, 0)]
        while pending:
            if time.monotonic() >= deadline or visited_entries >= max_entries:
                return
            current, depth = pending.pop()
            try:
                with os.scandir(current) as scanner:
                    children: list[os.DirEntry[str]] = []
                    for entry in scanner:
                        if time.monotonic() >= deadline or visited_entries >= max_entries:
                            return
                        visited_entries += 1
                        children.append(entry)
                        if len(children) >= max_entries:
                            break
            except OSError:
                continue
            children.sort(key=lambda entry: entry.name.casefold())
            for entry in reversed(children):
                child = Path(entry.path)
                child_depth = depth + 1
                try:
                    # Check the entry itself before yielding it or queueing it.
                    # This catches POSIX symlinks and Windows reparse-point
                    # files/directories rather than relying only on the later
                    # executable-file check.
                    if _has_reparse_component(child):
                        continue
                    if entry.name.casefold() in names and child_depth <= max_depth:
                        yield child
                    if child_depth < max_depth and entry.is_dir(follow_symlinks=False):
                        pending.append((child, child_depth))
                except OSError:
                    continue


def resolve_pdftoppm(
    *,
    explicit: str | os.PathLike[str] | None = None,
    path_entries: Iterable[str | os.PathLike[str]] | None = None,
    winget_roots: Iterable[str | os.PathLike[str]] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> PopplerResolution:
    """Resolve pdftoppm using explicit, current PATH, then dynamic WinGet paths.

    An explicitly supplied path is authoritative: a missing or non-executable
    explicit path fails closed instead of silently using a different binary.
    All returned paths are absolute and the candidate list is stable-sorted.
    """

    current_platform = platform or sys.platform
    environ = dict(os.environ if env is None else env)
    candidates: list[str] = []

    if explicit is None:
        explicit = environ.get("HWP_PDFTOPPM") or environ.get("HWP_PDFTOPPM_PATH") or environ.get("HWPX_PDFTOPPM")
    if explicit is not None and str(explicit).strip():
        explicit_path = _normalise_path(explicit)
        candidates.append(str(explicit_path))
        if not _is_executable_file(explicit_path, platform=current_platform):
            raise PopplerResolutionError(
                f"Configured explicit pdftoppm path is not executable: {explicit_path}"
            )
        return PopplerResolution(
            path=explicit_path,
            source="explicit",
            detail=f"Using configured pdftoppm: {explicit_path}",
            candidates=candidates,
            ok=True,
        )

    if path_entries is None:
        raw_path = environ.get("PATH", "")
        path_entries = raw_path.split(os.pathsep)
        # shutil.which handles PATHEXT and Windows path semantics more closely
        # than manually joining names when running on the target platform.
        for name in _candidate_names(current_platform):
            found = shutil.which(name, path=raw_path)
            if found:
                path = _normalise_path(found)
                candidates.append(str(path))
                if _is_executable_file(path, platform=current_platform):
                    return PopplerResolution(
                        path=path,
                        source="path",
                        detail=f"Using pdftoppm from PATH: {path}",
                        candidates=sorted(set(candidates), key=str.casefold),
                        ok=True,
                    )
    else:
        for candidate in _iter_path_candidates(path_entries, platform=current_platform):
            candidates.append(str(_normalise_path(candidate)))
            if _is_executable_file(candidate, platform=current_platform):
                path = _normalise_path(candidate)
                return PopplerResolution(
                    path=path,
                    source="path",
                    detail=f"Using pdftoppm from PATH: {path}",
                    candidates=sorted(set(candidates), key=str.casefold),
                    ok=True,
                )

    if winget_roots is None:
        winget_roots = _default_winget_roots(environ, platform=current_platform)
    else:
        winget_roots = [_normalise_path(item) for item in winget_roots]
    for candidate in _iter_winget_candidates(winget_roots, platform=current_platform):
        path = _normalise_path(candidate)
        candidates.append(str(path))
        if _is_executable_file(path, platform=current_platform):
            return PopplerResolution(
                path=path,
                source="winget",
                detail=f"Using dynamically discovered WinGet pdftoppm: {path}",
                candidates=sorted(set(candidates), key=str.casefold),
                ok=True,
            )

    return PopplerResolution(
        path=None,
        source="unresolved",
        detail=(
            "pdftoppm was not found. Set HWP_PDFTOPPM to an executable, "
            "put pdftoppm on PATH, or install Poppler in the explicit user-scope mode."
        ),
        candidates=sorted(set(candidates), key=str.casefold),
        ok=False,
    )


def require_pdftoppm(**kwargs: object) -> Path:
    result = resolve_pdftoppm(**kwargs)  # type: ignore[arg-type]
    if not result.ok or result.path is None:
        raise PopplerResolutionError(result.detail)
    return result.path


__all__ = [
    "PopplerResolution",
    "PopplerResolutionError",
    "resolve_pdftoppm",
    "require_pdftoppm",
]
