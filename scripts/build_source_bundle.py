from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Iterable

try:
    from scripts.source_bundle_policy import find_python_source_hygiene_violations, is_prohibited_member
except ImportError:  # direct script execution
    from source_bundle_policy import find_python_source_hygiene_violations, is_prohibited_member


class SourceBundleError(RuntimeError):
    """Raised when source bundle inputs are unsafe or cannot be reproduced."""


_RUNTIME_DIR_NAMES = {
    ".git",
    ".venv",
    ".venv313",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "spool",
    "receipts",
    "fixtures",
    "uploads",
    "output",
    "logs",
    "cache",
    "backups",
    "proofs",
    "evidence",
    "runtime",
    "queue",
    "documents",
    "customer",
    "projects",
    "sessions",
    "ocr",
    "renders",
    "env",
    "source-bundle",
    "artifacts",
    "archives",
    "staging",
    "temp",
    "tmp",
    "build",
    "dist",
    ".hwpx-install",
}
_RUNTIME_DIR_NAMES_CASEFOLDED = {name.casefold() for name in _RUNTIME_DIR_NAMES}
_RUNTIME_FILE_NAMES = {
    ".env",
    ".coverage",
}
_RUNTIME_FILE_NAMES_CASEFOLDED = {name.casefold() for name in _RUNTIME_FILE_NAMES}
_RUNTIME_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
}
_RUNTIME_ROOT_FILE_NAMES_CASEFOLDED = {
    "manifest.json",
    "source-manifest.json",
    "source_bundle_manifest.json",
}
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_MEMBER_CHARS = set('<>:"|?*')
_GIT_OID_LENGTHS = {40, 64}
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_FILES = 2048
_MAX_SOURCE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise SourceBundleError(f"cannot read source member: {path}") from exc
    return size, digest.hexdigest()


def _stable_file_hash(path: Path, label: str) -> tuple[int, str]:
    """Hash one output object without reopening a replaceable pathname."""

    lexical = _assert_safe_output_path(path, label)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(lexical, flags)
    except OSError as exc:
        raise SourceBundleError(f'{label} could not be opened without following links: {lexical}') from exc
    try:
        with os.fdopen(descriptor, 'rb', closefd=True) as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SourceBundleError(f'{label} is not a regular file: {lexical}')
            identity = (
                int(getattr(opened, 'st_dev', 0)),
                int(getattr(opened, 'st_ino', 0)),
                int(getattr(opened, 'st_size', 0)),
                int(getattr(opened, 'st_mtime_ns', 0)),
                int(getattr(opened, 'st_mode', 0)),
            )
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                size += len(chunk)
                digest.update(chunk)
            current = os.fstat(handle.fileno())
            current_identity = (
                int(getattr(current, 'st_dev', 0)),
                int(getattr(current, 'st_ino', 0)),
                int(getattr(current, 'st_size', 0)),
                int(getattr(current, 'st_mtime_ns', 0)),
                int(getattr(current, 'st_mode', 0)),
            )
            if identity != current_identity:
                raise SourceBundleError(f'{label} changed while being read: {lexical}')
            try:
                path_identity = os.lstat(lexical)
            except OSError as exc:
                raise SourceBundleError(f'{label} disappeared while being read: {lexical}') from exc
            lexical_identity = (
                int(getattr(path_identity, 'st_dev', 0)),
                int(getattr(path_identity, 'st_ino', 0)),
                int(getattr(path_identity, 'st_size', 0)),
                int(getattr(path_identity, 'st_mtime_ns', 0)),
                int(getattr(path_identity, 'st_mode', 0)),
            )
            if identity != lexical_identity:
                raise SourceBundleError(f'{label} path identity changed while being read: {lexical}')
            return size, digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_no_follow_descriptor(path: Path) -> int:
    """Open a source file without following a replaceable reparse leaf."""

    if os.name != 'nt':
        nofollow = getattr(os, 'O_NOFOLLOW', None)
        if nofollow is None:
            raise SourceBundleError('source file cannot be opened without no-follow support')
        flags = os.O_RDONLY | nofollow | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_BINARY', 0)
        return os.open(path, flags)

    # Windows has no portable O_NOFOLLOW.  OPEN_REPARSE_POINT makes the
    # CreateFileW handle refer to the leaf itself; fstat/lstat identity checks
    # below then reject the reparse object and any pathname replacement.
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000007,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080 | 0x00200000,  # FILE_ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, f'could not open source without following reparse points: {path}')
    handle_value = int(handle)
    try:
        return msvcrt.open_osfhandle(handle_value, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    except BaseException:
        kernel32.CloseHandle(ctypes.c_void_p(handle_value))
        raise


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(getattr(stat_result, 'st_dev', 0)),
        int(getattr(stat_result, 'st_ino', 0)),
        int(getattr(stat_result, 'st_size', 0)),
        int(getattr(stat_result, 'st_mtime_ns', 0)),
        int(getattr(stat_result, 'st_mode', 0)),
    )


