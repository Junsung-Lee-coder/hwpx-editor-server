from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping

from app.command_packages.commands.context.run import (
    _as_dict,
    _block_context,
    _line_context,
    _merge_paragraph_probe,
    _merge_visual_line_probe,
    _nearby_text,
    _paragraph_context,
    _safe,
    _safe_text,
    _selection_text_probes,
    _warn,
)
from app.edit_ops import _get_pos, _get_selected_pos, _get_selected_text, _select_text, _set_pos


_URL_OR_DOI_RE = re.compile(
    r'(?i)(?:https?://|www\.|doi\.org/|\bdoi:\s*|\b10\.\d{4,9}/[-._;()/:A-Z0-9]+)'
)
_TOKEN_CHAR_RE = re.compile(r'[A-Za-z0-9:/._%#?=&+\-]')
_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    # `selection_proof` observes the current live selection only. No targeting fields are accepted.
    return step


def _capture_state(hwp: Any) -> dict[str, Any]:
    state: dict[str, Any] = {'pos': list(_get_pos(hwp))}
    try:
        selected = _get_selected_pos(hwp)
        state['selected_pos'] = list(selected)
        state['had_selection'] = _has_selection(selected)
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        state['selected_pos_error'] = f'{type(exc).__name__}: {exc}'
        state['had_selection'] = False
    return state


def _has_selection(selected_pos: Any) -> bool:
    return bool(isinstance(selected_pos, (list, tuple)) and len(selected_pos) >= 1 and selected_pos[0])


def _int_or_none(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except Exception:
        return None


def _json_clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, str):
        text = value.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        cleaned: list[str] = []
        for ch in text:
            if ch in {'\n', '\t'}:
                cleaned.append(ch)
            elif unicodedata.category(ch).startswith('C'):
                cleaned.append(' ')
            else:
                cleaned.append(ch)
        return ''.join(cleaned)
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return str(value)


def _normalize_selected_pos(selected_pos: Any) -> dict[str, Any]:
    raw = list(selected_pos) if isinstance(selected_pos, (list, tuple)) else None
    has_selection = _has_selection(selected_pos)
    normalized: dict[str, Any] = {'raw': raw, 'has_selection': has_selection}
    if not (isinstance(selected_pos, (list, tuple)) and len(selected_pos) >= 7):
        normalized['available'] = raw is not None
        return normalized

    start = {
        'list_id': _int_or_none(selected_pos[1]),
        'paragraph_index': _int_or_none(selected_pos[2]),
        'offset': _int_or_none(selected_pos[3]),
    }
    end = {
        'list_id': _int_or_none(selected_pos[4]),
        'paragraph_index': _int_or_none(selected_pos[5]),
        'offset': _int_or_none(selected_pos[6]),
    }
    normalized.update(
        {
            'available': True,
            'start': start,
            'end': end,
            'same_paragraph': (
                start.get('list_id') == end.get('list_id')
                and start.get('paragraph_index') == end.get('paragraph_index')
                and start.get('paragraph_index') is not None
            ),
        }
    )
    return normalized


def _state_after(hwp: Any) -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        state['pos'] = list(_get_pos(hwp))
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        state['pos_error'] = f'{type(exc).__name__}: {exc}'
    try:
        selected = _get_selected_pos(hwp)
        state['selected_pos'] = list(selected)
        state['had_selection'] = _has_selection(selected)
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        state['selected_pos_error'] = f'{type(exc).__name__}: {exc}'
        state['had_selection'] = False
    return state


