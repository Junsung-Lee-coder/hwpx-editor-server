"""Pure planning and verification for exact table-structure edits.

Nothing here touches Hancom. The service mixin reads the table grid through
HWPML (the same ``SelectCtrlFront`` + ``GetTextFile('HWPML2X', 'saveblock')``
path pyhwpx uses for ``get_row_num``/``get_col_num``), and these helpers
decide whether an edit may run and whether the observed grid proves it did
exactly what was asked.

Cell coordinates on the public surface are 1-based ``row``/``col``, matching
Hancom's ``A1`` addresses (``A1`` is row 1, col 1).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Mapping

OP = 'table_structure_exact'
SCHEMA_VERSION = 'local-cli/table-structure-exact/v1'

# Hancom action ids per public action. Insert/delete repeat ``count`` times.
ROW_COL_ACTIONS: dict[str, str] = {
    'insert_row_above': 'TableInsertUpperRow',
    'insert_row_below': 'TableInsertLowerRow',
    'insert_col_left': 'TableInsertLeftColumn',
    'insert_col_right': 'TableInsertRightColumn',
    'delete_row': 'TableDeleteRow',
    'delete_col': 'TableDeleteColumn',
}
ACTIONS = frozenset({*ROW_COL_ACTIONS, 'merge_cells', 'split_cell'})

MAX_COUNT = 50
MAX_SPLIT = 20
MAX_GRID = 500


class TableStructureError(ValueError):
    """A plan or readback cannot be accepted."""


def column_letters(col: int) -> str:
    """1-based column number to Hancom letters (1 -> A, 27 -> AA)."""
    if col < 1:
        raise TableStructureError(f'column must be >= 1, got {col}')
    letters = ''
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord('A') + rem) + letters
    return letters


def cell_address(row: int, col: int) -> str:
    return f'{column_letters(col)}{row}'


def parse_cell_address(value: Any) -> tuple[int, int] | None:
    """``"B3"`` -> (row 3, col 2), 1-based. Returns None for anything else."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*([A-Za-z]+)(\d+)\s*', value)
    if not match:
        return None
    col = 0
    for char in match.group(1).upper():
        col = col * 26 + (ord(char) - ord('A') + 1)
    row = int(match.group(2))
    if row < 1 or col < 1:
        return None
    return row, col


def _int_attr(element: ET.Element, name: str) -> int | None:
    raw = element.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_table_grid_xml(xml_text: str) -> dict[str, Any]:
    """Read RowCount/ColCount and top-level cells of the first TABLE in HWPML.

    Only ``TABLE > ROW > CELL`` children of the outer table are counted, so
    tables nested inside cells do not leak into the grid. ``cells`` is None
    when the cell address/span attributes are missing, which makes
    cell-level verification (merge/split) fail closed.
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise TableStructureError('table readback returned no HWPML text')
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', xml_text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TableStructureError(f'table readback is not well-formed HWPML: {exc}') from exc
    table = root if root.tag == 'TABLE' else root.find('.//TABLE')
    if table is None:
        raise TableStructureError('table readback has no TABLE element')
    rows = _int_attr(table, 'RowCount')
    cols = _int_attr(table, 'ColCount')
    if rows is None or cols is None or rows < 1 or cols < 1:
        raise TableStructureError('table readback has no positive RowCount/ColCount')
    cells: list[dict[str, int]] | None = []
    for row_element in table.findall('ROW'):
        for cell in row_element.findall('CELL'):
            row_addr = _int_attr(cell, 'RowAddr')
            col_addr = _int_attr(cell, 'ColAddr')
            if row_addr is None or col_addr is None:
                cells = None
                break
            cells.append({
                'row': row_addr + 1,
                'col': col_addr + 1,
                'row_span': _int_attr(cell, 'RowSpan') or 1,
                'col_span': _int_attr(cell, 'ColSpan') or 1,
            })
        if cells is None:
            break
    if cells is not None:
        cells.sort(key=lambda item: (item['row'], item['col']))
    return {
        'rows': rows,
        'cols': cols,
        'cell_count': None if cells is None else len(cells),
        'cells': cells,
    }


def _cell_at(grid: Mapping[str, Any], row: int, col: int) -> dict[str, int] | None:
    for cell in grid.get('cells') or []:
        if cell['row'] == row and cell['col'] == col:
            return cell
    return None


def _covering_cell(grid: Mapping[str, Any], row: int, col: int) -> dict[str, int] | None:
    for cell in grid.get('cells') or []:
        if cell['row'] <= row < cell['row'] + cell['row_span'] and cell['col'] <= col < cell['col'] + cell['col_span']:
            return cell
    return None


def check_plan(step: Mapping[str, Any], before: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a step against the live grid before any mutation.

    Raises TableStructureError with a caller-facing reason; returns the
    normalized plan otherwise.
    """
    action = step['action']
    rows, cols = int(before['rows']), int(before['cols'])
    if rows != int(step['expected_rows']) or cols != int(step['expected_cols']):
        raise TableStructureError(
            f'table grid is {rows}x{cols} but the step expects {step["expected_rows"]}x{step["expected_cols"]}; re-inventory before editing'
        )
    row, col = int(step['row']), int(step['col'])
    if not (1 <= row <= rows and 1 <= col <= cols):
        raise TableStructureError(f'target cell {cell_address(row, col)} is outside the {rows}x{cols} table')
    plan: dict[str, Any] = {'action': action, 'row': row, 'col': col, 'address': cell_address(row, col)}
    count = int(step.get('count') or 1)
    if action in ROW_COL_ACTIONS:
        plan['count'] = count
        plan['hancom_action'] = ROW_COL_ACTIONS[action]
        if action.startswith('insert_row') and rows + count > MAX_GRID:
            raise TableStructureError(f'inserting {count} row(s) would exceed {MAX_GRID} rows')
        if action.startswith('insert_col') and cols + count > MAX_GRID:
            raise TableStructureError(f'inserting {count} column(s) would exceed {MAX_GRID} columns')
        if action == 'delete_row':
            if row + count - 1 > rows:
                raise TableStructureError(f'deleting {count} row(s) from row {row} runs past the last row {rows}')
            if rows - count < 1:
                raise TableStructureError('refusing to delete every row; delete the table as a control instead')
        if action == 'delete_col':
            if col + count - 1 > cols:
                raise TableStructureError(f'deleting {count} column(s) from column {col} runs past the last column {cols}')
            if cols - count < 1:
                raise TableStructureError('refusing to delete every column; delete the table as a control instead')
        return plan
    if before.get('cells') is None:
        raise TableStructureError(f'{action} needs cell-level readback (RowAddr/ColAddr), which this table did not report')
    if action == 'merge_cells':
        end_row, end_col = int(step['end_row']), int(step['end_col'])
        if not (row <= end_row <= rows and col <= end_col <= cols):
            raise TableStructureError(
                f'merge range {cell_address(row, col)}:{cell_address(end_row, end_col)} must run down/right inside the {rows}x{cols} table'
            )
        area = (end_row - row + 1) * (end_col - col + 1)
        if area < 2:
            raise TableStructureError('merge range must cover at least two cells')
        for r in range(row, end_row + 1):
            for c in range(col, end_col + 1):
                cell = _cell_at(before, r, c)
                if cell is None or cell['row_span'] != 1 or cell['col_span'] != 1:
                    raise TableStructureError(
                        f'merge range contains an already merged cell at {cell_address(r, c)}; only unmerged cells can be merged exactly'
                    )
        plan.update({'end_row': end_row, 'end_col': end_col, 'end_address': cell_address(end_row, end_col), 'area': area})
        return plan
    # split_cell
    split_rows, split_cols = int(step.get('split_rows') or 1), int(step.get('split_cols') or 1)
    if split_rows * split_cols < 2:
        raise TableStructureError('split_cell needs split_rows * split_cols >= 2')
    cell = _cell_at(before, row, col)
    if cell is None:
        owner = _covering_cell(before, row, col)
        where = cell_address(owner['row'], owner['col']) if owner else 'no cell'
        raise TableStructureError(f'{cell_address(row, col)} is covered by merged cell {where}; target its top-left address')
    if cell['row_span'] != 1 or cell['col_span'] != 1:
        raise TableStructureError(f'{cell_address(row, col)} is a merged cell; split_cell only accepts an unmerged cell')
    plan.update({'split_rows': split_rows, 'split_cols': split_cols})
    return plan


