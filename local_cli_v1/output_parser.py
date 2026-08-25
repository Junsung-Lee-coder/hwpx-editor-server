from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from typing import Any, Mapping


_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, '')]


def _safe_output_string(value: Any, *, max_chars: int = 1200) -> str:
    text = str(value)
    text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    cleaned: list[str] = []
    for ch in text:
        if ch in {'\n', '\t'}:
            cleaned.append(ch)
        elif unicodedata.category(ch).startswith('C'):
            cleaned.append(' ')
        else:
            cleaned.append(ch)
    result = ''.join(cleaned)
    if len(result) > max_chars:
        return result[:max_chars] + '…'
    return result


def _jsonable_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_safe_output_string(key, max_chars=200): _jsonable_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable_copy(item) for item in value]
    if isinstance(value, str):
        return _safe_output_string(value)
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return _safe_output_string(value)


def _compact_current_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    context = _as_dict(payload.get('context'))
    position = payload.get('cursor_summary')
    caret_pos = payload.get('caret_pos')
    cell_addr = payload.get('cell_addr')
    if not position and caret_pos not in (None, ''):
        position = f'pos {caret_pos}'
    if not position and cell_addr:
        position = f'cell {cell_addr}'
    return {
        'position': position,
        'selection': payload.get('selection_summary'),
        'current': payload.get('current_paragraph_preview') or context.get('current_paragraph_preview'),
        'warning': payload.get('warning'),
    }


def _normalize_step(raw_step: Any, fallback_index: int) -> dict[str, Any]:
    step = _as_dict(raw_step)
    index = step.get('index')
    if not isinstance(index, int):
        index = fallback_index
    result = _as_dict(step.get('result'))
    normalized = {
        'index': index,
        'label': step.get('label') or step.get('op') or f'step-{index}',
        'op': step.get('op'),
        'ok': bool(step.get('ok')),
        'dirty': bool(step.get('dirty')),
        'error': step.get('error'),
        'warnings': _as_str_list(step.get('warnings')),
        'result': _jsonable_copy(result),
        'before': _jsonable_copy(_as_dict(step.get('before'))),
        'after': _jsonable_copy(_as_dict(step.get('after'))),
    }
    known = {'index', 'label', 'op', 'ok', 'dirty', 'error', 'warnings', 'result', 'before', 'after'}
    extras = {str(key): _jsonable_copy(value) for key, value in step.items() if key not in known}
    if extras:
        normalized['extras'] = extras
    return normalized


