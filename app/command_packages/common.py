from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping


MACRO_MAX_STRING_CHARS = 10000
BUNDLE_SAFE_HACTION_NAMES = {
    'Cancel',
    'TableCellBlock',
    'TableLeftCell',
    'TableRightCell',
    'TableUpperCell',
    'TableLowerCell',
    'MoveDocBegin',
    'MoveDocEnd',
    'MoveLeft',
    'MoveRight',
    'MoveUp',
    'MoveDown',
}
BUNDLE_SAFE_PYHWPX_CALLS = {
    'CurFieldState',
    'get_pos',
    'GetPos',
    'get_selected_pos',
    'GetSelectedPos',
    'get_selected_text',
    'GetSelectedText',
    'get_text_file',
    'GetTextFile',
    'MoveDocBegin',
    'MoveDocEnd',
    'MoveLeft',
    'MoveRight',
    'MoveUp',
    'MoveDown',
}


def _raise(error_type: type[Exception], message: str) -> None:
    raise error_type(message, status_code=400)


def _clean_optional_text(step: dict[str, Any], index: int, key: str, error_type: type[Exception], *, max_chars: int = 500) -> None:
    if key in step and step.get(key) not in (None, ''):
        value = str(step.get(key) or '').strip()
        if len(value) > max_chars:
            _raise(error_type, f'command-bundle step {index} {key} is too long')
        step[key] = value
    elif key in step:
        step[key] = None


def _require_text(step: dict[str, Any], index: int, op: str, key: str, error_type: type[Exception], *, max_chars: int = 500, strip: bool = True) -> str:
    value = str(step.get(key) or '')
    check_value = value.strip()
    if not check_value:
        _raise(error_type, f'command-bundle step {index} {op} requires {key}')
    if len(value) > max_chars:
        _raise(error_type, f'command-bundle step {index} {key} is too long')
    cleaned = check_value if strip else value
    step[key] = cleaned
    return cleaned


def _validate_positive_int_fields(step: dict[str, Any], index: int, keys: tuple[str, ...], error_type: type[Exception]) -> None:
    for key in keys:
        if key not in step or step.get(key) in (None, ''):
            continue
        value = step.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _raise(error_type, f'command-bundle step {index} {key} must be a positive integer')


def _validate_page_range(step: dict[str, Any], index: int, error_type: type[Exception]) -> None:
    page_from = step.get('page_from')
    page_to = step.get('page_to')
    if page_from is not None and page_to is not None and int(page_to) < int(page_from):
        _raise(error_type, f'command-bundle step {index} page_to must be >= page_from')


def _require_scope(step: dict[str, Any], index: int, op: str, error_type: type[Exception]) -> None:
    if not step.get('section_anchor') and not step.get('page_from'):
        _raise(error_type, f'command-bundle step {index} {op} requires section_anchor or page_from')


def _require_control_identity(step: dict[str, Any], index: int, op: str, error_type: type[Exception]) -> None:
    if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
        _raise(error_type, f'command-bundle step {index} {op} requires exact target_id from inventory')
    if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
        _raise(error_type, f'command-bundle step {index} {op} requires expected_hash from inventory')
    if not step.get('expected_page'):
        _raise(error_type, f'command-bundle step {index} {op} requires expected_page')


def _validate_inventory_like(step: dict[str, Any], index: int, op: str, error_type: type[Exception]) -> dict[str, Any]:
    for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
        _clean_optional_text(step, index, key, error_type)
    _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls', 'resize_up_steps', 'resize_down_steps'), error_type)
    _validate_page_range(step, index, error_type)
    _require_scope(step, index, op, error_type)
    return step


def _validate_scoped_control(step: dict[str, Any], index: int, op: str, error_type: type[Exception]) -> dict[str, Any]:
    for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
        _clean_optional_text(step, index, key, error_type)
    _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls'), error_type)
    _validate_page_range(step, index, error_type)
    _require_scope(step, index, op, error_type)
    _require_control_identity(step, index, op, error_type)
    return step


_FIELD_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_:-]{0,79}$')