def evaluate_change(plan: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare before/after grids with what the plan requires.

    ``ok`` is True only when every expectation holds; ``reasons`` lists the
    ones that did not.
    """
    action = plan['action']
    expected: dict[str, Any] = {'rows': before['rows'], 'cols': before['cols']}
    count = int(plan.get('count') or 0)
    if action.startswith('insert_row'):
        expected['rows'] = before['rows'] + count
    elif action.startswith('insert_col'):
        expected['cols'] = before['cols'] + count
    elif action == 'delete_row':
        expected['rows'] = before['rows'] - count
    elif action == 'delete_col':
        expected['cols'] = before['cols'] - count
    reasons: list[str] = []
    for key in ('rows', 'cols'):
        if action == 'split_cell':
            # Splitting can add grid lines; it must never remove them.
            if after[key] < before[key]:
                reasons.append(f'{key} shrank from {before[key]} to {after[key]}')
        elif after[key] != expected[key]:
            reasons.append(f'{key} is {after[key]}, expected {expected[key]}')
    if action == 'merge_cells':
        expected['cell_count'] = before['cell_count'] - (plan['area'] - 1)
        expected['merged_cell'] = {
            'row': plan['row'],
            'col': plan['col'],
            'row_span': plan['end_row'] - plan['row'] + 1,
            'col_span': plan['end_col'] - plan['col'] + 1,
        }
        if after.get('cells') is None:
            reasons.append('post-merge readback has no cell-level data')
        else:
            if after['cell_count'] != expected['cell_count']:
                reasons.append(f'cell_count is {after["cell_count"]}, expected {expected["cell_count"]}')
            merged = _cell_at(after, plan['row'], plan['col'])
            if merged is None or {k: merged[k] for k in ('row_span', 'col_span')} != {
                'row_span': expected['merged_cell']['row_span'],
                'col_span': expected['merged_cell']['col_span'],
            }:
                reasons.append(f'{plan["address"]} does not span the requested range after merge: {merged!r}')
    elif action == 'split_cell':
        expected['cell_count'] = before['cell_count'] + plan['split_rows'] * plan['split_cols'] - 1
        if after.get('cells') is None:
            reasons.append('post-split readback has no cell-level data')
        elif after['cell_count'] != expected['cell_count']:
            reasons.append(f'cell_count is {after["cell_count"]}, expected {expected["cell_count"]}')
    return {
        'ok': not reasons,
        'expected': expected,
        'observed': {'rows': after['rows'], 'cols': after['cols'], 'cell_count': after.get('cell_count')},
        'reasons': reasons,
    }
