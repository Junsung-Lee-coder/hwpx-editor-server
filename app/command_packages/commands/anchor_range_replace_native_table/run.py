from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from app.command_packages.commands.context.run import _safe_text
from app.edit_ops import (
    EditOperationError,
    _delete_selection,
    _get_selected_text,
    _normalize_visible_text,
    _select_cursor_range,
    _selection_anchor_pos,
    _snapshot_cursor_context,
)
from app.local_cli_runtime import LocalCliRuntimeError, insert_native_table_at_cursor, snapshot_live_location

_MAX_ANCHOR_CHARS = 500
_MAX_EXPECTED_TEXT_CHARS = 50000
_MAX_OCCURRENCES = 12


def _raise(error_type: type[Exception], message: str) -> None:
    raise error_type(message, status_code=400)


def _sha256_text(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()


def _clean_required_text(step: dict[str, Any], key: str, index: int, error_type: type[Exception]) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        _raise(error_type, f'command-bundle step {index} anchor_range_replace_native_table requires non-empty {key}')
    text = value.strip()
    if len(text) > _MAX_ANCHOR_CHARS:
        _raise(error_type, f'command-bundle step {index} {key} is too long')
    step[key] = text
    return text


def _clean_optional_sha(value: Any, *, field: str, index: int, error_type: type[Exception]) -> str | None:
    if value in (None, ''):
        return None
    text = str(value or '').strip().lower()
    if not text.startswith('sha256:') or len(text) != len('sha256:') + 64:
        _raise(error_type, f'command-bundle step {index} {field} must be sha256:<64 hex chars>')
    try:
        int(text.removeprefix('sha256:'), 16)
    except ValueError:
        _raise(error_type, f'command-bundle step {index} {field} must be sha256:<64 hex chars>')
    return text


def _hash_cell_value(value: str) -> str:
    return _sha256_text(str(value))


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    _clean_required_text(step, 'section_anchor', index, error_type)
    _clean_required_text(step, 'start_anchor', index, error_type)
    _clean_required_text(step, 'end_before_anchor', index, error_type)
    if step['start_anchor'] == step['end_before_anchor']:
        _raise(error_type, f'command-bundle step {index} start_anchor and end_before_anchor must differ')

    for key in ('required_source_basename', 'forbid_source_basename'):
        if key in step and step.get(key) not in (None, ''):
            value = Path(str(step.get(key) or '').strip()).name
            if not value or len(value) > 260:
                _raise(error_type, f'command-bundle step {index} {key} must be a safe basename')
            step[key] = value
        elif key in step:
            step.pop(key, None)

    for key in ('expected_range_hash', 'expected_normalized_range_hash'):
        cleaned = _clean_optional_sha(step.get(key), field=key, index=index, error_type=error_type)
        if cleaned is not None:
            step[key] = cleaned
        elif key in step:
            step.pop(key, None)

    caption_text = str(step.get('caption_text') or '').strip()
    if caption_text:
        if len(caption_text) > _MAX_ANCHOR_CHARS:
            _raise(error_type, f'command-bundle step {index} caption_text is too long')
        step['caption_text'] = caption_text
    elif 'caption_text' in step:
        step.pop('caption_text', None)

    rows = step.get('rows')
    cols = step.get('cols')
    if isinstance(rows, bool) or not isinstance(rows, int) or not (1 <= rows <= 200):
        _raise(error_type, f'command-bundle step {index} rows must be integer 1..200')
    if isinstance(cols, bool) or not isinstance(cols, int) or not (1 <= cols <= 50):
        _raise(error_type, f'command-bundle step {index} cols must be integer 1..50')
    if cols != 4:
        _raise(error_type, f'command-bundle step {index} anchor_range_replace_native_table requires cols=4')
    cells = step.get('cells')
    if not isinstance(cells, list) or len(cells) != rows:
        _raise(error_type, f'command-bundle step {index} cells must contain exactly rows lists')
    cleaned_cells: list[list[str]] = []
    for row_index, row in enumerate(cells, start=1):
        if not isinstance(row, list) or len(row) != cols:
            _raise(error_type, f'command-bundle step {index} row {row_index} must contain exactly cols cells')
        cleaned_row: list[str] = []
        for col_index, cell in enumerate(row, start=1):
            if not isinstance(cell, str):
                _raise(error_type, f'command-bundle step {index} cell R{row_index}C{col_index} must be a string')
            if len(cell) > 2000:
                _raise(error_type, f'command-bundle step {index} cell R{row_index}C{col_index} is too long')
            cleaned_row.append(cell)
        cleaned_cells.append(cleaned_row)
    flat_values = [value for row in cleaned_cells for value in row]
    non_empty = [value for value in flat_values if value.strip()]
    if not non_empty:
        _raise(error_type, f'command-bundle step {index} anchor_range_replace_native_table rejects all-empty table content')
    step['cells'] = cleaned_cells

    field_name = str(step.get('field_name') or '').strip()
    if not field_name or len(field_name) > 80 or any(ch in field_name for ch in '\\/:*?"<>|'):
        _raise(error_type, f'command-bundle step {index} anchor_range_replace_native_table requires a safe field_name')
    step['field_name'] = field_name

    if step.get('confirm_replace') is not True:
        _raise(error_type, f'command-bundle step {index} anchor_range_replace_native_table requires confirm_replace=true')

    expected_hashes = [_hash_cell_value(value) for value in flat_values]
    if step.get('flat_value_hashes') != expected_hashes:
        _raise(error_type, f'command-bundle step {index} flat_value_hashes must match the cell values')
    if step.get('non_empty_token_count') != len(non_empty):
        _raise(error_type, f'command-bundle step {index} non_empty_token_count must match the cell values')
    if step.get('non_empty_token_preview') != non_empty[:8]:
        _raise(error_type, f'command-bundle step {index} non_empty_token_preview must match the cell values')
    if 'warnings' in step and (not isinstance(step.get('warnings'), list) or any(not isinstance(item, str) for item in step.get('warnings') or [])):
        _raise(error_type, f'command-bundle step {index} warnings must be a string array')
    if not isinstance(step.get('next_proof_required'), str) or not step.get('next_proof_required').strip():
        _raise(error_type, f'command-bundle step {index} next_proof_required must be non-empty text')
    return step


def _pos_tuple(match: Mapping[str, Any]) -> tuple[int, int, int]:
    snapshot = match.get('snapshot') if isinstance(match.get('snapshot'), dict) else {}
    pos = _selection_anchor_pos(snapshot)
    if pos is None:
        raise EditOperationError(f'live match has no selectable anchor position: {match}')
    return (int(pos[0]), int(pos[1]), int(pos[2]))


def _compare_pos(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return (left > right) - (left < right)


def _find_occurrences(service: Any, hwp: Any, query: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for occurrence in range(1, _MAX_OCCURRENCES + 1):
        try:
            match = service._find_live_match(hwp, query=query, occurrence=occurrence)  # noqa: SLF001
        except Exception as exc:
            if occurrence == 1:
                raise
            if getattr(exc, 'status_code', None) == 404 or 'No match found' in str(exc):
                break
            raise
        match['anchor_pos'] = list(_pos_tuple(match))
        matches.append(match)
    return matches


def _first_after(matches: list[dict[str, Any]], after_pos: tuple[int, int, int]) -> dict[str, Any] | None:
    ordered = sorted(matches, key=lambda item: _pos_tuple(item))
    for match in ordered:
        if _compare_pos(_pos_tuple(match), after_pos) > 0:
            return match
    return None


def _source_guard(handle: Any, hwp: Any, step: Mapping[str, Any]) -> dict[str, Any]:
    source_filename = str(getattr(handle, 'source_filename', '') or '').strip()
    source_basename = Path(source_filename).name if source_filename else ''
    document_path = str(getattr(hwp, 'Path', '') or '').strip()
    document_basename = Path(document_path).name if document_path else ''
    active_names = {name for name in (source_basename, document_basename) if name}
    required = str(step.get('required_source_basename') or '').strip()
    forbidden = str(step.get('forbid_source_basename') or '').strip()
    if required and required not in active_names:
        raise EditOperationError(
            f'anchor_range_replace_native_table source guard failed: required basename {required!r}, active={sorted(active_names)}'
        )
    if forbidden and forbidden in active_names:
        raise EditOperationError(
            f'anchor_range_replace_native_table source guard failed: forbidden basename {forbidden!r}, active={sorted(active_names)}'
        )
    return {
        'source_filename': source_filename or None,
        'source_basename': source_basename or None,
        'document_path': document_path or None,
        'document_basename': document_basename or None,
        'required_source_basename': required or None,
        'forbid_source_basename': forbidden or None,
    }


def _undo_best_effort(hwp: Any) -> dict[str, Any]:
    run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
    if callable(run):
        try:
            raw = run('Undo')
            return {'attempted': True, 'method': 'HAction.Run:Undo', 'succeeded': raw is None or bool(raw)}
        except Exception as exc:
            return {'attempted': True, 'method': 'HAction.Run:Undo', 'succeeded': False, 'error': f'{type(exc).__name__}: {exc}'}
    method = getattr(hwp, 'Undo', None)
    if callable(method):
        try:
            raw = method()
            return {'attempted': True, 'method': 'hwp.Undo', 'succeeded': raw is None or bool(raw)}
        except Exception as exc:
            return {'attempted': True, 'method': 'hwp.Undo', 'succeeded': False, 'error': f'{type(exc).__name__}: {exc}'}
    return {'attempted': False, 'succeeded': False, 'error': 'Undo action unavailable'}


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    hwp = handle.hwp
    warnings: list[str] = [str(item) for item in step.get('warnings') or []]
    source_guard = _source_guard(handle, hwp, step)
    before_location = snapshot_live_location(
        hwp=hwp,
        source_filename=str(getattr(handle, 'source_filename', '') or 'unknown'),
        working_copy_id=str(getattr(handle, 'session_id', '') or 'unknown'),
    )

    section_matches = _find_occurrences(service, hwp, str(step['section_anchor']))
    start_matches = _find_occurrences(service, hwp, str(step['start_anchor']))
    end_matches = _find_occurrences(service, hwp, str(step['end_before_anchor']))
    ordered_sections = sorted(section_matches, key=lambda item: _pos_tuple(item))
    ordered_starts = sorted(start_matches, key=lambda item: _pos_tuple(item))

    chosen_section: dict[str, Any] | None = None
    chosen_start: dict[str, Any] | None = None
    for section in ordered_sections:
        maybe_start = _first_after(ordered_starts, _pos_tuple(section))
        if maybe_start is not None:
            chosen_section = section
            chosen_start = maybe_start
            break
    if chosen_section is None or chosen_start is None:
        raise EditOperationError(
            'anchor_range_replace_native_table could not prove section_anchor precedes start_anchor; '
            f'section_positions={[m.get("anchor_pos") for m in section_matches]}, start_positions={[m.get("anchor_pos") for m in start_matches]}'
        )
    chosen_end = _first_after(end_matches, _pos_tuple(chosen_start))
    if chosen_end is None:
        raise EditOperationError(
            'anchor_range_replace_native_table could not prove end_before_anchor occurs after start_anchor; '
            f'start_pos={chosen_start.get("anchor_pos")}, end_positions={[m.get("anchor_pos") for m in end_matches]}'
        )

    start_pos = _pos_tuple(chosen_start)
    end_pos = _pos_tuple(chosen_end)
    if _compare_pos(start_pos, end_pos) >= 0:
        raise EditOperationError(f'anchor range is not forward: start_pos={start_pos}, end_pos={end_pos}')

    selected_pos = _select_cursor_range(hwp, start_cursor_pos=start_pos, end_cursor_pos=end_pos)
    selected_text = _get_selected_text(hwp, keep_select=True)
    if len(selected_text) > _MAX_EXPECTED_TEXT_CHARS:
        raise EditOperationError(f'anchor_range_replace_native_table selected range too long: {len(selected_text)} chars')
    selected_hash = _sha256_text(selected_text)
    normalized_selected_text = _normalize_visible_text(selected_text)
    normalized_selected_hash = _sha256_text(normalized_selected_text)
    normalized_start = _normalize_visible_text(str(step['start_anchor']))
    normalized_end = _normalize_visible_text(str(step['end_before_anchor']))
    if not _normalize_visible_text(selected_text).startswith(normalized_start):
        raise EditOperationError(
            'anchor_range_replace_native_table selected text does not start with start_anchor; '
            f'preview={_safe_text(selected_text, max_chars=320)!r}'
        )
    if normalized_end and normalized_end in _normalize_visible_text(selected_text):
        raise EditOperationError(
            'anchor_range_replace_native_table selected text unexpectedly includes end_before_anchor; '
            f'end_before_anchor={step["end_before_anchor"]!r}, preview={_safe_text(selected_text, max_chars=320)!r}'
        )
    if step.get('expected_range_hash') and step.get('expected_range_hash') != selected_hash:
        raise EditOperationError(
            f'anchor_range_replace_native_table expected_range_hash mismatch: expected={step.get("expected_range_hash")} actual={selected_hash}'
        )
    if step.get('expected_normalized_range_hash') and step.get('expected_normalized_range_hash') != normalized_selected_hash:
        raise EditOperationError(
            'anchor_range_replace_native_table expected_normalized_range_hash mismatch: '
            f'expected={step.get("expected_normalized_range_hash")} actual={normalized_selected_hash}'
        )

    selection_proof = {
        'selected_pos': list(selected_pos) if isinstance(selected_pos, (list, tuple)) else selected_pos,
        'len': len(selected_text),
        'hash': selected_hash,
        'normalized_len': len(normalized_selected_text),
        'normalized_hash': normalized_selected_hash,
        'preview': _safe_text(selected_text, max_chars=500),
        'starts_with_start_anchor': True,
        'includes_end_before_anchor': False,
    }

    before_delete_snapshot = _snapshot_cursor_context(hwp)
    deleted = False
    table_result: dict[str, Any] | None = None
    caption_insert_result: dict[str, Any] | None = None
    undo_after_failure: dict[str, Any] | None = None
    try:
        _delete_selection(hwp)
        deleted = True
        after_delete_snapshot = _snapshot_cursor_context(hwp)
        caption_text = str(step.get('caption_text') or '').strip()
        if caption_text:
            caption_strategy = service._insert_text_file_at_caret(  # noqa: SLF001
                hwp,
                text=caption_text + '\r\n',
                session_root=getattr(handle, 'session_root', None),
            )
            caption_insert_result = {
                'inserted': True,
                'text': caption_text,
                'text_hash': _sha256_text(caption_text),
                'strategy': caption_strategy,
            }
        table_result = insert_native_table_at_cursor(
            hwp,
            rows=int(step['rows']),
            cols=int(step['cols']),
            cells=step['cells'],
            field_name=str(step['field_name']),
        )
    except Exception as exc:
        if deleted:
            undo_after_failure = _undo_best_effort(hwp)
        raise LocalCliRuntimeError(
            'anchor_range_replace_native_table failed after range proof; '
            f'deleted={deleted}, undo_after_failure={undo_after_failure}, error={type(exc).__name__}: {exc}'
        ) from exc

    flat_values = [value for row in step['cells'] for value in row]
    assert table_result is not None
    table_result.update(
        {
            'cells': step['cells'],
            'flat_value_hashes': step.get('flat_value_hashes'),
            'non_empty_token_count': step.get('non_empty_token_count'),
            'non_empty_token_preview': step.get('non_empty_token_preview'),
            'flat_value_count': len(flat_values),
            'source_text_deleted': True,
            'old_plain_text_removal': 'deleted_by_anchor_range_replace_native_table',
            'next_proof_required': step.get('next_proof_required'),
        }
    )
    warnings.extend(str(item) for item in table_result.get('warnings') or [])
    after_location = snapshot_live_location(
        hwp=hwp,
        source_filename=str(getattr(handle, 'source_filename', '') or 'unknown'),
        working_copy_id=str(getattr(handle, 'session_id', '') or 'unknown'),
    )
    result = {
        'schema_version': manifest.get('version') or 'local-cli/anchor-range-replace-native-table/v1',
        'source_guard': source_guard,
        'anchors': {
            'section_anchor': step['section_anchor'],
            'start_anchor': step['start_anchor'],
            'end_before_anchor': step['end_before_anchor'],
            'section_match': {
                'occurrence': chosen_section.get('occurrence'),
                'pos': chosen_section.get('anchor_pos'),
                'matched_query': chosen_section.get('matched_query'),
            },
            'start_match': {
                'occurrence': chosen_start.get('occurrence'),
                'pos': chosen_start.get('anchor_pos'),
                'matched_query': chosen_start.get('matched_query'),
            },
            'end_match': {
                'occurrence': chosen_end.get('occurrence'),
                'pos': chosen_end.get('anchor_pos'),
                'matched_query': chosen_end.get('matched_query'),
            },
        },
        'selection': selection_proof,
        'before_delete': before_delete_snapshot,
        'after_delete': after_delete_snapshot,
        'caption_insert': caption_insert_result or {'inserted': False},
        'native_table': table_result,
        'old_source_text_cleanup': {
            'source_text_deleted': True,
            'deleted_range_hash': selected_hash,
            'deleted_range_normalized_hash': normalized_selected_hash,
        },
        'before_location': service._bundle_compact_location(before_location),  # noqa: SLF001
        'after_location': service._bundle_compact_location(after_location),  # noqa: SLF001
        'warnings': warnings,
    }
    return result, True, warnings