def _restore_state(hwp: Any, original: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {'restored': False, 'strategy': None, 'before_restore': _state_after(hwp)}
    selected = original.get('selected_pos')
    pos = original.get('pos')
    errors: list[str] = []

    if original.get('had_selection') and isinstance(selected, list) and selected:
        try:
            _select_text(hwp, tuple(selected))
            after_select = _state_after(hwp)
            if after_select.get('selected_pos') == selected:
                result.update({'restored': True, 'strategy': 'select_text(saved_selected_pos)', 'after_restore': after_select})
                return result
            errors.append('saved selection restore did not reproduce the original selected_pos')
        except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
            errors.append(f'selection restore failed: {type(exc).__name__}: {exc}')

    if isinstance(pos, list) and len(pos) >= 3:
        try:
            _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
            after_pos = _state_after(hwp)
            result.update({'restored': True, 'strategy': 'set_pos(saved_pos)', 'after_restore': after_pos})
            if errors:
                result['warnings'] = errors
            return result
        except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
            errors.append(f'position restore failed: {type(exc).__name__}: {exc}')

    result['errors'] = errors or ['saved live position/selection was not restorable']
    result['after_restore'] = _state_after(hwp)
    return result


def _read_selected_text(hwp: Any, has_selection: bool, warnings: list[str]) -> dict[str, Any]:
    if not has_selection:
        return {
            'text': None,
            'preview': None,
            'len': None,
            'is_null': True,
            'is_empty': None,
            'hash': None,
            'method': 'not_read_no_active_selection',
        }
    try:
        text = _get_selected_text(hwp, keep_select=True)
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        _warn(warnings, f'selected text unavailable: {type(exc).__name__}: {exc}')
        return {
            'text': None,
            'preview': None,
            'len': None,
            'is_null': True,
            'is_empty': None,
            'hash': None,
            'method': 'pyhwpx.get_selected_text(keep_select=True)',
            'error': f'{type(exc).__name__}: {exc}',
        }
    return {
        'text': text,
        'preview': _safe_text(text, max_chars=320),
        'len': len(text),
        'is_null': False,
        'is_empty': text == '',
        'hash': 'sha256:' + hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest(),
        'method': 'pyhwpx.get_selected_text(keep_select=True)',
    }


def _slice_boundary_context(paragraph_text: str | None, selected_pos: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        'method': 'same_paragraph_selected_pos_offsets',
        'approximation': True,
        'before_text': None,
        'after_text': None,
        'before_char': None,
        'after_char': None,
    }
    if not paragraph_text:
        result['method'] = 'unavailable'
        result['warning'] = 'current paragraph text unavailable for exact boundary context'
        return result
    if selected_pos.get('same_paragraph') is not True:
        result['method'] = 'unavailable_multi_paragraph_or_unknown_range'
        result['warning'] = 'exact boundary context is only inferred for same-paragraph selections'
        return result

    start = _as_dict(selected_pos.get('start'))
    end = _as_dict(selected_pos.get('end'))
    start_offset = _int_or_none(start.get('offset'))
    end_offset = _int_or_none(end.get('offset'))
    if start_offset is None or end_offset is None:
        result['method'] = 'unavailable_invalid_offsets'
        result['warning'] = 'selected_pos offsets are unavailable for boundary context'
        return result
    if end_offset < 0:
        end_offset = len(paragraph_text)
    if not (0 <= start_offset <= len(paragraph_text) and 0 <= end_offset <= len(paragraph_text)):
        _warn(warnings, 'selected_pos offsets are outside the paragraph text returned by Hancom; boundary context is best-effort')
        start_offset = max(0, min(start_offset, len(paragraph_text)))
        end_offset = max(0, min(end_offset, len(paragraph_text)))
    if end_offset < start_offset:
        start_offset, end_offset = end_offset, start_offset

    before_text = paragraph_text[max(0, start_offset - 80):start_offset]
    after_text = paragraph_text[end_offset:end_offset + 80]
    result.update(
        {
            'approximation': False,
            'start_offset': start_offset,
            'end_offset': end_offset,
            'before_text': _safe_text(before_text, max_chars=120),
            'after_text': _safe_text(after_text, max_chars=120),
            'before_char': paragraph_text[start_offset - 1] if start_offset > 0 else None,
            'after_char': paragraph_text[end_offset] if end_offset < len(paragraph_text) else None,
        }
    )
    return result


def _token_char(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 1 and _TOKEN_CHAR_RE.fullmatch(value) is not None


def _expanded_boundary_token(boundary: Mapping[str, Any], selected_text: str | None) -> str:
    before = str(boundary.get('before_text') or '')
    after = str(boundary.get('after_text') or '')
    text = selected_text or ''
    left = re.search(r'[A-Za-z0-9:/._%#?=&+\-]*$', before)
    right = re.match(r'[A-Za-z0-9:/._%#?=&+\-]*', after)
    return f"{left.group(0) if left else ''}{text}{right.group(0) if right else ''}"


def _ranges_equal(left: Any, right: Any) -> bool:
    if not (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))):
        return False
    if len(left) < 7 or len(right) < 7:
        return False
    try:
        return tuple(left[:7]) == tuple(right[:7])
    except Exception:
        return False


