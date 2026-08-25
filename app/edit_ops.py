from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from app.native_actions import NativeActionRunner


class EditOperationError(ValueError):
    pass


SUPPORTED_OPS = {
    'replace_all',
    'replace_text_safe',
    'replace_paragraph_safe',
    'replace_paragraph_range_safe',
    'paragraph_replace_native',
    'paragraph_range_replace_native',
    'replace_between_anchors_safe',
    'clone_text_style',
    'clone_paragraph_shape',
    'clone_paragraph_layout',
    'insert_after_text',
    'insert_before_text',
    'insert_at_document_end',
    'style_text',
    'style_text_in_paragraph',
    'align_paragraph',
    'paragraph_shape',
    'list_paragraph',
    'replace_empty_native_list_scaffold',
    'native_action',
    'cursor_replace_text',
    'cursor_delete_range',
    'cursor_insert_text',
    'cursor_snapshot',
    'control_delete_by_anchor',
    'table_cell_action',
    'table_cell_clear_text',
    'table_cell_replace_text',
    'table_patch_cells',
}

SUPPORTED_VALIDATION_KEYS = {
    'preserve_page_count',
    'max_page_count',
    'max_page_increase',
}

PASSTHROUGH_INSTRUCTION_KEYS = {
    'request_id',
    'intent_id',
    'idempotency_key',
    'template_version',
    'inspect_snapshot_id',
    'template_fingerprint',
    'resolved_via',
    'resolved_target_id',
    'execution_run_id',
    'validation_run_id',
    'compile_status',
    'failure_reason',
}

SUPPORTED_STEP_VERIFICATION_MODES = (
    'text-local',
    'text-broader',
    'image',
)


def _normalize_step_verification_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in SUPPORTED_STEP_VERIFICATION_MODES:
        return normalized
    return None


