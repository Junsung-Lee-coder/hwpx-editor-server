from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.source_bundle_policy import is_prohibited_member
except ImportError:  # direct script execution
    from source_bundle_policy import is_prohibited_member


class SourceBundleVerificationError(RuntimeError):
    """Raised when a source archive or manifest cannot be verified safely."""


_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_MEMBER_CHARS = set('<>:"|?*')
_RUNTIME_DIR_NAMES_CASEFOLDED = {
    ".git", ".venv", ".venv313", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "spool", "receipts", "fixtures", "uploads", "output", "logs", "cache", "backups", "proofs", "evidence",
    "runtime", "queue", "documents", "customer", "projects", "sessions", "ocr", "renders", "env",
    "source-bundle", "artifacts", "archives", "staging", "temp", "tmp", "build", "dist",
}
_RUNTIME_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z"}
_RUNTIME_ROOT_FILE_NAMES_CASEFOLDED = {"manifest.json", "source-manifest.json", "source_bundle_manifest.json"}

# Archive metadata is attacker-controlled until every bound is checked. The
# limits are deliberately independent so a small ZIP cannot reserve excessive
# memory/disk through a single member or a high compression ratio.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 2048
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0


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
        raise SourceBundleVerificationError(f"cannot read extracted source file: {path}") from exc
    return size, digest.hexdigest()