def _cached_selection_evidence(service: Any, binding: Mapping[str, Any] | None, live_selected_pos: Mapping[str, Any]) -> dict[str, Any]:
    cache = service._cached_selection_proof(binding) if isinstance(binding, Mapping) else {}  # noqa: SLF001
    selected_range = cache.get('selected_range') if isinstance(cache, Mapping) else None
    selected_text = str(cache.get('selected_text') or '') if isinstance(cache, Mapping) else ''
    live_raw = live_selected_pos.get('raw')
    live_has_selection = bool(live_selected_pos.get('has_selection'))
    cached_available = bool(selected_range)
    matches_live = _ranges_equal(live_raw, selected_range) if live_has_selection and cached_available else False
    return {
        'available': cached_available or bool(selected_text),
        'selected_range': selected_range,
        'selected_text': selected_text or None,
        'selected_text_preview': _safe_text(selected_text, max_chars=320) if selected_text else None,
        'selected_text_hash': cache.get('selected_text_hash') if isinstance(cache, Mapping) else None,
        'proof_source': cache.get('proof_source') if isinstance(cache, Mapping) else None,
        'matches_live_selection': matches_live,
        'live_selection_missing_but_cached_selection_exists': bool(cached_available and not live_has_selection),
        'cached_selection_mismatch': bool(cached_available and live_has_selection and not matches_live),
    }


