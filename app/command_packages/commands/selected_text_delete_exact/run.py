from __future__ import annotations

import hashlib
from typing import Any, Mapping

from app.command_packages.commands.context.run import _safe_text
from app.edit_ops import (
    EditOperationError,
    _delete_selection,
    _get_document_text,
    _get_selected_text,
    _normalize_visible_text,
    _snapshot_cursor_context,
)

_MAX_EXPECTED_TEXT_CHARS = 50000
_TABLE_SELECTION_MODES = {3, 19}
_RENDERED_PROOF_WARNING = (
    'Rendered before/after proof is still required before save/final delivery; this command only proves exact selected source text deletion.'
)


def _sha256_text(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()


def _raise(error_type: type[Exception], message: str) -> None:
    raise error_type(message, status_code=400)


def _clean_expected_hash(value: Any, *, field: str, index: int, error_type: type[Exception]) -> str:
    text = str(value or '').strip().lower()
    if not text:
        _raise(error_type, f'command-bundle step {index} selected_text_delete_exact requires {field}')
    if not text.startswith('sha256:') or len(text) != len('sha256:') + 64:
        _raise(error_type, f'command-bundle step {index} {field} must be sha256:<64 hex chars>')
    try:
        int(text.removeprefix('sha256:'), 16)
    except ValueError:
        _raise(error_type, f'command-bundle step {index} {field} must be sha256:<64 hex chars>')
    return text


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    expected_text = step.get('expected_text')
    if not isinstance(expected_text, str) or expected_text == '':
        _raise(error_type, f'command-bundle step {index} selected_text_delete_exact requires non-empty expected_text')
    if len(expected_text) > _MAX_EXPECTED_TEXT_CHARS:
        _raise(error_type, f'command-bundle step {index} expected_text is too long')
    step['expected_text'] = expected_text

    expected_hash = _clean_expected_hash(step.get('expected_hash'), field='expected_hash', index=index, error_type=error_type)
    actual_hash = _sha256_text(expected_text)
    if expected_hash != actual_hash:
        _raise(
            error_type,
            f'command-bundle step {index} expected_hash does not match expected_text (expected {actual_hash})',
        )
    step['expected_hash'] = expected_hash

    expected_normalized = _normalize_visible_text(expected_text)
    if not expected_normalized:
        _raise(error_type, f'command-bundle step {index} normalized expected_text is empty')
    actual_normalized_hash = _sha256_text(expected_normalized)
    if step.get('expected_normalized_hash') in (None, ''):
        step['expected_normalized_hash'] = actual_normalized_hash
    else:
        expected_normalized_hash = _clean_expected_hash(
            step.get('expected_normalized_hash'),
            field='expected_normalized_hash',
            index=index,
            error_type=error_type,
        )
        if expected_normalized_hash != actual_normalized_hash:
            _raise(
                error_type,
                f'command-bundle step {index} expected_normalized_hash does not match normalized expected_text (expected {actual_normalized_hash})',
            )
        step['expected_normalized_hash'] = expected_normalized_hash

    if step.get('confirm_cleanup') is not True:
        _raise(error_type, f'command-bundle step {index} selected_text_delete_exact requires confirm_cleanup=true')

    # Request payloads may only declare that deletion has not happened yet. Runtime proof is the only place
    # where source_text_deleted=true is allowed.
    if step.get('source_text_deleted') not in (None, False):
        _raise(error_type, f'command-bundle step {index} source_text_deleted may not be true before runtime deletion proof')
    step['source_text_deleted'] = False

    for key in ('native_table_proof_ref', 'native_table_proof_hash'):
        if key in step and step.get(key) not in (None, ''):
            value = str(step.get(key) or '').strip()
            if len(value) > 500:
                _raise(error_type, f'command-bundle step {index} {key} is too long')
            step[key] = value
        elif key in step:
            step[key] = None
    return step


def _has_active_selection(snapshot: Mapping[str, Any]) -> bool:
    selected_pos = snapshot.get('selected_pos')
    if snapshot.get('has_selection') is True:
        return True
    return bool(isinstance(selected_pos, (list, tuple)) and selected_pos and selected_pos[0])


def _selection_mode_is_table_mode(value: Any) -> bool:
    if value in _TABLE_SELECTION_MODES:
        return True
    try:
        if not isinstance(value, bool) and int(value) in _TABLE_SELECTION_MODES:
            return True
    except Exception:
        pass
    text = str(value or '').lower()
    return any(token in text for token in ('table', 'cell', 'tbl'))


def _table_context_blocked(snapshot: Mapping[str, Any]) -> bool:
    return bool(
        snapshot.get('is_cell') is True
        or snapshot.get('cell_addr')
        or snapshot.get('cell_ref')
        or _selection_mode_is_table_mode(snapshot.get('selection_mode'))
    )


def _live_location(handle: Any, warnings: list[str]) -> dict[str, Any]:
    try:
        from app.local_cli_runtime import snapshot_live_location

        return snapshot_live_location(
            hwp=handle.hwp,
            source_filename=str(getattr(handle, 'source_filename', '') or 'unknown'),
            working_copy_id=str(getattr(handle, 'session_id', '') or 'unknown'),
            include_nearby_context=False,
            include_document_snapshot=False,
        )
    except Exception as exc:  # pragma: no cover - live Hancom availability varies.
        warnings.append(f'live location unavailable: {type(exc).__name__}: {exc}')
        return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}


