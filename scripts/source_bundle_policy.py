"""Shared source-bundle exclusion policy for builders and receivers."""

from __future__ import annotations

from pathlib import PurePosixPath


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