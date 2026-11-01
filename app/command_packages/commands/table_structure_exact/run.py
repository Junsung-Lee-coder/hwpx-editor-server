from __future__ import annotations

from typing import Any, Mapping

from app.command_packages.common import (
    _clean_optional_text,
    _raise,
    _require_control_identity,
    _require_scope,
    _validate_page_range,
    _validate_positive_int_fields,
)
from app.table_structure import ACTIONS, MAX_COUNT, MAX_GRID, MAX_SPLIT, OP, ROW_COL_ACTIONS


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    for key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
        _clean_optional_text(step, index, key, error_type)
    _validate_positive_int_fields(
        step,
        index,
        ('page_from', 'page_to', 'expected_page', 'max_controls', 'row', 'col', 'end_row', 'end_col', 'count', 'split_rows', 'split_cols', 'expected_rows', 'expected_cols'),
        error_type,
    )
    _validate_page_range(step, index, error_type)
    _require_scope(step, index, OP, error_type)
    _require_control_identity(step, index, OP, error_type)
    action = str(step.get('action') or '').strip()
    if action not in ACTIONS:
        _raise(error_type, f'command-bundle step {index} {OP} action must be one of: {", ".join(sorted(ACTIONS))}')
    step['action'] = action
    for key in ('row', 'col', 'expected_rows', 'expected_cols'):
        if not step.get(key):
            _raise(error_type, f'command-bundle step {index} {OP} requires {key}')
    for key in ('row', 'col', 'end_row', 'end_col', 'expected_rows', 'expected_cols'):
        if step.get(key) and int(step[key]) > MAX_GRID:
            _raise(error_type, f'command-bundle step {index} {key} must be <= {MAX_GRID}')
    allowed_extra = {
        'merge_cells': {'end_row', 'end_col'},
        'split_cell': {'split_rows', 'split_cols'},
    }.get(action, {'count'} if action in ROW_COL_ACTIONS else set())
    stray = sorted(key for key in ('end_row', 'end_col', 'count', 'split_rows', 'split_cols') if step.get(key) and key not in allowed_extra)
    if stray:
        _raise(error_type, f'command-bundle step {index} {OP} action {action} does not take: {", ".join(stray)}')
    if action in ROW_COL_ACTIONS and not (1 <= int(step.get('count') or 1) <= MAX_COUNT):
        _raise(error_type, f'command-bundle step {index} count must be 1..{MAX_COUNT}')
    if action == 'merge_cells' and not (step.get('end_row') and step.get('end_col')):
        _raise(error_type, f'command-bundle step {index} merge_cells requires end_row and end_col')
    if action == 'split_cell':
        split_rows = int(step.get('split_rows') or 1)
        split_cols = int(step.get('split_cols') or 1)
        if split_rows > MAX_SPLIT or split_cols > MAX_SPLIT or split_rows * split_cols < 2:
            _raise(error_type, f'command-bundle step {index} split_cell needs split_rows/split_cols 1..{MAX_SPLIT} with a product >= 2')
    if step.get('confirm_layout') is not True:
        _raise(error_type, f'command-bundle step {index} {OP} requires confirm_layout=true')
    return step


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._bundle_table_structure_exact(handle.hwp, step)  # noqa: SLF001
    return result, True, list(result.get('warnings') or [])