def normalize_command_bundle(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize raw `/local-cli/command-bundle` JSON for local CLI formatting.

    This function is intentionally local-side only. It accepts the server's raw
    structured response, does not mutate it, preserves warnings/errors/unknowns,
    and returns a stable shape for human and JSON output formatters.
    """

    payload = _as_dict(raw_payload)
    raw_steps = _as_list(payload.get('steps'))
    steps = [_normalize_step(step, index) for index, step in enumerate(raw_steps, start=1)]
    inferred_ok = all(step.get('ok') for step in steps) if steps else bool(payload.get('ok'))
    ok = bool(payload.get('ok')) if 'ok' in payload else inferred_ok
    step_count = len(steps)
    completed_count = sum(1 for step in steps if step.get('ok'))
    summary = payload.get('summary') or f"command-bundle {'succeeded' if ok else 'stopped'}: {completed_count}/{step_count} step(s)"

    normalized = {
        'schema_version': 'local-output-parser/command-bundle/v1',
        'command': payload.get('command') or 'command-bundle',
        'ok': ok,
        'dirty': bool(payload.get('dirty')),
        'summary': summary,
        'completed_step_count': completed_count,
        'step_count': step_count,
        'warnings': _as_str_list(payload.get('warnings')),
        'before': _jsonable_copy(_as_dict(payload.get('before') or payload.get('before_location'))),
        'after': _jsonable_copy(_as_dict(payload.get('after') or payload.get('after_location'))),
        'steps': steps,
        'current_state': _compact_current_state(payload),
    }
    known = {
        'ok',
        'command',
        'summary',
        'dirty',
        'before',
        'after',
        'before_location',
        'after_location',
        'steps',
        'warnings',
        'context',
        'cursor_summary',
        'caret_pos',
        'cell_addr',
        'selection_summary',
        'current_paragraph_preview',
        'warning',
    }
    extras = {str(key): _jsonable_copy(value) for key, value in payload.items() if key not in known}
    if extras:
        normalized['extras'] = extras
    return normalized


def format_current_state_lines(payload: Mapping[str, Any]) -> list[str]:
    state = _as_dict(payload.get('current_state')) if 'current_state' in payload else _compact_current_state(payload)
    lines: list[str] = []
    if state.get('position'):
        lines.append(f"position: {state.get('position')}")
    if state.get('selection'):
        lines.append(f"selection: {state.get('selection')}")
    if state.get('current'):
        lines.append(f"current: {state.get('current')}")
    if state.get('warning'):
        lines.append(f"warning: {state.get('warning')}")
    return lines


def format_command_bundle_human(normalized_payload: Mapping[str, Any]) -> str:
    payload = _as_dict(normalized_payload)
    lines = [str(payload.get('summary') or 'command-bundle complete')]
    for raw_step in _as_list(payload.get('steps')):
        step = _as_dict(raw_step)
        status = 'ok' if step.get('ok') else 'failed'
        lines.append(f"{step.get('index')}. {step.get('label') or step.get('op')}: {status}")
        if step.get('error'):
            lines.append(f"   error: {step.get('error')}")
        result = _as_dict(step.get('result'))
        if result.get('text_preview'):
            lines.append(f"   text: {result.get('text_preview')}")
        if result.get('cell_addr'):
            lines.append(f"   cell: {result.get('cell_addr')}")
        if result.get('method_used'):
            lines.append(f"   method: {result.get('method_used')}")
        if result.get('proof_strength'):
            lines.append(f"   proof: {result.get('proof_strength')}")
        operation = _as_dict(result.get('operation'))
        if operation.get('op'):
            lines.append(f"   operation: {operation.get('op')}")
        desired = _as_dict(operation.get('desired'))
        if desired:
            lines.append(f"   desired: {', '.join(f'{key}={desired[key]!r}' for key in sorted(desired)[:8])}")
        changed = _as_dict(result.get('changed_values')) or _as_dict(result.get('changed_metrics')) or _as_dict(result.get('changed_style'))
        if changed:
            lines.append(f"   changed: {', '.join(sorted(changed)[:8])}")
        for warning in _as_str_list(step.get('warnings')):
            lines.append(f'   warning: {warning}')
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    lines.extend(format_current_state_lines(payload))
    return '\n'.join(lines)


def dumps_normalized_json(normalized_payload: Mapping[str, Any]) -> str:
    return json.dumps(_jsonable_copy(normalized_payload), ensure_ascii=False, indent=2)


def _first_step_result(normalized_payload: Mapping[str, Any], op: str) -> dict[str, Any]:
    for raw_step in _as_list(normalized_payload.get('steps')):
        step = _as_dict(raw_step)
        if step.get('op') == op:
            return _as_dict(step.get('result'))
    return {}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _all_warnings(normalized_payload: Mapping[str, Any]) -> list[str]:
    warnings = _as_str_list(normalized_payload.get('warnings'))
    for raw_step in _as_list(normalized_payload.get('steps')):
        warnings.extend(_as_str_list(_as_dict(raw_step).get('warnings')))
    return _dedupe_strings(warnings)


def summarize_selection_proof(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize bundle-backed `hwpx selection-proof` output."""

    normalized = normalize_command_bundle(raw_payload)
    proof = _first_step_result(normalized, 'selection_proof')
    selected_text = _as_dict(proof.get('selected_text'))
    selected_pos = _as_dict(_as_dict(proof.get('selection_state')).get('selected_pos'))
    missing = [
        field
        for field in ('selection_state', 'selected_text', 'boundary_context', 'risk_flags', 'restore_evidence')
        if proof.get(field) in (None, '')
    ]
    payload = {
        'schema_version': 'local-output-parser/selection-proof/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(proof.get('read_only')),
        'label': proof.get('label'),
        'selection_state': _jsonable_copy(_as_dict(proof.get('selection_state'))),
        'selected_text': _jsonable_copy(selected_text),
        'cached_selection': _jsonable_copy(_as_dict(proof.get('cached_selection'))),
        'boundary_context': _jsonable_copy(_as_dict(proof.get('boundary_context'))),
        'risk_flags': _jsonable_copy(_as_dict(proof.get('risk_flags'))),
        'restore_evidence': _jsonable_copy(_as_dict(proof.get('restore_evidence'))),
        'proof_evidence': _jsonable_copy(_as_dict(proof.get('proof_evidence'))),
        'selected_text_preview': selected_text.get('preview'),
        'selected_text_len': selected_text.get('len'),
        'has_selection': selected_pos.get('has_selection'),
        'warnings': _dedupe_strings([*_all_warnings(normalized), *_as_str_list(proof.get('warnings'))]),
        'parser_source': 'command-bundle:selection-proof',
        'information_complete': not missing,
    }
    if missing:
        payload['missing_information_fields'] = missing
    return payload


def _true_flag_names(flags: Mapping[str, Any]) -> list[str]:
    return [str(key) for key, value in flags.items() if value is True]


def format_selection_proof_human(raw_payload: Mapping[str, Any] | None) -> str:
    """Format bundle-backed `hwpx selection-proof` output for humans."""

    payload = summarize_selection_proof(raw_payload)
    lines = [str(payload.get('summary') or 'selection-proof complete')]
    lines.append(f"read-only: {'yes' if payload.get('read_only') else 'unknown'}")
    selection = _as_dict(payload.get('selection_state'))
    selected_pos = _as_dict(selection.get('selected_pos'))
    lines.append(f"has selection: {selection.get('has_selection')}")
    if selection.get('selection_mode') not in (None, ''):
        lines.append(f"selection mode: {selection.get('selection_mode')}")
    if selected_pos.get('raw') is not None:
        lines.append(f"selected pos: {selected_pos.get('raw')}")

    selected_text = _as_dict(payload.get('selected_text'))
    if selected_text.get('is_null') is True:
        lines.append('text: null')
    else:
        lines.append(f"text: {selected_text.get('preview') if selected_text.get('preview') is not None else ''}")
    if selected_text.get('len') is not None:
        lines.append(f"text length: {selected_text.get('len')}")
    if selected_text.get('hash'):
        lines.append(f"text hash: {selected_text.get('hash')}")

    cached = _as_dict(payload.get('cached_selection'))
    if cached.get('available'):
        lines.append(f"cached selection: {cached.get('selected_text_preview') or cached.get('selected_range')}")
        if cached.get('matches_live_selection') is not None:
            lines.append(f"cached matches live: {cached.get('matches_live_selection')}")

    boundary = _as_dict(payload.get('boundary_context'))
    if boundary.get('before_char') is not None or boundary.get('after_char') is not None:
        lines.append(f"boundary chars: before={boundary.get('before_char')!r} after={boundary.get('after_char')!r}")
    if boundary.get('before_text') not in (None, ''):
        lines.append(f"before: {boundary.get('before_text')}")
    if boundary.get('after_text') not in (None, ''):
        lines.append(f"after: {boundary.get('after_text')}")
    paragraph = _as_dict(boundary.get('paragraph_context'))
    if paragraph.get('paragraph_number_1based') not in (None, ''):
        lines.append(f"paragraph: #{paragraph.get('paragraph_number_1based')}")
    if paragraph.get('current_paragraph_preview') not in (None, ''):
        lines.append(f"paragraph current: {paragraph.get('current_paragraph_preview')}")
    line = _as_dict(boundary.get('line_context'))
    if line.get('current_visual_line_preview') not in (None, ''):
        lines.append(f"line current: {line.get('current_visual_line_preview')}")

    flags = _as_dict(payload.get('risk_flags'))
    active_flags = _true_flag_names(flags)
    lines.append(f"risk flags: {', '.join(active_flags) if active_flags else 'none'}")
    restore = _as_dict(payload.get('restore_evidence'))
    final_restore = _as_dict(restore.get('final_restore'))
    if final_restore:
        lines.append(f"restored: {final_restore.get('restored')} ({final_restore.get('strategy') or 'unknown strategy'})")
    if restore.get('document_modified_before') is not None or restore.get('document_modified_after') is not None:
        lines.append(f"modified before/after: {restore.get('document_modified_before')} -> {restore.get('document_modified_after')}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    if not payload.get('information_complete'):
        missing = ', '.join(_as_str_list(payload.get('missing_information_fields'))) or 'unknown fields'
        lines.append(f'warning: incomplete selection proof ({missing})')
    return '\n'.join(lines)


def format_where_from_bundle(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the local `hwpx where` payload from command-bundle output.

    `hwpx where` no longer needs exact legacy route parity. The useful public
    contract is position, selection, table/modified hints, and current paragraph
    proof when the server primitive returns it.
    """

    normalized = normalize_command_bundle(raw_payload)
    where_result = _first_step_result(normalized, 'where')
    location = _as_dict(where_result.get('location')) or _as_dict(normalized.get('after'))
    if not location:
        location = _as_dict(normalized.get('current_state'))
    payload = deepcopy(location)
    if payload.get('position') and not payload.get('cursor_summary'):
        payload['cursor_summary'] = payload.get('position')
    payload.setdefault('selection_summary', location.get('selection_summary') or location.get('selection') or 'none')
    payload.setdefault('current_paragraph_preview', location.get('current_paragraph_preview') or location.get('current'))
    payload['summary'] = normalized.get('summary')
    payload['ok'] = normalized.get('ok')
    payload['warnings'] = _all_warnings(normalized)
    payload['parser_source'] = 'command-bundle:where'
    useful_information_fields = {'cursor_summary', 'selection_summary'}
    missing = sorted(key for key in useful_information_fields if payload.get(key) in (None, ''))
    payload['information_complete'] = not missing
    if missing:
        payload['missing_information_fields'] = missing
    return payload


def format_where_bundle_human(raw_payload: Mapping[str, Any] | None) -> str:
    """Format bundle-backed `hwpx where` output for humans."""

    payload = format_where_from_bundle(raw_payload)
    lines: list[str] = []
    if payload.get('summary'):
        lines.append(str(payload.get('summary')))
    lines.append(f"position: {payload.get('cursor_summary') or 'unknown'}")
    lines.append(f"selection: {payload.get('selection_summary') or 'none'}")
    if payload.get('caret_in_table_cell') is not None:
        lines.append(f"in cell: {'yes' if payload.get('caret_in_table_cell') else 'no'}")
    if payload.get('document_is_modified') is not None:
        lines.append(f"modified: {'yes' if payload.get('document_is_modified') else 'no'}")
    if payload.get('current_paragraph_preview'):
        lines.append(f"current: {payload.get('current_paragraph_preview')}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    if not payload.get('information_complete'):
        missing = ', '.join(_as_str_list(payload.get('missing_information_fields'))) or 'unknown fields'
        lines.append(f'warning: incomplete where proof ({missing})')
    return '\n'.join(lines)


def summarize_selected_text_proof(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize selected-text-proof bundle output for local CLI output parsing."""

    normalized = normalize_command_bundle(raw_payload)
    text_result = _first_step_result(normalized, 'get_selected_text')
    missing = [
        field
        for field in ('text_preview', 'text_len', 'text_hash')
        if text_result.get(field) in (None, '')
    ]
    payload = {
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'selected_text_preview': text_result.get('text_preview'),
        'selected_text_len': text_result.get('text_len'),
        'selected_text_hash': text_result.get('text_hash'),
        'selected_text': text_result.get('selected_text'),
        'selected_text_normalized': text_result.get('selected_text_normalized'),
        'selection_source': text_result.get('selection_source'),
        'used_active_selection': text_result.get('used_active_selection'),
        'used_cached_selection': text_result.get('used_cached_selection'),
        'cached_selection_available': text_result.get('cached_selection_available'),
        'cached_selected_text_hash': text_result.get('cached_selected_text_hash'),
        'selected_text_verified_against_cache': text_result.get('selected_text_verified_against_cache'),
        'proof_method': text_result.get('proof_method'),
        'keep_select_requested': text_result.get('keep_select_requested'),
        'selected_range_before': text_result.get('selected_range_before'),
        'selected_range_after_read': text_result.get('selected_range_after_read'),
        'selected_range_restored': text_result.get('selected_range_restored'),
        'has_active_selection_before': text_result.get('has_active_selection_before'),
        'has_active_selection_after_read': text_result.get('has_active_selection_after_read'),
        'has_active_selection_restored': text_result.get('has_active_selection_restored'),
        'selection_preserved_after_read': text_result.get('selection_preserved_after_read'),
        'selection_restored': text_result.get('selection_restored'),
        'fail_closed_conditions': _as_str_list(text_result.get('fail_closed_conditions')),
        'before': normalized.get('before'),
        'after': normalized.get('after'),
        'warnings': _all_warnings(normalized),
        'parser_source': 'command-bundle:selected-text-proof',
        'information_complete': not missing,
    }
    if missing:
        payload['missing_information_fields'] = missing
    return payload


def format_selected_text_proof_human(raw_payload: Mapping[str, Any] | None) -> str:
    """Format selected-text-proof bundle output for humans."""

    payload = summarize_selected_text_proof(raw_payload)
    lines = [str(payload.get('summary') or 'selected-text-proof complete')]
    if payload.get('selected_text_preview') is not None:
        lines.append(f"text: {payload.get('selected_text_preview')}")
    if payload.get('selected_text_len') is not None:
        lines.append(f"text length: {payload.get('selected_text_len')}")
    if payload.get('selected_text_hash'):
        lines.append(f"text hash: {payload.get('selected_text_hash')}")
    if payload.get('proof_method'):
        lines.append(f"proof method: {payload.get('proof_method')}")
    if payload.get('selection_source'):
        lines.append(f"selection source: {payload.get('selection_source')}")
    if payload.get('selected_text_verified_against_cache') is not None:
        lines.append(f"cached proof verified: {payload.get('selected_text_verified_against_cache')}")
    if payload.get('selected_range_restored') is not None:
        lines.append(f"selected range: {payload.get('selected_range_restored')}")
    if payload.get('selection_restored') is not None:
        lines.append(f"selection restored: {payload.get('selection_restored')}")
    after = _as_dict(payload.get('after'))
    if after.get('cursor_summary'):
        lines.append(f"position: {after.get('cursor_summary')}")
    if after.get('selection_summary'):
        lines.append(f"selection: {after.get('selection_summary')}")
    if after.get('current_paragraph_preview'):
        lines.append(f"current: {after.get('current_paragraph_preview')}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    if not payload.get('information_complete'):
        missing = ', '.join(_as_str_list(payload.get('missing_information_fields'))) or 'unknown fields'
        lines.append(f'warning: incomplete selected-text proof ({missing})')
    return '\n'.join(lines)


def summarize_section_control_inventory(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a bundle-backed section/page control inventory response."""

    normalized = normalize_command_bundle(raw_payload)
    inventory = _first_step_result(normalized, 'control_inventory')
    items = _as_list(inventory.get('items'))
    return {
        'schema_version': 'local-output-parser/section-control-inventory/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(inventory.get('read_only')),
        'scope': _jsonable_copy(_as_dict(inventory.get('scope'))),
        'anchors': _jsonable_copy(_as_dict(inventory.get('anchors'))),
        'control_count_total': inventory.get('control_count_total'),
        'control_count_returned': inventory.get('control_count_returned', len(items)),
        'items': _jsonable_copy(items),
        'warnings': _all_warnings(normalized),
        'parser_source': 'command-bundle:section-control-inventory',
    }


def format_section_control_inventory_human(raw_payload: Mapping[str, Any] | None) -> str:
    payload = summarize_section_control_inventory(raw_payload)
    lines = [str(payload.get('summary') or 'section-control-inventory complete')]
    lines.append(f"read-only: {'yes' if payload.get('read_only') else 'unknown'}")
    lines.append(f"controls: {payload.get('control_count_returned', 0)} returned / {payload.get('control_count_total', '?')} total")
    scope = _as_dict(payload.get('scope'))
    scope_bits = []
    if scope.get('section_anchor'):
        scope_bits.append(f"section_anchor={scope.get('section_anchor')!r}")
    if scope.get('page_from'):
        scope_bits.append(f"pages={scope.get('page_from')}-{scope.get('page_to') or scope.get('page_from')}")
    if scope.get('around'):
        scope_bits.append(f"around={scope.get('around')!r}")
    if scope_bits:
        lines.append(f"scope: {', '.join(scope_bits)}")
    for raw_item in _as_list(payload.get('items')):
        item = _as_dict(raw_item)
        target = item.get('target_id') or f"control-{item.get('index', '?')}"
        page = item.get('page') if item.get('page') is not None else '?'
        ctrl_type = item.get('type') or item.get('ctrl_id') or 'control'
        proof_hash = item.get('proof_hash') or 'no-hash'
        lines.append(f"- {target} page={page} type={ctrl_type} hash={proof_hash}")
        if item.get('bounds'):
            lines.append(f"  bounds: {json.dumps(item.get('bounds'), ensure_ascii=False, default=str)}")
        if item.get('text_preview'):
            lines.append(f"  text: {item.get('text_preview')}")
        nearby = _as_dict(item.get('nearby'))
        if nearby.get('current_paragraph_preview'):
            lines.append(f"  nearby: {nearby.get('current_paragraph_preview')}")
        if item.get('target_match'):
            lines.append('  target proof: matched')
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    return '\n'.join(lines)


def summarize_section_table_frame_inventory(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a bundle-backed table/frame-flow inventory response."""

    normalized = normalize_command_bundle(raw_payload)
    inventory = _first_step_result(normalized, 'table_frame_inventory')
    items = _as_list(inventory.get('items'))
    groups = _as_list(inventory.get('anchor_groups'))
    return {
        'schema_version': 'local-output-parser/section-table-frame-inventory/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(inventory.get('read_only')),
        'scope': _jsonable_copy(_as_dict(inventory.get('scope'))),
        'anchors': _jsonable_copy(_as_dict(inventory.get('anchors'))),
        'control_count_total': inventory.get('control_count_total'),
        'control_count_returned': inventory.get('control_count_returned', len(items)),
        'items': _jsonable_copy(items),
        'anchor_groups': _jsonable_copy(groups),
        'next_tool_gap': inventory.get('next_tool_gap'),
        'warnings': _all_warnings(normalized),
        'parser_source': 'command-bundle:section-table-frame-inventory',
    }


def format_section_table_frame_inventory_human(raw_payload: Mapping[str, Any] | None) -> str:
    payload = summarize_section_table_frame_inventory(raw_payload)
    lines = [str(payload.get('summary') or 'section-table-frame-inventory complete')]
    lines.append(f"read-only: {'yes' if payload.get('read_only') else 'unknown'}")
    lines.append(f"controls: {payload.get('control_count_returned', 0)} returned / {payload.get('control_count_total', '?')} total")
    scope = _as_dict(payload.get('scope'))
    scope_bits = []
    if scope.get('section_anchor'):
        scope_bits.append(f"section_anchor={scope.get('section_anchor')!r}")
    if scope.get('page_from'):
        scope_bits.append(f"pages={scope.get('page_from')}-{scope.get('page_to') or scope.get('page_from')}")
    if scope.get('around'):
        scope_bits.append(f"around={scope.get('around')!r}")
    if scope_bits:
        lines.append(f"scope: {', '.join(scope_bits)}")
    for raw_group in _as_list(payload.get('anchor_groups')):
        group = _as_dict(raw_group)
        pages = ','.join(str(page) for page in _as_list(group.get('pages'))) or '?'
        types = ','.join(str(item) for item in _as_list(group.get('types'))) or '?'
        lines.append(f"- anchor {group.get('anchor_key')} pages={pages} types={types} controls={len(_as_list(group.get('items')))}")
        if group.get('fit_risk'):
            lines.append(f"  fit-risk: {group.get('fit_note')}")
        for raw_item in _as_list(group.get('items')):
            item = _as_dict(raw_item)
            shape = _as_dict(item.get('shape_properties'))
            values = _as_dict(shape.get('values'))
            size_bits = []
            if values.get('Width') is not None:
                size_bits.append(f"W={values.get('Width')}")
            if values.get('Height') is not None:
                size_bits.append(f"H={values.get('Height')}")
            if values.get('TreatAsChar') is not None:
                size_bits.append(f"TAC={values.get('TreatAsChar')}")
            if values.get('FlowWithText') is not None:
                size_bits.append(f"Flow={values.get('FlowWithText')}")
            suffix = f" ({', '.join(size_bits)})" if size_bits else ''
            lines.append(f"  - {item.get('target_id')} page={item.get('page') or '?'} type={item.get('type')}{suffix}")
    if payload.get('next_tool_gap'):
        lines.append(f"next tool gap: {payload.get('next_tool_gap')}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    return '\n'.join(lines)


def _metric_value(metrics: Mapping[str, Any], name: str) -> Any:
    metric = _as_dict(metrics.get(name))
    if 'value' in metric:
        return metric.get('value')
    if metric.get('error'):
        return f"error: {metric.get('error')}"
    return None


def summarize_table_cell_structure(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a read-only exact table/cell structure probe response."""

    normalized = normalize_command_bundle(raw_payload)
    probe = _first_step_result(normalized, 'table_cell_structure_exact')
    target_proof = _as_dict(probe.get('target_proof'))
    matched_before = _as_dict(target_proof.get('matched_before'))
    metrics = _as_dict(probe.get('metrics'))
    enter = _as_dict(probe.get('enter'))
    entered_snapshot = _as_dict(probe.get('entered_snapshot'))
    navigation_summary = _as_dict(probe.get('navigation_summary'))
    same_anchor_group = _as_dict(probe.get('same_anchor_group'))
    navigation = _as_list(probe.get('navigation'))
    moved_navigation = [item for item in navigation if _as_dict(item).get('moved')]
    table_width_mm = _metric_value(metrics, 'table_width_mm')
    table_height_mm = _metric_value(metrics, 'table_height_mm')
    col_width_mm = _metric_value(metrics, 'col_width_mm')
    row_height_mm = _metric_value(metrics, 'row_height_mm')
    single_cell_evidence = {
        'cell_addr': _metric_value(metrics, 'cell_addr_str') or enter.get('cell_addr') or entered_snapshot.get('cell_addr'),
        'cell_addr_zero_based': _metric_value(metrics, 'cell_addr_tuple'),
        'row_count': _metric_value(metrics, 'row_count'),
        'col_num': _metric_value(metrics, 'col_num'),
        'table_width_mm': table_width_mm,
        'table_height_mm': table_height_mm,
        'col_width_mm': col_width_mm,
        'row_height_mm': row_height_mm,
        'table_equals_cell_size': bool(
            table_width_mm not in (None, '')
            and table_height_mm not in (None, '')
            and col_width_mm not in (None, '')
            and row_height_mm not in (None, '')
            and table_width_mm == col_width_mm
            and table_height_mm == row_height_mm
        ),
        'any_navigation_moved': bool(navigation_summary.get('any_navigation_moved')),
        'addresses_seen_zero_based_col_row': _jsonable_copy(_as_list(navigation_summary.get('addresses_seen_zero_based_col_row'))),
    }
    return {
        'schema_version': 'local-output-parser/table-cell-structure-exact/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(probe.get('read_only')),
        'mutation': probe.get('mutation'),
        'scope': _jsonable_copy(_as_dict(probe.get('scope'))),
        'target': {
            'target_id': target_proof.get('target_id') or matched_before.get('target_id'),
            'expected_hash': target_proof.get('expected_hash'),
            'expected_page': target_proof.get('expected_page'),
            'actual_page': matched_before.get('page'),
            'proof_hash': matched_before.get('proof_hash'),
            'type': matched_before.get('type'),
        },
        'enter': {
            'is_cell': enter.get('is_cell'),
            'normal_edit_state': enter.get('normal_edit_state'),
            'cell_addr': enter.get('cell_addr'),
            'selection_mode': entered_snapshot.get('selection_mode'),
        },
        'single_cell_evidence': single_cell_evidence,
        'same_anchor_group': _jsonable_copy(same_anchor_group),
        'navigation': _jsonable_copy(navigation),
        'moved_navigation_count': len(moved_navigation),
        'clipping_owner_hypothesis': probe.get('clipping_owner_hypothesis'),
        'next_safe_step_hint': probe.get('next_safe_step_hint'),
        'doc_backed_api_actions': _as_str_list(probe.get('doc_backed_api_actions')),
        'warnings': [*_all_warnings(normalized), *_as_str_list(probe.get('warnings'))],
        'parser_source': 'command-bundle:table-cell-structure-exact',
    }


def format_table_cell_structure_human(raw_payload: Mapping[str, Any] | None) -> str:
    payload = summarize_table_cell_structure(raw_payload)
    lines = [str(payload.get('summary') or 'table-cell-structure-exact complete')]
    lines.append(f"read-only: {'yes' if payload.get('read_only') else 'unknown'}")
    if payload.get('mutation') not in (None, False):
        lines.append(f"mutation: {payload.get('mutation')}")
    target = _as_dict(payload.get('target'))
    if target:
        lines.append(
            f"target: {target.get('target_id') or '?'} page={target.get('actual_page') or target.get('expected_page') or '?'} "
            f"type={target.get('type') or '?'} hash={target.get('proof_hash') or target.get('expected_hash') or 'no-hash'}"
        )
    enter = _as_dict(payload.get('enter'))
    if enter:
        lines.append(
            f"entered: cell={enter.get('cell_addr') or '?'} "
            f"normal_edit_state={enter.get('normal_edit_state')} selection_mode={enter.get('selection_mode')}"
        )
    evidence = _as_dict(payload.get('single_cell_evidence'))
    if evidence:
        lines.append(
            f"size: table={evidence.get('table_width_mm')} x {evidence.get('table_height_mm')} mm; "
            f"cell/col/row={evidence.get('col_width_mm')} x {evidence.get('row_height_mm')} mm"
        )
        lines.append(f"row/col: rows={evidence.get('row_count')} cols={evidence.get('col_num')}")
        lines.append(
            f"navigation: moved={evidence.get('any_navigation_moved')} "
            f"addresses={evidence.get('addresses_seen_zero_based_col_row') or []}"
        )
        if evidence.get('table_equals_cell_size') and not evidence.get('any_navigation_moved'):
            lines.append('finding: single-cell/non-navigable table container evidence')
    group = _as_dict(payload.get('same_anchor_group'))
    if group:
        control_ids = ', '.join(str(item) for item in _as_list(group.get('control_ids')))
        types = ', '.join(str(item) for item in _as_list(group.get('types')))
        lines.append(f"same-anchor: types={types or '?'} controls={control_ids or '?'}")
        if group.get('fit_risk'):
            lines.append(f"fit-risk: {group.get('fit_note')}")
    if payload.get('clipping_owner_hypothesis'):
        lines.append(f"hypothesis: {payload.get('clipping_owner_hypothesis')}")
    if payload.get('next_safe_step_hint'):
        lines.append(f"next: {payload.get('next_safe_step_hint')}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    return '\n'.join(lines)



def _context_public_location(location: Mapping[str, Any]) -> dict[str, Any]:
    public = _jsonable_copy(_as_dict(location))
    if isinstance(public, dict) and 'caret_in_table_cell' in public:
        public['in_table_cell'] = public.pop('caret_in_table_cell')
    return public if isinstance(public, dict) else {}


def summarize_context(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a bundle-backed structured edit-position context card."""

    normalized = normalize_command_bundle(raw_payload)
    context = _first_step_result(normalized, 'context')
    return {
        'schema_version': 'local-output-parser/context/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(context.get('read_only')),
        'label': context.get('label'),
        'location': _context_public_location(_as_dict(context.get('location'))),
        'page': _jsonable_copy(_as_dict(context.get('page'))),
        'current_cursor': _jsonable_copy(_as_dict(context.get('current_cursor'))),
        'block_context': _jsonable_copy(_as_dict(context.get('block_context'))),
        'paragraph_context': _jsonable_copy(_as_dict(context.get('paragraph_context'))),
        'line_context': _jsonable_copy(_as_dict(context.get('line_context'))),
        'selection_text_probes': _jsonable_copy(_as_dict(context.get('selection_text_probes'))),
        'nearby_text': _jsonable_copy(_as_dict(context.get('nearby_text'))),
        'style_summary': _jsonable_copy(_as_dict(context.get('style_summary'))),
        'nearby_objects': _jsonable_copy(_as_dict(context.get('nearby_objects'))),
        'structure_signals': _jsonable_copy(_as_dict(context.get('structure_signals'))),
        'document': _jsonable_copy(_as_dict(context.get('document'))),
        'warnings': [*_all_warnings(normalized), *_as_str_list(context.get('warnings'))],
        'parser_source': 'command-bundle:context',
    }


def _context_style_bits(style_summary: Mapping[str, Any]) -> list[str]:
    character = _as_dict(style_summary.get('character'))
    paragraph = _as_dict(style_summary.get('paragraph'))
    bits: list[str] = []
    if character.get('font_size_pt') not in (None, ''):
        bits.append(f"font_size_pt={character.get('font_size_pt')}")
    if character.get('face_name') not in (None, ''):
        bits.append(f"face={character.get('face_name')}")
    if character.get('bold') not in (None, ''):
        bits.append(f"bold={character.get('bold')}")
    if paragraph.get('align') not in (None, ''):
        bits.append(f"align={paragraph.get('align')}")
    if paragraph.get('line_spacing') not in (None, ''):
        bits.append(f"line_spacing={paragraph.get('line_spacing')}")
    return bits


def format_context_human(raw_payload: Mapping[str, Any] | None) -> str:
    """Format bundle-backed `hwpx context` output for humans."""

    payload = summarize_context(raw_payload)
    lines = [str(payload.get('summary') or 'context complete')]
    location = _as_dict(payload.get('location'))
    page = _as_dict(payload.get('page'))
    block = _as_dict(payload.get('block_context'))
    paragraph = _as_dict(payload.get('paragraph_context'))
    line = _as_dict(payload.get('line_context'))
    nearby = _as_dict(payload.get('nearby_text'))
    style = _as_dict(payload.get('style_summary'))
    objects = _as_dict(payload.get('nearby_objects'))
    document = _as_dict(payload.get('document'))

    if document.get('name'):
        lines.append(f"document: {document.get('name')}")
    lines.append(f"current edit position: {location.get('cursor_summary') or 'unknown'}")
    if page.get('current') is not None:
        page_text = f"page: {page.get('current')}"
        if page.get('page_count') not in (None, ''):
            page_text += f" / {page.get('page_count')}"
        lines.append(page_text)
    if location.get('selection_summary'):
        lines.append(f"selection: {location.get('selection_summary')}")

    block_type = block.get('current_block_type') or 'unknown'
    cell = _as_dict(block.get('cell'))
    if cell.get('addr'):
        lines.append(
            f"block: {block_type} {cell.get('addr')} "
            f"(row={cell.get('row_1based') or '?'}, col={cell.get('col_1based') or '?'})"
        )
    else:
        lines.append(f"block: {block_type}")

    paragraph_number = paragraph.get('paragraph_number_1based')
    if paragraph_number not in (None, '') or paragraph.get('paragraph_index') not in (None, ''):
        paragraph_text = f"paragraph: #{paragraph_number}" if paragraph_number not in (None, '') else f"paragraph index: {paragraph.get('paragraph_index')}"
        paragraph_bits = []
        if paragraph.get('list_id') not in (None, ''):
            paragraph_bits.append(f"list={paragraph.get('list_id')}")
        if paragraph.get('offset') not in (None, ''):
            paragraph_bits.append(f"offset={paragraph.get('offset')}")
        if paragraph_bits:
            paragraph_text += f" ({', '.join(paragraph_bits)})"
        lines.append(paragraph_text)

    if line:
        line_number = line.get('line_number')
        line_index = line.get('line_index')
        line_preview = line.get('current_visual_line_preview')
        line_label = (
            f"line #{line_number}"
            if line_number not in (None, '')
            else (f"line index {line_index}" if line_index not in (None, '') else ('visual line text available' if line_preview not in (None, '') else 'visual line unavailable'))
        )
        line_bits = []
        if line.get('page_current') not in (None, ''):
            page_part = f"page={line.get('page_current')}"
            if line.get('page_count') not in (None, ''):
                page_part += f"/{line.get('page_count')}"
            line_bits.append(page_part)
        if line.get('offset_in_paragraph') not in (None, ''):
            line_bits.append(f"paragraph offset={line.get('offset_in_paragraph')}")
        if line.get('approximation') is True:
            line_bits.append('best-effort')
        lines.append(f"line: {line_label}" + (f" ({', '.join(line_bits)})" if line_bits else ''))
        if line_preview not in (None, ''):
            lines.append(f"line current: {line_preview}")

    if nearby.get('before') not in (None, ''):
        lines.append(f"nearby before: {nearby.get('before')}")
    if nearby.get('current') not in (None, ''):
        lines.append(f"nearby current: {nearby.get('current')}")
    if nearby.get('after') not in (None, ''):
        lines.append(f"nearby after: {nearby.get('after')}")

    style_bits = _context_style_bits(style)
    if style_bits:
        lines.append(f"style: {', '.join(style_bits)}")

    parent_ctrl = _as_dict(objects.get('parent_ctrl'))
    selected_ctrl = _as_dict(objects.get('selected_control'))
    object_bits = []
    if parent_ctrl.get('type') or parent_ctrl.get('ctrl_id'):
        object_bits.append(f"parent={parent_ctrl.get('type') or parent_ctrl.get('ctrl_id')}")
    if selected_ctrl.get('available') and (selected_ctrl.get('type') or selected_ctrl.get('ctrl_id')):
        object_bits.append(f"selected={selected_ctrl.get('type') or selected_ctrl.get('ctrl_id')}")
    if object_bits:
        lines.append(f"nearby objects: {', '.join(object_bits)}")

    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    return '\n'.join(lines)

def summarize_style_inspect(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a bundle-backed style-inspect response."""

    normalized = normalize_command_bundle(raw_payload)
    inspect = _first_step_result(normalized, 'style_inspect')
    return {
        'schema_version': 'local-output-parser/style-inspect/v1',
        'ok': normalized.get('ok'),
        'summary': normalized.get('summary'),
        'read_only': bool(inspect.get('read_only')),
        'match': inspect.get('match'),
        'match_evidence': _jsonable_copy(_as_dict(inspect.get('match_evidence'))),
        'position': _jsonable_copy(_as_dict(inspect.get('position'))),
        'selection': _jsonable_copy(_as_dict(inspect.get('selection'))),
        'character': _jsonable_copy(_as_dict(inspect.get('character'))),
        'paragraph': _jsonable_copy(_as_dict(inspect.get('paragraph'))),
        'list': _jsonable_copy(_as_dict(inspect.get('list'))),
        'raw': _jsonable_copy(_as_dict(inspect.get('raw'))),
        'warnings': _all_warnings(normalized),
        'parser_source': 'command-bundle:style-inspect',
    }


def format_style_inspect_human(raw_payload: Mapping[str, Any] | None) -> str:
    payload = summarize_style_inspect(raw_payload)
    lines = [str(payload.get('summary') or 'style-inspect complete')]
    if payload.get('match'):
        evidence = _as_dict(payload.get('match_evidence'))
        lines.append(f"match: {payload.get('match')!r} ({'found' if evidence.get('found') else 'not found'})")
    position = _as_dict(payload.get('position'))
    if position.get('cursor_summary') or position.get('pos'):
        lines.append(f"position: {position.get('cursor_summary') or position.get('pos')}")
    selection = _as_dict(payload.get('selection'))
    if selection.get('selection_summary'):
        lines.append(f"selection: {selection.get('selection_summary')}")
    character = _as_dict(payload.get('character'))
    if character:
        bits = []
        for key in ('font_size_pt', 'face_name', 'bold'):
            if character.get(key) not in (None, ''):
                bits.append(f'{key}={character.get(key)}')
        if bits:
            lines.append(f"character: {', '.join(bits)}")
    paragraph = _as_dict(payload.get('paragraph'))
    if paragraph:
        bits = []
        for key in ('align', 'left_margin', 'indent', 'line_spacing'):
            if paragraph.get(key) not in (None, ''):
                bits.append(f'{key}={paragraph.get(key)}')
        if bits:
            lines.append(f"paragraph: {', '.join(bits)}")
    list_state = _as_dict(payload.get('list'))
    if list_state:
        bits = []
        for key in ('enabled', 'kind', 'level', 'marker'):
            if list_state.get(key) not in (None, ''):
                bits.append(f'{key}={list_state.get(key)}')
        if bits:
            lines.append(f"list: {', '.join(bits)}")
    for warning in _as_str_list(payload.get('warnings')):
        lines.append(f'warning: {warning}')
    return '\n'.join(lines)


def summarize_typography_overview(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize bundle-backed `typography-overview` output into bounded JSON."""

    normalized = normalize_command_bundle(raw_payload)
    overview = _first_step_result(normalized, 'typography_overview')
    summary = _as_dict(overview.get('summary'))
    if not summary:
        # The server also exposes a human summary string; keep numeric defaults stable.
        summary = {}
    return {
        'schema_version': 'local-output-parser/typography-overview/v1',
        'ok': bool(overview.get('ok')) if 'ok' in overview else bool(normalized.get('ok')),
        'read_only': bool(overview.get('read_only', True)),
        'scope': overview.get('scope') or 'document',
        'backend': overview.get('backend') or _as_dict(overview.get('generated')).get('backend'),
        'summary_text': overview.get('summary') if isinstance(overview.get('summary'), str) else normalized.get('summary'),
        'document': _jsonable_copy(_as_dict(overview.get('document'))),
        'metrics': _jsonable_copy(summary),
        'global_fonts': _jsonable_copy(_as_list(overview.get('global_fonts'))[:20]),
        'global_sizes': _jsonable_copy(_as_list(overview.get('global_sizes'))[:20]),
        'font_size_distribution': _jsonable_copy(_as_list(overview.get('font_size_distribution'))[:20]),
        'style_variants': _jsonable_copy(_as_list(overview.get('style_variants'))[:20]),
        'sections': _jsonable_copy(_as_list(overview.get('sections'))[:30]),
        'pages': _jsonable_copy(_as_list(overview.get('pages'))[:30]),
        'page_scope_available': bool(overview.get('page_scope_available')),
        'page_scope_note': overview.get('page_scope_note'),
        'unresolved_char_shape_ids': _jsonable_copy(_as_list(overview.get('unresolved_char_shape_ids'))[:20]),
        'evidence_limitations': _as_str_list(overview.get('evidence_limitations')),
        'artifact': _jsonable_copy(_as_dict(overview.get('artifact'))),
        'caps': _jsonable_copy(_as_dict(overview.get('caps'))),
        'warnings': _all_warnings(normalized),
        'parser_source': 'command-bundle:typography-overview',
    }


def _value_ratio_rows(rows: list[Any], *, max_items: int = 5) -> str:
    bits: list[str] = []
    for raw in rows[:max_items]:
        row = _as_dict(raw)
        value = row.get('value')
        if value in (None, ''):
            value = row.get('font_family') or row.get('font_size_pt') or row.get('char_shape_id')
        chars = row.get('chars')
        ratio = row.get('ratio')
        if isinstance(ratio, (int, float)):
            bits.append(f'{value}({chars}, {float(ratio) * 100:.1f}%)')
        else:
            bits.append(f'{value}({chars})')
    return ', '.join(bits) if bits else 'unavailable'


def _font_size_distribution_rows(rows: list[Any], *, max_fonts: int = 5, max_sizes: int = 5) -> str:
    bits: list[str] = []
    for raw in rows[:max_fonts]:
        row = _as_dict(raw)
        font = row.get('font_family') or row.get('value') or 'unknown'
        size_text = _value_ratio_rows(_as_list(row.get('sizes')), max_items=max_sizes)
        segment = f'{font}: {size_text}'
        bold_ratio = row.get('bold_char_ratio')
        if isinstance(bold_ratio, (int, float)):
            segment += f'; bold={float(bold_ratio) * 100:.1f}%'
        bits.append(segment)
    return ' | '.join(bits) if bits else 'unavailable'


def format_typography_overview_human(raw_payload: Mapping[str, Any] | None) -> str:
    payload = summarize_typography_overview(raw_payload)
    metrics = _as_dict(payload.get('metrics'))
    lines = [str(payload.get('summary_text') or 'typography overview complete')]
    lines.append(f"backend: {payload.get('backend') or 'unknown'}")
    lines.append(
        'metrics: '
        f"chars={metrics.get('total_chars', 0)}, "
        f"spans={metrics.get('total_spans', 0)}, "
        f"fonts={metrics.get('unique_font_families', 0)}, "
        f"sizes={metrics.get('unique_font_sizes', 0)}, "
        f"variants={metrics.get('unique_style_variants', 0)}, "
        f"bold={float(metrics.get('bold_char_ratio') or 0) * 100:.1f}%"
    )
    lines.append(f"top fonts: {_value_ratio_rows(_as_list(payload.get('global_fonts')))}")
    lines.append(f"top sizes: {_value_ratio_rows(_as_list(payload.get('global_sizes')))}")
    lines.append(f"font size distribution: {_font_size_distribution_rows(_as_list(payload.get('font_size_distribution')))}")
    section_bits = []
    for section in _as_list(payload.get('sections'))[:5]:
        item = _as_dict(section)
        section_bits.append(
            f"section {item.get('section_index')}: chars={item.get('chars')} fonts={item.get('font_count')} sizes={item.get('size_count')} variants={item.get('style_variant_count')}"
        )
    if section_bits:
        lines.append(f"sections: {'; '.join(section_bits)}")
    if payload.get('page_scope_note'):
        lines.append(f"page note: {payload.get('page_scope_note')}")
    artifact = _as_dict(payload.get('artifact'))
    if artifact.get('path'):
        artifact_line = f"artifact: {artifact.get('path')}"
        if artifact.get('sha256'):
            artifact_line += f" sha256={artifact.get('sha256')}"
        lines.append(artifact_line)
    limitations = _as_str_list(payload.get('evidence_limitations'))
    if limitations:
        lines.append(f"limitations: {'; '.join(limitations[:3])}")
    warnings = _as_str_list(payload.get('warnings'))
    lines.append(f"warnings: {'; '.join(warnings) if warnings else 'none'}")
    return '\n'.join(lines)


def _int_cap(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _text_payload(value: Any) -> dict[str, Any]:
    payload = _as_dict(value)
    preview = payload.get('preview')
    if preview is None and payload.get('text') not in (None, ''):
        preview = _safe_output_string(payload.get('text'), max_chars=320)
    result = _jsonable_copy(payload)
    if preview is not None:
        result['preview'] = _safe_output_string(preview, max_chars=320)
    if result.get('normalized_hash') in (None, '') and preview not in (None, ''):
        result['normalized_hash'] = 'sha256:' + hashlib.sha256(str(preview).encode('utf-8', errors='replace')).hexdigest()
    return result


def _normalize_readback_block(raw_block: Any, *, expected_inside_table: bool | None, warnings: list[str]) -> dict[str, Any]:
    block = _as_dict(raw_block)
    inside_table = block.get('inside_table')
    if not isinstance(inside_table, bool):
        if expected_inside_table is None:
            inside_table = False
            warnings.append(f"readback block {block.get('block_id') or '<unknown>'} missing inside_table; defaulted false")
        else:
            inside_table = expected_inside_table
            warnings.append(f"readback block {block.get('block_id') or '<unknown>'} missing inside_table; inferred {inside_table}")
    elif expected_inside_table is not None and inside_table is not expected_inside_table:
        warnings.append(
            f"readback block {block.get('block_id') or '<unknown>'} inside_table={inside_table} conflicts with container expectation {expected_inside_table}"
        )
    result = {
        'block_id': block.get('block_id'),
        'block_type': block.get('block_type') or ('table_cell' if inside_table else 'paragraph'),
        'inside_table': inside_table,
        'page_candidate': block.get('page_candidate'),
        'location': _jsonable_copy(_as_dict(block.get('location'))),
        'table': _jsonable_copy(_as_dict(block.get('table'))) if inside_table or block.get('table') is not None else None,
        'text': _text_payload(block.get('text')),
        'style': _jsonable_copy(_as_dict(block.get('style'))),
        'context': _jsonable_copy(_as_dict(block.get('context'))),
        'warnings': _as_str_list(block.get('warnings')),
    }
    for key, value in block.items():
        if key not in result and key not in {'text', 'table', 'style', 'context', 'warnings'}:
            result[key] = _jsonable_copy(value)
    return result


def _cap_readback_items(items: list[Any], cap: int, *, key: str, warnings: list[str]) -> tuple[list[Any], bool]:
    if len(items) <= cap:
        return items, False
    warnings.append(f'{key} truncated to {cap} item(s); use raw_artifact_path/read-manifest for full detail')
    return items[:cap], True


def _compact_document_text_summary(raw_summary: Any) -> dict[str, Any]:
    summary = _as_dict(raw_summary)
    if not summary:
        return {}
    result: dict[str, Any] = {
        'available': bool(summary.get('available')) if 'available' in summary else False,
        'method': summary.get('method'),
        'char_count': summary.get('char_count'),
        'nonempty_line_count': summary.get('nonempty_line_count'),
        'hash': summary.get('hash'),
        'table_separation': summary.get('table_separation'),
    }
    if summary.get('error') not in (None, ''):
        result['error'] = _safe_output_string(summary.get('error'), max_chars=240)
    if summary.get('reason') not in (None, ''):
        result['reason'] = _safe_output_string(summary.get('reason'), max_chars=240)
    preview_lines = [_safe_output_string(line, max_chars=160) for line in _as_list(summary.get('preview_lines'))[:5] if line not in (None, '')]
    if preview_lines:
        result['preview_lines'] = preview_lines
        result['preview_line_count_returned'] = len(preview_lines)
    known = {'available', 'method', 'char_count', 'nonempty_line_count', 'hash', 'table_separation', 'error', 'reason', 'preview_lines'}
    extras = {str(key): _jsonable_copy(value) for key, value in summary.items() if key not in known}
    if extras:
        result['extras'] = extras
    return {key: value for key, value in result.items() if value not in (None, '')}


def _readback_result_from_raw(raw_payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if raw_payload.get('schema_version') == 'local-output-parser/readback/v1':
        return _as_dict(raw_payload), {'ok': raw_payload.get('ok'), 'summary': raw_payload.get('summary')}, _as_str_list(raw_payload.get('warnings'))
    normalized = normalize_command_bundle(raw_payload)
    return _first_step_result(normalized, 'readback'), normalized, _all_warnings(normalized)


def summarize_readback(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize bundle-backed `hwpx readback` output into bounded JSON."""

    raw = _as_dict(raw_payload)
    readback, normalized, warnings = _readback_result_from_raw(raw)
    warnings = _dedupe_strings([*warnings, *_as_str_list(readback.get('warnings'))])
    scope = str(readback.get('scope') or 'unknown')
    caps_raw = _as_dict(readback.get('caps'))
    max_blocks = _int_cap(caps_raw.get('max_blocks'), 300)
    max_table_cells = _int_cap(caps_raw.get('max_table_cells'), 800)
    max_controls = _int_cap(caps_raw.get('max_controls'), 2048)

    outside_blocks_raw = _as_list(readback.get('outside_text_blocks'))
    table_cells_raw = _as_list(readback.get('table_cells'))
    controls_raw = _as_list(readback.get('controls'))
    outside_blocks = [_normalize_readback_block(item, expected_inside_table=False, warnings=warnings) for item in outside_blocks_raw]
    table_cells = [_normalize_readback_block(item, expected_inside_table=True, warnings=warnings) for item in table_cells_raw]
    controls = [_jsonable_copy(_as_dict(item)) for item in controls_raw]
    document_text_summary = _compact_document_text_summary(readback.get('document_text_summary'))

    outside_blocks, outside_truncated = _cap_readback_items(outside_blocks, max_blocks, key='outside_text_blocks', warnings=warnings)
    table_cells, table_truncated = _cap_readback_items(table_cells, max_table_cells, key='table_cells', warnings=warnings)
    controls, controls_truncated = _cap_readback_items(controls, max_controls, key='controls', warnings=warnings)
    truncated = bool(caps_raw.get('truncated')) or outside_truncated or table_truncated or controls_truncated

    current_block_raw = readback.get('current_block')
    current_block = None
    if isinstance(current_block_raw, Mapping):
        expected = True if _as_dict(current_block_raw).get('inside_table') is True else None
        current_block = _normalize_readback_block(current_block_raw, expected_inside_table=expected, warnings=warnings)

    structure = _as_dict(readback.get('structure_summary'))
    if 'outside_text_block_count' not in structure:
        structure['outside_text_block_count'] = len(outside_blocks_raw)
    if 'table_cell_count' not in structure:
        structure['table_cell_count'] = len(table_cells_raw)
    if 'control_count' not in structure:
        structure['control_count'] = len(controls_raw)
    if 'table_count' not in structure:
        structure['table_count'] = sum(1 for item in controls_raw if str(_as_dict(item).get('type') or _as_dict(item).get('ctrl_id') or '').lower() in {'tbl', 'table'})
    if 'image_like_control_count' not in structure:
        structure['image_like_control_count'] = sum(
            1 for item in controls_raw if str(_as_dict(item).get('type') or _as_dict(item).get('ctrl_id') or '').lower() in {'pic', 'gso', 'shape', 'image'}
        )
    structure.setdefault('page_break_or_blank_risks', [])
    structure.setdefault('risk_flags', [])

    page = _jsonable_copy(_as_dict(readback.get('page')))
    warnings = _dedupe_strings([*warnings, *_as_str_list(_as_dict(page).get('warnings'))])
    if scope in {'page', 'document'} and structure.get('broad_text_block_enumeration_available') is False:
        warnings.append('page/document outside_text_blocks and table_cells are current-block-only; use document_text_summary and raw_artifact_path for broad text evidence')
    if structure.get('table_count') and not table_cells_raw:
        warnings.append('native table controls were found but table cell text enumeration is unavailable in this readback')

    return {
        'schema_version': 'local-output-parser/readback/v1',
        'ok': bool(readback.get('ok')) if 'ok' in readback else bool(normalized.get('ok')),
        'read_only': bool(readback.get('read_only', True)),
        'scope': scope,
        'summary': readback.get('summary') or normalized.get('summary') or f'readback {scope}: read-only summary',
        'document': _jsonable_copy(_as_dict(readback.get('document'))),
        'page': page,
        'structure_summary': _jsonable_copy(structure),
        'style_summary': _jsonable_copy(_as_dict(readback.get('style_summary'))),
        'document_text_summary': document_text_summary,
        'current_block': current_block,
        'selection': _jsonable_copy(_as_dict(readback.get('selection'))),
        'outside_text_blocks': outside_blocks,
        'table_cells': table_cells,
        'controls': controls,
        'caps': {
            'max_blocks': max_blocks,
            'max_table_cells': max_table_cells,
            'max_controls': max_controls,
            'truncated': truncated,
            'raw_artifact_path': caps_raw.get('raw_artifact_path'),
            'raw_artifact_sha256': caps_raw.get('raw_artifact_sha256'),
            'raw_artifact_bytes': caps_raw.get('raw_artifact_bytes'),
        },
        'warnings': _dedupe_strings(warnings),
        'parser_source': 'command-bundle:readback',
    }


def _count_preview(counts: Mapping[str, Any], *, max_items: int = 2) -> str:
    items = list(counts.items())[:max_items]
    return ', '.join(f'{key}({value})' for key, value in items) if items else 'unknown'


def _format_current_readback_block(block: Mapping[str, Any]) -> list[str]:
    if not block:
        return []
    lines: list[str] = []
    table = _as_dict(block.get('table'))
    style = _as_dict(block.get('style'))
    text = _as_dict(block.get('text'))
    location = _as_dict(block.get('location'))
    if block.get('inside_table'):
        cell_addr = table.get('cell_addr') or _as_dict(block.get('cell')).get('addr') or '?'
        lines.append(
            f"position: page {block.get('page_candidate') or '?'}, block=table_cell {cell_addr}, "
            f"row={table.get('row_1based') or '?'} col={table.get('col_1based') or '?'}"
        )
    else:
        paragraph = location.get('paragraph_index')
        offset = location.get('offset')
        lines.append(f"position: page {block.get('page_candidate') or '?'}, block={block.get('block_type') or 'paragraph'}, outside-table, paragraph={paragraph if paragraph is not None else '?'} offset={offset if offset is not None else '?'}")
    style_bits = []
    for key, label in (('font_family', 'font'), ('font_size_pt', 'size'), ('bold', 'bold'), ('align', 'align'), ('line_spacing', 'line_spacing')):
        if style.get(key) not in (None, ''):
            suffix = 'pt' if key == 'font_size_pt' else ''
            style_bits.append(f'{label}={style.get(key)}{suffix}')
    if style_bits:
        lines.append(f"style: {', '.join(style_bits)}")
    if text.get('preview') not in (None, ''):
        lines.append(f"line: {text.get('preview')}")
    context = _as_dict(block.get('context'))
    nearby = []
    for key in ('previous', 'current', 'next'):
        if context.get(key) not in (None, ''):
            nearby.append(f'{key}="{context.get(key)}"')
    if nearby:
        lines.append(f"nearby: {' | '.join(nearby)}")
    return lines


def format_readback_human(raw_payload: Mapping[str, Any] | None) -> str:
    """Format bundle-backed `hwpx readback` output for compact human review."""

    payload = summarize_readback(raw_payload)
    scope = payload.get('scope') or 'unknown'
    document = _as_dict(payload.get('document'))
    page = _as_dict(payload.get('page'))
    structure = _as_dict(payload.get('structure_summary'))
    style = _as_dict(payload.get('style_summary'))
    document_text = _as_dict(payload.get('document_text_summary'))
    caps = _as_dict(payload.get('caps'))
    lines = [str(payload.get('summary') or f'readback {scope}: read-only ok')]
    if lines[0].startswith('command-bundle'):
        lines[0] = f'readback {scope}: read-only ok'
    if document.get('name'):
        doc_line = f"document: {document.get('name')}"
        if document.get('page_count') not in (None, ''):
            doc_line += f" pages={document.get('page_count')}"
        lines.append(doc_line)
    if page.get('range'):
        page_range = _as_dict(page.get('range'))
        lines.append(f"page range: {page_range.get('from') or '?'}-{page_range.get('to') or '?'} ({page.get('method') or 'method unavailable'})")
    elif page.get('current') not in (None, ''):
        lines.append(f"page: {page.get('current')} ({page.get('method') or 'method unavailable'})")
    lines.extend(_format_current_readback_block(_as_dict(payload.get('current_block'))))
    if structure.get('broad_text_block_enumeration_available') is False:
        lines.append('coverage: broad text block/table-cell arrays are current-block-only; document_text_summary is flattened fallback')
    lines.append(
        'structure: '
        f"outside_blocks={structure.get('outside_text_block_count', 0)}, "
        f"tables={structure.get('table_count', 0)}, "
        f"table_cells={structure.get('table_cell_count', 0)}, "
        f"controls={structure.get('control_count', 0)}, "
        f"images={structure.get('image_like_control_count', 0)}"
    )
    family = style.get('body_font_family_candidate') or _count_preview(_as_dict(style.get('font_family_counts')))
    size = style.get('body_font_size_pt_candidate') or _count_preview(_as_dict(style.get('font_size_pt_counts')))
    mixed_font = 'warn' if style.get('mixed_font_warning') else 'no'
    mixed_size = 'warn' if style.get('mixed_size_warning') else 'no'
    lines.append(f"body style: font={family}; size={size}; mixed font={mixed_font}; mixed size={mixed_size}")

    if document_text:
        if document_text.get('available') is True:
            doc_text_line = (
                'document text: '
                f"chars={document_text.get('char_count') if document_text.get('char_count') not in (None, '') else '?'}, "
                f"lines={document_text.get('nonempty_line_count') if document_text.get('nonempty_line_count') not in (None, '') else '?'}"
            )
            if document_text.get('hash'):
                doc_text_line += f" hash={document_text.get('hash')}"
            preview_lines = _as_list(document_text.get('preview_lines'))
            if preview_lines:
                doc_text_line += f" preview=\"{' | '.join(str(line) for line in preview_lines[:2])}\""
            lines.append(doc_text_line)
        else:
            reason = document_text.get('reason') or document_text.get('error') or document_text.get('method') or 'unavailable'
            lines.append(f"document text: unavailable ({reason})")

    tables: list[str] = []
    for cell in _as_list(payload.get('table_cells'))[:3]:
        table = _as_dict(_as_dict(cell).get('table'))
        tables.append(
            f"{table.get('target_id') or _as_dict(cell).get('block_id') or 'table'} "
            f"cell={table.get('cell_addr') or '?'} page={_as_dict(cell).get('page_candidate') or '?'}"
        )
    if not tables:
        for control in _as_list(payload.get('controls')):
            item = _as_dict(control)
            kind = str(item.get('type') or item.get('ctrl_id') or '').lower()
            if kind in {'tbl', 'table'} or 'table' in kind:
                tables.append(f"{item.get('target_id') or 'table'} page={item.get('page') or '?'} hash={item.get('proof_hash') or 'no-hash'}")
            if len(tables) >= 3:
                break
    lines.append(f"native tables: {'; '.join(tables) if tables else 'none or unavailable'}")
    risk_flags = _as_str_list(structure.get('risk_flags'))
    if risk_flags:
        lines.append(f"risk flags: {', '.join(risk_flags)}")
    if caps.get('raw_artifact_path'):
        artifact_line = f"artifact: {caps.get('raw_artifact_path')}"
        if caps.get('raw_artifact_sha256'):
            artifact_line += f" sha256={caps.get('raw_artifact_sha256')}"
        lines.append(artifact_line)
    if caps.get('truncated'):
        lines.append('caps: printed arrays truncated; inspect artifact for full detail')
    warnings = _as_str_list(payload.get('warnings'))
    lines.append(f"warnings: {'; '.join(warnings) if warnings else 'none'}")
    return '\n'.join(lines)
