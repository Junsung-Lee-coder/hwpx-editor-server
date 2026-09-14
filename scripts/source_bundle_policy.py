"""Shared source-bundle exclusion policy for builders and receivers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


PROHIBITED_SUFFIXES = frozenset({
    '.hwp', '.hwpx', '.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp',
    '.pem', '.key', '.p12', '.pfx', '.crt', '.cer', '.der', '.kdbx',
    '.pyc', '.pyo', '.pyd', '.pid', '.db', '.sqlite', '.sqlite3', '.log', '.zip', '.tar',
    '.gz', '.bz2', '.xz', '.7z',
})
PROHIBITED_BASENAMES = frozenset({
    'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'authorized_keys', 'known_hosts',
    'credentials', 'credentials.json', 'secrets', 'secrets.json', 'secret.json',
    'token', 'tokens.json', 'cookie', 'cookies.json', 'private_key', 'private-key',
    'service-account.json', '.env', '.coverage', '.ds_store', 'thumbs.db',
})
PROHIBITED_PREFIXES = frozenset({'config.local.', 'config.override.', 'local.settings.'})
PROHIBITED_STEM_MARKERS = frozenset({'credential', 'secret', 'password', 'passwd', 'token', 'cookie'})
RUNTIME_DIR_NAMES = frozenset({
    '.git', '.venv', '.venv313', '__pycache__', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', '.tox', 'spool', 'receipts', 'fixtures', 'uploads', 'output',
    'logs', 'cache', 'backups', 'proofs', 'evidence', 'runtime', 'queue',
    'documents', 'customer', 'projects', 'sessions', 'ocr', 'renders', 'env',
    'source-bundle', 'artifacts', 'archives', 'staging', 'temp', 'tmp', 'build', 'dist',
    'venv', '.egg-info', 'htmlcov', '.vscode', '.idea',
})

# These markers describe implementation-stage prose that must not ship. Keep
# each phrase assembled so this policy does not flag its own source.
UNFINISHED_PYTHON_SOURCE_PHRASES = (
    'later' + ' slice',
    'still being ' + 'wired',
    'thin API-side ' + 'scaffold',
    'thin API side ' + 'scaffold',
    'unfinished' + ' implementation',
    'placeholder' + ' implementation',
)


def is_prohibited_member(raw_name: str) -> bool:
    """Return whether a source member is private, document, or runtime data."""

    parts = PurePosixPath(str(raw_name).replace('\\', '/')).parts
    runtime_dirs = {item.casefold() for item in RUNTIME_DIR_NAMES}
    if any(part.casefold() in runtime_dirs or part.casefold().startswith('.hwpx-install') for part in parts):
        return True
    path = PurePosixPath('/'.join(parts))
    for part in parts:
        folded = part.casefold()
        if (
            folded.startswith('.env')
            or folded in PROHIBITED_BASENAMES
            or any(folded.startswith(prefix) for prefix in PROHIBITED_PREFIXES)
        ):
            return True
        if any(marker in folded for marker in PROHIBITED_STEM_MARKERS):
            return True
        if any(folded.endswith(suffix) for suffix in PROHIBITED_SUFFIXES):
            return True
    # Command-package manifests are source code when nested under their
    # command directory; only root-level build/runtime manifests are private.
    if len(parts) == 1 and (
        path.name.casefold() in {
        'manifest.json', 'source-manifest.json', 'source_bundle_manifest.json',
        } or path.name.casefold().endswith('.manifest.json')
    ):
        return True
    return False


def find_python_source_hygiene_violations(
    source_root: Path,
    relative_paths: list[Path] | tuple[Path, ...] | None = None,
) -> list[str]:
    """Find unfinished-stage markers in every shipped Python source file."""

    root = Path(source_root)
    if relative_paths is None:
        candidates = sorted(root.rglob('*.py'), key=lambda item: item.as_posix().casefold())
    else:
        candidates = [
            root / Path(relative)
            for relative in relative_paths
            if Path(relative).suffix.casefold() == '.py'
        ]
    violations: list[str] = []
    markers = tuple((phrase, phrase.casefold()) for phrase in UNFINISHED_PYTHON_SOURCE_PHRASES)
    for path in candidates:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if is_prohibited_member(relative.as_posix()) or path.is_symlink() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as exc:
            violations.append(f'{relative.as_posix()}: unreadable Python source ({type(exc).__name__})')
            continue
        for line_number, line in enumerate(lines, start=1):
            folded_line = line.casefold()
            for phrase, folded_phrase in markers:
                if folded_phrase in folded_line:
                    violations.append(f'{relative.as_posix()}:{line_number}: {phrase}')
    return violations