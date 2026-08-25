from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


class RawReadbackMismatch(RuntimeError):
    def __init__(self, message: str, *, readback: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.readback = dict(readback or {})


def raw_sha256(value: str) -> str:
    return 'sha256:' + hashlib.sha256(str(value or '').encode('utf-8', errors='replace')).hexdigest()


def normalize_visible_text(value: str) -> str:
    return ' '.join(str(value or '').replace('\r\n', '\n').replace('\r', '\n').split())


def normalized_sha256(value: str) -> str:
    return 'sha256:' + hashlib.sha256(normalize_visible_text(value).encode('utf-8', errors='replace')).hexdigest()


def line_sequence(value: str) -> list[str]:
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    if text == '':
        return []
    return text.split('\n')


def line_break_counts(value: str) -> dict[str, int]:
    text = str(value or '')
    crlf = text.count('\r\n')
    without_crlf = text.replace('\r\n', '')
    return {
        'crlf': crlf,
        'lf': without_crlf.count('\n'),
        'cr': without_crlf.count('\r'),
    }


def _safe_operation_id(value: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(value or '').strip()).strip('.-')
    return safe[:80] or 'operation'


def _line_sequence_hash(value: str) -> str:
    data = json.dumps(line_sequence(value), ensure_ascii=False, separators=(',', ':')).encode('utf-8', errors='replace')
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _coerce_log(value: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, Mapping):
            cleaned.append({str(k): v for k, v in item.items() if isinstance(k, str)})
        else:
            cleaned.append({'value': str(item)})
    return cleaned


def build_raw_target_readback(
    *,
    session_root: Path,
    operation_id: str,
    raw_text: str,
    target_identity: Mapping[str, Any],
    intended_text: str | None = None,
    fallback_transform_log: Sequence[Mapping[str, Any]] | None = None,
    fail_on_mismatch: bool = False,
) -> dict[str, Any]:
    """Persist raw post-mutation target text and return compact proof metadata.

    This helper is intentionally dependency-light so local Linux workers can unit-test
    raw `after_text` proof behavior without importing the full FastAPI/pyhwpx server.
    It does not mutate document packages; it only writes sidecar proof artifacts under
    the live local-CLI session root.
    """

    raw_text = str(raw_text or '')
    artifact_dir = Path(session_root) / 'operation-readback'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    safe_id = _safe_operation_id(operation_id)
    raw_path = artifact_dir / f'{stamp}-{safe_id}-after.txt'
    manifest_path = artifact_dir / f'{stamp}-{safe_id}-manifest.json'
    raw_path.write_text(raw_text, encoding='utf-8', newline='')

    actual_lines = line_sequence(raw_text)
    actual = {
        'raw_sha256': raw_sha256(raw_text),
        'normalized_hash': normalized_sha256(raw_text),
        'line_sequence_hash': _line_sequence_hash(raw_text),
        'line_count': len(actual_lines),
        'char_count': len(raw_text),
        'line_break_counts': line_break_counts(raw_text),
    }

    expected: dict[str, Any] | None = None
    checks: dict[str, bool | None] = {
        'raw_text_match': None,
        'normalized_hash_match': None,
        'line_count_match': None,
        'line_sequence_match': None,
    }
    failures: list[str] = []
    if intended_text is not None:
        intended = str(intended_text or '')
        intended_lines = line_sequence(intended)
        expected = {
            'raw_sha256': raw_sha256(intended),
            'normalized_hash': normalized_sha256(intended),
            'line_sequence_hash': _line_sequence_hash(intended),
            'line_count': len(intended_lines),
            'char_count': len(intended),
            'line_break_counts': line_break_counts(intended),
        }
        checks = {
            'raw_text_match': raw_text == intended,
            'normalized_hash_match': actual['normalized_hash'] == expected['normalized_hash'],
            'line_count_match': actual['line_count'] == expected['line_count'],
            'line_sequence_match': actual['line_sequence_hash'] == expected['line_sequence_hash'],
        }
        if not checks['normalized_hash_match']:
            failures.append('normalized_hash_mismatch')
        if not checks['line_count_match']:
            failures.append('line_count_mismatch')
        if not checks['line_sequence_match']:
            failures.append('line_sequence_mismatch')

    payload: dict[str, Any] = {
        'schema_version': 'local-cli/raw-target-readback/v1',
        'operation_id': operation_id,
        'created_at_epoch_ms': stamp,
        'target_identity': dict(target_identity),
        'raw_text_path': str(raw_path),
        'raw_sha256': actual['raw_sha256'],
        'normalized_hash': actual['normalized_hash'],
        'line_sequence_hash': actual['line_sequence_hash'],
        'line_count': actual['line_count'],
        'char_count': actual['char_count'],
        'line_break_counts': actual['line_break_counts'],
        'expected': expected,
        'checks': checks,
        'failures': failures,
        'fallback_transform_log': _coerce_log(fallback_transform_log),
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    if fail_on_mismatch and failures:
        raise RawReadbackMismatch(
            f'raw target readback mismatch: {", ".join(failures)}; manifest={manifest_path}',
            readback=payload,
        )
    return payload