def _git_mode_from_stat(mode: int) -> str:
    if not stat.S_ISREG(mode):
        raise SourceBundleError('Git source member is not a regular file')
    return '100755' if mode & stat.S_IXUSR else '100644'


def _write_atomic_output(path: Path, data: bytes, label: str) -> None:
    """Publish output bytes without following a raced output symlink."""

    output = _assert_safe_output_path(path, label)
    output.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_output_path(output, label)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f'.{output.name}.',
            suffix='.tmp',
            dir=output.parent,
            mode='wb',
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            try:
                os.fsync(temporary.fileno())
            except OSError:
                pass
        _assert_safe_output_path(output, label)
        os.replace(temporary_path, output)
        temporary_path = None
        _assert_safe_output_path(output, label)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _is_filesystem_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_reparse_ancestors(source_root: Path, relative: Path | None = None) -> None:
    current = source_root
    if _is_filesystem_reparse_point(current):
        raise SourceBundleError(f"source root is a symlink or reparse point: {source_root}")
    if relative is None:
        return
    for part in relative.parts:
        current = current / part
        if _is_filesystem_reparse_point(current):
            raise SourceBundleError(f"source path contains a symlink or reparse ancestor: {current}")


def _assert_safe_output_path(path: Path, label: str) -> Path:
    """Keep output custody lexical; never resolve through a mutable link."""

    output = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = output
    while True:
        if _is_filesystem_reparse_point(current):
            raise SourceBundleError(f"{label} is a symlink or reparse point: {output}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return output


def _is_git_oid(value: object) -> bool:
    return isinstance(value, str) and len(value) in _GIT_OID_LENGTHS and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _run_git(source_root: Path, arguments: list[str], *, error: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceBundleError(error) from exc
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SourceBundleError(f"Git returned non-UTF-8 output for {error}") from exc


def _safe_member_name(raw: str) -> str:
    name = str(raw).replace("\\", "/")
    path = PurePosixPath(name)
    if not name or name.startswith("/") or any(":" in part for part in path.parts):
        raise SourceBundleError(f"unsafe source member path: {raw!r}")
    if "\x00" in name or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError(f"unsafe source member path: {raw!r}")
    for part in path.parts:
        if any(ord(char) < 32 or char in _WINDOWS_INVALID_MEMBER_CHARS for char in part):
            raise SourceBundleError(f"unsafe source member path: {raw!r}")
        if part != part.rstrip(" .") or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise SourceBundleError(f"unsafe source member path: {raw!r}")
    normalized = path.as_posix()
    if normalized != name or normalized.startswith("../"):
        raise SourceBundleError(f"unsafe source member path: {raw!r}")
    return normalized


def _is_excluded(source_root: Path, relative: Path, *, archive_path: Path, manifest_path: Path) -> bool:
    parts = relative.parts
    absolute = (source_root / relative).absolute()
    if absolute in {archive_path.absolute(), manifest_path.absolute()}:
        return True
    if is_prohibited_member(relative.as_posix()):
        return True
    if any(part.casefold() in _RUNTIME_DIR_NAMES_CASEFOLDED or part.casefold().startswith(".hwpx-install") for part in parts):
        return True
    if relative.name.casefold().startswith(".env") or relative.name.casefold() in _RUNTIME_FILE_NAMES_CASEFOLDED or (
        relative.parent == Path('.') and relative.name.casefold() in _RUNTIME_ROOT_FILE_NAMES_CASEFOLDED
    ) or relative.suffix.casefold() in _RUNTIME_SUFFIXES:
        # Do not let a symlink hide behind an exclusion rule; it must reach the
        # explicit source-member safety check and fail closed.
        return not (source_root / relative).is_symlink()
    return False


def _git_repository_root(source_root: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        if (source_root / ".git").exists():
            raise SourceBundleError("Git metadata exists but repository discovery failed")
        return None
    try:
        repository_root = Path(completed.stdout.strip()).resolve()
    except (OSError, RuntimeError) as exc:
        raise SourceBundleError("Git repository root could not be resolved") from exc
    if repository_root != source_root:
        # A nested directory of a larger checkout is not an exact repository
        # source root. Use the normal filesystem path policy in that case.
        return None
    return repository_root


def _git_tracked_paths(source_root: Path) -> list[Path] | None:
    if _git_repository_root(source_root) is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        # Once Git metadata is present, a filesystem walk would silently turn
        # an exact tracked-source build into an untracked-source bundle.
        raise SourceBundleError("Git metadata exists but tracked-file discovery failed") from exc
    paths = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            paths.append(Path(raw.decode("utf-8")))
        except UnicodeDecodeError as exc:
            raise SourceBundleError("Git tracked path is not valid UTF-8") from exc
    return paths


def _git_identity(source_root: Path, tracked_paths: list[Path] | None) -> tuple[str, str] | None:
    if tracked_paths is None:
        return None
    commit = _run_git(source_root, ["rev-parse", "--verify", "HEAD"], error="Git HEAD identity could not be read")
    tree = _run_git(source_root, ["rev-parse", "--verify", "HEAD^{tree}"], error="Git tree identity could not be read")
    if not _is_git_oid(commit) or not _is_git_oid(tree):
        raise SourceBundleError("Git commit/tree identity has invalid syntax")
    try:
        status = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceBundleError("Git worktree cleanliness could not be checked") from exc
    dirty_records = []
    for raw in status.stdout.split(b"\0"):
        if not raw:
            continue
        if not raw.startswith(b"?? "):
            dirty_records.append(raw.decode("utf-8", errors="replace"))
    if dirty_records:
        raise SourceBundleError(
            "Git worktree contains tracked changes; source bundle requires clean tree bytes: "
            + ", ".join(dirty_records[:8])
        )
    return commit, tree


def _verify_git_member(source_root: Path, relative: Path, commit: str) -> str:
    return _git_tree_entry(source_root, relative, commit)[0]


def _git_tree_entry(source_root: Path, relative: Path, commit: str) -> tuple[str, str]:
    name = relative.as_posix()
    # Use a byte-preserving command for the NUL-delimited tree record and
    # reject ambiguous/multiple entries.
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "ls-tree", "-z", commit, "--", name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceBundleError(f"Git tree entry is missing for tracked source member: {name}") from exc
    records = [record for record in completed.stdout.split(b'\0') if record]
    if len(records) != 1:
        raise SourceBundleError(f"Git tree entry is ambiguous for source member: {name}")
    header, separator, listed_name = records[0].partition(b'\t')
    if not separator:
        raise SourceBundleError(f"Git tree entry is malformed for source member: {name}")
    try:
        mode, object_type, object_id = header.decode('ascii').split(' ')
        listed = listed_name.decode('utf-8')
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceBundleError(f"Git tree entry is malformed for source member: {name}") from exc
    if listed != name or object_type != 'blob' or mode not in {'100644', '100755'} or not _is_git_oid(object_id):
        raise SourceBundleError(f"Git tree entry is not a regular supported blob for source member: {name}")
    return object_id.lower(), mode


def _walk_source_paths(source_root: Path, *, archive_path: Path, manifest_path: Path) -> list[Path]:
    result: list[Path] = []
    tracked = _git_tracked_paths(source_root)
    candidates: Iterable[Path]
    if tracked is not None:
        candidates = tracked
    else:
        candidates = (
            path.relative_to(source_root)
            for path in source_root.rglob("*")
            if path.is_file() or path.is_symlink()
        )
    seen: set[str] = set()
    for relative in candidates:
        relative = Path(relative)
        _assert_no_reparse_ancestors(source_root, relative)
        if (source_root / relative).is_symlink():
            raise SourceBundleError(f"symlinked source member is not allowed: {relative.as_posix()}")
        if _is_excluded(source_root, relative, archive_path=archive_path, manifest_path=manifest_path):
            continue
        name = _safe_member_name(relative.as_posix())
        key = name.casefold()
        if key in seen:
            raise SourceBundleError(f"duplicate source member path: {name}")
        seen.add(key)
        path = source_root / relative
        if path.is_symlink():
            raise SourceBundleError(f"symlinked source member is not allowed: {name}")
        if not path.is_file():
            raise SourceBundleError(f"source member is not a regular file: {name}")
        result.append(relative)
        if len(result) > _MAX_SOURCE_FILES:
            raise SourceBundleError(f"source bundle contains more than {_MAX_SOURCE_FILES} files")
    return sorted(result, key=lambda item: item.as_posix().casefold())


def _git_hash_file(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "hash-object", "--no-filters", "--", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceBundleError(f"Git could not hash staged source member: {path}") from exc
    digest = completed.stdout.strip()
    if not _is_git_oid(digest):
        raise SourceBundleError(f"Git returned an invalid staged source hash: {path}")
    return digest


def _stage_source_paths(
    source_root: Path,
    paths: list[Path],
    *,
    git_commit: str | None,
) -> tuple[Path, list[dict[str, object]]]:
    stage_root = Path(tempfile.mkdtemp(prefix="hwpx-source-stage-"))
    metadata: list[dict[str, object]] = []
    total_size = 0
    try:
        for relative in paths:
            _assert_no_reparse_ancestors(source_root, relative)
            source_path = source_root / relative
            if not source_path.is_file() or _is_filesystem_reparse_point(source_path):
                raise SourceBundleError(f"source member is not a stable regular file: {relative.as_posix()}")
            expected_git_entry = _git_tree_entry(source_root, relative, git_commit) if git_commit is not None else None
            destination_path = stage_root / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            source_identity: tuple[int, int, int, int, int] | None = None
            source_descriptor = -1
            try:
                source_descriptor = _open_no_follow_descriptor(source_path)
                source_handle = os.fdopen(source_descriptor, 'rb', closefd=True)
                source_descriptor = -1
                with source_handle, destination_path.open("xb") as destination_handle:
                    opened = os.fstat(source_handle.fileno())
                    if not stat.S_ISREG(opened.st_mode) or int(getattr(opened, 'st_file_attributes', 0)) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                        raise SourceBundleError(f"source member is not a stable regular file: {relative.as_posix()}")
                    source_identity = _file_identity(opened)
                    if expected_git_entry is not None and _git_mode_from_stat(opened.st_mode) != expected_git_entry[1]:
                        raise SourceBundleError(
                            f"source member mode does not match Git tree {git_commit}: {relative.as_posix()}"
                        )
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        destination_handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if size > _MAX_SOURCE_MEMBER_BYTES or total_size + size > _MAX_SOURCE_TOTAL_BYTES:
                            raise SourceBundleError("source bundle exceeds bounded member or aggregate byte limits")
            except OSError as exc:
                raise SourceBundleError(f"cannot stage source member: {relative.as_posix()}") from exc
            finally:
                if source_descriptor >= 0:
                    os.close(source_descriptor)
            if source_identity is None:
                raise SourceBundleError(f"source member identity was not captured: {relative.as_posix()}")
            try:
                current_identity = _file_identity(source_path.lstat())
            except OSError as exc:
                raise SourceBundleError(f"source member disappeared while being staged: {relative.as_posix()}") from exc
            if source_identity != current_identity:
                raise SourceBundleError(f"source member changed while being staged: {relative.as_posix()}")
            if git_commit is not None:
                expected_blob = expected_git_entry[0]
                actual_blob = _git_hash_file(destination_path)
                if expected_blob != actual_blob:
                    raise SourceBundleError(
                        f"source member bytes do not match Git tree {git_commit}: {relative.as_posix()}"
                    )
            metadata.append({
                "path": _safe_member_name(relative.as_posix()),
                "size": size,
                "sha256": digest.hexdigest(),
                **({"git_mode": expected_git_entry[1]} if expected_git_entry is not None else {}),
            })
            total_size += size
        return stage_root, metadata
    except BaseException:
        import shutil

        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _write_deterministic_archive(source_root: Path, paths: list[Path], archive_path: Path) -> None:
    archive_path = _assert_safe_output_path(archive_path, 'source archive output')
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_output_path(archive_path, 'source archive output')
    with tempfile.NamedTemporaryFile(prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w") as handle:
            for relative in paths:
                name = _safe_member_name(relative.as_posix())
                data = (source_root / relative).read_bytes()
                compressor = zlib.compressobj(level=9, wbits=-15)
                compressed_size = len(compressor.compress(data) + compressor.flush())
                compress_type = zipfile.ZIP_STORED
                if not data or compressed_size <= 0 or len(data) / compressed_size <= 100.0:
                    compress_type = zipfile.ZIP_DEFLATED
                info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = compress_type
                info.create_system = 0
                info.external_attr = 0o100644 << 16
                info.extra = b""
                info.comment = b""
                handle.writestr(info, data, compresslevel=9 if compress_type == zipfile.ZIP_DEFLATED else None)
        _assert_safe_output_path(archive_path, 'source archive output')
        os.replace(temporary_path, archive_path)
        temporary_path = None
        _assert_safe_output_path(archive_path, 'source archive output')
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_source_bundle(
    *,
    source_root: Path,
    archive_path: Path,
    manifest_path: Path,
    repository: str,
    commit: str | None = None,
    tree: str | None = None,
) -> dict[str, object]:
    source_root = Path(source_root).expanduser()
    lexical_source_root = Path(os.path.abspath(os.fspath(source_root)))
    _assert_no_reparse_ancestors(lexical_source_root)
    source_root = lexical_source_root.resolve()
    archive_path = _assert_safe_output_path(Path(archive_path), "source archive output")
    manifest_path = _assert_safe_output_path(Path(manifest_path), "source manifest output")
    if archive_path == manifest_path:
        raise SourceBundleError("source archive and manifest outputs must be different files")
    if not source_root.is_dir():
        raise SourceBundleError(f"source root is not a directory: {source_root}")
    if not isinstance(repository, str) or not repository.strip():
        raise SourceBundleError("source identity field is empty: repository")
    tracked = _git_tracked_paths(source_root)
    # Validate lexical member safety before Git status so a replaced ancestor
    # cannot be hidden behind the dirty-worktree check.
    paths = _walk_source_paths(source_root, archive_path=archive_path, manifest_path=manifest_path)
    hygiene_violations = find_python_source_hygiene_violations(source_root, paths)
    if hygiene_violations:
        raise SourceBundleError(
            'Python source hygiene check failed: ' + '; '.join(hygiene_violations[:20])
        )
    git_identity = _git_identity(source_root, tracked)
    if git_identity is not None:
        derived_commit, derived_tree = git_identity
        if commit is not None and commit != derived_commit:
            raise SourceBundleError("caller-supplied commit does not match the clean Git HEAD")
        if tree is not None and tree != derived_tree:
            raise SourceBundleError("caller-supplied tree does not match the clean Git HEAD tree")
        commit = derived_commit
        tree = derived_tree
        identity_source = "git"
        identity_verified = True
    else:
        for field, value in (("commit", commit), ("tree", tree)):
            if not _is_git_oid(value):
                raise SourceBundleError(f"source identity field is not a valid Git object id: {field}")
        identity_source = "asserted-gitless"
        identity_verified = False
    stage_root, files = _stage_source_paths(source_root, paths, git_commit=commit if identity_verified else None)
    try:
        _write_deterministic_archive(stage_root, paths, archive_path)
        archive_size, archive_sha256 = _stable_file_hash(archive_path, 'source archive output')
        if archive_size > _MAX_ARCHIVE_BYTES:
            raise SourceBundleError(f"source archive exceeds bounded size limit of {_MAX_ARCHIVE_BYTES} bytes")
    finally:
        import shutil

        shutil.rmtree(stage_root, ignore_errors=True)
    manifest: dict[str, object] = {
        "schema_version": "hwpx/source-bundle/v1",
        "repository": str(repository),
        "commit": str(commit),
        "tree": str(tree),
        "identity_source": identity_source,
        "identity_verified": identity_verified,
        "file_count": len(files),
        "files": files,
        "archive": archive_path.name,
        "archive_sha256": archive_sha256,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(manifest_payload.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise SourceBundleError(f"source manifest exceeds bounded size limit of {_MAX_MANIFEST_BYTES} bytes")
    _write_atomic_output(manifest_path, manifest_payload.encode('utf-8'), 'source manifest output')
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a deterministic Git-less HWPX source bundle")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--tree")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = build_source_bundle(
        source_root=args.source_root,
        archive_path=args.archive,
        manifest_path=args.manifest,
        repository=args.repository,
        commit=args.commit,
        tree=args.tree,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