def _extract_step_summary_metadata(op: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {}
    metadata = op.get('metadata') if isinstance(op.get('metadata'), dict) else {}
    verification = op.get('verification') if isinstance(op.get('verification'), dict) else {}

    raw_step_id = op.get('step_id')
    if raw_step_id in (None, ''):
        raw_step_id = metadata.get('step_id')
    if isinstance(raw_step_id, str) and raw_step_id.strip():
        item['step_id'] = raw_step_id.strip()

    verification_mode = _normalize_step_verification_mode(op.get('verification_mode'))
    if verification_mode is None:
        verification_mode = _normalize_step_verification_mode(verification.get('mode'))
    if verification_mode is None:
        verification_mode = _normalize_step_verification_mode(metadata.get('verification_mode'))
    if verification_mode is not None:
        item['verification_mode'] = verification_mode

    raw_needs_visual = op.get('needs_visual_verification')
    if raw_needs_visual is None:
        raw_needs_visual = verification.get('needs_visual_verification')
    if raw_needs_visual is None:
        raw_needs_visual = metadata.get('needs_visual_verification')
    if isinstance(raw_needs_visual, bool):
        item['needs_visual_verification'] = raw_needs_visual

    # Preserve the compiler's explicit behavioral role split in the runtime summary so callers can
    # review entry/verify/apply/evidence steps separately instead of reconstructing intent later.
    for source_key, summary_key in (
        ('step_role', 'step_role'),
        ('step_purpose', 'step_purpose'),
        ('risk_prevented', 'risk_prevented'),
        ('next_step_intent', 'next_step_intent'),
    ):
        value = metadata.get(source_key)
        if isinstance(value, str) and value.strip():
            item[summary_key] = value.strip()

    return item


def normalize_operations(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and 'operations' in raw:
        raw = raw['operations']
    if not isinstance(raw, list) or not raw:
        raise EditOperationError('instructions must be a non-empty JSON array, or an object with an operations array')

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EditOperationError(f'operation #{index} must be an object')
        op = str(item.get('op', '')).strip()
        if not op:
            raise EditOperationError(f'operation #{index} is missing op')
        if op not in SUPPORTED_OPS:
            supported = ', '.join(sorted(SUPPORTED_OPS))
            raise EditOperationError(f'operation #{index} uses unsupported op={op!r}. Supported: {supported}')
        normalized.append(dict(item))
    return normalized


def normalize_validation(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EditOperationError('validation must be an object when provided')

    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in SUPPORTED_VALIDATION_KEYS:
            supported = ', '.join(sorted(SUPPORTED_VALIDATION_KEYS))
            raise EditOperationError(f'unsupported validation key {key!r}. Supported: {supported}')
        if key == 'preserve_page_count':
            if not isinstance(value, bool):
                raise EditOperationError('validation.preserve_page_count must be a boolean')
            normalized[key] = value
        elif key == 'max_page_count':
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise EditOperationError('validation.max_page_count must be a positive integer')
            normalized[key] = value
        elif key == 'max_page_increase':
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EditOperationError('validation.max_page_increase must be a non-negative integer')
            normalized[key] = value

    return normalized


def normalize_instruction_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and 'operations' in raw:
        operations = normalize_operations(raw.get('operations'))
        validation = normalize_validation(raw.get('validation'))
        normalized: dict[str, Any] = {
            'operations': operations,
            'validation': validation,
        }
        for key in PASSTHROUGH_INSTRUCTION_KEYS:
            if raw.get(key) is not None:
                normalized[key] = raw.get(key)
        if raw.get('precondition') is not None:
            if not isinstance(raw.get('precondition'), dict):
                raise EditOperationError('precondition must be an object when provided')
            normalized['precondition'] = raw.get('precondition')
        if raw.get('metadata') is not None:
            if not isinstance(raw.get('metadata'), dict):
                raise EditOperationError('metadata must be an object when provided')
            normalized['metadata'] = raw.get('metadata')
        return normalized

    return {
        'operations': normalize_operations(raw),
        'validation': {},
    }


def load_operations_from_file(path: str | Path) -> list[dict[str, Any]]:
    return load_instruction_payload_from_file(path)['operations']


def load_instruction_payload_from_file(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return normalize_instruction_payload(data)


def _require_text(op: dict[str, Any], key: str) -> str:
    value = op.get(key)
    if not isinstance(value, str) or value == '':
        raise EditOperationError(f"operation {op.get('op')!r} requires non-empty string field {key!r}")
    return value


def _bool(op: dict[str, Any], key: str, default: bool) -> bool:
    value = op.get(key, default)
    return value if isinstance(value, bool) else default


def _scope(op: dict[str, Any]) -> str:
    value = str(op.get('apply', 'all')).strip().lower()
    if value not in {'all', 'first'}:
        raise EditOperationError(f"operation {op.get('op')!r} apply must be 'all' or 'first'")
    return value


def _number(op: dict[str, Any], key: str) -> float | int | None:
    value = op.get(key)
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditOperationError(f"operation {op.get('op')!r} field {key!r} must be a number")
    return value


def _normalize_list_level(op: dict[str, Any]) -> int:
    value = op.get('level', 1)
    if value is None or value == '':
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EditOperationError(f"operation {op.get('op')!r} field 'level' must be a positive integer")
    return value


def _clone_shape(shape: Any) -> Any:
    if isinstance(shape, dict):
        return dict(shape)
    if hasattr(shape, 'copy'):
        try:
            copied = shape.copy()
            if copied is not None:
                return copied
        except Exception:
            pass
    return shape


def _set_shape_list_level(shape: Any, level: int) -> bool:
    if level < 1:
        return False

    for field in ('HeadingLevel', 'ParaLevel', 'Level'):
        if isinstance(shape, dict):
            if field in shape:
                shape[field] = int(level)
                return True
            continue
        if hasattr(shape, field):
            try:
                setattr(shape, field, int(level))
                return True
            except Exception:
                continue
    return False


def _apply_parashape_with_optional_level(hwp: Any, shape: Any, *, level: int) -> bool:
    if not hasattr(hwp, 'set_parashape'):
        raise EditOperationError('pyhwpx set_parashape is unavailable on this machine')
    applied_shape = _clone_shape(shape)
    _set_shape_list_level(applied_shape, level)
    hwp.set_parashape(applied_shape)
    return True


def _promote_current_list_level(hwp: Any, *, level: int) -> bool:
    if level < 1 or not hasattr(hwp, 'get_parashape') or not hasattr(hwp, 'set_parashape'):
        return False
    try:
        pset = hwp.get_parashape()
    except Exception:
        return False
    if pset is None:
        return False
    updated = _clone_shape(pset)
    if not _set_shape_list_level(updated, level):
        return False
    hwp.set_parashape(updated)
    return True


def _color(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise EditOperationError(f'{field_name} must be an RGB number or #RRGGBB string')
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('#'):
            text = text[1:]
        if text.lower().startswith('0x'):
            text = text[2:]
        if len(text) == 6:
            try:
                return int(text, 16)
            except ValueError as exc:
                raise EditOperationError(f'{field_name} has invalid color value: {value!r}') from exc
    raise EditOperationError(f'{field_name} must be an RGB number or #RRGGBB string')


def _collect_text_style_kwargs(op: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if 'face_name' in op:
        kwargs['FaceName'] = op['face_name']
    if 'height_pt' in op:
        kwargs['Height'] = op['height_pt']
    if 'bold' in op:
        kwargs['Bold'] = bool(op['bold'])
    if 'italic' in op:
        kwargs['Italic'] = bool(op['italic'])
    if 'underline' in op:
        if op['underline']:
            kwargs['UnderlineType'] = 1
            kwargs['UnderlineShape'] = int(op.get('underline_shape', 1))
        else:
            kwargs['UnderlineType'] = 0
    if 'strikeout' in op:
        kwargs['StrikeOutType'] = bool(op['strikeout'])
    if 'text_color_rgb' in op:
        kwargs['TextColor'] = _color(op['text_color_rgb'], 'text_color_rgb')
    if 'shade_color_rgb' in op:
        kwargs['ShadeColor'] = _color(op['shade_color_rgb'], 'shade_color_rgb')
    spacing = _number(op, 'spacing')
    if spacing is not None:
        kwargs['Spacing'] = int(spacing)
    ratio = _number(op, 'ratio')
    if ratio is not None:
        kwargs['Ratio'] = int(ratio)
    return kwargs


def _move_doc_begin(hwp: Any) -> None:
    if hasattr(hwp, 'MoveDocBegin'):
        hwp.MoveDocBegin()


def _move_doc_end(hwp: Any) -> None:
    if hasattr(hwp, 'MoveDocEnd'):
        hwp.MoveDocEnd()


def _move_after_selection(hwp: Any) -> None:
    if hasattr(hwp, 'MoveRight'):
        hwp.MoveRight()


def _get_selected_pos(hwp: Any) -> tuple[Any, ...]:
    if hasattr(hwp, 'get_selected_pos'):
        return hwp.get_selected_pos()
    if hasattr(hwp, 'GetSelectedPos'):
        return hwp.GetSelectedPos()
    raise EditOperationError('pyhwpx get_selected_pos is unavailable on this machine')


def _get_selected_text(hwp: Any, *, keep_select: bool = True) -> str:
    if hasattr(hwp, 'get_selected_text'):
        return str(hwp.get_selected_text(keep_select=keep_select))
    if hasattr(hwp, 'GetSelectedText'):
        return str(hwp.GetSelectedText(keep_select=keep_select))
    raise EditOperationError('pyhwpx get_selected_text is unavailable on this machine')


def _get_pos(hwp: Any) -> tuple[Any, ...]:
    if hasattr(hwp, 'get_pos'):
        value = hwp.get_pos()
        return tuple(value) if isinstance(value, (list, tuple)) else (value,)
    if hasattr(hwp, 'GetPos'):
        value = hwp.GetPos()
        return tuple(value) if isinstance(value, (list, tuple)) else (value,)
    raise EditOperationError('pyhwpx get_pos is unavailable on this machine')


def _get_current_field_name(hwp: Any) -> str | None:
    if hasattr(hwp, 'GetCurFieldName'):
        try:
            return str(hwp.GetCurFieldName())
        except Exception:
            return None
    return None


def _get_selection_mode(hwp: Any) -> Any:
    method = getattr(hwp, 'SelectionMode', None)
    if callable(method):
        try:
            return method()
        except Exception as exc:
            return f'error:{exc}'
    value = getattr(hwp, 'SelectionMode', None)
    if value is not None:
        return value
    return None


def _is_cell_context(hwp: Any) -> bool | None:
    if hasattr(hwp, 'is_cell'):
        try:
            return bool(hwp.is_cell())
        except Exception:
            return None
    return None


def _get_cell_addr(hwp: Any) -> str | None:
    if hasattr(hwp, 'get_cell_addr'):
        try:
            value = hwp.get_cell_addr()
            if isinstance(value, str):
                text = value.strip().upper()
                if re.fullmatch(r'[A-Z]+\d+', text):
                    return text
        except Exception:
            pass
    key_indicator = getattr(hwp, 'KeyIndicator', None)
    if callable(key_indicator):
        try:
            indicator = key_indicator()
            if indicator and len(indicator) >= 9:
                status = str(indicator[8])
                matched = re.match(r'^\(([A-Z]+\d+)\)', status)
                if matched:
                    return matched.group(1)
        except Exception:
            pass
    return None


def _cell_addr_to_ref(cell_addr: str | None) -> dict[str, Any] | None:
    if not cell_addr or not isinstance(cell_addr, str):
        return None
    matched = re.fullmatch(r'([A-Z]+)(\d+)', cell_addr.strip().upper())
    if not matched:
        return None
    col_letters = matched.group(1)
    row_1based = int(matched.group(2))
    col_1based = 0
    for ch in col_letters:
        col_1based = (col_1based * 26) + (ord(ch) - ord('A') + 1)
    return {
        'addr': f'{col_letters}{row_1based}',
        'row_1based': row_1based,
        'col_1based': col_1based,
        'row_index': row_1based - 1,
        'col_index': col_1based - 1,
        'col_letters': col_letters,
    }


def _snapshot_cursor_context(hwp: Any) -> dict[str, Any]:
    cell_addr = _get_cell_addr(hwp)
    snapshot: dict[str, Any] = {
        'pos': list(_get_pos(hwp)),
        'field_name': _get_current_field_name(hwp),
        'selection_mode': _get_selection_mode(hwp),
        'is_cell': _is_cell_context(hwp),
        'cell_addr': cell_addr,
        'cell_ref': _cell_addr_to_ref(cell_addr),
    }
    try:
        selected = _get_selected_pos(hwp)
        snapshot['selected_pos'] = list(selected)
        snapshot['has_selection'] = bool(selected and selected[0])
    except Exception as exc:
        snapshot['selected_pos_error'] = str(exc)
    return snapshot


def _selection_anchor_pos(snapshot: dict[str, Any] | None) -> tuple[int, int, int] | None:
    if not isinstance(snapshot, dict):
        return None
    selected = snapshot.get('selected_pos')
    if not (isinstance(selected, list) and len(selected) >= 4 and selected[0]):
        return None
    try:
        return (int(selected[1]), int(selected[2]), int(selected[3]))
    except Exception:
        return None


def _normalize_selected_text_expectations(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, str) else value
    if not isinstance(raw_values, list):
        raise EditOperationError(f"cursor_snapshot field {field_name!r} must be a string or list of strings when provided")
    normalized: list[str] = []
    for item in raw_values:
        if not isinstance(item, str):
            raise EditOperationError(f"cursor_snapshot field {field_name!r} entries must be strings")
        token = item.strip()
        if not token:
            raise EditOperationError(f"cursor_snapshot field {field_name!r} entries must be non-empty strings")
        normalized.append(token)
    return normalized


def _selected_text_contains_probe(selected_text: str, probe: str) -> bool:
    raw_selected = str(selected_text or '')
    raw_probe = str(probe or '')
    if not raw_probe:
        return False
    if raw_probe in raw_selected:
        return True
    normalized_selected = _normalize_visible_text(raw_selected)
    normalized_probe = _normalize_visible_text(raw_probe)
    return bool(normalized_probe) and normalized_probe in normalized_selected


def _capture_selected_text_snapshot(hwp: Any) -> dict[str, Any]:
    selected_text = _get_selected_text(hwp, keep_select=True)
    return {
        'selected_text': selected_text,
        'selected_text_normalized': _normalize_visible_text(selected_text),
    }


def _preview_text(value: Any, *, limit: int = 180) -> str:
    text = str(value or '').replace('\r', ' ').replace('\n', ' ').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


def _capture_nearby_text_context(hwp: Any, *, preview_limit: int = 180) -> dict[str, Any]:
    try:
        pos = _get_pos(hwp)
        if len(pos) < 3:
            raise EditOperationError(f'unexpected cursor position shape: {pos!r}')
        list_id, para, cursor_pos = int(pos[0]), int(pos[1]), int(pos[2])
    except Exception as exc:
        return {'capture_error': str(exc)}

    restore_pos = (list_id, para, cursor_pos)
    context: dict[str, Any] = {
        'cursor_pos': [list_id, para, cursor_pos],
    }
    for label, target_para in (
        ('previous', para - 1),
        ('current', para),
        ('next', para + 1),
    ):
        if target_para < 0:
            context[f'{label}_paragraph_preview'] = None
            continue
        try:
            paragraph_text = _get_paragraph_text_by_position(
                hwp,
                list_id=list_id,
                para=target_para,
                restore_pos=restore_pos,
            )
        except Exception as exc:
            context[f'{label}_paragraph_preview'] = None
            context[f'{label}_paragraph_error'] = str(exc)
            continue
        context[f'{label}_paragraph_preview'] = _preview_text(paragraph_text, limit=preview_limit) if paragraph_text else ''
    return context


def _apply_selected_text_proof(
    snapshot: dict[str, Any],
    *,
    expected_absent_any: list[str],
    expected_present_any: list[str],
) -> None:
    selected_text = str(snapshot.get('selected_text') or '')
    effective_has_selection = bool(snapshot.get('has_selection')) or bool(selected_text)
    if expected_absent_any or expected_present_any:
        if not effective_has_selection:
            raise EditOperationError(
                'cursor_snapshot selected-text proof requires an active selection or captured selected text; '
                f'snapshot={snapshot}'
            )

    proof: dict[str, Any] = {
        'expected_absent_any': list(expected_absent_any),
        'expected_present_any': list(expected_present_any),
        'effective_has_selection': effective_has_selection,
        'matched_absent_any': [term for term in expected_absent_any if _selected_text_contains_probe(selected_text, term)],
        'matched_present_any': [term for term in expected_present_any if _selected_text_contains_probe(selected_text, term)],
    }
    proof['absent_any_ok'] = not proof['matched_absent_any']
    proof['present_any_ok'] = True if not expected_present_any else bool(proof['matched_present_any'])
    snapshot['selected_text_proof'] = proof

    if proof['matched_absent_any']:
        raise EditOperationError(
            'cursor_snapshot selected text still contains forbidden residue '
            f"{proof['matched_absent_any']!r}; selected_text={selected_text!r}"
        )
    if expected_present_any and not proof['matched_present_any']:
        raise EditOperationError(
            'cursor_snapshot selected text did not contain any required proof token '
            f"{expected_present_any!r}; selected_text={selected_text!r}"
        )


def _capture_table_cell_selected_text_proof(
    hwp: Any,
    *,
    expected_cell_addr: str | None,
    expected_selection_mode: int | None,
    expected_is_cell: bool | None,
    expected_absent_any: list[str],
    expected_present_any: list[str],
    op_name: str,
    action: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _select_current_cell_contents(hwp, expected_cell_addr=expected_cell_addr)
    snapshot = _snapshot_cursor_context(hwp)
    if expected_cell_addr is not None and snapshot.get('cell_addr') != expected_cell_addr:
        raise EditOperationError(
            f"{op_name} expected proof cell_addr={expected_cell_addr!r} but got {snapshot.get('cell_addr')!r}; snapshot={snapshot}"
        )
    verification = _verify_table_snapshot(
        op_name=op_name,
        action=action,
        after=snapshot,
        expected_selection_mode=expected_selection_mode,
        expected_is_cell=expected_is_cell,
    )
    snapshot.update(_capture_selected_text_snapshot(hwp))
    _apply_selected_text_proof(
        snapshot,
        expected_absent_any=expected_absent_any,
        expected_present_any=expected_present_any,
    )
    return snapshot, verification


def _delete_selection(hwp: Any) -> None:
    if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
        for action_name in ('Delete', 'Erase'):
            try:
                raw = hwp.HAction.Run(action_name)
                if raw is None or bool(raw):
                    return
            except Exception:
                pass
    errors: list[str] = []
    for method_name, arg_sets in (
        ('Delete', ((), (True,), (False,))),
        ('Erase', ((),)),
        ('DeleteBack', ((), (True,), (False,))),
    ):
        method = getattr(hwp, method_name, None)
        if not callable(method):
            continue
        for args in arg_sets:
            try:
                raw = method(*args)
                if raw is None or bool(raw):
                    return
            except TypeError as exc:
                errors.append(f'{method_name}{args}: {exc}')
                continue
            except Exception as exc:
                errors.append(f'{method_name}{args}: {exc}')
                continue
    raise EditOperationError('pyhwpx delete selection is unavailable on this machine')


def _put_field_text(hwp: Any, field: str, text: str) -> str:
    method = getattr(hwp, 'put_field_text', None)
    if not callable(method):
        method = getattr(hwp, 'PutFieldText', None)
    if not callable(method):
        raise EditOperationError('pyhwpx put_field_text is unavailable on this machine')

    attempts: list[tuple[tuple[Any, ...], dict[str, Any], str]] = [
        ((field, text), {}, 'field_text'),
        (({field: text},), {}, 'mapping'),
        ((), {'field': field, 'text': text}, 'kwargs'),
    ]
    type_errors: list[str] = []
    for args, kwargs, strategy in attempts:
        try:
            method(*args, **kwargs)
            return strategy
        except TypeError as exc:
            type_errors.append(f'{strategy}: {exc}')
            continue
        except Exception as exc:
            raise EditOperationError(f'put_field_text failed for field={field!r}: {exc}') from exc
    raise EditOperationError(
        'put_field_text did not accept any supported call pattern: ' + '; '.join(type_errors)
    )


def _clear_current_cell_text(hwp: Any, *, expected_cell_addr: str | None = None) -> dict[str, Any]:
    before = _snapshot_cursor_context(hwp)
    if before.get('is_cell') is not True:
        raise EditOperationError(f'clear_current_cell_text requires cursor inside a table cell; snapshot={before}')
    if expected_cell_addr is not None and before.get('cell_addr') != expected_cell_addr:
        raise EditOperationError(
            f"clear_current_cell_text expected cell_addr={expected_cell_addr!r} but got {before.get('cell_addr')!r}; snapshot={before}"
        )

    _select_current_cell_contents(hwp, expected_cell_addr=expected_cell_addr)
    selected_text = _get_selected_text(hwp, keep_select=True)
    selected = _snapshot_cursor_context(hwp)
    if expected_cell_addr is not None and selected.get('cell_addr') != expected_cell_addr:
        raise EditOperationError(
            f"clear_current_cell_text expected selected cell_addr={expected_cell_addr!r} but got {selected.get('cell_addr')!r}; selected={selected}"
        )
    if selected.get('selection_mode') not in {3, 19} and not selected.get('has_selection'):
        raise EditOperationError(
            'clear_current_cell_text could not establish a live cell selection before delete; '
            f'selected={selected}, selected_text={selected_text!r}'
        )

    _delete_selection(hwp)
    after = _snapshot_cursor_context(hwp)
    if expected_cell_addr is not None and after.get('cell_addr') != expected_cell_addr:
        raise EditOperationError(
            f"clear_current_cell_text expected final cell_addr={expected_cell_addr!r} but got {after.get('cell_addr')!r}; after={after}"
        )

    return {
        'before': before,
        'selected': selected,
        'selected_text': selected_text,
        'after': after,
    }


def _move_to_field(hwp: Any, field: str, *, select: bool = False) -> bool:
    if hasattr(hwp, 'move_to_field'):
        return bool(hwp.move_to_field(field, text=True, start=True, select=select))
    if hasattr(hwp, 'MoveToField'):
        return bool(hwp.MoveToField(field, 0, True, True, select))
    raise EditOperationError('pyhwpx move_to_field is unavailable on this machine')


def _select_current_cell_contents(hwp: Any, *, expected_cell_addr: str | None = None) -> dict[str, Any]:
    before = _snapshot_cursor_context(hwp)
    if before.get('is_cell') is not True:
        raise EditOperationError(f'table cell selection requires cursor inside a table cell; snapshot={before}')
    if expected_cell_addr is not None and before.get('cell_addr') != expected_cell_addr:
        raise EditOperationError(
            f"table cell selection expected cell_addr={expected_cell_addr!r} but got {before.get('cell_addr')!r}; snapshot={before}"
        )

    try:
        _run_table_cell_action(hwp, 'block')
        after = _snapshot_cursor_context(hwp)
        return {
            'strategy': 'table_cell_block',
            'before': before,
            'after': after,
        }
    except EditOperationError as block_exc:
        target_cell_addr = expected_cell_addr or before.get('cell_addr')
        if not isinstance(target_cell_addr, str) or not target_cell_addr:
            raise block_exc

        try:
            moved = _move_to_field(hwp, target_cell_addr, select=True)
        except Exception as field_exc:
            raise EditOperationError(
                'table cell selection failed after block fallback; '
                f'cell_addr={target_cell_addr!r}, block_error={block_exc}, field_select_error={field_exc}'
            ) from field_exc
        if not moved:
            raise EditOperationError(
                'table cell selection failed after block fallback; '
                f'cell_addr={target_cell_addr!r}, block_error={block_exc}, field_select_result={moved!r}'
            )

        after = _snapshot_cursor_context(hwp)
        if expected_cell_addr is not None and after.get('cell_addr') != expected_cell_addr:
            raise EditOperationError(
                'table cell selection field fallback moved to the wrong cell; '
                f"expected={expected_cell_addr!r}, actual={after.get('cell_addr')!r}, "
                f'before={before}, after={after}, block_error={block_exc}'
            )
        has_selection = bool(after.get('has_selection'))
        selected_text = None
        if not has_selection:
            try:
                selected_text = _get_selected_text(hwp, keep_select=True)
            except Exception:
                selected_text = None
            has_selection = bool(selected_text)
        if not has_selection:
            raise EditOperationError(
                'table cell selection field fallback did not produce a live selection; '
                f'cell_addr={target_cell_addr!r}, before={before}, after={after}, block_error={block_exc}'
            )
        return {
            'strategy': 'move_to_field_select',
            'before': before,
            'after': after,
            'selected_text': selected_text,
            'block_error': str(block_exc),
        }


def _parse_field_list_entries(field_list: str | None) -> list[dict[str, Any]]:
    """Return generated field-name entries while preserving duplicate suffixes.

    Hancom/pyhwpx field lists can expose duplicated names as A2{{0}}/A2{{1}} or
    similar. The base address is convenient for table-local navigation, but the
    raw entry/suffix is the evidence we need before deciding whether a global
    PutFieldText('A2', ...) call would be safe.
    """
    if not field_list:
        return []
    entries: list[dict[str, Any]] = []
    for part in str(field_list).split('\x02'):
        token = part.strip()
        if not token:
            continue
        suffix_match = re.search(r'\{\{(\d+)\}\}$', token)
        base = re.sub(r'\{\{\d+\}\}$', '', token)
        if re.fullmatch(r'[A-Z]+\d+', base):
            entries.append(
                {
                    'raw': token,
                    'addr': base,
                    'duplicate_suffix': int(suffix_match.group(1)) if suffix_match else None,
                }
            )
    return entries


def _parse_field_list_addresses(field_list: str | None) -> list[str]:
    return [entry['addr'] for entry in _parse_field_list_entries(field_list)]


def _field_scope_report_from_field_list(field_list: str | None, target_fields: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
    normalized_targets: list[str] = []
    for raw in target_fields:
        if not isinstance(raw, str):
            continue
        token = raw.strip().upper()
        if token and token not in normalized_targets:
            normalized_targets.append(token)

    entries = _parse_field_list_entries(field_list)
    by_addr: dict[str, list[str]] = {}
    for entry in entries:
        by_addr.setdefault(str(entry['addr']), []).append(str(entry['raw']))

    duplicate_targets = [addr for addr in normalized_targets if len(by_addr.get(addr, [])) > 1]
    missing_targets = [addr for addr in normalized_targets if addr not in by_addr]
    report = {
        'target_fields': normalized_targets,
        'field_count': len(entries),
        'raw_field_list_preview': str(field_list or '')[:240],
        'raw_by_target': {addr: by_addr.get(addr, []) for addr in normalized_targets},
        'duplicate_targets': duplicate_targets,
        'missing_targets': missing_targets,
        'safe_for_global_field_write': bool(normalized_targets) and not duplicate_targets and not missing_targets,
    }
    return report


def _capture_global_field_write_preflight(hwp: Any, target_fields: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
    """Fail-closed evidence for global PutFieldText safety.

    This does not authorize a write by itself. Callers that still want to use a
    generated field-name write must require safe_for_global_field_write=True;
    table-scoped primitives should prefer cursor/table navigation regardless.
    """
    get_field_list = getattr(hwp, 'get_field_list', None)
    if not callable(get_field_list):
        return {
            'available': False,
            'safe_for_global_field_write': False,
            'reason': 'get_field_list_unavailable',
            'target_fields': [str(item).strip().upper() for item in target_fields if isinstance(item, str)],
        }

    attempts: list[dict[str, Any]] = []
    for args in ((0, 1), (1, 1), (0, 0), (1, 0), ()):  # pyhwpx versions differ.
        try:
            field_list = get_field_list(*args)
        except TypeError as exc:
            attempts.append({'args': list(args), 'ok': False, 'error': f'TypeError: {exc}'})
            continue
        except Exception as exc:
            attempts.append({'args': list(args), 'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
            continue
        report = _field_scope_report_from_field_list(str(field_list or ''), target_fields)
        report.update({'available': True, 'args': list(args), 'attempts': attempts + [{'args': list(args), 'ok': True}]})
        return report

    return {
        'available': False,
        'safe_for_global_field_write': False,
        'reason': 'get_field_list_failed',
        'attempts': attempts,
        'target_fields': [str(item).strip().upper() for item in target_fields if isinstance(item, str)],
    }


def _normalize_expected_table_fingerprint(value: Any, *, field_name: str) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        token = value.get('fingerprint')
        return value, str(token).strip() if token is not None and str(token).strip() else None
    if isinstance(value, str) and value.strip():
        return None, value.strip()
    raise EditOperationError(f"{field_name} must be a non-empty string or object when provided")


def _move_to_cell_scoped(hwp: Any, target_cell_addr: str, *, max_steps: int = 128) -> dict[str, Any]:
    target_ref = _cell_addr_to_ref(target_cell_addr)
    if target_ref is None:
        raise EditOperationError(f'table-scoped cell navigation got invalid target cell address: {target_cell_addr!r}')

    snapshot = _snapshot_cursor_context(hwp)
    if snapshot.get('is_cell') is not True:
        raise EditOperationError(f'table-scoped cell navigation requires an in-table cursor; target={target_cell_addr!r}, snapshot={snapshot}')
    if snapshot.get('cell_addr') == target_ref['addr']:
        return {'target_cell_addr': target_ref['addr'], 'start': snapshot, 'steps': [], 'reached': True}

    steps: list[dict[str, Any]] = []
    visited = {str(snapshot.get('cell_addr') or '')}
    for _ in range(max_steps):
        current_ref = _cell_addr_to_ref(snapshot.get('cell_addr'))
        if current_ref is None:
            raise EditOperationError(
                f'table-scoped cell navigation lost readable cell address before reaching {target_ref["addr"]!r}; '
                f'snapshot={snapshot}, steps={steps}'
            )
        current_row = int(current_ref['row_1based'])
        current_col = int(current_ref['col_1based'])
        target_row = int(target_ref['row_1based'])
        target_col = int(target_ref['col_1based'])
        if current_row < target_row:
            action = 'down'
        elif current_row > target_row:
            action = 'up'
        elif current_col < target_col:
            action = 'right'
        else:
            action = 'left'

        before = dict(snapshot)
        _run_table_cell_action(hwp, action)
        snapshot = _snapshot_cursor_context(hwp)
        step = {'action': action, 'before': before, 'after': snapshot}
        steps.append(step)
        if snapshot.get('is_cell') is not True:
            raise EditOperationError(
                f'table-scoped cell navigation left the table while moving to {target_ref["addr"]!r}; step={step}, steps={steps}'
            )
        if snapshot.get('cell_addr') == target_ref['addr']:
            return {'target_cell_addr': target_ref['addr'], 'start': steps[0]['before'], 'steps': steps, 'reached': True}
        visit_key = str(snapshot.get('cell_addr') or '')
        if not visit_key or visit_key in visited:
            raise EditOperationError(
                f'table-scoped cell navigation could not reach {target_ref["addr"]!r} without cycling; '
                f'current={snapshot}, visited={sorted(visited)}, steps={steps}'
            )
        visited.add(visit_key)

    raise EditOperationError(
        f'table-scoped cell navigation exceeded max_steps={max_steps} while moving to {target_ref["addr"]!r}; '
        f'last_snapshot={snapshot}, steps={steps}'
    )


def _resolve_current_table_cell_for_replacement(hwp: Any, target_cell_addr: str) -> dict[str, Any]:
    """Resolve a generated A1-style cell address inside the *current* table only.

    This is the fail-closed resolver for official forms where many tables reuse
    generated field names such as A2/B2/C2. It deliberately avoids
    MoveToField/PutFieldText/global generated-field writes; callers must first
    place the caret inside the intended table (for example by an anchor find or
    an exact cursor/table identity proof), then this helper navigates by native
    table-cell actions within that table.
    """

    target_ref = _cell_addr_to_ref(target_cell_addr)
    if target_ref is None:
        raise EditOperationError(f'current-table cell replacement got invalid target cell address: {target_cell_addr!r}')

    start = _snapshot_cursor_context(hwp)
    if start.get('is_cell') is not True:
        raise EditOperationError(
            'current-table cell replacement requires the cursor to already be inside the intended table; '
            f'refusing global generated-field navigation for target={target_ref["addr"]!r}; start={start}'
        )

    navigation = _move_to_cell_scoped(hwp, target_ref['addr'])
    target_snapshot = _snapshot_cursor_context(hwp)
    if target_snapshot.get('cell_addr') != target_ref['addr']:
        raise EditOperationError(
            'current-table cell replacement landed in the wrong cell after scoped navigation; '
            f'expected={target_ref["addr"]!r}, actual={target_snapshot.get("cell_addr")!r}, '
            f'navigation={navigation}, target_snapshot={target_snapshot}'
        )
    if target_snapshot.get('is_cell') is not True:
        raise EditOperationError(
            'current-table cell replacement left table context after scoped navigation; '
            f'target={target_ref["addr"]!r}, target_snapshot={target_snapshot}'
        )

    global_field_write_preflight = _capture_global_field_write_preflight(hwp, [target_ref['addr']])
    global_field_write_preflight['decision'] = 'rejected_for_current_table_cell_replace'
    if not global_field_write_preflight.get('safe_for_global_field_write'):
        global_field_write_preflight['reason'] = 'duplicate_or_missing_generated_field_names_fail_closed'

    return {
        'match_strategy': 'current_table_scoped_cell_navigation',
        'matched_query': target_ref['addr'],
        'requested_cell_addr': target_ref['addr'],
        'start_snapshot': start,
        'target_snapshot': target_snapshot,
        'navigation': navigation,
        'global_field_write_preflight': global_field_write_preflight,
    }


def _restore_cursor_from_snapshot(hwp: Any, snapshot: dict[str, Any] | None) -> None:
    if not isinstance(snapshot, dict):
        return
    pos = snapshot.get('pos')
    if isinstance(pos, list) and len(pos) >= 3:
        try:
            _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
        except Exception:
            return


def _selected_text_proves_replacement(*, selected_text: str, replace_text: str) -> bool:
    if replace_text == '':
        return _normalize_visible_text(selected_text) == ''
    return _selected_text_matches_expected(selected_text=selected_text, expected_text=replace_text)


def _reject_typed_list_marker_if_needed(text: str, *, kind: str, allow_typed_markers: bool) -> None:
    if allow_typed_markers or kind == 'none':
        return
    if re.match(r'^\s*(?:[-*•·●○■□▪▫◦]|\(?\d+[.)]|[①-⑳])\s+', text):
        raise EditOperationError(
            'replace_empty_native_list_scaffold item text must omit typed bullet/number markers; '
            f'kind={kind!r}, text={text!r}'
        )


def _normalize_visible_text(value: str | None) -> str:
    return ' '.join(str(value or '').split()).strip()


def _live_table_fingerprint_from_field_list(hwp: Any, field_list: str | None) -> dict[str, Any]:
    addresses = _parse_field_list_addresses(field_list)
    if not addresses:
        return {}

    by_row: dict[int, dict[int, str]] = {}
    max_col = 0
    for address in addresses:
        ref = _cell_addr_to_ref(address)
        if ref is None:
            continue
        row = int(ref['row_1based'])
        col = int(ref['col_1based'])
        max_col = max(max_col, col)
        if not _move_to_field(hwp, address):
            raise EditOperationError(f'failed to move_to_field({address!r}) while building live table fingerprint')
        _select_current_cell_contents(hwp, expected_cell_addr=address)
        cell_text = _normalize_visible_text(_get_selected_text(hwp, keep_select=True))
        by_row.setdefault(row, {})[col] = cell_text

    row_count = max(by_row) if by_row else 0
    rows: list[list[str]] = []
    for row_index in range(1, row_count + 1):
        row_values: list[str] = []
        row_map = by_row.get(row_index, {})
        for col_index in range(1, max_col + 1):
            row_values.append(row_map.get(col_index, ''))
        rows.append(row_values)

    header_row = rows[0] if rows else []
    first_col_keys = [row[0] for row in rows[1:] if row and row[0]]
    payload = {
        'row_count': len(rows),
        'col_count': max_col,
        'header_row': header_row,
        'first_col_keys_sample': first_col_keys[:8],
    }
    payload['fingerprint'] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    return payload


def _set_pos(hwp: Any, list_id: int, para: int, pos: int) -> None:
    if hasattr(hwp, 'set_pos'):
        hwp.set_pos(list_id, para, pos)
        return
    if hasattr(hwp, 'SetPos'):
        hwp.SetPos(list_id, para, pos)
        return
    raise EditOperationError('pyhwpx set_pos is unavailable on this machine')


def _select_text(hwp: Any, selected_range: tuple[Any, ...]) -> None:
    if hasattr(hwp, 'select_text'):
        hwp.select_text(selected_range)
        return
    raise EditOperationError('pyhwpx select_text is unavailable on this machine')


def _normalize_cursor_pos(value: Any, *, field_name: str = 'cursor_pos') -> tuple[int, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise EditOperationError(f"{field_name} must be a 3-item [list, para, pos] sequence when provided")
    normalized: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise EditOperationError(f"{field_name} entries must be integers")
        normalized.append(int(item))
    return (normalized[0], normalized[1], normalized[2])


def _cursor_pos_from_selected_range(selected_range: Any) -> tuple[int, int, int] | None:
    if not isinstance(selected_range, (list, tuple)) or len(selected_range) < 4:
        return None
    if not selected_range or not selected_range[0]:
        return None
    try:
        return (int(selected_range[1]), int(selected_range[2]), int(selected_range[3]))
    except Exception:
        return None


def _normalize_cursor_range(
    start_value: Any,
    end_value: Any,
    *,
    start_field_name: str = 'start_cursor_pos',
    end_field_name: str = 'end_cursor_pos',
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    start_cursor_pos = _normalize_cursor_pos(start_value, field_name=start_field_name)
    end_cursor_pos = _normalize_cursor_pos(end_value, field_name=end_field_name)
    if start_cursor_pos is None or end_cursor_pos is None:
        raise EditOperationError(
            f'{start_field_name} and {end_field_name} must both be provided as 3-item [list, para, pos] sequences'
        )
    return start_cursor_pos, end_cursor_pos


def _select_cursor_range(
    hwp: Any,
    *,
    start_cursor_pos: tuple[int, int, int],
    end_cursor_pos: tuple[int, int, int],
) -> tuple[Any, ...]:
    _set_pos(hwp, start_cursor_pos[0], start_cursor_pos[1], start_cursor_pos[2])
    _select_text(
        hwp,
        (
            True,
            int(start_cursor_pos[0]),
            int(start_cursor_pos[1]),
            int(start_cursor_pos[2]),
            int(end_cursor_pos[0]),
            int(end_cursor_pos[1]),
            int(end_cursor_pos[2]),
        ),
    )
    return _get_selected_pos(hwp)


def _select_whole_paragraph_for_current_selection(hwp: Any) -> tuple[Any, ...]:
    selected = _get_selected_pos(hwp)
    if not selected or not selected[0]:
        current_pos = _get_pos(hwp)
        if len(current_pos) >= 3:
            try:
                list_id, para, _pos = int(current_pos[0]), int(current_pos[1]), int(current_pos[2])
                if hasattr(hwp, 'select_text'):
                    hwp.select_text(para, 0, para, -1, list_id)
                else:
                    _select_text(hwp, (True, list_id, para, 0, list_id, para, -1))
                selected = _get_selected_pos(hwp)
            except Exception:
                selected = _get_selected_pos(hwp)
    if not selected or not selected[0]:
        raise EditOperationError('expected selected text after find() before paragraph replace')
    _, slist, spara, _spos, elist, epara, _epos = selected
    if slist == elist:
        if hasattr(hwp, 'select_text'):
            hwp.select_text(spara, 0, epara, -1, slist)
        else:
            raise EditOperationError('pyhwpx select_text is unavailable on this machine')
    else:
        _select_text(hwp, (True, slist, spara, 0, elist, epara, -1))
    return _get_selected_pos(hwp)


def _select_paragraph_with_trailing_break_for_current_selection(hwp: Any) -> tuple[Any, ...]:
    selected = _get_selected_pos(hwp)
    if not selected or not selected[0]:
        raise EditOperationError('expected selected text after find() before paragraph replace')
    _, slist, spara, _spos, elist, epara, _epos = selected
    if slist != elist or spara != epara:
        return _select_whole_paragraph_for_current_selection(hwp)

    _set_pos(hwp, int(slist), int(spara), 0)
    if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
        hwp.HAction.Run('MoveSelNextParaBegin')
        moved = _get_selected_pos(hwp)
        if moved and moved[0]:
            return moved

    return _select_whole_paragraph_for_current_selection(hwp)


def _move_to_table_identity(
    hwp: Any,
    *,
    cursor_pos: tuple[int, int, int] | None,
    expected_cell_addr: str | None,
    expected_table_fingerprint: dict[str, Any] | None = None,
    require_in_cell: bool,
) -> dict[str, Any]:
    if cursor_pos is not None:
        _set_pos(hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])
    snapshot = _snapshot_cursor_context(hwp)

    def _recover_expected_cell(current_snapshot: dict[str, Any]) -> dict[str, Any]:
        if expected_cell_addr is None:
            return current_snapshot
        if current_snapshot.get('is_cell') is not True:
            return current_snapshot
        if current_snapshot.get('cell_addr') == expected_cell_addr:
            return current_snapshot

        fill_addr_field = getattr(hwp, 'fill_addr_field', None)
        if callable(fill_addr_field):
            try:
                fill_addr_field()
            except Exception:
                pass
            current_snapshot = _snapshot_cursor_context(hwp)
            if current_snapshot.get('cell_addr') == expected_cell_addr:
                return current_snapshot

        try:
            moved = _move_to_field(hwp, expected_cell_addr)
        except Exception:
            moved = False
        if moved:
            current_snapshot = _snapshot_cursor_context(hwp)
            if current_snapshot.get('cell_addr') == expected_cell_addr:
                return current_snapshot

        target_ref = _cell_addr_to_ref(expected_cell_addr)
        current_ref = _cell_addr_to_ref(current_snapshot.get('cell_addr'))
        for _ in range(16):
            if current_snapshot.get('is_cell') is not True or current_ref is None or target_ref is None:
                break
            if current_snapshot.get('cell_addr') == expected_cell_addr:
                break
            current_row = int(current_ref['row_1based'])
            current_col = int(current_ref['col_1based'])
            target_row = int(target_ref['row_1based'])
            target_col = int(target_ref['col_1based'])
            if current_row > target_row:
                action = 'up'
            elif current_row < target_row:
                action = 'down'
            elif current_col > target_col:
                action = 'left'
            else:
                action = 'right'
            _run_table_cell_action(hwp, action)
            current_snapshot = _snapshot_cursor_context(hwp)
            current_ref = _cell_addr_to_ref(current_snapshot.get('cell_addr'))
        return current_snapshot

    if isinstance(expected_table_fingerprint, dict):
        current_signature = _capture_current_table_signature(hwp)
        current_field_list_signature = None
        current_single_cell_text_match = False
        if snapshot.get('is_cell') is True and not _table_signature_matches(expected_table_fingerprint, current_signature):
            fill_addr_field = getattr(hwp, 'fill_addr_field', None)
            get_field_list = getattr(hwp, 'get_field_list', None)
            if callable(fill_addr_field) and callable(get_field_list):
                try:
                    # A live find() can land on the correct shared single-cell A1 table even when
                    # table_to_df() reports an empty 0x1 shell. Before abandoning the current hit,
                    # rebuild the table fingerprint from field-address navigation at the current
                    # location; otherwise the fallback scan can miss the target entirely.
                    fill_addr_field()
                    field_list = get_field_list(1, 1)
                    current_field_list_signature = _live_table_fingerprint_from_field_list(hwp, field_list)
                    if expected_cell_addr is not None:
                        _move_to_field(hwp, expected_cell_addr)
                    snapshot = _snapshot_cursor_context(hwp)
                except Exception:
                    current_field_list_signature = None
            if not _table_signature_matches(expected_table_fingerprint, current_field_list_signature):
                current_single_cell_text_match = _current_single_cell_text_matches_expected_table(
                    hwp,
                    expected_table_fingerprint=expected_table_fingerprint,
                    expected_cell_addr=expected_cell_addr,
                )
        if snapshot.get('is_cell') is not True or not (
            _table_signature_matches(expected_table_fingerprint, current_signature)
            or _table_signature_matches(expected_table_fingerprint, current_field_list_signature)
            or current_single_cell_text_match
        ):
            scanned = _scan_tables_for_fingerprint(
                hwp,
                expected_table_fingerprint=expected_table_fingerprint,
                expected_cell_addr=expected_cell_addr,
            )
            if scanned.get('match') is None:
                raise EditOperationError(
                    'table identity resolution could not lock the expected table fingerprint; '
                    f'cursor_pos={cursor_pos}, expected_cell_addr={expected_cell_addr!r}, '
                    f'current_snapshot={snapshot}, current_table_signature={current_signature}, '
                    f'current_field_list_signature={current_field_list_signature}, '
                    f'table_probes={scanned.get("probes")}, '
                    f'table_match_candidates={scanned.get("matching_candidates")}'
                )
            snapshot = dict(scanned['match']['snapshot'])
            snapshot['table_probe_candidate'] = scanned['match']['candidate']
            snapshot['table_signature'] = scanned['match']['table_signature']
            snapshot['table_probes'] = scanned.get('probes') or []
            snapshot['table_match_candidates'] = scanned.get('matching_candidates') or []
            snapshot['table_match_candidate_count'] = len(snapshot['table_match_candidates'])
            if snapshot['table_match_candidate_count'] > 1:
                snapshot['table_match_selection'] = {
                    'selected_candidate': scanned['match']['candidate'],
                    'reason': 'first_exact_table_fingerprint_match',
                }
        elif current_field_list_signature is not None and _table_signature_matches(expected_table_fingerprint, current_field_list_signature):
            snapshot = dict(snapshot)
            snapshot['table_signature'] = current_field_list_signature
        elif current_signature is not None:
            snapshot = dict(snapshot)
            snapshot['table_signature'] = current_signature
        snapshot = _recover_expected_cell(snapshot)
        if 'table_signature' not in snapshot:
            current_signature = _capture_current_table_signature(hwp)
            if current_signature is not None:
                snapshot = dict(snapshot)
                snapshot['table_signature'] = current_signature
    if require_in_cell and snapshot.get('is_cell') is not True:
        raise EditOperationError(
            f'table identity resolution requires cursor inside a table cell; current snapshot={snapshot}'
        )
    if expected_cell_addr is not None:
        actual_cell_addr = snapshot.get('cell_addr')
        if actual_cell_addr != expected_cell_addr:
            raise EditOperationError(
                f'table identity resolution expected cell_addr={expected_cell_addr!r} but got {actual_cell_addr!r}; snapshot={snapshot}'
            )
    return snapshot


def _get_ctrl_anchor_pos(hwp: Any, ctrl: Any, *, option: int = 1) -> tuple[int, int, int] | None:
    get_ctrl_pos = getattr(hwp, 'get_ctrl_pos', None)
    if callable(get_ctrl_pos):
        try:
            value = get_ctrl_pos(ctrl, option=option)
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                return (int(value[0]), int(value[1]), int(value[2]))
        except Exception:
            pass

    get_anchor_pos = getattr(ctrl, 'GetAnchorPos', None)
    if callable(get_anchor_pos):
        try:
            anchor_pos = get_anchor_pos(option)
            item = getattr(anchor_pos, 'Item', None)
            if callable(item):
                return (
                    int(item('List')),
                    int(item('Para')),
                    int(item('Pos')),
                )
        except Exception:
            return None
    return None


def _anchor_pos_matches_cursor(anchor_pos: tuple[int, int, int] | None, cursor_pos: tuple[int, int, int] | None) -> bool:
    if anchor_pos is None or cursor_pos is None:
        return False
    try:
        return int(anchor_pos[0]) == int(cursor_pos[0]) and int(anchor_pos[1]) == int(cursor_pos[1])
    except Exception:
        return False


def _enumerate_controls_headctrl(hwp: Any, *, max_controls: int = 2048) -> tuple[list[Any], str]:
    head_ctrl = getattr(hwp, 'HeadCtrl', None)
    if callable(head_ctrl):
        head_ctrl = head_ctrl()
    if head_ctrl is not None:
        controls: list[Any] = []
        ctrl = head_ctrl
        for _ in range(max_controls):
            if ctrl is None:
                break
            controls.append(ctrl)
            ctrl = getattr(ctrl, 'Next', None)
        else:
            raise EditOperationError(f'control enumeration exceeded max_controls={max_controls}')
        return controls, 'HeadCtrl->Next'

    ctrl_list = getattr(hwp, 'ctrl_list', None)
    if ctrl_list is not None:
        try:
            return list(ctrl_list or []), 'ctrl_list'
        except Exception as exc:
            raise EditOperationError(f'control enumeration via ctrl_list failed: {exc}') from exc

    raise EditOperationError('control enumeration is unavailable on this machine; HeadCtrl and ctrl_list are both missing')


def _inventory_controls_at_cell(
    hwp: Any,
    *,
    expected_cell_addr: str | None,
    target_ctrl_id: str | None = None,
    expected_anchor_pos: tuple[int, int, int] | None = None,
    anchor_option: int = 1,
    max_controls: int = 2048,
) -> tuple[dict[str, Any], list[Any]]:
    original_pos = None
    try:
        original_pos = _get_pos(hwp)
    except Exception:
        original_pos = None

    controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
    in_cell_controls: list[dict[str, Any]] = []
    anchored_controls: list[dict[str, Any]] = []
    matching_ctrls: list[Any] = []
    matching_target_controls: list[dict[str, Any]] = []
    matching_target_controls_anywhere: list[dict[str, Any]] = []

    try:
        for index, ctrl in enumerate(controls):
            ctrl_id = None
            try:
                raw_ctrl_id = getattr(ctrl, 'CtrlID', None)
                ctrl_id = str(raw_ctrl_id) if raw_ctrl_id is not None else None
            except Exception:
                ctrl_id = None

            user_desc = None
            try:
                raw_user_desc = getattr(ctrl, 'UserDesc', None)
                user_desc = str(raw_user_desc) if raw_user_desc is not None else None
            except Exception:
                user_desc = None

            anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=anchor_option)
            if anchor_pos is None:
                continue

            _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
            anchor_snapshot = _snapshot_cursor_context(hwp)
            anchor_matches_cursor = _anchor_pos_matches_cursor(anchor_pos, expected_anchor_pos)

            ctrl_inst_id = None
            try:
                raw_ctrl_inst_id = getattr(ctrl, 'CtrlInstID', None)
                ctrl_inst_id = str(raw_ctrl_inst_id) if raw_ctrl_inst_id is not None else None
            except Exception:
                ctrl_inst_id = None

            item = {
                'index': index,
                'ctrl_id': ctrl_id,
                'ctrl_inst_id': ctrl_inst_id,
                'user_desc': user_desc,
                'anchor_pos': [anchor_pos[0], anchor_pos[1], anchor_pos[2]],
                'anchor_matches_expected_cursor': anchor_matches_cursor,
                'anchor_snapshot': {
                    'pos': anchor_snapshot.get('pos'),
                    'cell_addr': anchor_snapshot.get('cell_addr'),
                    'field_name': anchor_snapshot.get('field_name'),
                    'selection_mode': anchor_snapshot.get('selection_mode'),
                    'is_cell': anchor_snapshot.get('is_cell'),
                },
            }
            if expected_cell_addr is None or anchor_snapshot.get('cell_addr') == expected_cell_addr:
                in_cell_controls.append(item)
            if anchor_matches_cursor:
                anchored_controls.append(item)
            if target_ctrl_id is not None and ctrl_id == target_ctrl_id:
                matching_target_controls_anywhere.append(item)
                if expected_anchor_pos is not None:
                    if anchor_matches_cursor:
                        matching_ctrls.append(ctrl)
                        matching_target_controls.append(item)
                elif expected_cell_addr is None or anchor_snapshot.get('cell_addr') == expected_cell_addr:
                    matching_ctrls.append(ctrl)
                    matching_target_controls.append(item)
    finally:
        if original_pos is not None and len(original_pos) >= 3:
            try:
                _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
            except Exception:
                pass

    report = {
        'enumeration_mode': enumeration_mode,
        'control_count_total': len(controls),
        'expected_cell_addr': expected_cell_addr,
        'expected_anchor_pos': list(expected_anchor_pos) if expected_anchor_pos is not None else None,
        'controls_in_expected_cell': in_cell_controls,
        'controls_in_expected_cell_count': len(in_cell_controls),
        'controls_at_expected_anchor': anchored_controls,
        'controls_at_expected_anchor_count': len(anchored_controls),
        'target_ctrl_id': target_ctrl_id,
        'matching_target_controls': matching_target_controls,
        'matching_target_count': len(matching_target_controls),
        'matching_target_controls_anywhere': matching_target_controls_anywhere,
        'matching_target_count_anywhere': len(matching_target_controls_anywhere),
    }
    report['inventory_exists'] = bool(report['control_count_total'])
    return report, matching_ctrls


def _delete_ctrl(hwp: Any, ctrl: Any) -> bool:
    delete_ctrl = getattr(hwp, 'DeleteCtrl', None)
    if callable(delete_ctrl):
        try:
            result = delete_ctrl(ctrl)
            return True if result is None else bool(result)
        except TypeError:
            pass
        except Exception as exc:
            raise EditOperationError(f'DeleteCtrl failed: {exc}') from exc

    underlying = getattr(hwp, 'hwp', None)
    delete_ctrl = getattr(underlying, 'DeleteCtrl', None)
    if callable(delete_ctrl):
        try:
            raw_ctrl = getattr(ctrl, '_com_obj', ctrl)
            result = delete_ctrl(ctrl=raw_ctrl)
            return True if result is None else bool(result)
        except Exception as exc:
            raise EditOperationError(f'underlying DeleteCtrl failed: {exc}') from exc

    raise EditOperationError('DeleteCtrl is unavailable on this machine')


def _apply_control_delete_by_anchor(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    target_ctrl_id = _require_text(op, 'target_ctrl_id')
    expected_cell_addr = op.get('expected_cell_addr')
    if expected_cell_addr is not None and (not isinstance(expected_cell_addr, str) or not expected_cell_addr):
        raise EditOperationError("control_delete_by_anchor field 'expected_cell_addr' must be a non-empty string when provided")
    anchor_option = op.get('anchor_option', 1)
    if isinstance(anchor_option, bool) or not isinstance(anchor_option, int):
        raise EditOperationError("control_delete_by_anchor field 'anchor_option' must be an integer")
    max_controls = op.get('max_controls', 2048)
    if isinstance(max_controls, bool) or not isinstance(max_controls, int) or max_controls <= 0:
        raise EditOperationError("control_delete_by_anchor field 'max_controls' must be a positive integer")
    matching_target_ordinal_anywhere = op.get('matching_target_ordinal_anywhere')
    if matching_target_ordinal_anywhere is not None and (
        isinstance(matching_target_ordinal_anywhere, bool)
        or not isinstance(matching_target_ordinal_anywhere, int)
        or matching_target_ordinal_anywhere <= 0
    ):
        raise EditOperationError(
            "control_delete_by_anchor field 'matching_target_ordinal_anywhere' must be a positive integer when provided"
        )
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    require_in_cell = _bool(op, 'require_in_cell', True)
    transitions: list[dict[str, Any]] = []

    def _perform() -> None:
        if cursor_pos is not None:
            before = _move_to_table_identity(
                hwp,
                cursor_pos=cursor_pos,
                expected_cell_addr=expected_cell_addr,
                require_in_cell=require_in_cell,
            )
        else:
            before = _snapshot_cursor_context(hwp)
            if expected_cell_addr is not None and before.get('cell_addr') != expected_cell_addr:
                raise EditOperationError(
                    f"control_delete_by_anchor expected cell_addr={expected_cell_addr!r} before delete but got {before.get('cell_addr')!r}; snapshot={before}"
                )
        if require_in_cell and before.get('is_cell') is not True:
            raise EditOperationError(
                'control_delete_by_anchor requires cursor inside a table cell before delete; '
                f'current snapshot={before}'
            )

        expected_anchor_pos = _selection_anchor_pos(before)
        if expected_anchor_pos is None and isinstance(before.get('pos'), list) and len(before['pos']) >= 3:
            expected_anchor_pos = (int(before['pos'][0]), int(before['pos'][1]), int(before['pos'][2]))
        pre_inventory, matching_ctrls = _inventory_controls_at_cell(
            hwp,
            expected_cell_addr=expected_cell_addr,
            target_ctrl_id=target_ctrl_id,
            expected_anchor_pos=expected_anchor_pos,
            anchor_option=anchor_option,
            max_controls=max_controls,
        )

        target_ctrl = matching_ctrls[0] if matching_ctrls else None
        matched_via = 'anchor' if target_ctrl is not None else None
        target_anchor_pos_for_post = expected_anchor_pos
        ordinal_fallback_item: dict[str, Any] | None = None
        if target_ctrl is None and matching_target_ordinal_anywhere is not None:
            anywhere = pre_inventory.get('matching_target_controls_anywhere') or []
            ordinal_index = matching_target_ordinal_anywhere - 1
            if ordinal_index < len(anywhere):
                ordinal_fallback_item = anywhere[ordinal_index]
                controls, _ = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
                inventory_index = ordinal_fallback_item.get('index')
                if isinstance(inventory_index, int) and 0 <= inventory_index < len(controls):
                    candidate_ctrl = controls[inventory_index]
                    if str(getattr(candidate_ctrl, 'CtrlID', None)) == target_ctrl_id:
                        target_ctrl = candidate_ctrl
                        matched_via = 'ordinal_anywhere'
                        raw_anchor = ordinal_fallback_item.get('anchor_pos')
                        if isinstance(raw_anchor, list) and len(raw_anchor) >= 3:
                            try:
                                target_anchor_pos_for_post = (int(raw_anchor[0]), int(raw_anchor[1]), int(raw_anchor[2]))
                            except Exception:
                                target_anchor_pos_for_post = expected_anchor_pos
        if target_ctrl is None:
            raise EditOperationError(
                f'control_delete_by_anchor did not find target_ctrl_id={target_ctrl_id!r} '
                f'at expected_cell_addr={expected_cell_addr!r}; inventory={pre_inventory}'
            )

        target_anchor_pos = _get_ctrl_anchor_pos(hwp, target_ctrl, option=anchor_option)
        if target_anchor_pos is not None:
            _set_pos(hwp, target_anchor_pos[0], target_anchor_pos[1], target_anchor_pos[2])
            if matched_via == 'ordinal_anywhere':
                target_anchor_pos_for_post = target_anchor_pos
        delete_succeeded = _delete_ctrl(hwp, target_ctrl)
        after_delete = _snapshot_cursor_context(hwp)

        post_inventory, _remaining = _inventory_controls_at_cell(
            hwp,
            expected_cell_addr=expected_cell_addr,
            target_ctrl_id=target_ctrl_id,
            expected_anchor_pos=target_anchor_pos_for_post,
            anchor_option=anchor_option,
            max_controls=max_controls,
        )
        if post_inventory.get('matching_target_count'):
            raise EditOperationError(
                f'control_delete_by_anchor target_ctrl_id={target_ctrl_id!r} still exists after delete; '
                f'post_inventory={post_inventory}'
            )

        transitions.append(
            {
                'before': before,
                'pre_delete_inventory': pre_inventory,
                'control_found': target_ctrl is not None,
                'matched_via': matched_via,
                'matching_target_ordinal_anywhere': matching_target_ordinal_anywhere,
                'ordinal_fallback_item': ordinal_fallback_item,
                'delete_succeeded': bool(delete_succeeded),
                'after_delete': after_delete,
                'post_delete_inventory': post_inventory,
                'post_delete_zero_match': post_inventory.get('matching_target_count', 0) == 0,
                'target_ctrl_id': target_ctrl_id,
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
                'target_anchor_pos_for_post': list(target_anchor_pos_for_post) if target_anchor_pos_for_post is not None else None,
            }
        )

    if cursor_pos is not None:
        _perform()
        count = 1
    elif isinstance(target, str) and target:
        count = _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_perform)
    else:
        _perform()
        count = 1

    return {'matches': count, 'target_ctrl_id': target_ctrl_id, 'transitions': transitions}


def _normalize_table_text_value(value: Any) -> str:
    return ' '.join(str(value or '').split())


def _tokenize_overlap_text(text: str) -> set[str]:
    return {token for token in re.split(r'[^0-9A-Za-z가-힣]+', _normalize_table_text_value(text)) if token}


def _selected_text_matches_expected(*, selected_text: str, expected_text: str) -> bool:
    selected_compact = _normalize_table_text_value(selected_text)
    expected_compact = _normalize_table_text_value(expected_text)
    if not selected_compact or not expected_compact:
        return False
    if selected_compact in expected_compact or expected_compact in selected_compact:
        return True
    selected_tokens = _tokenize_overlap_text(selected_compact)
    expected_tokens = _tokenize_overlap_text(expected_compact)
    if not selected_tokens or not expected_tokens:
        return False
    overlap = len(selected_tokens & expected_tokens)
    shared_ratio = overlap / max(1, min(len(selected_tokens), len(expected_tokens)))
    return overlap >= 2 or shared_ratio >= 0.35


def _current_single_cell_text_matches_expected_table(
    hwp: Any,
    *,
    expected_table_fingerprint: dict[str, Any] | None,
    expected_cell_addr: str | None,
) -> bool:
    if not isinstance(expected_table_fingerprint, dict):
        return False
    if int(expected_table_fingerprint.get('row_count', 0) or 0) != 1:
        return False
    if int(expected_table_fingerprint.get('col_count', 0) or 0) != 1:
        return False

    expected_header_row = expected_table_fingerprint.get('header_row') or []
    if not isinstance(expected_header_row, list) or len(expected_header_row) != 1:
        return False
    expected_text = _normalize_visible_text(expected_header_row[0])
    if not expected_text:
        return False

    snapshot = _snapshot_cursor_context(hwp)
    if snapshot.get('is_cell') is not True:
        return False
    if expected_cell_addr is not None and snapshot.get('cell_addr') != expected_cell_addr:
        return False

    try:
        _select_current_cell_contents(hwp, expected_cell_addr=expected_cell_addr)
        selected_text = _normalize_visible_text(_get_selected_text(hwp, keep_select=True))
    except Exception:
        return False
    if not selected_text:
        return False
    return _selected_text_matches_expected(selected_text=selected_text, expected_text=expected_text)


def _capture_current_table_signature(hwp: Any) -> dict[str, Any] | None:
    table_to_df = getattr(hwp, 'table_to_df', None)
    if not callable(table_to_df):
        return None
    try:
        df = table_to_df()
    except Exception:
        return None
    try:
        filled = df.fillna('').astype(str)
        rows = [
            [_normalize_table_text_value(cell) for cell in row]
            for row in filled.values.tolist()
        ]
        shape = list(getattr(filled, 'shape', (len(rows), len(rows[0]) if rows else 0)))
    except Exception:
        return None
    row_count = int(shape[0]) if len(shape) >= 1 else len(rows)
    col_count = int(shape[1]) if len(shape) >= 2 else (len(rows[0]) if rows else 0)
    return {
        'row_count': row_count,
        'col_count': col_count,
        'header_row': rows[0] if rows else [],
    }


def _table_signature_matches(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    expected_row_count = expected.get('row_count')
    expected_col_count = expected.get('col_count')
    if isinstance(expected_row_count, int) and actual.get('row_count') != expected_row_count:
        return False
    if isinstance(expected_col_count, int) and actual.get('col_count') != expected_col_count:
        return False
    expected_header_row = expected.get('header_row')
    if isinstance(expected_header_row, list) and expected_header_row:
        normalized_expected = [_normalize_table_text_value(cell) for cell in expected_header_row]
        normalized_actual = [_normalize_table_text_value(cell) for cell in (actual.get('header_row') or [])]
        if normalized_actual != normalized_expected:
            return False
    return True


def _scan_tables_for_fingerprint(
    hwp: Any,
    *,
    expected_table_fingerprint: dict[str, Any] | None,
    expected_cell_addr: str | None,
    max_tables: int = 64,
) -> dict[str, Any]:
    if not isinstance(expected_table_fingerprint, dict):
        return {'match': None, 'probes': []}
    expected_header_row = expected_table_fingerprint.get('header_row')
    has_informative_fingerprint = bool(
        (isinstance(expected_header_row, list) and expected_header_row)
        or isinstance(expected_table_fingerprint.get('row_count'), int)
        or isinstance(expected_table_fingerprint.get('col_count'), int)
    )
    get_into_nth_table = getattr(hwp, 'get_into_nth_table', None)
    if not callable(get_into_nth_table):
        return {'match': None, 'probes': []}
    move_doc_begin = getattr(hwp, 'MoveDocBegin', None)
    probes: list[dict[str, Any]] = []
    enterable_candidates: list[dict[str, Any]] = []
    matching_candidates: list[dict[str, Any]] = []
    for candidate in range(max_tables):
        try:
            if callable(move_doc_begin):
                move_doc_begin()
            get_into_nth_table(candidate)
            snapshot = _snapshot_cursor_context(hwp)
            actual_signature = _capture_current_table_signature(hwp)
            field_list_signature = None
            single_cell_text_match = False
            field_list_signature_error = None
            if snapshot.get('is_cell') is True and (
                actual_signature is None
                or not _table_signature_matches(expected_table_fingerprint, actual_signature)
            ):
                fill_addr_field = getattr(hwp, 'fill_addr_field', None)
                get_field_list = getattr(hwp, 'get_field_list', None)
                if callable(fill_addr_field) and callable(get_field_list):
                    try:
                        # Reopened proof handles can expose field-address navigation even when
                        # table_to_df() cannot reconstruct the visible cell text. Fall back to a
                        # field-list-derived signature so shared single-cell A1 tables can still be
                        # re-entered by semantic table identity after serialization.
                        fill_addr_field()
                        field_list = get_field_list(1, 1)
                        field_list_signature = _live_table_fingerprint_from_field_list(hwp, field_list)
                        if expected_cell_addr is not None:
                            _move_to_field(hwp, expected_cell_addr)
                            snapshot = _snapshot_cursor_context(hwp)
                    except Exception as exc:
                        field_list_signature_error = str(exc)
                if not _table_signature_matches(expected_table_fingerprint, field_list_signature):
                    single_cell_text_match = _current_single_cell_text_matches_expected_table(
                        hwp,
                        expected_table_fingerprint=expected_table_fingerprint,
                        expected_cell_addr=expected_cell_addr,
                    )
            matched_signature = actual_signature
            if _table_signature_matches(expected_table_fingerprint, field_list_signature):
                matched_signature = field_list_signature
            elif single_cell_text_match:
                matched_signature = dict(expected_table_fingerprint)
            probe = {
                'candidate': candidate,
                'is_cell': snapshot.get('is_cell'),
                'cell_addr': snapshot.get('cell_addr'),
                'row_count': matched_signature.get('row_count') if isinstance(matched_signature, dict) else None,
                'col_count': matched_signature.get('col_count') if isinstance(matched_signature, dict) else None,
                'header_preview': None,
            }
            if field_list_signature_error is not None:
                probe['field_list_signature_error'] = field_list_signature_error
            if isinstance(field_list_signature, dict):
                probe['field_list_signature'] = field_list_signature
            if isinstance(matched_signature, dict):
                header_row = matched_signature.get('header_row') or []
                if isinstance(header_row, list) and header_row:
                    compact_header = ' | '.join(_normalize_table_text_value(cell) for cell in header_row)
                    probe['header_preview'] = compact_header[:180] + ('...' if len(compact_header) > 180 else '')
            if len(probes) < 16:
                probes.append(probe)
            if snapshot.get('is_cell') is not True:
                continue
            enterable_candidates.append(
                {
                    'candidate': candidate,
                    'snapshot': snapshot,
                    'table_signature': matched_signature,
                }
            )
            if _table_signature_matches(expected_table_fingerprint, matched_signature):
                if expected_cell_addr is not None and snapshot.get('cell_addr') != expected_cell_addr:
                    snapshot = dict(snapshot)
                    snapshot['expected_cell_addr'] = expected_cell_addr
                    snapshot['cell_addr_hint_mismatch'] = True
                matching_candidates.append(
                    {
                        'candidate': candidate,
                        'snapshot': snapshot,
                        'table_signature': matched_signature,
                    }
                )
        except Exception as exc:
            if len(probes) < 16:
                probes.append({'candidate': candidate, 'error': str(exc)})
            continue
    if matching_candidates:
        return {
            'match': matching_candidates[0],
            'probes': probes,
            'matching_candidates': matching_candidates,
        }
    if len(enterable_candidates) == 1:
        heuristic_match = dict(enterable_candidates[0])
        heuristic_snapshot = dict(heuristic_match['snapshot'])
        if not has_informative_fingerprint:
            heuristic_snapshot['heuristic_only_enterable_table'] = True
            heuristic_match['snapshot'] = heuristic_snapshot
            return {'match': heuristic_match, 'probes': probes, 'matching_candidates': []}
        if expected_cell_addr is not None and heuristic_snapshot.get('cell_addr') == expected_cell_addr:
            heuristic_snapshot['heuristic_only_enterable_table_with_expected_cell_addr'] = True
            heuristic_match['snapshot'] = heuristic_snapshot
            return {'match': heuristic_match, 'probes': probes, 'matching_candidates': []}
    return {'match': None, 'probes': probes, 'matching_candidates': []}


def _get_current_paragraph_text(hwp: Any) -> str:
    original_selection = _get_selected_pos(hwp)
    paragraph_selection = _select_whole_paragraph_for_current_selection(hwp)
    try:
        return _get_selected_text(hwp, keep_select=True)
    finally:
        if original_selection and original_selection[0]:
            _select_text(hwp, original_selection)
        elif paragraph_selection and paragraph_selection[0]:
            _select_text(hwp, paragraph_selection)


def _get_current_paragraph_text_at_cursor(hwp: Any) -> str:
    pos = _get_pos(hwp)
    if len(pos) < 3:
        raise EditOperationError(f'unexpected cursor position shape: {pos!r}')
    list_id, para, cursor_pos = int(pos[0]), int(pos[1]), int(pos[2])
    if hasattr(hwp, 'select_text'):
        hwp.select_text(para, 0, para, -1, list_id)
    else:
        raise EditOperationError('pyhwpx select_text is unavailable on this machine')
    try:
        return _get_selected_text(hwp, keep_select=False)
    finally:
        _set_pos(hwp, list_id, para, cursor_pos)


def _get_document_text(hwp: Any) -> str:
    if hasattr(hwp, 'get_text_file'):
        return str(hwp.get_text_file(option='') or '')
    if hasattr(hwp, 'GetTextFile'):
        return str(hwp.GetTextFile('UNICODE', '') or '')
    raise EditOperationError('pyhwpx get_text_file/GetTextFile is unavailable on this machine')


def _get_paragraph_text_by_position(hwp: Any, *, list_id: int, para: int, restore_pos: tuple[int, int, int] | None = None) -> str | None:
    try:
        if hasattr(hwp, 'select_text'):
            hwp.select_text(para, 0, para, -1, list_id)
        else:
            raise EditOperationError('pyhwpx select_text is unavailable on this machine')
        return _get_selected_text(hwp, keep_select=False)
    except Exception:
        return None
    finally:
        if restore_pos is not None:
            _set_pos(hwp, int(restore_pos[0]), int(restore_pos[1]), int(restore_pos[2]))


def _looks_like_packed_paragraph(text: str) -> bool:
    normalized = text.replace('\r', ' ').replace('\n', ' ').strip()
    if not normalized:
        return False

    bullet_markers = ['■', '●', '•', '◦', '▪', '※', '□', '▶', '▷']
    bullet_hits = sum(normalized.count(marker) for marker in bullet_markers)
    if bullet_hits >= 2:
        return True

    if len(re.findall(r'(?:^|\s)(?:\d+\.|[A-Za-z]\.|[가-하]\.)\s+', normalized)) >= 2:
        return True

    if len(re.findall(r'(?:^|\s)[①-⑳]', normalized)) >= 2:
        return True

    return False


def _guard_against_packed_paragraph(hwp: Any, op: dict[str, Any]) -> None:
    if _bool(op, 'allow_packed_paragraph', False):
        return

    paragraph_text = _get_current_paragraph_text(hwp)
    if _looks_like_packed_paragraph(paragraph_text):
        preview = paragraph_text.replace('\r', ' ').replace('\n', ' ').strip()
        if len(preview) > 160:
            preview = preview[:157] + '...'
        raise EditOperationError(
            'packed paragraph detected around the current match; safe replace is blocked by default. '
            f'Set allow_packed_paragraph=true only after manual validation. Paragraph preview: {preview}'
        )


def _strip_leading_anchor_prefixes(text: str) -> list[str]:
    variants: list[str] = []
    current = str(text or '')
    patterns = [
        r'^\s*[①-⑳]\s*',
        r'^\s*[(\[]?[0-9]+[)\].:-]?\s*',
        r'^\s*[가-하][.)]\s*',
        r'^\s*[A-Za-z][.)]\s*',
        r'^\s*[■●•◦▪※□▶▷]\s*',
    ]

    for _ in range(4):
        updated = current
        for pattern in patterns:
            updated = re.sub(pattern, '', updated, count=1)
        updated = updated.strip()
        if not updated or updated == current:
            break
        variants.append(updated)
        current = updated

    return variants


def _build_find_candidates(target: str) -> list[tuple[str, bool]]:
    compact = ' '.join(target.split())
    candidates: list[tuple[str, bool]] = [(target, True)]
    if compact and compact != target:
        candidates.append((compact, True))
    for stripped in _strip_leading_anchor_prefixes(compact or target):
        candidates.append((stripped, True))
        compact_stripped = ' '.join(stripped.split())
        if compact_stripped and compact_stripped != stripped:
            candidates.append((compact_stripped, True))
        head = re.split(r'[:：\-–,，(\[]', compact_stripped or stripped, maxsplit=1)[0].strip()
        if len(head) >= 4 and head != compact_stripped:
            candidates.append((head, False))
    if len(compact) >= 24:
        for length in (64, 48, 32, 24):
            if len(compact) >= length:
                candidates.append((compact[:length], False))
        if len(compact) >= 24:
            candidates.append((compact[:24], False))

    deduped: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _find_matches(hwp: Any, target: str, *, match_case: bool, whole_word: bool, apply: str, callback) -> int:
    count = 0
    for candidate_text, allow_whole_word in _build_find_candidates(target):
        _move_doc_begin(hwp)
        while hwp.find(
            candidate_text,
            direction='Forward',
            MatchCase=1 if match_case else 0,
            WholeWordOnly=1 if (whole_word and allow_whole_word) else 0,
        ):
            callback()
            count += 1
            _move_after_selection(hwp)
            if apply == 'first':
                return count
        if count > 0:
            break
    return count


def _is_retryable_table_identity_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        message.startswith('table_patch_cells table fingerprint mismatch before apply:')
        or message.startswith('table identity resolution expected cell_addr=')
        or message.startswith('table identity resolution requires cursor inside a table cell;')
    )


def _safe_replace_matches(
    hwp: Any,
    target: str,
    replace_text: str,
    *,
    apply: str,
    match_case: bool,
    whole_word: bool,
    paragraph_mode: bool = False,
    allow_packed_paragraph: bool = False,
    allow_target_in_replace: bool = False,
    expected_present_after: str | None = None,
    expected_absent_after: str | None = None,
) -> int:
    if apply == 'all' and target and target in replace_text and not allow_target_in_replace:
        raise EditOperationError('safe replace with apply=all cannot use replacement text containing the find text')

    count = 0
    for candidate_text, allow_whole_word in _build_find_candidates(target):
        _move_doc_begin(hwp)
        while hwp.find(
            candidate_text,
            direction='Forward',
            MatchCase=1 if match_case else 0,
            WholeWordOnly=1 if (whole_word and allow_whole_word) else 0,
        ):
            _guard_against_packed_paragraph(hwp, {
                'op': 'replace_paragraph_safe' if paragraph_mode else 'replace_text_safe',
                'allow_packed_paragraph': allow_packed_paragraph,
            })
            original_selection = _get_selected_pos(hwp)
            if paragraph_mode:
                working_selection = _select_paragraph_with_trailing_break_for_current_selection(hwp)
            else:
                working_selection = original_selection

            normalized_expected_absent = None
            before_absent_count = None
            normalized_expected_present = None
            before_present_count = None
            if paragraph_mode and expected_present_after is not None:
                normalized_expected_present = ' '.join(expected_present_after.split())
                if normalized_expected_present:
                    before_document_text_for_present = ' '.join(_get_document_text(hwp).split())
                    before_present_count = before_document_text_for_present.count(normalized_expected_present)
            if paragraph_mode and expected_absent_after is not None:
                normalized_expected_absent = ' '.join(expected_absent_after.split())
                if normalized_expected_absent:
                    before_document_text = ' '.join(_get_document_text(hwp).split())
                    before_absent_count = before_document_text.count(normalized_expected_absent)

            _delete_selection(hwp)
            if hasattr(hwp, 'insert_text'):
                hwp.insert_text(replace_text)
            else:
                raise EditOperationError('pyhwpx insert_text is unavailable on this machine')

            if paragraph_mode and (expected_present_after is not None or expected_absent_after is not None):
                current_pos = _get_pos(hwp)
                if len(current_pos) < 3:
                    raise EditOperationError(f'unexpected cursor position shape after replace_paragraph_safe: {current_pos!r}')
                list_id, para, cursor_pos = int(current_pos[0]), int(current_pos[1]), int(current_pos[2])
                current_paragraph_text = _get_current_paragraph_text_at_cursor(hwp)
                next_paragraph_text = _get_paragraph_text_by_position(
                    hwp,
                    list_id=list_id,
                    para=para + 1,
                    restore_pos=(list_id, para, cursor_pos),
                )
                normalized_current = ' '.join(current_paragraph_text.split())
                if expected_present_after is not None:
                    normalized_expected_present = ' '.join(expected_present_after.split())
                    if normalized_expected_present and normalized_expected_present not in normalized_current:
                        after_document_text_for_present = ' '.join(_get_document_text(hwp).split())
                        after_present_count = after_document_text_for_present.count(normalized_expected_present)
                        if before_present_count is None or after_present_count <= before_present_count:
                            raise EditOperationError(
                                'replace_paragraph_safe post-invariant failed: '
                                'expected inserted paragraph text not found after apply; '
                                f'current_paragraph={current_paragraph_text!r}; '
                                f'before_present_count={before_present_count}; after_present_count={after_present_count}'
                            )
                if expected_absent_after is not None:
                    normalized_expected_absent = ' '.join(expected_absent_after.split())
                    normalized_replace_text = ' '.join(replace_text.split())
                    normalized_next = ' '.join((next_paragraph_text or '').split())
                    if (
                        normalized_expected_absent
                        and normalized_expected_absent != normalized_replace_text
                        and (
                            normalized_expected_absent in normalized_current
                            or normalized_expected_absent in normalized_next
                        )
                    ):
                        raise EditOperationError(
                            'replace_paragraph_safe post-invariant failed: '
                            'original paragraph text is still present after apply; '
                            f'current_paragraph={current_paragraph_text!r}; next_paragraph={next_paragraph_text!r}'
                        )
                    if (
                        normalized_expected_absent
                        and normalized_expected_absent != normalized_replace_text
                        and before_absent_count is not None
                    ):
                        after_document_text = ' '.join(_get_document_text(hwp).split())
                        after_absent_count = after_document_text.count(normalized_expected_absent)
                        if after_absent_count >= before_absent_count:
                            raise EditOperationError(
                                'replace_paragraph_safe post-invariant failed: '
                                'document-wide target count did not decrease after apply; '
                                f'before_count={before_absent_count}; after_count={after_absent_count}; '
                                f'expected_absent_after={expected_absent_after!r}'
                            )

            count += 1
            if apply == 'first':
                return count

            if paragraph_mode:
                _, _slist, _spara, _spos, elist, epara, _epos = working_selection
                _set_pos(hwp, int(elist), int(epara), -1)
            else:
                _, _slist, _spara, _spos, elist, epara, epos = original_selection
                next_pos = int(epos) - 1 + len(replace_text)
                if next_pos < 0:
                    next_pos = 0
                _set_pos(hwp, int(elist), int(epara), next_pos)
        if count > 0:
            break

    return count


def _safe_replace_between_anchors(
    hwp: Any,
    start_anchor: str,
    end_anchor: str,
    replace_text: str,
    *,
    apply: str,
    match_case: bool,
    whole_word: bool,
    allow_packed_paragraph: bool = False,
) -> int:
    if start_anchor == end_anchor:
        raise EditOperationError('replace_between_anchors_safe requires different start_anchor and end_anchor values')

    _move_doc_begin(hwp)
    count = 0
    while hwp.find(
        start_anchor,
        direction='Forward',
        MatchCase=1 if match_case else 0,
        WholeWordOnly=1 if whole_word else 0,
    ):
        _guard_against_packed_paragraph(hwp, {
            'op': 'replace_between_anchors_safe',
            'allow_packed_paragraph': allow_packed_paragraph,
        })
        original_selection = _get_selected_pos(hwp)
        working_selection = _select_whole_paragraph_for_current_selection(hwp)
        paragraph_text = _get_selected_text(hwp, keep_select=True)

        start_idx = paragraph_text.find(start_anchor)
        if start_idx < 0:
            raise EditOperationError('start_anchor was matched by find() but not found in selected paragraph text')
        content_start = start_idx + len(start_anchor)
        end_idx = paragraph_text.find(end_anchor, content_start)
        if end_idx < 0:
            raise EditOperationError(
                f'end_anchor {end_anchor!r} was not found after start_anchor {start_anchor!r} in the matched paragraph'
            )

        new_paragraph_text = paragraph_text[:content_start] + replace_text + paragraph_text[end_idx:]

        _delete_selection(hwp)
        if hasattr(hwp, 'insert_text'):
            hwp.insert_text(new_paragraph_text)
        else:
            raise EditOperationError('pyhwpx insert_text is unavailable on this machine')

        count += 1
        if apply == 'first':
            break

        _, _slist, _spara, _spos, elist, epara, _epos = working_selection
        next_pos = end_idx + len(replace_text)
        if next_pos < 0:
            next_pos = 0
        _set_pos(hwp, int(elist), int(epara), next_pos)

    return count


def _safe_replace_paragraph_range(
    hwp: Any,
    start_find: str,
    end_find: str,
    replace_text: str,
    *,
    apply: str,
    match_case: bool,
    whole_word: bool,
    allow_packed_paragraph: bool = False,
) -> int:
    def _recover_current_paragraph_selection(find_text: str, label: str) -> tuple[Any, ...] | None:
        before = _snapshot_cursor_context(hwp)
        selected_before = _get_selected_pos(hwp)
        if selected_before and selected_before[0]:
            return _select_whole_paragraph_for_current_selection(hwp)
        current_pos = _get_pos(hwp)
        if len(current_pos) < 3:
            return None
        current_para_text = _get_current_paragraph_text_at_cursor(hwp)
        if find_text not in current_para_text:
            return None
        _set_pos(hwp, int(current_pos[0]), int(current_pos[1]), 0)
        if hasattr(hwp, 'select_text'):
            hwp.select_text(int(current_pos[1]), 0, int(current_pos[1]), -1, int(current_pos[0]))
        else:
            raise EditOperationError('pyhwpx select_text is unavailable on this machine')
        recovered = _get_selected_pos(hwp)
        if recovered and recovered[0]:
            return recovered
        after = _snapshot_cursor_context(hwp)
        raise EditOperationError(
            f'{label} selection recovery failed after find-hit; '
            f'before={before}, after={after}, current_para_text={current_para_text[:240]!r}'
        )

    _move_doc_begin(hwp)
    count = 0

    while hwp.find(
        start_find,
        direction='Forward',
        MatchCase=1 if match_case else 0,
        WholeWordOnly=1 if whole_word else 0,
    ):
        _guard_against_packed_paragraph(hwp, {
            'op': 'replace_paragraph_range_safe',
            'allow_packed_paragraph': allow_packed_paragraph,
        })
        start_selection = _select_whole_paragraph_for_current_selection(hwp)
        if not start_selection or not start_selection[0]:
            raise EditOperationError('expected selected text after finding range start paragraph')
        _, start_list, start_para, _start_pos, end_list, end_para, _end_pos = start_selection

        _set_pos(hwp, int(end_list), int(end_para), -1)
        if not hwp.find(
            end_find,
            direction='Forward',
            MatchCase=1 if match_case else 0,
            WholeWordOnly=1 if whole_word else 0,
        ):
            raise EditOperationError(f'end paragraph for replace_paragraph_range_safe not found: {end_find!r}')

        try:
            end_selection = _select_whole_paragraph_for_current_selection(hwp)
        except EditOperationError:
            if not cluster_method_body:
                raise
            end_selection = _recover_current_paragraph_selection(end_find, 'method-p112 end')
        if not end_selection or not end_selection[0]:
            raise EditOperationError('expected selected text after finding range end paragraph')
        _, range_start_list, range_start_para, _range_start_pos, range_end_list, range_end_para, _range_end_pos = end_selection

        start_position = (int(start_list), int(start_para))
        end_position = (int(range_end_list), int(range_end_para))
        if end_position < start_position:
            raise EditOperationError('replace_paragraph_range_safe end paragraph resolved before start paragraph')

        if int(start_list) == int(range_end_list):
            if hasattr(hwp, 'select_text'):
                hwp.select_text(int(start_para), 0, int(range_end_para), -1, int(start_list))
            else:
                raise EditOperationError('pyhwpx select_text is unavailable on this machine')
        else:
            _select_text(hwp, (True, int(start_list), int(start_para), 0, int(range_end_list), int(range_end_para), -1))

        _delete_selection(hwp)
        if hasattr(hwp, 'insert_text'):
            hwp.insert_text(replace_text)
        else:
            raise EditOperationError('pyhwpx insert_text is unavailable on this machine')

        count += 1
        if apply == 'first':
            break

        _set_pos(hwp, int(start_list), int(start_para), -1)

    return count


def _safe_insert_relative_to_matches(
    hwp: Any,
    target: str,
    insert_text: str,
    *,
    where: str,
    apply: str,
    match_case: bool,
    whole_word: bool,
    allow_packed_paragraph: bool = False,
) -> int:
    if where not in {'before', 'after'}:
        raise EditOperationError(f'unsupported insert location: {where!r}')

    _move_doc_begin(hwp)
    count = 0
    while hwp.find(
        target,
        direction='Forward',
        MatchCase=1 if match_case else 0,
        WholeWordOnly=1 if whole_word else 0,
    ):
        original_selection = _get_selected_pos(hwp)
        if not original_selection or not original_selection[0]:
            raise EditOperationError('expected selected text after find() before relative insert')

        _, slist, spara, spos, elist, epara, epos = original_selection
        if where == 'before':
            _set_pos(hwp, int(slist), int(spara), int(spos))
        else:
            _set_pos(hwp, int(elist), int(epara), int(epos) - 1)

        if hasattr(hwp, 'insert_text'):
            hwp.insert_text(insert_text)
        else:
            raise EditOperationError('pyhwpx insert_text is unavailable on this machine')

        count += 1
        if apply == 'first':
            break

        next_pos = int(epos) - 1 + len(insert_text)
        if next_pos < 0:
            next_pos = 0
        _set_pos(hwp, int(elist), int(epara), next_pos)

    return count


def _capture_paragraph_shape(hwp: Any, target: str, *, match_case: bool, whole_word: bool) -> dict[str, Any] | None:
    _move_doc_begin(hwp)
    if not _find_with_fallbacks(hwp, target, match_case=match_case, whole_word=whole_word):
        print(f'WARN: clone_paragraph_shape source text not found, skipping clone: {target!r}')
        return None

    _select_whole_paragraph_for_current_selection(hwp)
    if hasattr(hwp, 'get_parashape_as_dict'):
        shape = hwp.get_parashape_as_dict()
        if not isinstance(shape, dict) or not shape:
            raise EditOperationError('pyhwpx get_parashape_as_dict returned an empty result')
        return shape
    raise EditOperationError('pyhwpx get_parashape_as_dict is unavailable on this machine')


def _capture_char_shape(hwp: Any, target: str, *, match_case: bool, whole_word: bool) -> dict[str, Any] | None:
    _move_doc_begin(hwp)
    if not _find_with_fallbacks(hwp, target, match_case=match_case, whole_word=whole_word):
        print(f'WARN: clone_text_style source text not found, skipping clone: {target!r}')
        return None

    if hasattr(hwp, 'get_charshape_as_dict'):
        shape = hwp.get_charshape_as_dict()
        if not isinstance(shape, dict) or not shape:
            raise EditOperationError('pyhwpx get_charshape_as_dict returned an empty result')
        return shape
    raise EditOperationError('pyhwpx get_charshape_as_dict is unavailable on this machine')


def _find_with_fallbacks(hwp: Any, target: str, *, match_case: bool, whole_word: bool) -> bool:
    for text, allow_whole_word in _build_find_candidates(target):
        if not text:
            continue
        _move_doc_begin(hwp)
        if bool(
            hwp.find(
                text,
                direction='Forward',
                MatchCase=1 if match_case else 0,
                WholeWordOnly=1 if (whole_word and allow_whole_word) else 0,
            )
        ):
            return True
    return False


def _apply_clone_text_style(hwp: Any, op: dict[str, Any]) -> int:
    source_find = _require_text(op, 'source_find')
    target = _require_text(op, 'find')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    source_shape = _capture_char_shape(hwp, source_find, match_case=match_case, whole_word=whole_word)
    if not source_shape:
        return 0

    def _callback() -> None:
        if hasattr(hwp, 'set_charshape'):
            hwp.set_charshape(source_shape)
        else:
            raise EditOperationError('pyhwpx set_charshape is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_clone_paragraph_shape(hwp: Any, op: dict[str, Any]) -> int:
    source_find = _require_text(op, 'source_find')
    target = _require_text(op, 'find')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    source_shape = _capture_paragraph_shape(hwp, source_find, match_case=match_case, whole_word=whole_word)
    if not source_shape:
        return 0

    def _callback() -> None:
        _select_whole_paragraph_for_current_selection(hwp)
        if hasattr(hwp, 'set_parashape'):
            hwp.set_parashape(source_shape)
        else:
            raise EditOperationError('pyhwpx set_parashape is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _filter_layout_only_paragraph_shape(source_shape: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        'AlignType',
        'BreakNonLatinWord',
        'LineSpacing',
        'Indentation',
        'LeftMargin',
        'RightMargin',
        'PrevSpacing',
        'NextSpacing',
        'Condense',
        'TextAlignment',
        'PagebreakBefore',
        'KeepLinesTogether',
        'KeepWithNext',
        'WidowOrphan',
        'LineWrap',
        'FontLineHeight',
        'AutoSpaceEAsianNum',
        'AutoSpaceEAsianEng',
        'SnapToGrid',
    }
    return {key: value for key, value in source_shape.items() if key in allowed_fields}


def _apply_clone_paragraph_layout(hwp: Any, op: dict[str, Any]) -> int:
    source_find = _require_text(op, 'source_find')
    target = _require_text(op, 'find')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    source_shape = _capture_paragraph_shape(hwp, source_find, match_case=match_case, whole_word=whole_word)
    if not source_shape:
        return 0
    layout_shape = _filter_layout_only_paragraph_shape(source_shape)
    if not layout_shape:
        return 0

    def _callback() -> None:
        _select_whole_paragraph_for_current_selection(hwp)
        if hasattr(hwp, 'set_parashape'):
            hwp.set_parashape(layout_shape)
        else:
            raise EditOperationError('pyhwpx set_parashape is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _normalize_align(value: str) -> str:
    mapping = {
        'left': 'Left',
        'right': 'Right',
        'center': 'Center',
        'justify': 'Justify',
        'distribute': 'Distribute',
        'distributespace': 'DistributeSpace',
        'distribute_space': 'DistributeSpace',
    }
    key = value.replace(' ', '').replace('-', '_').lower()
    if key not in mapping:
        raise EditOperationError(f'unsupported align value: {value!r}')
    return mapping[key]


def _apply_style_text(hwp: Any, op: dict[str, Any]) -> int:
    target = _require_text(op, 'find')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)

    kwargs = _collect_text_style_kwargs(op)

    if not kwargs:
        raise EditOperationError(
            'style_text requires at least one style field such as face_name, height_pt, bold, italic, underline, text_color_rgb'
        )

    def _callback() -> None:
        if hasattr(hwp, 'set_font'):
            hwp.set_font(**kwargs)
        else:
            raise EditOperationError('pyhwpx set_font is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_style_text_in_paragraph(hwp: Any, op: dict[str, Any]) -> int:
    paragraph_find = _require_text(op, 'paragraph_find')
    target = _require_text(op, 'text')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    occurrence = op.get('occurrence', 1)
    if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1:
        raise EditOperationError("style_text_in_paragraph field 'occurrence' must be a positive integer")

    kwargs = _collect_text_style_kwargs(op)
    if not kwargs:
        raise EditOperationError(
            'style_text_in_paragraph requires at least one style field such as face_name, height_pt, bold, italic, underline, text_color_rgb'
        )

    def _callback() -> None:
        paragraph_selection = _select_whole_paragraph_for_current_selection(hwp)
        paragraph_text = _get_selected_text(hwp, keep_select=True)
        _, slist, spara, _spos, elist, epara, _epos = paragraph_selection
        if int(slist) != int(elist) or int(spara) != int(epara):
            raise EditOperationError(
                'style_text_in_paragraph only supports single-paragraph matches; '
                f'got selection={paragraph_selection!r}'
            )

        start_idx = -1
        search_from = 0
        for _ in range(occurrence):
            start_idx = paragraph_text.find(target, search_from)
            if start_idx < 0:
                raise EditOperationError(
                    f'style_text_in_paragraph could not find target {target!r} occurrence={occurrence} '
                    f'within paragraph {paragraph_text!r}'
                )
            search_from = start_idx + len(target)

        end_idx = start_idx + len(target)
        _select_cursor_range(
            hwp,
            start_cursor_pos=(int(slist), int(spara), int(start_idx)),
            end_cursor_pos=(int(slist), int(spara), int(end_idx)),
        )
        if hasattr(hwp, 'set_font'):
            hwp.set_font(**kwargs)
        else:
            raise EditOperationError('pyhwpx set_font is unavailable on this machine')

    return _find_matches(hwp, paragraph_find, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_align_paragraph(hwp: Any, op: dict[str, Any]) -> int:
    target = _require_text(op, 'find')
    align = _normalize_align(_require_text(op, 'align'))
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)

    def _callback() -> None:
        if hasattr(hwp, 'set_para'):
            hwp.set_para(AlignType=align)
        else:
            raise EditOperationError('pyhwpx set_para is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_paragraph_shape(hwp: Any, op: dict[str, Any]) -> int:
    target = _require_text(op, 'find')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)

    kwargs: dict[str, Any] = {}
    if 'align' in op:
        kwargs['AlignType'] = _normalize_align(_require_text(op, 'align'))

    number_fields = {
        'line_spacing': 'LineSpacing',
        'indentation': 'Indentation',
        'left_margin': 'LeftMargin',
        'right_margin': 'RightMargin',
        'prev_spacing': 'PrevSpacing',
        'next_spacing': 'NextSpacing',
        'condense': 'Condense',
        'text_alignment': 'TextAlignment',
    }
    for src, dest in number_fields.items():
        value = _number(op, src)
        if value is not None:
            kwargs[dest] = int(value) if src in {'line_spacing', 'condense', 'text_alignment'} else float(value)

    bool_fields = {
        'pagebreak_before': 'PagebreakBefore',
        'keep_lines_together': 'KeepLinesTogether',
        'keep_with_next': 'KeepWithNext',
        'widow_orphan': 'WidowOrphan',
        'line_wrap': 'LineWrap',
        'font_line_height': 'FontLineHeight',
        'auto_space_easian_num': 'AutoSpaceEAsianNum',
        'auto_space_easian_eng': 'AutoSpaceEAsianEng',
        'snap_to_grid': 'SnapToGrid',
    }
    for src, dest in bool_fields.items():
        if src in op:
            kwargs[dest] = 1 if bool(op[src]) else 0

    break_non_latin_word = _number(op, 'break_non_latin_word')
    if break_non_latin_word is not None:
        kwargs['BreakNonLatinWord'] = int(break_non_latin_word)

    if not kwargs:
        raise EditOperationError('paragraph_shape requires at least one paragraph option')

    def _callback() -> None:
        if hasattr(hwp, 'set_para'):
            hwp.set_para(**kwargs)
        else:
            raise EditOperationError('pyhwpx set_para is unavailable on this machine')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_native_list_kind_current(hwp: Any, *, kind: str, level: int, source_shape: Any | None = None) -> str:
    normalized_kind = kind.strip().lower()

    def _apply_native_shape_overlay() -> None:
        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)

    if normalized_kind == 'none':
        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)
            return 'source_parashape_none'
        if hasattr(hwp, 'get_parashape') and hasattr(hwp, 'set_parashape') and hasattr(hwp, 'HeadType'):
            pset = hwp.get_parashape()
            try:
                setattr(pset, 'HeadingType', hwp.HeadType('None'))
            except Exception as exc:
                raise EditOperationError(f'list none failed while setting HeadingType: {exc}') from exc
            try:
                hwp.set_parashape(pset)
                return 'heading_type_none'
            except Exception as exc:
                raise EditOperationError(f'list none failed while applying ParaShape: {exc}') from exc
        raise EditOperationError('pyhwpx HeadingType=None path is unavailable on this machine')

    if normalized_kind == 'number':
        if hasattr(hwp, 'PutParaNumber'):
            hwp.PutParaNumber()
            _promote_current_list_level(hwp, level=level)
            _apply_native_shape_overlay()
            return 'PutParaNumber'
        if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
            hwp.HAction.Run('PutParaNumber')
            _promote_current_list_level(hwp, level=level)
            _apply_native_shape_overlay()
            return 'HAction.Run(PutParaNumber)'
        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)
            return 'source_parashape_number'
        raise EditOperationError('pyhwpx PutParaNumber is unavailable on this machine')

    if normalized_kind == 'outline':
        if hasattr(hwp, 'PutOutlinleNumber'):
            hwp.PutOutlinleNumber()
            _promote_current_list_level(hwp, level=level)
            _apply_native_shape_overlay()
            return 'PutOutlinleNumber'
        if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
            hwp.HAction.Run('PutOutlineNumber')
            _promote_current_list_level(hwp, level=level)
            _apply_native_shape_overlay()
            return 'HAction.Run(PutOutlineNumber)'
        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)
            return 'source_parashape_outline'
        raise EditOperationError('pyhwpx PutOutlineNumber is unavailable on this machine')

    if normalized_kind == 'bullet':
        if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
            try:
                if hwp.HAction.Run('PutBullet'):
                    _promote_current_list_level(hwp, level=level)
                    _apply_native_shape_overlay()
                    return 'HAction.Run(PutBullet)'
            except Exception:
                pass

        if hasattr(hwp, 'PutBullet'):
            try:
                hwp.PutBullet()
                _promote_current_list_level(hwp, level=level)
                _apply_native_shape_overlay()
                return 'PutBullet'
            except Exception:
                pass

        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)
            return 'source_parashape_bullet'

        if hasattr(hwp, 'get_parashape') and hasattr(hwp, 'set_parashape') and hasattr(hwp, 'HeadType'):
            pset = hwp.get_parashape()
            try:
                setattr(pset, 'HeadingType', hwp.HeadType('Bullet'))
                _set_shape_list_level(pset, level)
            except Exception as exc:
                raise EditOperationError(f'bullet paragraph fallback failed while setting HeadingType: {exc}') from exc
            try:
                hwp.set_parashape(pset)
                return 'heading_type_bullet'
            except Exception as exc:
                raise EditOperationError(f'bullet paragraph fallback failed while applying ParaShape: {exc}') from exc

        raise EditOperationError('bullet paragraph automation path is unavailable on this machine')

    raise EditOperationError(f'unsupported native list kind={normalized_kind!r}. Expected none, bullet, number, or outline.')


def _normalize_scaffold_items(op: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = op.get('items')
    if raw_items is None:
        raw_text = op.get('replace')
        if isinstance(raw_text, str) and raw_text.strip():
            raw_items = [{'text': line} for line in raw_text.splitlines() if line.strip()]
    if not isinstance(raw_items, list) or not raw_items:
        raise EditOperationError("replace_empty_native_list_scaffold requires non-empty 'items' or multiline 'replace'")

    default_kind = str(op.get('kind', 'bullet')).strip().lower()
    default_level = _normalize_list_level(op)
    allow_typed_markers = _bool(op, 'allow_typed_markers', False)
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if isinstance(raw, str):
            item = {'text': raw, 'kind': default_kind, 'level': default_level}
        elif isinstance(raw, dict):
            item_text = raw.get('text')
            if not isinstance(item_text, str) or not item_text.strip():
                raise EditOperationError(f"replace_empty_native_list_scaffold item #{index} requires non-empty string 'text'")
            item_kind = str(raw.get('kind', default_kind)).strip().lower()
            item_level_raw = raw.get('level', default_level)
            if isinstance(item_level_raw, bool) or not isinstance(item_level_raw, int) or item_level_raw < 1:
                raise EditOperationError(f"replace_empty_native_list_scaffold item #{index} field 'level' must be a positive integer")
            item = {'text': item_text, 'kind': item_kind, 'level': item_level_raw}
        else:
            raise EditOperationError(f'replace_empty_native_list_scaffold item #{index} must be a string or object')
        _reject_typed_list_marker_if_needed(item['text'], kind=item['kind'], allow_typed_markers=allow_typed_markers)
        normalized.append(item)
    return normalized


def _apply_replace_empty_native_list_scaffold(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    if cursor_pos is None and (not isinstance(target, str) or not target):
        raise EditOperationError("replace_empty_native_list_scaffold requires either non-empty 'find' or 'cursor_pos'")

    items = _normalize_scaffold_items(op)
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    require_empty_scaffold = _bool(op, 'require_empty_scaffold', False)
    forbidden_concatenations = _normalize_selected_text_expectations(
        op.get('forbidden_concatenations'),
        field_name='forbidden_concatenations',
    )
    source_find = op.get('source_find')
    source_shape = None
    if isinstance(source_find, str) and source_find:
        source_shape = _capture_paragraph_shape(hwp, source_find, match_case=match_case, whole_word=whole_word)

    transitions: list[dict[str, Any]] = []

    def _perform() -> None:
        if cursor_pos is not None:
            _set_pos(hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])
        before = _snapshot_cursor_context(hwp)
        original_selection = _select_whole_paragraph_for_current_selection(hwp)
        selected_text = _get_selected_text(hwp, keep_select=True)
        if require_empty_scaffold and _normalize_visible_text(selected_text):
            raise EditOperationError(
                'replace_empty_native_list_scaffold expected an empty native scaffold paragraph but selected non-empty text; '
                f'selected_text={selected_text!r}, before={before}'
            )
        if not original_selection or not original_selection[0]:
            raise EditOperationError('replace_empty_native_list_scaffold could not select the scaffold paragraph')
        _, start_list, start_para, _start_pos, _end_list, _end_para, _end_pos = original_selection
        _delete_selection(hwp)
        insert_text = '\r\n'.join(str(item['text']) for item in items)
        if hasattr(hwp, 'insert_text'):
            hwp.insert_text(insert_text)
        else:
            raise EditOperationError('pyhwpx insert_text is unavailable on this machine')

        item_proofs: list[dict[str, Any]] = []
        for offset, item in enumerate(items):
            para = int(start_para) + offset
            _set_pos(hwp, int(start_list), para, 0)
            if hasattr(hwp, 'select_text'):
                hwp.select_text(para, 0, para, -1, int(start_list))
            else:
                raise EditOperationError('pyhwpx select_text is unavailable on this machine')
            list_strategy = _apply_native_list_kind_current(
                hwp,
                kind=str(item['kind']),
                level=int(item['level']),
                source_shape=source_shape,
            )
            readback = _get_selected_text(hwp, keep_select=True)
            if not _selected_text_matches_expected(selected_text=readback, expected_text=str(item['text'])):
                raise EditOperationError(
                    'replace_empty_native_list_scaffold readback mismatch after native list apply; '
                    f'item={item}, readback={readback!r}'
                )
            normalized_readback = _normalize_visible_text(readback)
            matched_forbidden = [term for term in forbidden_concatenations if _normalize_visible_text(term) in normalized_readback]
            if matched_forbidden:
                raise EditOperationError(
                    'replace_empty_native_list_scaffold readback contains forbidden collapsed heading/body text; '
                    f'matched={matched_forbidden}, readback={readback!r}'
                )
            item_proofs.append(
                {
                    'index': offset + 1,
                    'cursor_pos': [int(start_list), para, 0],
                    'kind': item['kind'],
                    'level': item['level'],
                    'text': item['text'],
                    'readback': readback,
                    'list_strategy': list_strategy,
                }
            )

        after = _snapshot_cursor_context(hwp)
        transitions.append(
            {
                'before': before,
                'selected_scaffold_text': selected_text,
                'insert_text': insert_text,
                'item_count': len(items),
                'item_proofs': item_proofs,
                'after': after,
            }
        )

    if cursor_pos is not None:
        _perform()
        count = 1
    else:
        count = _find_matches(hwp, str(target), match_case=match_case, whole_word=whole_word, apply=scope, callback=_perform)
    return {'matches': count, 'item_count': len(items), 'transitions': transitions}


def _apply_list_paragraph(hwp: Any, op: dict[str, Any]) -> int:
    target = _require_text(op, 'find')
    kind = _require_text(op, 'kind').strip().lower()
    level = _normalize_list_level(op)
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    source_find = op.get('source_find')
    source_shape = None
    if isinstance(source_find, str) and source_find:
        source_shape = _capture_paragraph_shape(hwp, source_find, match_case=match_case, whole_word=whole_word)

    def _apply_native_shape_overlay() -> None:
        if source_shape is not None:
            _apply_parashape_with_optional_level(hwp, source_shape, level=level)

    def _callback() -> None:
        _select_whole_paragraph_for_current_selection(hwp)

        if kind == 'none':
            if source_shape is not None:
                _apply_parashape_with_optional_level(hwp, source_shape, level=level)
                return
            if hasattr(hwp, 'get_parashape') and hasattr(hwp, 'set_parashape') and hasattr(hwp, 'HeadType'):
                pset = hwp.get_parashape()
                try:
                    setattr(pset, 'HeadingType', hwp.HeadType('None'))
                except Exception as exc:
                    raise EditOperationError(f'list_paragraph none failed while setting HeadingType: {exc}') from exc
                try:
                    hwp.set_parashape(pset)
                    return
                except Exception as exc:
                    raise EditOperationError(f'list_paragraph none failed while applying ParaShape: {exc}') from exc
            raise EditOperationError('pyhwpx HeadingType=None path is unavailable on this machine')

        if kind == 'number':
            if hasattr(hwp, 'PutParaNumber'):
                hwp.PutParaNumber()
                _promote_current_list_level(hwp, level=level)
                _apply_native_shape_overlay()
                return
            if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
                hwp.HAction.Run('PutParaNumber')
                _promote_current_list_level(hwp, level=level)
                _apply_native_shape_overlay()
                return
            if source_shape is not None:
                _apply_parashape_with_optional_level(hwp, source_shape, level=level)
                return
            raise EditOperationError('pyhwpx PutParaNumber is unavailable on this machine')

        if kind == 'outline':
            if hasattr(hwp, 'PutOutlinleNumber'):
                hwp.PutOutlinleNumber()
                _promote_current_list_level(hwp, level=level)
                _apply_native_shape_overlay()
                return
            if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
                hwp.HAction.Run('PutOutlineNumber')
                _promote_current_list_level(hwp, level=level)
                _apply_native_shape_overlay()
                return
            if source_shape is not None:
                _apply_parashape_with_optional_level(hwp, source_shape, level=level)
                return
            raise EditOperationError('pyhwpx PutOutlineNumber is unavailable on this machine')

        if kind == 'bullet':
            if hasattr(hwp, 'HAction') and hasattr(hwp.HAction, 'Run'):
                try:
                    if hwp.HAction.Run('PutBullet'):
                        _promote_current_list_level(hwp, level=level)
                        _apply_native_shape_overlay()
                        return
                except Exception:
                    pass

            if hasattr(hwp, 'PutBullet'):
                try:
                    hwp.PutBullet()
                    _promote_current_list_level(hwp, level=level)
                    _apply_native_shape_overlay()
                    return
                except Exception:
                    pass

            if source_shape is not None:
                _apply_parashape_with_optional_level(hwp, source_shape, level=level)
                return

            if hasattr(hwp, 'get_parashape') and hasattr(hwp, 'set_parashape') and hasattr(hwp, 'HeadType'):
                pset = hwp.get_parashape()
                try:
                    setattr(pset, 'HeadingType', hwp.HeadType('Bullet'))
                    _set_shape_list_level(pset, level)
                except Exception as exc:
                    raise EditOperationError(f'bullet paragraph fallback failed while setting HeadingType: {exc}') from exc
                try:
                    hwp.set_parashape(pset)
                    return
                except Exception as exc:
                    raise EditOperationError(f'bullet paragraph fallback failed while applying ParaShape: {exc}') from exc

            raise EditOperationError('bullet paragraph automation path is unavailable on this machine')

        raise EditOperationError(f'unsupported list_paragraph kind={kind!r}. Expected none, bullet, number, or outline.')

    return _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)


def _apply_native_action(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    action = _require_text(op, 'action')
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    mode = str(op.get('mode', 'auto')).strip().lower()
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    parameters = op.get('parameters') if isinstance(op.get('parameters'), dict) else {}
    set_name = op.get('set_name') if isinstance(op.get('set_name'), str) and op.get('set_name') else None

    runner = NativeActionRunner(hwp)
    transitions: list[dict[str, Any]] = []

    def _perform() -> None:
        before = _snapshot_cursor_context(hwp)
        if mode == 'run':
            result = runner.run(action)
        elif mode == 'execute':
            result = runner.execute(action, parameters=parameters, set_name=set_name)
        else:
            result = runner.auto(action, parameters=parameters, set_name=set_name)
        if not result.succeeded:
            raise EditOperationError(
                f'native_action failed: action={action!r}, mode={mode!r}, '
                f'strategy={result.strategy!r}, error={result.error!r}'
            )
        after = _snapshot_cursor_context(hwp)
        transitions.append(
            {
                'before': before,
                'after': after,
                'action': action,
                'mode': result.mode,
                'strategy': result.strategy,
                'details': result.details,
            }
        )

    if cursor_pos is not None:
        _set_pos(hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])
        _perform()
        count = 1
    elif isinstance(target, str) and target:
        def _callback() -> None:
            _select_whole_paragraph_for_current_selection(hwp)
            _perform()

        count = _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_callback)
    else:
        _perform()
        count = 1

    return {'matches': count, 'action': action, 'mode': mode, 'transitions': transitions}


def _apply_cursor_insert_text(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    if cursor_pos is None:
        raise EditOperationError("cursor_insert_text requires non-empty 'cursor_pos'")
    insert_text = str(op.get('insert', ''))
    delete_selection = _bool(op, 'delete_selection', False)

    _set_pos(hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])
    before = _snapshot_cursor_context(hwp)
    if delete_selection and before.get('has_selection'):
        _delete_selection(hwp)
    if hasattr(hwp, 'insert_text'):
        hwp.insert_text(insert_text)
    else:
        raise EditOperationError('pyhwpx insert_text is unavailable on this machine')
    after = _snapshot_cursor_context(hwp)
    return {
        'matches': 1,
        'transitions': [
            {
                'before': before,
                'after': after,
                'insert_text': insert_text,
                'target_cursor_pos': list(cursor_pos),
            }
        ],
    }


def _apply_cursor_replace_text(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    start_cursor_pos, end_cursor_pos = _normalize_cursor_range(
        op.get('start_cursor_pos'),
        op.get('end_cursor_pos'),
    )
    replace_text = str(op.get('replace', ''))
    expected_selected_text = op.get('expected_selected_text')
    if expected_selected_text is not None and not isinstance(expected_selected_text, str):
        raise EditOperationError("cursor_replace_text field 'expected_selected_text' must be a string when provided")

    _set_pos(hwp, start_cursor_pos[0], start_cursor_pos[1], start_cursor_pos[2])
    before = _snapshot_cursor_context(hwp)
    selected_pos = _select_cursor_range(
        hwp,
        start_cursor_pos=start_cursor_pos,
        end_cursor_pos=end_cursor_pos,
    )
    selected_text = _get_selected_text(hwp, keep_select=True)
    if expected_selected_text is not None and selected_text != expected_selected_text:
        raise EditOperationError(
            f"cursor_replace_text expected selected text {expected_selected_text!r} but got {selected_text!r}"
        )
    _delete_selection(hwp)
    if hasattr(hwp, 'insert_text'):
        hwp.insert_text(replace_text)
    else:
        raise EditOperationError('pyhwpx insert_text is unavailable on this machine')
    after = _snapshot_cursor_context(hwp)
    return {
        'matches': 1,
        'transitions': [
            {
                'before': before,
                'selected_pos': list(selected_pos),
                'selected_text': selected_text,
                'after': after,
                'replace_text': replace_text,
                'start_cursor_pos': list(start_cursor_pos),
                'end_cursor_pos': list(end_cursor_pos),
            }
        ],
    }


def _apply_cursor_delete_range(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    start_cursor_pos, end_cursor_pos = _normalize_cursor_range(
        op.get('start_cursor_pos'),
        op.get('end_cursor_pos'),
    )
    expected_selected_text = op.get('expected_selected_text')
    if expected_selected_text is not None and not isinstance(expected_selected_text, str):
        raise EditOperationError("cursor_delete_range field 'expected_selected_text' must be a string when provided")

    _set_pos(hwp, start_cursor_pos[0], start_cursor_pos[1], start_cursor_pos[2])
    before = _snapshot_cursor_context(hwp)
    selected_pos = _select_cursor_range(
        hwp,
        start_cursor_pos=start_cursor_pos,
        end_cursor_pos=end_cursor_pos,
    )
    selected_text = _get_selected_text(hwp, keep_select=True)
    if expected_selected_text is not None and selected_text != expected_selected_text:
        raise EditOperationError(
            f"cursor_delete_range expected selected text {expected_selected_text!r} but got {selected_text!r}"
        )
    _delete_selection(hwp)
    after = _snapshot_cursor_context(hwp)
    return {
        'matches': 1,
        'transitions': [
            {
                'before': before,
                'selected_pos': list(selected_pos),
                'selected_text': selected_text,
                'after': after,
                'start_cursor_pos': list(start_cursor_pos),
                'end_cursor_pos': list(end_cursor_pos),
            }
        ],
    }


def _run_table_cell_action(hwp: Any, action: str) -> None:
    action_map: dict[str, tuple[str, ...]] = {
        'left': ('TableLeftCell',),
        'right': ('TableRightCell',),
        'up': ('TableUpperCell',),
        'down': ('TableLowerCell',),
        'block': ('TableCellBlock',),
        'block_row': ('TableCellBlockRow',),
        'block_col': ('TableCellBlockCol',),
        'extend': ('TableCellBlockExtend', 'TableCellBlockExtendAbs'),
    }
    names = action_map.get(action)
    if not names:
        supported = ', '.join(sorted(action_map))
        raise EditOperationError(f'table_cell_action unsupported action={action!r}. Supported: {supported}')

    def _safe_snapshot() -> dict[str, Any] | None:
        try:
            return _snapshot_cursor_context(hwp)
        except Exception:
            return None

    def _observed_success(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
        if action not in {'block', 'block_row', 'block_col', 'extend'}:
            return False
        if not isinstance(after, dict):
            return False
        if after.get('is_cell') is not True:
            return False
        if after.get('has_selection'):
            return True
        selection_mode = after.get('selection_mode')
        if action == 'block':
            return selection_mode in {3, 19}
        if action in {'block_row', 'block_col', 'extend'}:
            if selection_mode in {3, 19}:
                return True
            if isinstance(before, dict):
                before_selected = bool(before.get('has_selection'))
                before_mode = before.get('selection_mode')
                return before_selected or before_mode != selection_mode
        return False

    def _force_block_probe(before: dict[str, Any] | None) -> bool:
        if action != 'block':
            return False
        current = _safe_snapshot()
        if not isinstance(current, dict) or current.get('is_cell') is not True:
            return False
        try:
            _get_selected_text(hwp, keep_select=True)
        except Exception:
            return False
        return _observed_success(before, _safe_snapshot())

    for name in names:
        before = _safe_snapshot()
        method = getattr(hwp, name, None)
        if callable(method):
            raw = method()
            if raw is None or bool(raw):
                return
            if _observed_success(before, _safe_snapshot()):
                return
        haction = getattr(hwp, 'HAction', None)
        run = getattr(haction, 'Run', None)
        if callable(run):
            try:
                raw = run(name)
                if raw is None or bool(raw):
                    return
            except Exception:
                pass
            if _observed_success(before, _safe_snapshot()):
                return
        try:
            native_result = NativeActionRunner(hwp).execute(name)
        except Exception:
            native_result = None
        if native_result is not None:
            if native_result.succeeded or _observed_success(before, _safe_snapshot()):
                return
        if _force_block_probe(before):
            return
    raise EditOperationError(f'table_cell_action failed for action={action!r}; methods tried={names!r}')


def _apply_cursor_snapshot(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    expected_cell_addr = op.get('expected_cell_addr')
    expected_table_fingerprint = op.get('expected_table_fingerprint')
    if expected_cell_addr is not None and (not isinstance(expected_cell_addr, str) or not expected_cell_addr):
        raise EditOperationError("cursor_snapshot field 'expected_cell_addr' must be a non-empty string when provided")
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    select_paragraph = _bool(op, 'select_paragraph', False)
    capture_selected_text = _bool(op, 'capture_selected_text', False)
    expected_selected_text_absent_any = _normalize_selected_text_expectations(
        op.get('expected_selected_text_absent_any'),
        field_name='expected_selected_text_absent_any',
    )
    expected_selected_text_present_any = _normalize_selected_text_expectations(
        op.get('expected_selected_text_present_any'),
        field_name='expected_selected_text_present_any',
    )
    if expected_selected_text_absent_any or expected_selected_text_present_any:
        capture_selected_text = True
    snapshots: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}

    def _capture() -> None:
        identity_snapshot = None
        if cursor_pos is not None or expected_cell_addr is not None or isinstance(expected_table_fingerprint, dict):
            identity_snapshot = _move_to_table_identity(
                hwp,
                cursor_pos=cursor_pos,
                expected_cell_addr=expected_cell_addr,
                expected_table_fingerprint=expected_table_fingerprint if isinstance(expected_table_fingerprint, dict) else None,
                require_in_cell=False,
            )
        if select_paragraph:
            _select_whole_paragraph_for_current_selection(hwp)
        snapshot = _snapshot_cursor_context(hwp)
        if (
            op.get('probe_identity_key') == '048e254af391e295'
            and snapshot.get('is_cell') is not True
        ):
            primitive_attempts: list[dict[str, Any]] = diagnostics.setdefault('entry_primitive_attempts', [])
            primitive_specs: list[tuple[str, Any]] = [
                ('HAction.Run(TableCellBlock)', lambda: getattr(getattr(hwp, 'HAction'), 'Run')('TableCellBlock')),
            ]
            for primitive_name, primitive_call in primitive_specs:
                before_attempt = dict(snapshot)
                try:
                    call_result = {'ok': True, 'value': primitive_call()}
                except Exception as exc:
                    call_result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
                after_attempt = _snapshot_cursor_context(hwp)
                primitive_attempts.append({
                    'primitive': primitive_name,
                    'before': before_attempt,
                    'call': call_result,
                    'after': after_attempt,
                    'is_cell_became_true': before_attempt.get('is_cell') is not True and after_attempt.get('is_cell') is True,
                    'cell_addr_after': after_attempt.get('cell_addr'),
                })
                snapshot = after_attempt
                if snapshot.get('is_cell') is True:
                    break
            if snapshot.get('is_cell') is not True:
                raise EditOperationError(
                    'method-p110 entry primitives exhausted without entering table cell; '
                    f'attempts={primitive_attempts}'
                )
        if capture_selected_text:
            snapshot.update(_capture_selected_text_snapshot(hwp))
            _apply_selected_text_proof(
                snapshot,
                expected_absent_any=expected_selected_text_absent_any,
                expected_present_any=expected_selected_text_present_any,
            )
        if isinstance(expected_table_fingerprint, dict):
            snapshot['expected_table_fingerprint'] = expected_table_fingerprint
        if isinstance(identity_snapshot, dict):
            for key in ('table_signature', 'table_probe_candidate'):
                if key in identity_snapshot:
                    snapshot[key] = identity_snapshot[key]
        snapshots.append(snapshot)

    if cursor_pos is not None:
        _capture()
        count = 1
    elif isinstance(target, str) and target:
        count = _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_capture)
        if count <= 0 and isinstance(expected_table_fingerprint, dict):
            scanned = _scan_tables_for_fingerprint(
                hwp,
                expected_table_fingerprint=expected_table_fingerprint,
                expected_cell_addr=expected_cell_addr,
            )
            diagnostics['table_probe'] = scanned.get('probes') or []
            diagnostics['table_match_candidates'] = scanned.get('matching_candidates') or []
            if scanned.get('match') is not None:
                snapshot = dict(scanned['match']['snapshot'])
                snapshot['table_probe_candidate'] = scanned['match']['candidate']
                snapshot['table_signature'] = scanned['match']['table_signature']
                snapshot['table_probes'] = scanned.get('probes') or []
                snapshot['table_match_candidates'] = scanned.get('matching_candidates') or []
                snapshot['table_match_candidate_count'] = len(snapshot['table_match_candidates'])
                if snapshot['table_match_candidate_count'] > 1:
                    snapshot['table_match_selection'] = {
                        'selected_candidate': scanned['match']['candidate'],
                        'reason': 'first_exact_table_fingerprint_match',
                    }
                snapshots.append(snapshot)
                count = 1
    else:
        _capture()
        count = 1

    result = {'matches': count, 'snapshots': snapshots}
    if diagnostics:
        result['diagnostics'] = diagnostics
    return result


def _verify_table_snapshot(*, op_name: str, action: str | None, after: dict[str, Any], expected_selection_mode: int | None, expected_is_cell: bool | None) -> dict[str, Any]:
    verification: dict[str, Any] = {}
    if expected_selection_mode is not None:
        actual_selection_mode = after.get('selection_mode')
        verification['expected_selection_mode'] = expected_selection_mode
        verification['actual_selection_mode'] = actual_selection_mode
        verification['selection_mode_ok'] = actual_selection_mode == expected_selection_mode
        if actual_selection_mode != expected_selection_mode:
            label = f'{op_name} action={action!r}' if action is not None else op_name
            raise EditOperationError(
                f"{label} expected selection_mode={expected_selection_mode} but got {actual_selection_mode}; after={after}"
            )
    if expected_is_cell is not None:
        actual_is_cell = after.get('is_cell')
        verification['expected_is_cell'] = expected_is_cell
        verification['actual_is_cell'] = actual_is_cell
        verification['is_cell_ok'] = actual_is_cell is expected_is_cell
        if actual_is_cell is not expected_is_cell:
            label = f'{op_name} action={action!r}' if action is not None else op_name
            raise EditOperationError(
                f"{label} expected is_cell={expected_is_cell} but got {actual_is_cell}; after={after}"
            )
    return verification


def _apply_table_cell_action(hwp: Any, op: dict[str, Any]) -> dict[str, Any]:
    action = _require_text(op, 'action').strip().lower()
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    expected_cell_addr = op.get('expected_cell_addr')
    if expected_cell_addr is not None and (not isinstance(expected_cell_addr, str) or not expected_cell_addr):
        raise EditOperationError("table_cell_action field 'expected_cell_addr' must be a non-empty string when provided")
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    require_in_cell = _bool(op, 'require_in_cell', True)
    select_paragraph = _bool(op, 'select_paragraph', False)
    expected_selection_mode = op.get('expected_selection_mode')
    expected_is_cell = op.get('expected_is_cell')
    if expected_selection_mode is not None and (isinstance(expected_selection_mode, bool) or not isinstance(expected_selection_mode, int)):
        raise EditOperationError("table_cell_action field 'expected_selection_mode' must be an integer when provided")
    if expected_is_cell is not None and not isinstance(expected_is_cell, bool):
        raise EditOperationError("table_cell_action field 'expected_is_cell' must be a boolean when provided")
    transitions: list[dict[str, Any]] = []

    def _perform() -> None:
        if cursor_pos is not None:
            before = _move_to_table_identity(
                hwp,
                cursor_pos=cursor_pos,
                expected_cell_addr=expected_cell_addr,
                require_in_cell=require_in_cell,
            )
        else:
            before = _snapshot_cursor_context(hwp)
            if expected_cell_addr is not None and before.get('cell_addr') != expected_cell_addr:
                raise EditOperationError(
                    f"table_cell_action expected cell_addr={expected_cell_addr!r} before action but got {before.get('cell_addr')!r}; snapshot={before}"
                )
        if select_paragraph:
            _select_whole_paragraph_for_current_selection(hwp)
            before = _snapshot_cursor_context(hwp)
        if require_in_cell and before.get('is_cell') is not True:
            raise EditOperationError(
                f'table_cell_action requires cursor inside a table cell before action={action!r}; '
                f'current snapshot={before}'
            )
        _run_table_cell_action(hwp, action)
        after = _snapshot_cursor_context(hwp)
        verification = _verify_table_snapshot(
            op_name='table_cell_action',
            action=action,
            after=after,
            expected_selection_mode=expected_selection_mode,
            expected_is_cell=expected_is_cell,
        )
        transitions.append({'before': before, 'after': after, 'action': action, 'verification': verification})

    if cursor_pos is not None:
        _perform()
        count = 1
    elif isinstance(target, str) and target:
        count = _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_perform)
    else:
        _perform()
        count = 1

    return {'matches': count, 'action': action, 'transitions': transitions}


def _apply_table_patch_cells(hwp: Any, op: dict[str, Any], *, runtime_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    entry_find = op.get('entry_find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    expected_entry_cell_addr = op.get('expected_entry_cell_addr')
    if expected_entry_cell_addr is None and isinstance(op.get('cell_addr'), str) and op.get('cell_addr'):
        expected_entry_cell_addr = op.get('cell_addr')
    if expected_entry_cell_addr is not None and (not isinstance(expected_entry_cell_addr, str) or not expected_entry_cell_addr):
        raise EditOperationError("table_patch_cells field 'expected_entry_cell_addr' must be a non-empty string when provided")
    if cursor_pos is None and (not isinstance(entry_find, str) or not entry_find):
        raise EditOperationError("table_patch_cells requires either non-empty 'entry_find' or 'cursor_pos'")

    raw_patches = op.get('patches')
    if not isinstance(raw_patches, list) or not raw_patches:
        raise EditOperationError("table_patch_cells field 'patches' must be a non-empty list")
    patches: list[dict[str, str]] = []
    for index, item in enumerate(raw_patches, start=1):
        if not isinstance(item, dict):
            raise EditOperationError(f"table_patch_cells patch #{index} must be an object")
        cell_addr = item.get('cell_addr')
        replace_text = item.get('replace')
        if not isinstance(cell_addr, str) or not cell_addr:
            raise EditOperationError(f"table_patch_cells patch #{index} requires non-empty string 'cell_addr'")
        normalized_ref = _cell_addr_to_ref(cell_addr)
        if normalized_ref is None:
            raise EditOperationError(f"table_patch_cells patch #{index} has invalid cell_addr={cell_addr!r}")
        patches.append({'cell_addr': normalized_ref['addr'], 'replace': str(replace_text or '')})

    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    require_in_cell = _bool(op, 'require_in_cell', True)
    expected_table_fingerprint_obj, expected_table_fingerprint_token = _normalize_expected_table_fingerprint(
        op.get('expected_table_fingerprint'),
        field_name="table_patch_cells field 'expected_table_fingerprint'",
    )
    expected_selection_mode = op.get('expected_selection_mode', 3)
    expected_is_cell = op.get('expected_is_cell', True)
    if isinstance(expected_selection_mode, bool) or not isinstance(expected_selection_mode, int):
        raise EditOperationError("table_patch_cells field 'expected_selection_mode' must be an integer")
    if not isinstance(expected_is_cell, bool):
        raise EditOperationError("table_patch_cells field 'expected_is_cell' must be a boolean")

    transitions: list[dict[str, Any]] = []

    def _perform(*, cursor_pos_override: tuple[int, int, int] | None = None) -> None:
        effective_cursor_pos = cursor_pos if cursor_pos is not None else cursor_pos_override
        before = _move_to_table_identity(
            hwp,
            cursor_pos=effective_cursor_pos,
            expected_cell_addr=expected_entry_cell_addr,
            expected_table_fingerprint=expected_table_fingerprint_obj,
            require_in_cell=require_in_cell,
        )

        fill_addr_field_error = None
        fill_addr_field = getattr(hwp, 'fill_addr_field', None)
        if callable(fill_addr_field):
            try:
                fill_addr_field()
            except Exception as exc:
                fill_addr_field_error = str(exc)

        field_list = None
        if hasattr(hwp, 'get_field_list'):
            try:
                field_list = hwp.get_field_list(1, 1)
            except Exception:
                field_list = None
        live_table_fingerprint: dict[str, Any] = {
            'skipped': 'not_required_without_expected_table_fingerprint',
            'reason': 'avoid_global_field_navigation_cursor_taint_before_scoped_writes',
        }
        if expected_table_fingerprint_token is not None:
            fingerprint_start = _snapshot_cursor_context(hwp)
            if field_list:
                try:
                    live_table_fingerprint = _live_table_fingerprint_from_field_list(hwp, field_list)
                except Exception as exc:
                    live_table_fingerprint = {'error': f'{type(exc).__name__}: {exc}'}
                finally:
                    _restore_cursor_from_snapshot(hwp, fingerprint_start)
            actual_fingerprint = live_table_fingerprint.get('fingerprint') if isinstance(live_table_fingerprint, dict) else None
            if actual_fingerprint != expected_table_fingerprint_token:
                raise EditOperationError(
                    'table_patch_cells table fingerprint mismatch before scoped apply: '
                    f'expected={expected_table_fingerprint_token!r}, actual={actual_fingerprint!r}, '
                    f'live_table_fingerprint={live_table_fingerprint}, fill_addr_field_error={fill_addr_field_error!r}'
                )

        global_field_write_preflight = _capture_global_field_write_preflight(
            hwp,
            [patch['cell_addr'] for patch in patches],
        )
        global_field_write_preflight['decision'] = 'rejected_for_table_patch_cells'
        _restore_cursor_from_snapshot(hwp, before)
        if not global_field_write_preflight.get('safe_for_global_field_write'):
            global_field_write_preflight['reason'] = 'duplicate_or_missing_generated_field_names_fail_closed'

        patch_results: list[dict[str, Any]] = []
        for patch in patches:
            target_cell_addr = patch['cell_addr']
            navigation = _move_to_cell_scoped(hwp, target_cell_addr)
            current = _snapshot_cursor_context(hwp)
            if current.get('cell_addr') != target_cell_addr:
                raise EditOperationError(
                    f"table_patch_cells scoped navigation expected cell_addr={target_cell_addr!r} but got {current.get('cell_addr')!r}; "
                    f'navigation={navigation}, snapshot={current}'
                )

            clear_result = _clear_current_cell_text(hwp, expected_cell_addr=target_cell_addr)
            verification = _verify_table_snapshot(
                op_name='table_patch_cells',
                action='clear_current_cell_text',
                after=clear_result['selected'],
                expected_selection_mode=expected_selection_mode,
                expected_is_cell=expected_is_cell,
            )
            if hasattr(hwp, 'insert_text'):
                hwp.insert_text(patch['replace'])
            else:
                raise EditOperationError('pyhwpx insert_text is unavailable on this machine')
            after_insert = _snapshot_cursor_context(hwp)
            if after_insert.get('cell_addr') != target_cell_addr:
                raise EditOperationError(
                    f"table_patch_cells expected final cell_addr={target_cell_addr!r} but got {after_insert.get('cell_addr')!r}; "
                    f'after_insert={after_insert}'
                )
            readback_snapshot, readback_verification = _capture_table_cell_selected_text_proof(
                hwp,
                expected_cell_addr=target_cell_addr,
                expected_selection_mode=expected_selection_mode,
                expected_is_cell=expected_is_cell,
                expected_absent_any=[],
                expected_present_any=[],
                op_name='table_patch_cells',
                action='readback_current_cell_after_insert',
            )
            readback_text = str(readback_snapshot.get('selected_text') or '')
            if not _selected_text_proves_replacement(selected_text=readback_text, replace_text=patch['replace']):
                raise EditOperationError(
                    'table_patch_cells readback mismatch after scoped replacement: '
                    f"cell_addr={target_cell_addr!r}, expected={patch['replace']!r}, readback={readback_text!r}"
                )
            _restore_cursor_from_snapshot(hwp, after_insert)
            patch_results.append({
                'cell_addr': target_cell_addr,
                'navigation': navigation,
                'before': current,
                'selected': clear_result['selected'],
                'selected_text': clear_result['selected_text'],
                'after_clear': clear_result['after'],
                'after': after_insert,
                'readback': readback_snapshot,
                'verification': verification,
                'readback_verification': readback_verification,
                'replace_text': patch['replace'],
                'replace_strategy': 'scoped_cell_navigation_clear_insert_readback',
            })
        transitions.append({
            'entry_before': before,
            'field_list': field_list,
            'fill_addr_field_error': fill_addr_field_error,
            'global_field_write_preflight': global_field_write_preflight,
            'live_table_fingerprint': live_table_fingerprint,
            'patch_results': patch_results,
        })

    if cursor_pos is not None:
        _perform()
        return {'matches': 1, 'transitions': transitions}

    matched = 0
    last_retryable_error: EditOperationError | None = None
    for candidate_text, allow_whole_word in _build_find_candidates(str(entry_find)):
        _move_doc_begin(hwp)
        while hwp.find(
            candidate_text,
            direction='Forward',
            MatchCase=1 if match_case else 0,
            WholeWordOnly=1 if (whole_word and allow_whole_word) else 0,
        ):
            matched += 1
            matched_selection = None
            try:
                matched_selection = _get_selected_pos(hwp)
            except Exception:
                matched_selection = None
            try:
                matched_cursor_pos = _cursor_pos_from_selected_range(matched_selection)
                _perform(cursor_pos_override=matched_cursor_pos)
                return {'matches': 1, 'transitions': transitions}
            except EditOperationError as exc:
                if not _is_retryable_table_identity_error(exc):
                    raise
                last_retryable_error = exc
                if matched_selection and matched_selection[0]:
                    try:
                        _select_text(hwp, matched_selection)
                    except Exception:
                        pass
                _move_after_selection(hwp)
                continue
        if matched > 0:
            break

    if last_retryable_error is not None:
        raise last_retryable_error
    return {'matches': 0, 'transitions': transitions}


def _apply_table_cell_clear_text(hwp: Any, op: dict[str, Any], *, runtime_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    cursor_pos_from_snapshot = op.get('cursor_pos_from_snapshot')
    if cursor_pos is None and cursor_pos_from_snapshot is not None:
        if cursor_pos_from_snapshot != 'last':
            raise EditOperationError("table_cell_clear_text field 'cursor_pos_from_snapshot' only supports value 'last'")
        if isinstance(runtime_snapshot, dict) and runtime_snapshot.get('pos') is not None:
            cursor_pos = _normalize_cursor_pos(runtime_snapshot.get('pos'), field_name='runtime_snapshot.pos')

    expected_cell_addr = op.get('expected_cell_addr')
    expected_cell_addr_from_snapshot = op.get('expected_cell_addr_from_snapshot')
    if expected_cell_addr is None and isinstance(op.get('cell_addr'), str) and op.get('cell_addr'):
        expected_cell_addr = op.get('cell_addr')
    if expected_cell_addr is None and expected_cell_addr_from_snapshot is not None:
        if expected_cell_addr_from_snapshot != 'last':
            raise EditOperationError("table_cell_clear_text field 'expected_cell_addr_from_snapshot' only supports value 'last'")
        if isinstance(runtime_snapshot, dict) and runtime_snapshot.get('cell_addr'):
            expected_cell_addr = runtime_snapshot.get('cell_addr')
    if expected_cell_addr is not None and (not isinstance(expected_cell_addr, str) or not expected_cell_addr):
        raise EditOperationError("table_cell_clear_text field 'expected_cell_addr' must be a non-empty string when provided")

    require_in_cell = _bool(op, 'require_in_cell', True)
    expected_selection_mode = op.get('expected_selection_mode', 3)
    expected_is_cell = op.get('expected_is_cell', True)
    if isinstance(expected_selection_mode, bool) or not isinstance(expected_selection_mode, int):
        raise EditOperationError("table_cell_clear_text field 'expected_selection_mode' must be an integer")
    if not isinstance(expected_is_cell, bool):
        raise EditOperationError("table_cell_clear_text field 'expected_is_cell' must be a boolean")

    before = _move_to_table_identity(
        hwp,
        cursor_pos=cursor_pos,
        expected_cell_addr=expected_cell_addr,
        require_in_cell=require_in_cell,
    )
    clear_result = _clear_current_cell_text(hwp, expected_cell_addr=expected_cell_addr)
    verification = _verify_table_snapshot(
        op_name='table_cell_clear_text',
        action='clear_current_cell_text',
        after=clear_result['selected'],
        expected_selection_mode=expected_selection_mode,
        expected_is_cell=expected_is_cell,
    )
    return {
        'matches': 1,
        'transitions': [
            {
                'before': before,
                'selected': clear_result['selected'],
                'selected_text': clear_result['selected_text'],
                'after': clear_result['after'],
                'verification': verification,
                'target_cursor_pos': list(cursor_pos) if cursor_pos is not None else None,
                'target_cell_addr': expected_cell_addr,
            }
        ],
    }


def _apply_table_cell_replace_text(hwp: Any, op: dict[str, Any], *, runtime_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    target = op.get('find')
    cursor_pos = _normalize_cursor_pos(op.get('cursor_pos'))
    cursor_pos_from_snapshot = op.get('cursor_pos_from_snapshot')
    if cursor_pos is None and cursor_pos_from_snapshot is not None:
        if cursor_pos_from_snapshot != 'last':
            raise EditOperationError("table_cell_replace_text field 'cursor_pos_from_snapshot' only supports value 'last'")
        if isinstance(runtime_snapshot, dict) and runtime_snapshot.get('pos') is not None:
            cursor_pos = _normalize_cursor_pos(runtime_snapshot.get('pos'), field_name='runtime_snapshot.pos')

    expected_cell_addr = op.get('expected_cell_addr')
    expected_cell_addr_from_snapshot = op.get('expected_cell_addr_from_snapshot')
    if expected_cell_addr is None and isinstance(op.get('cell_addr'), str) and op.get('cell_addr'):
        expected_cell_addr = op.get('cell_addr')
    if expected_cell_addr is None and expected_cell_addr_from_snapshot is not None:
        if expected_cell_addr_from_snapshot != 'last':
            raise EditOperationError("table_cell_replace_text field 'expected_cell_addr_from_snapshot' only supports value 'last'")
        if isinstance(runtime_snapshot, dict) and runtime_snapshot.get('cell_addr'):
            expected_cell_addr = runtime_snapshot.get('cell_addr')
    if expected_cell_addr is not None and (not isinstance(expected_cell_addr, str) or not expected_cell_addr):
        raise EditOperationError("table_cell_replace_text field 'expected_cell_addr' must be a non-empty string when provided")
    expected_table_fingerprint = op.get('expected_table_fingerprint')
    if expected_table_fingerprint is None and isinstance(runtime_snapshot, dict):
        runtime_table_fingerprint = runtime_snapshot.get('expected_table_fingerprint')
        if isinstance(runtime_table_fingerprint, dict):
            expected_table_fingerprint = runtime_table_fingerprint
    if expected_table_fingerprint is not None and not isinstance(expected_table_fingerprint, dict):
        raise EditOperationError("table_cell_replace_text field 'expected_table_fingerprint' must be an object when provided")
    if cursor_pos is None and (not isinstance(target, str) or not target):
        raise EditOperationError("table_cell_replace_text requires either non-empty 'find', 'cursor_pos', or snapshot-derived cursor identity")

    replace_text = str(op.get('replace', ''))
    expected_selected_text = str(op.get('expected_selected_text') or '')
    scope = _scope(op)
    match_case = _bool(op, 'match_case', True)
    whole_word = _bool(op, 'whole_word', False)
    select_paragraph = _bool(op, 'select_paragraph', True)
    require_in_cell = _bool(op, 'require_in_cell', True)
    expected_selection_mode = op.get('expected_selection_mode', 3)
    expected_is_cell = op.get('expected_is_cell', True)
    disable_put_field_text = _bool(op, 'disable_put_field_text', False)
    capture_selected_text_after = _bool(op, 'capture_selected_text_after', False)
    expected_selected_text_absent_any_after = _normalize_selected_text_expectations(
        op.get('expected_selected_text_absent_any_after'),
        field_name='expected_selected_text_absent_any_after',
    )
    expected_selected_text_present_any_after = _normalize_selected_text_expectations(
        op.get('expected_selected_text_present_any_after'),
        field_name='expected_selected_text_present_any_after',
    )
    if expected_selected_text_absent_any_after or expected_selected_text_present_any_after:
        capture_selected_text_after = True
    if isinstance(expected_selection_mode, bool) or not isinstance(expected_selection_mode, int):
        raise EditOperationError("table_cell_replace_text field 'expected_selection_mode' must be an integer")
    if not isinstance(expected_is_cell, bool):
        raise EditOperationError("table_cell_replace_text field 'expected_is_cell' must be a boolean")

    transitions: list[dict[str, Any]] = []

    def _capture_post_write_proof() -> dict[str, Any] | None:
        if not capture_selected_text_after:
            return None
        if expected_cell_addr is not None:
            try:
                before_block = _move_to_cell_scoped(hwp, expected_cell_addr)
            except Exception:
                before_block = _move_to_table_identity(
                    hwp,
                    cursor_pos=cursor_pos,
                    expected_cell_addr=expected_cell_addr,
                    expected_table_fingerprint=expected_table_fingerprint,
                    require_in_cell=require_in_cell,
                )
        else:
            before_block = _move_to_table_identity(
                hwp,
                cursor_pos=cursor_pos,
                expected_cell_addr=expected_cell_addr,
                expected_table_fingerprint=expected_table_fingerprint,
                require_in_cell=require_in_cell,
            )
        _select_current_cell_contents(hwp, expected_cell_addr=expected_cell_addr)
        selected_after = _snapshot_cursor_context(hwp)
        selected_after.update(_capture_selected_text_snapshot(hwp))
        verification = _verify_table_snapshot(
            op_name='table_cell_replace_text',
            action='post_write_block',
            after=selected_after,
            expected_selection_mode=expected_selection_mode,
            expected_is_cell=expected_is_cell,
        )
        _apply_selected_text_proof(
            selected_after,
            expected_absent_any=expected_selected_text_absent_any_after,
            expected_present_any=expected_selected_text_present_any_after,
        )
        return {
            'before_block': before_block,
            'selected': selected_after,
            'verification': verification,
        }

    def _perform() -> None:
        before = _move_to_table_identity(
            hwp,
            cursor_pos=cursor_pos,
            expected_cell_addr=expected_cell_addr,
            expected_table_fingerprint=expected_table_fingerprint,
            require_in_cell=require_in_cell,
        )
        before_context = _capture_nearby_text_context(hwp)
        put_field_text_preflight = None
        if not disable_put_field_text and expected_cell_addr is not None and (hasattr(hwp, 'put_field_text') or hasattr(hwp, 'PutFieldText')):
            fill_addr_field = getattr(hwp, 'fill_addr_field', None)
            if callable(fill_addr_field):
                fill_addr_field()
            put_field_text_preflight = _capture_global_field_write_preflight(hwp, [expected_cell_addr])
            if put_field_text_preflight.get('safe_for_global_field_write') and _move_to_field(hwp, expected_cell_addr):
                replace_strategy = _put_field_text(hwp, expected_cell_addr, replace_text)
                if not _move_to_field(hwp, expected_cell_addr):
                    raise EditOperationError(f"table_cell_replace_text failed to re-enter field({expected_cell_addr!r}) after put_field_text")
                after = _snapshot_cursor_context(hwp)
                if expected_cell_addr is not None and after.get('cell_addr') != expected_cell_addr:
                    raise EditOperationError(
                        f"table_cell_replace_text expected final cell_addr={expected_cell_addr!r} but got {after.get('cell_addr')!r}; after={after}"
                    )
                verification = _verify_table_snapshot(
                    op_name='table_cell_replace_text',
                    action='put_field_text',
                    after=after,
                    expected_selection_mode=None,
                    expected_is_cell=expected_is_cell,
                )
                after_context = _capture_nearby_text_context(hwp)
                post_write_proof = _capture_post_write_proof()
                transitions.append({
                    'before': before,
                    'before_context': before_context,
                    'selected': None,
                    'selected_text': None,
                    'after_clear': None,
                    'after': after,
                    'after_context': after_context,
                    'verification': verification,
                    'post_write_proof': post_write_proof,
                    'replace_text': replace_text,
                    'replace_text_preview': _preview_text(replace_text),
                    'replace_strategy': replace_strategy,
                    'put_field_text_preflight': put_field_text_preflight,
                    'target_cursor_pos': list(cursor_pos) if cursor_pos is not None else None,
                    'target_cell_addr': expected_cell_addr,
                })
                return
        if select_paragraph:
            selection_before = _get_selected_pos(hwp)
            if selection_before and selection_before[0]:
                _select_whole_paragraph_for_current_selection(hwp)
                before = _move_to_table_identity(
                    hwp,
                    cursor_pos=None,
                    expected_cell_addr=expected_cell_addr,
                    expected_table_fingerprint=expected_table_fingerprint,
                    require_in_cell=require_in_cell,
                )
        clear_result = _clear_current_cell_text(hwp, expected_cell_addr=expected_cell_addr)
        if expected_selected_text and not _selected_text_matches_expected(
            selected_text=str(clear_result.get('selected_text') or ''),
            expected_text=expected_selected_text,
        ):
            raise EditOperationError(
                'table_cell_replace_text selected_text mismatch before replace: '
                f"expected_overlap_with={expected_selected_text[:160]!r}, "
                f"selected_text={str(clear_result.get('selected_text') or '')[:160]!r}"
            )
        selected = clear_result['selected']
        verification = _verify_table_snapshot(
            op_name='table_cell_replace_text',
            action='clear_current_cell_text',
            after=selected,
            expected_selection_mode=expected_selection_mode,
            expected_is_cell=expected_is_cell,
        )
        if hasattr(hwp, 'insert_text'):
            hwp.insert_text(replace_text)
        else:
            raise EditOperationError('pyhwpx insert_text is unavailable on this machine')
        after = _snapshot_cursor_context(hwp)
        if expected_cell_addr is not None and after.get('cell_addr') != expected_cell_addr:
            raise EditOperationError(
                f"table_cell_replace_text expected final cell_addr={expected_cell_addr!r} but got {after.get('cell_addr')!r}; after={after}"
            )
        after_context = _capture_nearby_text_context(hwp)
        post_write_proof = _capture_post_write_proof()
        transitions.append({
            'before': before,
            'before_context': before_context,
            'selected': selected,
            'selected_text': clear_result['selected_text'],
            'after_clear': clear_result['after'],
            'after': after,
            'after_context': after_context,
            'verification': verification,
            'post_write_proof': post_write_proof,
            'replace_text': replace_text,
            'replace_text_preview': _preview_text(replace_text),
            'put_field_text_preflight': put_field_text_preflight,
            'target_cursor_pos': list(cursor_pos) if cursor_pos is not None else None,
            'target_cell_addr': expected_cell_addr,
        })

    if cursor_pos is not None:
        _perform()
        count = 1
    else:
        count = _find_matches(hwp, target, match_case=match_case, whole_word=whole_word, apply=scope, callback=_perform)
    return {'matches': count, 'transitions': transitions}


def apply_edit_operations(
    hwp: Any,
    operations: list[dict[str, Any]],
    step_recorder: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    last_cursor_snapshot: dict[str, Any] | None = None

    def _summary_item(index: int, op_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        item = {'index': index, 'op': op_type, **payload, **_extract_step_summary_metadata(op)}
        if bool(op.get('allow_zero_match')):
            item['allow_zero_match'] = True
        return item

    def _record_step(item: dict[str, Any], *, operation: dict[str, Any] | None = None) -> None:
        if step_recorder is not None:
            payload = dict(item)
            payload.setdefault('state', 'succeeded')
            if isinstance(operation, dict):
                payload['operation'] = dict(operation)
            step_recorder(payload)

    def _record_summary(index: int, op_type: str, payload: dict[str, Any]) -> None:
        item = _summary_item(index, op_type, payload)
        summary.append(item)
        _record_step(item, operation=op)

    for index, op in enumerate(operations, start=1):
        try:
            op_type = op['op']
            if op_type == 'replace_all':
                find_text = _require_text(op, 'find')
                replace_text = str(op.get('replace', ''))
                if _bool(op, 'regex', False):
                    raise EditOperationError('replace_all regex mode is not supported in the safe anchor implementation')
                count = _safe_replace_matches(
                    hwp,
                    find_text,
                    replace_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    paragraph_mode=False,
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                )
                _record_summary(index, op_type, {'changed': bool(count), 'matches': count})
                continue

            if op_type == 'replace_text_safe':
                find_text = _require_text(op, 'find')
                replace_text = str(op.get('replace', ''))
                count = _safe_replace_matches(
                    hwp,
                    find_text,
                    replace_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    paragraph_mode=False,
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                )
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type in {'replace_paragraph_safe', 'paragraph_replace_native'}:
                find_text = _require_text(op, 'find')
                replace_text = _require_text(op, 'replace')
                count = _safe_replace_matches(
                    hwp,
                    find_text,
                    replace_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    paragraph_mode=True,
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                    expected_present_after=str(op.get('expected_present_after')) if op.get('expected_present_after') is not None else None,
                    expected_absent_after=str(op.get('expected_absent_after')) if op.get('expected_absent_after') is not None else None,
                )
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type in {'replace_paragraph_range_safe', 'paragraph_range_replace_native'}:
                start_find = _require_text(op, 'start_find')
                end_find = _require_text(op, 'end_find')
                replace_text = _require_text(op, 'replace')
                count = _safe_replace_paragraph_range(
                    hwp,
                    start_find,
                    end_find,
                    replace_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                )
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'replace_between_anchors_safe':
                start_anchor = _require_text(op, 'start_anchor')
                end_anchor = _require_text(op, 'end_anchor')
                replace_text = str(op.get('replace', ''))
                count = _safe_replace_between_anchors(
                    hwp,
                    start_anchor,
                    end_anchor,
                    replace_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                )
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'clone_text_style':
                count = _apply_clone_text_style(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'clone_paragraph_shape':
                count = _apply_clone_paragraph_shape(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'clone_paragraph_layout':
                count = _apply_clone_paragraph_layout(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'insert_after_text':
                find_text = _require_text(op, 'find')
                insert_text = _require_text(op, 'insert')
                count = _safe_replace_matches(
                    hwp,
                    find_text,
                    find_text + insert_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    paragraph_mode=False,
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                    allow_target_in_replace=True,
                )
                _record_summary(index, op_type, {'changed': bool(count), 'matches': count})
                continue

            if op_type == 'insert_before_text':
                find_text = _require_text(op, 'find')
                insert_text = _require_text(op, 'insert')
                count = _safe_replace_matches(
                    hwp,
                    find_text,
                    insert_text + find_text,
                    apply=_scope(op),
                    match_case=_bool(op, 'match_case', True),
                    whole_word=_bool(op, 'whole_word', False),
                    paragraph_mode=False,
                    allow_packed_paragraph=_bool(op, 'allow_packed_paragraph', False),
                    allow_target_in_replace=True,
                )
                _record_summary(index, op_type, {'changed': bool(count), 'matches': count})
                continue

            if op_type == 'insert_at_document_end':
                insert_text = _require_text(op, 'insert')
                _move_doc_end(hwp)
                if _bool(op, 'new_paragraph_before', False):
                    insert_text = '\r\n' + insert_text
                if hasattr(hwp, 'insert_text'):
                    hwp.insert_text(insert_text)
                else:
                    raise EditOperationError('pyhwpx insert_text is unavailable on this machine')
                _record_summary(index, op_type, {'changed': True})
                continue

            if op_type == 'style_text':
                count = _apply_style_text(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'style_text_in_paragraph':
                count = _apply_style_text_in_paragraph(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'align_paragraph':
                count = _apply_align_paragraph(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'paragraph_shape':
                count = _apply_paragraph_shape(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'list_paragraph':
                count = _apply_list_paragraph(hwp, op)
                _record_summary(index, op_type, {'matches': count})
                continue

            if op_type == 'replace_empty_native_list_scaffold':
                result = _apply_replace_empty_native_list_scaffold(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'native_action':
                result = _apply_native_action(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'cursor_replace_text':
                result = _apply_cursor_replace_text(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'cursor_delete_range':
                result = _apply_cursor_delete_range(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'cursor_insert_text':
                result = _apply_cursor_insert_text(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'cursor_snapshot':
                result = _apply_cursor_snapshot(hwp, op)
                snapshots = result.get('snapshots')
                if snapshots:
                    last_cursor_snapshot = snapshots[-1]
                else:
                    last_cursor_snapshot = None
                _record_summary(index, op_type, result)
                continue

            if op_type == 'control_delete_by_anchor':
                result = _apply_control_delete_by_anchor(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'table_cell_action':
                result = _apply_table_cell_action(hwp, op)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'table_cell_clear_text':
                result = _apply_table_cell_clear_text(hwp, op, runtime_snapshot=last_cursor_snapshot)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'table_cell_replace_text':
                result = _apply_table_cell_replace_text(hwp, op, runtime_snapshot=last_cursor_snapshot)
                _record_summary(index, op_type, result)
                continue

            if op_type == 'table_patch_cells':
                result = _apply_table_patch_cells(hwp, op, runtime_snapshot=last_cursor_snapshot)
                _record_summary(index, op_type, result)
                continue

            raise EditOperationError(f'unsupported operation #{index}: {op_type}')
        except Exception as exc:
            failed_op_type = op.get('op') if isinstance(op, dict) else None
            failure_detail = str(exc) or repr(exc)
            _record_step(
                _summary_item(
                    index,
                    str(failed_op_type or 'unknown'),
                    {
                        'state': 'failed',
                        'detail': failure_detail,
                    },
                ),
                operation=op if isinstance(op, dict) else None,
            )
            raise

    return summary