def _hash_cell_value(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _validate_native_table_insert(step: dict[str, Any], index: int, error_type: type[Exception]) -> dict[str, Any]:
    if step.get('confirm_native_table') is not True:
        _raise(error_type, f'command-bundle step {index} native_table_insert requires confirm_native_table=true')
    rows = step.get('rows')
    cols = step.get('cols')
    if isinstance(rows, bool) or not isinstance(rows, int) or not (1 <= rows <= 200):
        _raise(error_type, f'command-bundle step {index} native_table_insert rows must be integer 1..200')
    if isinstance(cols, bool) or not isinstance(cols, int) or not (1 <= cols <= 50):
        _raise(error_type, f'command-bundle step {index} native_table_insert cols must be integer 1..50')
    cells = step.get('cells')
    if not isinstance(cells, list) or len(cells) != rows:
        _raise(error_type, f'command-bundle step {index} native_table_insert cells must contain exactly rows lists')
    cleaned_cells: list[list[str]] = []
    for row_index, row in enumerate(cells, start=1):
        if not isinstance(row, list) or len(row) != cols:
            _raise(error_type, f'command-bundle step {index} native_table_insert row {row_index} must contain exactly cols cells')
        cleaned_row: list[str] = []
        for col_index, value in enumerate(row, start=1):
            if not isinstance(value, str):
                _raise(error_type, f'command-bundle step {index} native_table_insert cell R{row_index}C{col_index} must be a string')
            if len(value) > 2000:
                _raise(error_type, f'command-bundle step {index} native_table_insert cell R{row_index}C{col_index} is too long')
            cleaned_row.append(value)
        cleaned_cells.append(cleaned_row)
    flat_values = [value for row in cleaned_cells for value in row]
    non_empty = [value for value in flat_values if value.strip()]
    if not non_empty:
        _raise(error_type, f'command-bundle step {index} native_table_insert rejects all-empty content')

    field_name = str(step.get('field_name') or '').strip()
    if not _FIELD_NAME_RE.fullmatch(field_name):
        _raise(error_type, f'command-bundle step {index} native_table_insert requires a safe field_name')

    if step.get('source_text_deleted') is not False:
        _raise(error_type, f'command-bundle step {index} native_table_insert must keep source_text_deleted=false')
    if step.get('old_plain_text_removal') != 'deferred_until_rendered_proof':
        _raise(error_type, f'command-bundle step {index} native_table_insert old_plain_text_removal must be deferred_until_rendered_proof')

    hashes = step.get('flat_value_hashes')
    expected_hashes = [_hash_cell_value(value) for value in flat_values]
    if hashes != expected_hashes:
        _raise(error_type, f'command-bundle step {index} native_table_insert flat_value_hashes must match the cell values')
    if step.get('non_empty_token_count') != len(non_empty):
        _raise(error_type, f'command-bundle step {index} native_table_insert non_empty_token_count must match the cell values')
    preview = step.get('non_empty_token_preview')
    if preview != non_empty[:8]:
        _raise(error_type, f'command-bundle step {index} native_table_insert non_empty_token_preview must match the cell values')
    warnings = step.get('warnings')
    if warnings is not None and (not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings)):
        _raise(error_type, f'command-bundle step {index} native_table_insert warnings must be a string array')
    if step.get('next_proof_required') not in (None, '') and not isinstance(step.get('next_proof_required'), str):
        _raise(error_type, f'command-bundle step {index} native_table_insert next_proof_required must be text')

    step['rows'] = rows
    step['cols'] = cols
    step['cells'] = cleaned_cells
    step['field_name'] = field_name
    step['flat_value_hashes'] = expected_hashes
    step['non_empty_token_count'] = len(non_empty)
    step['non_empty_token_preview'] = non_empty[:8]
    return step