def _risk_flags(*, selected_text: str | None, selected_pos: Mapping[str, Any], boundary: Mapping[str, Any], block: Mapping[str, Any], location: Mapping[str, Any], restore: Mapping[str, Any], cached_selection: Mapping[str, Any]) -> dict[str, bool]:
    text = selected_text or ''
    has_selection = bool(selected_pos.get('has_selection'))
    first = text[0] if text else None
    last = text[-1] if text else None
    before_char = boundary.get('before_char')
    after_char = boundary.get('after_char')
    expanded_token = _expanded_boundary_token(boundary, selected_text)
    endpoint_inside_token = bool((_token_char(before_char) and _token_char(first)) or (_token_char(last) and _token_char(after_char)))
    url_like_touch = bool(_URL_OR_DOI_RE.search(text) or _URL_OR_DOI_RE.search(expanded_token))
    multi_paragraph = bool(selected_pos.get('same_paragraph') is False and has_selection)
    selection_break = bool('\n' in text or '\r' in text or multi_paragraph)
    return {
        'empty_selection': (not has_selection) or selected_text == '',
        'multi_paragraph_selection': multi_paragraph,
        'starts_or_ends_inside_url_like_token': bool(endpoint_inside_token and (url_like_touch or '://' in expanded_token or '.' in expanded_token)),
        'touches_url_or_doi_like_token': url_like_touch,
        'leading_or_trailing_whitespace': bool(text and (text[0].isspace() or text[-1].isspace())),
        'selection_contains_paragraph_break': selection_break,
        'selection_in_table_cell': bool(block.get('inside_table') or location.get('caret_in_table_cell') or location.get('cur_field_state') == 1),
        'restore_failed': restore.get('restored') is False,
        'live_selection_missing_but_cached_selection_exists': bool(cached_selection.get('live_selection_missing_but_cached_selection_exists')),
        'cached_selection_mismatch': bool(cached_selection.get('cached_selection_mismatch')),
        'tool_live_selection_disagreement': bool(cached_selection.get('live_selection_missing_but_cached_selection_exists') or cached_selection.get('cached_selection_mismatch')),
    }


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    from app.local_cli_runtime import snapshot_live_location

    warnings: list[str] = []
    original = _safe('position/selection before proof', lambda: _capture_state(handle.hwp), {}, warnings)
    location_before = _safe(
        'live location before proof',
        lambda: snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
            include_nearby_context=False,
        ),
        {},
        warnings,
    )
    selected_pos = _normalize_selected_pos(original.get('selected_pos'))
    cached_selection = _cached_selection_evidence(service, binding, selected_pos)
    if cached_selection.get('live_selection_missing_but_cached_selection_exists'):
        _warn(warnings, 'live Hancom selection is empty, but the tool has a cached selected range/text from the previous selection command')
    if cached_selection.get('cached_selection_mismatch'):
        _warn(warnings, 'live Hancom selection differs from the tool cached selected range')
    selected_text = _read_selected_text(handle.hwp, bool(selected_pos.get('has_selection')), warnings)
    after_text_read = _safe('position/selection after selected-text read', lambda: _state_after(handle.hwp), {}, warnings)

    restore_after_read = _restore_state(handle.hwp, original) if original else {'restored': False, 'errors': ['original state unavailable']}
    for warning in restore_after_read.get('warnings') or []:
        _warn(warnings, str(warning))
    if restore_after_read.get('restored') is False:
        _warn(warnings, 'selection proof could not restore the original selection/position after reading selected text')

    location_for_context = _safe(
        'live location for boundary context',
        lambda: snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
            include_nearby_context=False,
        ),
        {},
        warnings,
    )
    page_evidence = _safe('page evidence', lambda: service._bundle_page_evidence(handle.hwp), {'page': None, 'method': None}, warnings)  # noqa: SLF001
    block = _block_context(location_for_context)
    nearby_text = _nearby_text(location_for_context, warnings)
    paragraph_context = _paragraph_context(block, nearby_text)
    text_probes = _safe('selection text probes', lambda: _selection_text_probes(handle.hwp, warnings), {}, warnings)
    probe_paragraph = _as_dict(text_probes.get('paragraph'))
    if probe_paragraph:
        paragraph_context = _merge_paragraph_probe(paragraph_context, probe_paragraph)
    line_context = _line_context(page_evidence, location_for_context, block)
    probe_line = _as_dict(text_probes.get('visual_line'))
    if probe_line:
        line_context = _merge_visual_line_probe(line_context, probe_line)
    for probe in (probe_paragraph, _as_dict(text_probes.get('visual_line'))):
        restore = _as_dict(probe.get('restore'))
        if restore and restore.get('restored') is False:
            _warn(warnings, f"context probe restore failed: {restore.get('error') or 'unknown error'}")

    paragraph_text = paragraph_context.get('current_paragraph_text') or paragraph_context.get('current_paragraph_preview')
    boundary_core = _slice_boundary_context(str(paragraph_text) if paragraph_text not in (None, '') else None, selected_pos, warnings)

    final_restore = _restore_state(handle.hwp, original) if original else {'restored': False, 'errors': ['original state unavailable']}
    for warning in final_restore.get('warnings') or []:
        _warn(warnings, str(warning))
    if final_restore.get('restored') is False:
        _warn(warnings, 'selection proof could not restore the original selection/position after context probes')

    location_after = _safe(
        'live location after proof',
        lambda: snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
            include_nearby_context=False,
        ),
        {},
        warnings,
    )
    restore_evidence = {
        'before': original,
        'after_selected_text_read': after_text_read,
        'restore_after_selected_text_read': restore_after_read,
        'final_restore': final_restore,
        'after': _state_after(handle.hwp),
        'document_modified_before': location_before.get('document_is_modified'),
        'document_modified_after': location_after.get('document_is_modified'),
    }
    boundary_context = {
        **boundary_core,
        'paragraph_context': paragraph_context,
        'line_context': line_context,
    }
    risk_flags = _risk_flags(
        selected_text=selected_text.get('text') if isinstance(selected_text.get('text'), str) else None,
        selected_pos=selected_pos,
        boundary=boundary_core,
        block=block,
        location=location_for_context,
        restore=final_restore,
        cached_selection=cached_selection,
    )

    result = {
        'schema_version': manifest.get('version') or 'local-cli/selection-proof/v1-package',
        'read_only': True,
        'label': step.get('label') or 'selection-proof:active-selection',
        'selection_state': {
            'has_selection': selected_pos.get('has_selection'),
            'selection_mode': location_for_context.get('selection_mode') or _as_dict(location_for_context.get('cursor')).get('selection_mode'),
            'selected_pos': selected_pos,
            'position_before': original.get('pos'),
            'position_after': restore_evidence.get('after', {}).get('pos'),
        },
        'selected_text': selected_text,
        'cached_selection': cached_selection,
        'boundary_context': boundary_context,
        'risk_flags': risk_flags,
        'restore_evidence': restore_evidence,
        'proof_evidence': {
            'location_before': service._bundle_compact_location(location_before) if location_before else {},  # noqa: SLF001
            'location_after': service._bundle_compact_location(location_after) if location_after else {},  # noqa: SLF001
            'block_context': block,
            'selection_text_probes': text_probes,
        },
        'warnings': warnings,
    }
    return _json_clean(result), False, warnings