def _document_text_count(hwp: Any, warnings: list[str]) -> dict[str, Any]:
    try:
        text = _get_document_text(hwp)
        return {'available': True, 'char_count': len(text), 'normalized_char_count': len(_normalize_visible_text(text))}
    except Exception as exc:  # pragma: no cover - live Hancom availability varies.
        warnings.append(f'document text count unavailable: {type(exc).__name__}: {exc}')
        return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    hwp = handle.hwp
    warnings: list[str] = [_RENDERED_PROOF_WARNING]
    expected_text = str(step.get('expected_text') or '')
    expected_normalized_text = _normalize_visible_text(expected_text)
    expected_hash = str(step.get('expected_hash') or '')
    expected_normalized_hash = str(step.get('expected_normalized_hash') or _sha256_text(expected_normalized_text))

    before = _snapshot_cursor_context(hwp)
    before_location = _live_location(handle, warnings)
    before_document_text_count = _document_text_count(hwp, warnings)
    if not _has_active_selection(before):
        raise EditOperationError(f'selected_text_delete_exact requires an active selection before deletion; before={before}')
    if _table_context_blocked(before):
        raise EditOperationError(f'selected_text_delete_exact refuses to delete in table/cell context; before={before}')

    selected_text = _get_selected_text(hwp, keep_select=True)
    selected_hash = _sha256_text(selected_text)
    selected_normalized_text = _normalize_visible_text(selected_text)
    selected_normalized_hash = _sha256_text(selected_normalized_text)
    selected_proof = {
        'preview': _safe_text(selected_text, max_chars=320),
        'len': len(selected_text),
        'hash': selected_hash,
        'normalized_preview': _safe_text(selected_normalized_text, max_chars=320),
        'normalized_len': len(selected_normalized_text),
        'normalized_hash': selected_normalized_hash,
    }
    if selected_text == '':
        raise EditOperationError(f'selected_text_delete_exact refuses to delete an empty selected text range; before={before}')
    exact_text_match = selected_text == expected_text
    exact_hash_match = selected_hash == expected_hash
    normalized_text_match = selected_normalized_text == expected_normalized_text
    normalized_hash_match = selected_normalized_hash == expected_normalized_hash
    if not exact_text_match or not exact_hash_match:
        raise EditOperationError(
            'selected_text_delete_exact selected text exact SHA256/text does not match expected source text; '
            'normalized matching is diagnostic only and never permits deletion when exact hash differs; '
            f'expected_hash={expected_hash}, expected_normalized_hash={expected_normalized_hash}, selected={selected_proof}'
        )

    _delete_selection(hwp)
    after = _snapshot_cursor_context(hwp)
    after_location = _live_location(handle, warnings)
    after_document_text_count = _document_text_count(hwp, warnings)
    result = {
        'schema_version': manifest.get('version') or 'local-cli/selected-text-delete-exact/v1',
        'source_text_deleted': True,
        'cleanup_separate_from_native_insertion': True,
        'selection_required': 'exact active plain-text selection outside table/cell context',
        'rendered_before_after_proof_required': True,
        'expected_hash': expected_hash,
        'expected_normalized_hash': expected_normalized_hash,
        'expected_text_preview': _safe_text(expected_text, max_chars=320),
        'selected_text': selected_proof,
        'match_policy': {
            'exact_text_match': exact_text_match,
            'exact_hash_match': exact_hash_match,
            'normalized_text_match': normalized_text_match,
            'normalized_hash_match': normalized_hash_match,
            'normalization_role': 'diagnostic_only; deletion requires exact selected text SHA256/text match',
        },
        'before': {
            'selection': {
                'has_selection': _has_active_selection(before),
                'selected_pos': before.get('selected_pos'),
                'selection_mode': before.get('selection_mode'),
            },
            'cursor_context': before,
            'location': before_location,
            'document_text_count': before_document_text_count,
        },
        'after': {
            'selection': {
                'has_selection': _has_active_selection(after),
                'selected_pos': after.get('selected_pos'),
                'selection_mode': after.get('selection_mode'),
            },
            'cursor_context': after,
            'location': after_location,
            'document_text_count': after_document_text_count,
        },
        'proof_refs': {
            'native_table_proof_ref': step.get('native_table_proof_ref'),
            'native_table_proof_hash': step.get('native_table_proof_hash'),
        },
        'warnings': list(warnings),
    }
    return result, True, warnings