def validate_step_for_op(op: str, *, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    if op == 'hwp_action':
        action = service._validate_action_name(str(step.get('action_name') or ''))  # noqa: SLF001
        if action not in BUNDLE_SAFE_HACTION_NAMES:
            safe = ', '.join(sorted(BUNDLE_SAFE_HACTION_NAMES))
            _raise(error_type, f'command-bundle step {index} hwp_action {action!r} is not allowed. Safe actions: {safe}')
        step['action_name'] = action
    elif op == 'pyhwpx_call':
        path, _segments = service._validate_macro_path(str(step.get('method_path') or ''))  # noqa: SLF001
        if path not in BUNDLE_SAFE_PYHWPX_CALLS:
            safe = ', '.join(sorted(BUNDLE_SAFE_PYHWPX_CALLS))
            _raise(error_type, f'command-bundle step {index} pyhwpx_call {path!r} is not allowed. Safe paths: {safe}')
        cleaned_args, cleaned_kwargs = service._validate_macro_args(step.get('args') or [], step.get('kwargs') or {})  # noqa: SLF001
        step['method_path'] = path
        step['args'] = cleaned_args
        step['kwargs'] = cleaned_kwargs
    elif op == 'save_document':
        pass
    elif op == 'set_text_file':
        text = step.get('text')
        if not isinstance(text, str) or not text:
            _raise(error_type, f'command-bundle step {index} set_text_file requires non-empty text')
        if len(text) > MACRO_MAX_STRING_CHARS:
            _raise(error_type, f'command-bundle step {index} set_text_file text is too long')
        fmt = str(step.get('format') or 'UNICODE').strip().upper()
        option = str(step.get('option') or 'insertfile').strip().lower()
        if fmt != 'UNICODE' or option != 'insertfile':
            _raise(error_type, f'command-bundle step {index} set_text_file only supports format=UNICODE and option=insertfile')
        step['format'] = fmt
        step['option'] = option
    elif op == 'get_selected_text':
        if 'keep_select' in step and not isinstance(step.get('keep_select'), bool):
            _raise(error_type, f'command-bundle step {index} keep_select must be boolean when provided')
    elif op == 'style_inspect':
        if 'match' in step and step.get('match') not in (None, ''):
            value = str(step.get('match') or '').strip()
            if len(value) > 500:
                _raise(error_type, f'command-bundle step {index} match is too long')
            step['match'] = value
        elif 'match' in step:
            step['match'] = None
        if 'keep_position' in step and not isinstance(step.get('keep_position'), bool):
            _raise(error_type, f'command-bundle step {index} keep_position must be boolean when provided')
    elif op == 'paragraph_style_apply_exact':
        value = str(step.get('match') or '').strip()
        if not value:
            _raise(error_type, f'command-bundle step {index} paragraph_style_apply_exact requires match')
        if len(value) > 500:
            _raise(error_type, f'command-bundle step {index} match is too long')
        step['match'] = value
        expected_page = step.get('expected_page')
        if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page <= 0:
            _raise(error_type, f'command-bundle step {index} paragraph_style_apply_exact requires positive integer expected_page')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} paragraph_style_apply_exact requires confirm_layout=true')
        for bool_key in ('keep_with_next', 'widow_orphan'):
            if bool_key in step and step.get(bool_key) is not None and not isinstance(step.get(bool_key), bool):
                _raise(error_type, f'command-bundle step {index} {bool_key} must be boolean')
        if 'pagebreak_before' in step and step.get('pagebreak_before') is not None:
            pagebreak_before = step.get('pagebreak_before')
            if isinstance(pagebreak_before, bool) or not isinstance(pagebreak_before, int) or int(pagebreak_before) not in (0, 1):
                _raise(error_type, f'command-bundle step {index} pagebreak_before must be 0 or 1')
    elif op == 'paragraph_delete_exact':
        value = str(step.get('match') or '').strip()
        if not value:
            _raise(error_type, f'command-bundle step {index} paragraph_delete_exact requires match')
        if len(value) > 500:
            _raise(error_type, f'command-bundle step {index} match is too long')
        step['match'] = value
        expected_page = step.get('expected_page')
        if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page <= 0:
            _raise(error_type, f'command-bundle step {index} paragraph_delete_exact requires positive integer expected_page')
        occurrence = int(step.get('occurrence_on_page') or 1)
        if not (1 <= occurrence <= 200):
            _raise(error_type, f'command-bundle step {index} occurrence_on_page must be 1..200')
        step['occurrence_on_page'] = occurrence
        for guard_key in ('expected_previous_contains', 'expected_next_contains'):
            if step.get(guard_key) not in (None, ''):
                guard = str(step.get(guard_key) or '').strip()
                if len(guard) > 500:
                    _raise(error_type, f'command-bundle step {index} {guard_key} is too long')
                step[guard_key] = guard
            elif guard_key in step:
                step[guard_key] = None
        max_page = step.get('max_page_after')
        if max_page not in (None, ''):
            if isinstance(max_page, bool) or not isinstance(max_page, int) or max_page <= 0:
                _raise(error_type, f'command-bundle step {index} max_page_after must be a positive integer')
        if step.get('confirm_remove') is not True:
            _raise(error_type, f'command-bundle step {index} paragraph_delete_exact requires confirm_remove=true')
    elif op in {'paragraph_join_previous_exact', 'paragraph_join_next_exact'}:
        value = str(step.get('match') or '').strip()
        if not value:
            _raise(error_type, f'command-bundle step {index} {op} requires match')
        if len(value) > 500:
            _raise(error_type, f'command-bundle step {index} match is too long')
        step['match'] = value
        expected_page = step.get('expected_page')
        if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page <= 0:
            _raise(error_type, f'command-bundle step {index} {op} requires positive integer expected_page')
        count_key = 'delete_back_count' if op == 'paragraph_join_previous_exact' else 'delete_count'
        count_value = int(step.get(count_key) or 1)
        if not (1 <= count_value <= 5):
            _raise(error_type, f'command-bundle step {index} {count_key} must be 1..5')
        step[count_key] = count_value
        page_key = 'max_page_after' if op == 'paragraph_join_previous_exact' else 'max_next_page_after'
        max_page = step.get(page_key)
        if max_page not in (None, ''):
            if isinstance(max_page, bool) or not isinstance(max_page, int) or max_page <= 0:
                _raise(error_type, f'command-bundle step {index} {page_key} must be a positive integer')
        guard_key = 'expected_previous_contains' if op == 'paragraph_join_previous_exact' else 'expected_next_contains'
        if step.get(guard_key) not in (None, ''):
            guard = str(step.get(guard_key) or '').strip()
            if len(guard) > 500:
                _raise(error_type, f'command-bundle step {index} {guard_key} is too long')
            step[guard_key] = guard
        elif guard_key in step:
            step[guard_key] = None
        if op == 'paragraph_join_next_exact' and step.get('next_match') not in (None, ''):
            nxt = str(step.get('next_match') or '').strip()
            if len(nxt) > 500:
                _raise(error_type, f'command-bundle step {index} next_match is too long')
            step['next_match'] = nxt
        elif op == 'paragraph_join_next_exact' and 'next_match' in step:
            step['next_match'] = None
        if op == 'paragraph_join_next_exact' and 'insert_line_break' in step and not isinstance(step.get('insert_line_break'), bool):
            _raise(error_type, f'command-bundle step {index} insert_line_break must be boolean')
        if op == 'paragraph_join_next_exact' and 'move_to_line_end' in step and not isinstance(step.get('move_to_line_end'), bool):
            _raise(error_type, f'command-bundle step {index} move_to_line_end must be boolean')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} {op} requires confirm_layout=true')
    elif op in {'control_inventory', 'table_frame_inventory'}:
        _validate_inventory_like(step, index, op, error_type)
    elif op == 'control_join_previous_exact':
        for key in ('target_id', 'expected_hash'):
            _require_text(step, index, 'control_join_previous_exact', key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls', 'delete_back_count', 'max_page_after'), error_type)
        _validate_page_range(step, index, error_type)
        if not str(step.get('target_id')).startswith('ctrl/'):
            _raise(error_type, f'command-bundle step {index} control_join_previous_exact requires exact target_id from inventory')
        if not str(step.get('expected_hash')).startswith('sha256:'):
            _raise(error_type, f'command-bundle step {index} control_join_previous_exact requires expected_hash from inventory')
        if not step.get('expected_page'):
            _raise(error_type, f'command-bundle step {index} control_join_previous_exact requires expected_page')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} control_join_previous_exact requires confirm_layout=true')
        count_value = int(step.get('delete_back_count') or 1)
        if not (1 <= count_value <= 5):
            _raise(error_type, f'command-bundle step {index} delete_back_count must be 1..5')
        step['delete_back_count'] = count_value
    elif op == 'paragraph_rehome_exact':
        for key in ('delete_match', 'insert_before_match', 'insert_text'):
            _require_text(step, index, 'paragraph_rehome_exact', key, error_type, max_chars=1000, strip=False)
        for key in ('delete_expected_page', 'insert_expected_page'):
            value = step.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                _raise(error_type, f'command-bundle step {index} {key} must be a positive integer')
        if step.get('expected_text_delta') not in (None, 0):
            _raise(error_type, f'command-bundle step {index} paragraph_rehome_exact expected_text_delta must be 0')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} paragraph_rehome_exact requires confirm_layout=true')
    elif op == 'control_delete_exact':
        _validate_scoped_control(step, index, op, error_type)
        if step.get('confirm_remove') is not True:
            _raise(error_type, f'command-bundle step {index} control_delete_exact requires confirm_remove=true')
    elif op in {'exact_control_select_proof', 'table_cell_structure_exact'}:
        _validate_scoped_control(step, index, op, error_type)
    elif op == 'cell_format_exact':
        for key in ('section_anchor', 'around', 'target_id', 'expected_hash', 'vertical_align'):
            _clean_optional_text(step, index, key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls'), error_type)
        for key in ('cell_margin_hu', 'cell_margin_mm'):
            if key not in step or step.get(key) in (None, ''):
                continue
            value = step.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _raise(error_type, f'command-bundle step {index} {key} must be numeric')
            step[key] = float(value)
        _validate_page_range(step, index, error_type)
        _require_scope(step, index, op, error_type)
        _require_control_identity(step, index, op, error_type)
        selectors = [key for key in ('cell_margin_hu', 'cell_margin_mm', 'vertical_align') if step.get(key) is not None]
        if len(selectors) != 1:
            _raise(error_type, f'command-bundle step {index} cell_format_exact requires exactly one format selector')
        if step.get('cell_margin_hu') is not None and not (0.0 <= float(step.get('cell_margin_hu')) <= 20000.0):
            _raise(error_type, f'command-bundle step {index} cell_margin_hu out of safe range')
        if step.get('cell_margin_mm') is not None and not (0.0 <= float(step.get('cell_margin_mm')) <= 70.0):
            _raise(error_type, f'command-bundle step {index} cell_margin_mm out of safe range')
        if step.get('vertical_align') is not None and step.get('vertical_align') not in {'top', 'center', 'middle', 'bottom'}:
            _raise(error_type, f'command-bundle step {index} vertical_align must be top, center, middle, or bottom')
        if step.get('vertical_align') == 'middle':
            step['vertical_align'] = 'center'
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} cell_format_exact requires confirm_layout=true')
    elif op == 'table_split_exact':
        for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
            _clean_optional_text(step, index, key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'down_rows', 'max_controls'), error_type)
        _validate_page_range(step, index, error_type)
        _require_scope(step, index, op, error_type)
        _require_control_identity(step, index, op, error_type)
        if not (1 <= int(step.get('down_rows') or 0) <= 200):
            _raise(error_type, f'command-bundle step {index} table_split_exact requires down_rows 1..200')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} table_split_exact requires confirm_layout=true')
    elif op == 'table_column_width_exact':
        for key in ('section_anchor', 'around', 'target_id', 'expected_hash', 'expected_preimage_sha256', 'expected_cell_inventory_hash', 'expected_document_text_hash', 'expected_bindata_manifest_hash'):
            _clean_optional_text(step, index, key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'expected_text_char_count', 'expected_nonempty_line_count', 'expected_rows', 'expected_cols', 'expected_control_count', 'max_controls'), error_type)
        div0_count = step.get('expected_div0_count')
        if isinstance(div0_count, bool) or not isinstance(div0_count, int) or div0_count < 0:
            _raise(error_type, f'command-bundle step {index} expected_div0_count must be a non-negative integer')
        _validate_page_range(step, index, error_type)
        _require_scope(step, index, op, error_type)
        _require_control_identity(step, index, op, error_type)
        widths = step.get('requested_widths_mm')
        if not isinstance(widths, list) or not widths:
            _raise(error_type, f'command-bundle step {index} table_column_width_exact requires requested_widths_mm')
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0 for value in widths):
            _raise(error_type, f'command-bundle step {index} requested_widths_mm must contain positive numbers')
        expected_cols = step.get('expected_cols')
        if isinstance(expected_cols, int) and expected_cols > 0 and len(widths) != expected_cols:
            _raise(error_type, f'command-bundle step {index} requested_widths_mm must match expected_cols')
        for key in ('expected_total_width_mm', 'expected_table_height_mm'):
            value = step.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0:
                _raise(error_type, f'command-bundle step {index} {key} must be a positive number')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} table_column_width_exact requires confirm_layout=true')
    elif op == 'control_move_resize_exact':
        for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
            _clean_optional_text(step, index, key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls'), error_type)
        for key in ('scale_percent', 'move_dx_mm', 'move_dy_mm'):
            if key not in step or step.get(key) in (None, ''):
                continue
            value = step.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _raise(error_type, f'command-bundle step {index} {key} must be numeric')
            step[key] = float(value)
        _validate_page_range(step, index, error_type)
        _require_scope(step, index, op, error_type)
        _require_control_identity(step, index, op, error_type)
        scale_percent = step.get('scale_percent')
        move_dx_mm = float(step.get('move_dx_mm') or 0.0)
        move_dy_mm = float(step.get('move_dy_mm') or 0.0)
        if scale_percent is None and move_dx_mm == 0.0 and move_dy_mm == 0.0:
            _raise(error_type, f'command-bundle step {index} control_move_resize_exact requires scale_percent or non-zero move delta')
        if scale_percent is not None and not (5.0 <= float(scale_percent) <= 200.0):
            _raise(error_type, f'command-bundle step {index} scale_percent must be 5..200')
        if abs(move_dx_mm) > 300.0 or abs(move_dy_mm) > 300.0:
            _raise(error_type, f'command-bundle step {index} move deltas must be within +/-300mm')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} control_move_resize_exact requires confirm_layout=true')
    elif op == 'cell_row_fit_exact':
        for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
            _clean_optional_text(step, index, key, error_type)
        _validate_positive_int_fields(step, index, ('page_from', 'page_to', 'expected_page', 'max_controls'), error_type)
        for key in ('row_height_percent', 'row_height_hu', 'row_height_mm', 'char_height_percent'):
            if key not in step or step.get(key) in (None, ''):
                continue
            value = step.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _raise(error_type, f'command-bundle step {index} {key} must be numeric')
            step[key] = float(value)
        _validate_page_range(step, index, error_type)
        _require_scope(step, index, op, error_type)
        _require_control_identity(step, index, op, error_type)
        selectors = [key for key in ('row_height_percent', 'row_height_hu', 'row_height_mm', 'resize_up_steps', 'resize_down_steps', 'line_spacing', 'char_height_percent') if step.get(key) is not None]
        if len(selectors) != 1:
            _raise(error_type, f'command-bundle step {index} cell_row_fit_exact requires exactly one row height selector')
        if step.get('row_height_percent') is not None and not (20.0 <= float(step.get('row_height_percent')) <= 120.0):
            _raise(error_type, f'command-bundle step {index} row_height_percent must be 20..120')
        if step.get('row_height_hu') is not None and not (1000.0 <= float(step.get('row_height_hu')) <= 200000.0):
            _raise(error_type, f'command-bundle step {index} row_height_hu out of safe range')
        if step.get('row_height_mm') is not None and not (3.0 <= float(step.get('row_height_mm')) <= 700.0):
            _raise(error_type, f'command-bundle step {index} row_height_mm out of safe range')
        for key in ('resize_up_steps', 'resize_down_steps'):
            if step.get(key) is not None and not (1 <= int(step.get(key)) <= 200):
                _raise(error_type, f'command-bundle step {index} {key} must be 1..200')
        if step.get('line_spacing') is not None:
            value = step.get('line_spacing')
            if isinstance(value, bool) or not isinstance(value, int) or not (80 <= value <= 200):
                _raise(error_type, f'command-bundle step {index} line_spacing must be integer 80..200')
        if step.get('char_height_percent') is not None and not (70.0 <= float(step.get('char_height_percent')) <= 110.0):
            _raise(error_type, f'command-bundle step {index} char_height_percent must be 70..110')
        if step.get('confirm_layout') is not True:
            _raise(error_type, f'command-bundle step {index} cell_row_fit_exact requires confirm_layout=true')
    elif op == 'native_table_insert':
        _validate_native_table_insert(step, index, error_type)
    elif op in {'control_inventory', 'table_frame_inventory', 'export_pdf', 'where'}:
        pass
    else:
        pass
    return step