def _is_sha256_hex(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def _is_git_oid(value: object) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _is_runtime_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    if any(part.casefold() in _RUNTIME_DIR_NAMES_CASEFOLDED or part.casefold().startswith(".hwpx-install") for part in parts):
        return True
    leaf = path.name.casefold()
    return (
        leaf.startswith(".env")
        or (len(parts) == 1 and leaf in _RUNTIME_ROOT_FILE_NAMES_CASEFOLDED)
        or path.suffix.casefold() in _RUNTIME_SUFFIXES
    )


def _safe_member_name(raw: str) -> str:
    name = str(raw)
    if "\x00" in name:
        raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or name.startswith("/") or any(":" in part for part in path.parts):
        raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
    for part in path.parts:
        if any(ord(char) < 32 or char in _WINDOWS_INVALID_MEMBER_CHARS for char in part):
            raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
        if part != part.rstrip(" .") or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
    normalized = path.as_posix()
    if normalized != name or normalized.startswith("../"):
        raise SourceBundleVerificationError(f"unsafe archive member path: {raw!r}")
    if _is_runtime_member_name(normalized):
        raise SourceBundleVerificationError(f"runtime archive member is not allowed: {raw!r}")
    if is_prohibited_member(normalized):
        raise SourceBundleVerificationError(f"prohibited private or generated source member: {raw!r}")
    return normalized


def _is_symlink_or_reparse(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return stat.S_ISLNK(mode) or stat.S_ISDIR(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode)


def _is_filesystem_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_destination_path(destination: Path) -> None:
    current = destination
    while True:
        if current.exists() or current.is_symlink():
            if _is_filesystem_reparse_point(current):
                raise SourceBundleVerificationError(f"destination path contains a symlink or reparse point: {current}")
            if not current.is_dir():
                raise SourceBundleVerificationError(f"destination path component is not a directory: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_input_file(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    if not lexical.exists() or not lexical.is_file():
        raise SourceBundleVerificationError(f"{label} is not a regular file: {lexical}")
    current = lexical
    while True:
        if _is_filesystem_reparse_point(current):
            raise SourceBundleVerificationError(f"{label} path contains a symlink or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return lexical.resolve()
        current = parent


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise SourceBundleVerificationError(
                f"source manifest exceeds the bounded size limit of {MAX_MANIFEST_BYTES} bytes"
            )
        with path.open("rb") as handle:
            payload = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise SourceBundleVerificationError(
                f"source manifest exceeds the bounded size limit of {MAX_MANIFEST_BYTES} bytes"
            )
        raw = json.loads(payload.decode("utf-8"))
    except SourceBundleVerificationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SourceBundleVerificationError(f"cannot read source manifest: {path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        raise SourceBundleVerificationError("source manifest must contain a files list")
    if len(raw["files"]) > MAX_MANIFEST_ENTRIES:
        raise SourceBundleVerificationError(
            f"source manifest contains more than the bounded limit of {MAX_MANIFEST_ENTRIES} entries"
        )
    if raw.get("schema_version") != "hwpx/source-bundle/v1" or any(
        not isinstance(raw.get(field), str) or not raw[field].strip()
        for field in ("repository", "commit", "tree")
    ):
        raise SourceBundleVerificationError("source manifest is missing required source identity fields")
    if not _is_sha256_hex(raw.get("archive_sha256")):
        raise SourceBundleVerificationError("source manifest is missing a valid archive_sha256")
    if "identity_source" not in raw or "identity_verified" not in raw:
        raise SourceBundleVerificationError("source manifest must explicitly declare identity provenance")
    identity_source = raw.get("identity_source")
    identity_verified = raw.get("identity_verified")
    if not isinstance(identity_source, str) or identity_source not in {"git", "asserted-gitless"} or not isinstance(identity_verified, bool):
        raise SourceBundleVerificationError("source manifest contains invalid identity provenance")
    if identity_verified and (identity_source != "git" or not _is_git_oid(raw["commit"]) or not _is_git_oid(raw["tree"])):
        raise SourceBundleVerificationError("verified Git identity requires valid commit/tree object ids")
    if identity_source == "git" and not identity_verified:
        raise SourceBundleVerificationError("Git identity must be marked verified")
    return raw


def _validate_identity_binding(
    manifest: dict[str, Any],
    *,
    expected_repository: str | None,
    expected_commit: str | None,
    expected_tree: str | None,
    expected_manifest_sha256: str | None,
    actual_manifest_sha256: str,
    expected_archive_sha256: str | None = None,
) -> dict[str, Any]:
    supplied_identity = (expected_repository, expected_commit, expected_tree)
    supplied_count = sum(value is not None for value in supplied_identity)
    if supplied_count not in {0, 3}:
        raise SourceBundleVerificationError(
            "independent source identity must include repository, commit, and tree together"
        )
    if expected_manifest_sha256 is not None and not _is_sha256_hex(expected_manifest_sha256):
        raise SourceBundleVerificationError("expected manifest sha256 is invalid")
    if expected_archive_sha256 is not None and not _is_sha256_hex(expected_archive_sha256):
        raise SourceBundleVerificationError("expected archive sha256 is invalid")
    if expected_manifest_sha256 is not None and expected_manifest_sha256.casefold() != actual_manifest_sha256:
        raise SourceBundleVerificationError(
            f"manifest sha256 mismatch: expected {expected_manifest_sha256}, got {actual_manifest_sha256}"
        )
    if manifest["identity_source"] == "git":
        # A manifest's `identity_verified` flag is producer-controlled.  A
        # standalone receiver may only preserve that claim when the caller
        # binds all source coordinates and the exact manifest bytes out of
        # band.  Otherwise the result is not Git-verified, even if the ZIP
        # and manifest are internally self-consistent.
        if supplied_count != 3 or expected_manifest_sha256 is None:
            raise SourceBundleVerificationError(
                "verified Git identity requires an independent repository/commit/tree and manifest sha256 binding"
            )
    if manifest["identity_source"] == "asserted-gitless" and (
        supplied_count != 3 or expected_manifest_sha256 is None or expected_archive_sha256 is None
    ):
        raise SourceBundleVerificationError(
            "asserted-gitless source identity requires independent repository/commit/tree, manifest, and archive bindings"
        )
    if supplied_count == 3:
        expected_values = (str(expected_repository), str(expected_commit), str(expected_tree))
        actual_values = (str(manifest["repository"]), str(manifest["commit"]), str(manifest["tree"]))
        if expected_values[0] != actual_values[0] or any(
            expected.casefold() != actual.casefold()
            for expected, actual in zip(expected_values[1:], actual_values[1:])
        ):
            raise SourceBundleVerificationError(
                "source identity does not match the independently supplied repository/commit/tree"
            )
    return {
        "independent_identity_supplied": supplied_count == 3,
        "manifest_sha256": actual_manifest_sha256,
        "expected_manifest_sha256_supplied": expected_manifest_sha256 is not None,
        "expected_archive_sha256_supplied": expected_archive_sha256 is not None,
    }


def _validate_archive_resources(infos: list[zipfile.ZipInfo]) -> tuple[int, int, float]:
    if len(infos) > MAX_MANIFEST_ENTRIES:
        raise SourceBundleVerificationError(
            f"archive contains more than the bounded limit of {MAX_MANIFEST_ENTRIES} members"
        )
    total_uncompressed = 0
    total_compressed = 0
    maximum_ratio = 0.0
    for info in infos:
        declared_size = int(info.file_size)
        compressed_size = int(info.compress_size)
        if declared_size < 0 or compressed_size < 0:
            raise SourceBundleVerificationError(f"archive member has invalid size metadata: {info.filename!r}")
        if declared_size > MAX_MEMBER_BYTES:
            raise SourceBundleVerificationError(
                f"archive member exceeds the bounded size limit of {MAX_MEMBER_BYTES} bytes: {info.filename!r}"
            )
        total_uncompressed += declared_size
        total_compressed += compressed_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise SourceBundleVerificationError(
                f"archive exceeds the bounded uncompressed size limit of {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes"
            )
        if declared_size:
            if compressed_size <= 0:
                raise SourceBundleVerificationError(
                    f"archive member has an invalid compression size: {info.filename!r}"
                )
            ratio = declared_size / compressed_size
            maximum_ratio = max(maximum_ratio, ratio)
            if ratio > MAX_COMPRESSION_RATIO:
                raise SourceBundleVerificationError(
                    f"archive member compression ratio exceeds {MAX_COMPRESSION_RATIO:g}: {info.filename!r}"
                )
    return total_uncompressed, total_compressed, maximum_ratio


def _stream_extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    total_written: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    try:
        with archive.open(info, "r") as source, target.open("xb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_MEMBER_BYTES or total_written + written > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise SourceBundleVerificationError(
                        f"archive member exceeded extraction resource bounds: {info.filename!r}"
                    )
                output.write(chunk)
                digest.update(chunk)
    except SourceBundleVerificationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SourceBundleVerificationError(f"could not safely extract archive member: {info.filename!r}") from exc
    if written != int(info.file_size):
        raise SourceBundleVerificationError(
            f"archive member size differs from its declaration: {info.filename!r}"
        )
    return written, digest.hexdigest()


def _validate_extracted_path(destination: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    path = destination.joinpath(*parts)
    current = destination
    for part in parts:
        current = current / part
        if _is_filesystem_reparse_point(current):
            raise SourceBundleVerificationError(f"extracted path contains a symlink or reparse point: {current}")
    return path


def verify_source_bundle(
    *,
    archive_path: Path,
    manifest_path: Path,
    destination: Path,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    expected_tree: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_archive_sha256: str | None = None,
) -> dict[str, Any]:
    archive_path = _validate_input_file(Path(archive_path), "archive")
    manifest_path = _validate_input_file(Path(manifest_path), "manifest")
    archive_bytes = archive_path.stat().st_size
    if archive_bytes > MAX_ARCHIVE_BYTES:
        raise SourceBundleVerificationError(
            f"archive exceeds the bounded size limit of {MAX_ARCHIVE_BYTES} bytes"
        )
    destination = Path(os.path.abspath(os.fspath(Path(destination).expanduser())))
    _validate_destination_path(destination)
    manifest = _load_manifest(manifest_path)
    actual_manifest_sha = _sha256_file(manifest_path)[1]
    # Check ZIP resource bounds before trusting any producer identity fields.
    # A malformed/untrusted archive must fail on its own safety predicate,
    # rather than being masked by a missing external identity binding.
    with zipfile.ZipFile(archive_path, "r") as resource_check:
        _validate_archive_resources(resource_check.infolist())
    identity_binding = _validate_identity_binding(
        manifest,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
        expected_tree=expected_tree,
        expected_manifest_sha256=expected_manifest_sha256,
        actual_manifest_sha256=actual_manifest_sha,
        expected_archive_sha256=expected_archive_sha256,
    )
    expected_archive_sha = str(manifest.get("archive_sha256") or "")
    actual_archive_sha = _sha256_file(archive_path)[1]
    if manifest.get('identity_source') == 'asserted-gitless' and not expected_archive_sha256:
        raise SourceBundleVerificationError(
            'asserted-gitless source identity requires an independent archive SHA-256 binding'
        )
    if expected_archive_sha256 is not None and str(expected_archive_sha256).casefold() != actual_archive_sha.casefold():
        raise SourceBundleVerificationError(
            f'independent archive sha256 mismatch: expected {expected_archive_sha256}, got {actual_archive_sha}'
        )
    if expected_archive_sha != actual_archive_sha:
        raise SourceBundleVerificationError(
            f"archive sha256 mismatch: expected {expected_archive_sha}, got {actual_archive_sha}"
        )
    expected_files: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or not item.get("path"):
            raise SourceBundleVerificationError("source manifest contains an invalid file entry")
        name = _safe_member_name(str(item["path"]))
        key = name.casefold()
        if key in expected_files:
            raise SourceBundleVerificationError(f"duplicate manifest member path: {name}")
        size = item.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_MEMBER_BYTES
            or not _is_sha256_hex(item.get("sha256"))
        ):
            raise SourceBundleVerificationError(f"source manifest contains invalid metadata for: {name}")
        expected_files[key] = {"path": name, "size": size, "sha256": str(item["sha256"])}
    declared_file_count = manifest.get("file_count")
    if (
        not isinstance(declared_file_count, int)
        or isinstance(declared_file_count, bool)
        or declared_file_count != len(expected_files)
    ):
        raise SourceBundleVerificationError(
            f"manifest file_count mismatch: declared={declared_file_count!r}, entries={len(expected_files)}"
        )

    unsafe_members: list[str] = []
    duplicate_members: list[str] = []
    actual_by_key: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(archive_path, "r") as handle:
        infos = handle.infolist()
        total_uncompressed, total_compressed, maximum_ratio = _validate_archive_resources(infos)
        for info in infos:
            try:
                name = _safe_member_name(info.filename)
            except SourceBundleVerificationError:
                unsafe_members.append(info.filename)
                continue
            if _is_symlink_or_reparse(info):
                unsafe_members.append(name)
                continue
            key = name.casefold()
            if key in actual_by_key:
                duplicate_members.append(name)
                continue
            actual_by_key[key] = info
        if unsafe_members or duplicate_members:
            raise SourceBundleVerificationError(
                f"unsafe archive members: {len(unsafe_members)}; duplicate members: {len(duplicate_members)}"
            )
        expected_keys = set(expected_files)
        actual_keys = set(actual_by_key)
        missing = sorted(expected_files[key]["path"] for key in expected_keys - actual_keys)
        unexpected = sorted(actual_by_key[key].filename for key in actual_keys - expected_keys)
        if missing or unexpected:
            raise SourceBundleVerificationError(
                f"archive member set mismatch: missing={missing!r}, unexpected={unexpected!r}"
            )
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                raise SourceBundleVerificationError(
                    f"destination must be a new or empty directory: {destination}"
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _validate_destination_path(destination)
        # Extract only after every member and manifest path has passed validation.
        with tempfile.TemporaryDirectory(prefix="hwpx-source-verify-", dir=destination.parent) as staging_raw:
            staging = Path(staging_raw)
            extracted_bytes = 0
            for key in sorted(actual_by_key):
                info = actual_by_key[key]
                expected = expected_files[key]
                name = expected["path"]
                target = staging.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written, digest = _stream_extract_member(
                    handle,
                    info,
                    target,
                    total_written=extracted_bytes,
                )
                extracted_bytes += written
                if written != expected["size"] or digest != expected["sha256"]:
                    raise SourceBundleVerificationError(
                        f"archive member does not match manifest: {name}"
                    )
            if extracted_bytes != total_uncompressed:
                raise SourceBundleVerificationError("archive extraction byte total differs from ZIP metadata")
            if destination.exists():
                destination.rmdir()
            os.replace(staging, destination)

    mismatches: list[dict[str, Any]] = []
    for key in sorted(expected_files):
        expected = expected_files[key]
        path = _validate_extracted_path(destination, expected["path"])
        try:
            actual_size, actual_sha = _sha256_file(path)
        except (OSError, SourceBundleVerificationError) as exc:
            mismatches.append({"path": expected["path"], "error": str(exc)})
            continue
        if actual_size != expected["size"] or actual_sha != expected["sha256"]:
            mismatches.append({
                "path": expected["path"],
                "expected_size": expected["size"],
                "actual_size": actual_size,
                "expected_sha256": expected["sha256"],
                "actual_sha256": actual_sha,
            })
    if mismatches:
        raise SourceBundleVerificationError(f"extracted source file mismatch count: {len(mismatches)}")
    return {
        "schema_version": "hwpx/source-bundle-verification/v1",
        "archive_sha256": actual_archive_sha,
        "archive_bytes": archive_bytes,
        "file_count": len(expected_files),
        "uncompressed_bytes": total_uncompressed,
        "compressed_bytes": total_compressed,
        "maximum_compression_ratio": maximum_ratio,
        "mismatch_count": len(mismatches),
        "unsafe_member_count": len(unsafe_members),
        "duplicate_member_count": len(duplicate_members),
        "identity_source": manifest["identity_source"],
        "identity_verified": bool(manifest["identity_verified"]),
        **identity_binding,
        "destination": str(destination),
        "manifest_path": str(manifest_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify and safely extract a deterministic HWPX source bundle")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-repository")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tree")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-archive-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = verify_source_bundle(
        archive_path=args.archive,
        manifest_path=args.manifest,
        destination=args.destination,
        expected_repository=args.expected_repository,
        expected_commit=args.expected_commit,
        expected_tree=args.expected_tree,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_archive_sha256=args.expected_archive_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