def run_step_for_op(op: str, *, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    from app.local_cli_runtime import LocalCliRuntimeError, export_document_pdf, insert_native_table_at_cursor, save_document, snapshot_live_location

    warnings: list[str] = []
    if op == 'control_inventory':
        result = service._bundle_control_inventory(handle.hwp, step)  # noqa: SLF001
        return result, False, list(result.get('warnings') or [])
    if op == 'table_frame_inventory':
        result = service._bundle_table_frame_inventory(handle.hwp, step)  # noqa: SLF001
        return result, False, list(result.get('warnings') or [])
    if op == 'control_delete_exact':
        result = service._bundle_control_delete_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'exact_control_select_proof':
        result = service._bundle_exact_control_select_proof(handle.hwp, step)  # noqa: SLF001
        return result, False, list(result.get('warnings') or [])
    if op == 'table_cell_structure_exact':
        result = service._bundle_table_cell_structure_exact(handle.hwp, step)  # noqa: SLF001
        return result, False, list(result.get('warnings') or [])
    if op == 'paragraph_rehome_exact':
        result = service._bundle_paragraph_rehome_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'control_join_previous_exact':
        result = service._bundle_control_join_previous_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'control_move_resize_exact':
        result = service._bundle_control_move_resize_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'cell_row_fit_exact':
        result = service._bundle_cell_row_fit_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'table_split_exact':
        result = service._bundle_table_split_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'table_column_width_exact':
        result = service._bundle_table_column_width_exact(handle, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'cell_format_exact':
        result = service._bundle_cell_format_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'native_table_insert':
        result = insert_native_table_at_cursor(
            handle.hwp,
            rows=int(step['rows']),
            cols=int(step['cols']),
            cells=step['cells'],
            field_name=str(step['field_name']),
        )
        flat_values = [value for row in step['cells'] for value in row]
        result.update(
            {
                'cells': step['cells'],
                'flat_value_hashes': step.get('flat_value_hashes'),
                'non_empty_token_count': step.get('non_empty_token_count'),
                'non_empty_token_preview': step.get('non_empty_token_preview'),
                'flat_value_count': len(flat_values),
                'next_proof_required': step.get('next_proof_required'),
                'split_by_column': step.get('split_by_column'),
                'split_column_index': step.get('split_column_index'),
                'split_group_value': step.get('split_group_value'),
                'split_group_index': step.get('split_group_index'),
                'split_group_count': step.get('split_group_count'),
                'split_group_hash': step.get('split_group_hash'),
                'snapshot': service._bundle_compact_snapshot(handle.hwp),  # noqa: SLF001
            }
        )
        warnings.extend(str(item) for item in step.get('warnings') or [])
        warnings.extend(str(item) for item in result.get('warnings') or [])
        result['warnings'] = warnings
        return result, True, warnings
    if op == 'style_inspect':
        result = service._bundle_style_inspect(handle.hwp, step)  # noqa: SLF001
        return result, False, list(result.get('warnings') or [])
    if op == 'paragraph_style_apply_exact':
        result = service._bundle_paragraph_style_apply_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'paragraph_delete_exact':
        result = service._bundle_paragraph_delete_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'paragraph_join_previous_exact':
        result = service._bundle_paragraph_join_previous_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'paragraph_join_next_exact':
        result = service._bundle_paragraph_join_next_exact(handle.hwp, step)  # noqa: SLF001
        return result, True, list(result.get('warnings') or [])
    if op == 'export_pdf':
        artifact_path = export_document_pdf(
            session_root=handle.session_root,
            source_filename=handle.source_filename,
            hwp=handle.hwp,
            log_path=handle.log_path,
        )
        return {
            'artifact_kind': 'export',
            'artifact_path': str(artifact_path),
            'filename': service._artifact_name(kind='export', source_filename=handle.source_filename),  # noqa: SLF001
            'download_path': service._artifact_download_path(session_id=handle.session_id, kind='export'),  # noqa: SLF001
        }, False, warnings
    if op == 'hwp_action':
        action = service._validate_action_name(str(step.get('action_name') or ''))  # noqa: SLF001
        if action not in BUNDLE_SAFE_HACTION_NAMES:
            safe = ', '.join(sorted(BUNDLE_SAFE_HACTION_NAMES))
            raise LocalCliRuntimeError(f'hwp_action {action!r} is not allowed in command-bundle. Safe actions: {safe}')
        run = getattr(getattr(handle.hwp, 'HAction', None), 'Run', None)
        if not callable(run):
            raise LocalCliRuntimeError('HAction.Run is unavailable on this machine')
        raw_result = run(action)
        succeeded = raw_result is None or bool(raw_result)
        warnings.append('hwp_action is allowlisted for navigation/selection only; destructive actions such as Delete/Erase are rejected.')
        return {
            'action': action,
            'succeeded': succeeded,
            'result_type': type(raw_result).__name__,
            'result_preview': service._macro_result_preview(raw_result),  # noqa: SLF001
            'snapshot': service._bundle_compact_snapshot(handle.hwp),  # noqa: SLF001
        }, False, warnings
    if op == 'pyhwpx_call':
        path, segments = service._validate_macro_path(str(step.get('method_path') or ''))  # noqa: SLF001
        if path not in BUNDLE_SAFE_PYHWPX_CALLS:
            safe = ', '.join(sorted(BUNDLE_SAFE_PYHWPX_CALLS))
            raise LocalCliRuntimeError(f'pyhwpx_call {path!r} is not allowed in command-bundle. Safe paths: {safe}')
        cleaned_args, cleaned_kwargs = service._validate_macro_args(step.get('args') or [], step.get('kwargs') or {})  # noqa: SLF001
        leaf = service._resolve_public_macro_leaf(handle.hwp, segments)  # noqa: SLF001
        mode = 'call' if callable(leaf) else 'property'
        if mode == 'property' and (cleaned_args or cleaned_kwargs):
            raise LocalCliRuntimeError('pyhwpx_call property access does not accept args or kwargs')
        raw_result = leaf(*cleaned_args, **cleaned_kwargs) if callable(leaf) else leaf
        if not (raw_result is None or isinstance(raw_result, (bool, int, float, str, list, tuple, dict))):
            raise LocalCliRuntimeError('pyhwpx_call result is not JSON-previewable')
        return {
            'path': path,
            'mode': mode,
            'result_type': type(raw_result).__name__,
            'result_preview': service._macro_result_preview(raw_result),  # noqa: SLF001
            'snapshot': service._bundle_compact_snapshot(handle.hwp),  # noqa: SLF001
        }, False, warnings
    if op == 'save_document':
        save_document(handle.hwp)
        return {
            'schema_version': 'local-cli/save-document/v1',
            'read_only': False,
            'mutation': 'save-document',
            'snapshot': service._bundle_compact_snapshot(handle.hwp),  # noqa: SLF001
        }, False, warnings
    if op == 'set_text_file':
        text = service._bundle_require_text(step, 'text')  # noqa: SLF001
        fmt = str(step.get('format') or 'UNICODE').strip().upper()
        option = str(step.get('option') or 'insertfile').strip().lower()
        if fmt != 'UNICODE' or option != 'insertfile':
            raise LocalCliRuntimeError('set_text_file command-bundle op only supports format=UNICODE and option=insertfile')
        before_snapshot = service._bundle_compact_snapshot(handle.hwp)  # noqa: SLF001
        strategy = service._insert_text_file_at_caret(handle.hwp, text=text, session_root=handle.session_root)  # noqa: SLF001
        after_snapshot = service._bundle_compact_snapshot(handle.hwp)  # noqa: SLF001
        raw_target_readback, readback_warnings = service._capture_set_text_file_target_readback(  # noqa: SLF001
            handle.hwp,
            session_root=handle.session_root,
            intended_text=text,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            insert_strategy=strategy,
            fail_on_mismatch=None,
        )
        warnings.extend(readback_warnings)
        return {
            'text_len': len(text),
            'text_hash': service._text_proof_hash(text),  # noqa: SLF001
            'strategy': strategy,
            'raw_target_readback': raw_target_readback,
            'snapshot': service._bundle_compact_snapshot(handle.hwp),  # noqa: SLF001
        }, True, warnings
    if op == 'get_selected_text':
        keep_select = step.get('keep_select') is not False
        result = service._capture_selected_text_proof_for_bundle(handle.hwp, keep_select=keep_select, selection_cache=binding)  # noqa: SLF001
        warnings.extend(str(item) for item in result.get('warnings') or [])
        return result, False, warnings
    if op == 'where':
        location = snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
        )
        return {'location': service._bundle_compact_location(location)}, False, warnings  # noqa: SLF001
    raise LocalCliRuntimeError(f'Unsupported command-bundle op: {op}')
