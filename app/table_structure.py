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

import hashlib
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
                # Whitespace-normalized text: the per-cell content fingerprint
                # that tells which rows/columns actually moved or vanished.
                'text': ' '.join(''.join(cell.itertext()).split()),
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


def public_grid(grid: Mapping[str, Any]) -> dict[str, Any]:
    """Grid for responses and logs: cell text replaced by a short hash and length."""
    cells = grid.get('cells')
    return {
        'rows': grid['rows'],
        'cols': grid['cols'],
        'cell_count': grid.get('cell_count'),
        'cells': None if cells is None else [
            {
                **{key: cell[key] for key in ('row', 'col', 'row_span', 'col_span')},
                'text_sha256': hashlib.sha256(cell['text'].encode('utf-8')).hexdigest()[:16],
                'text_chars': len(cell['text']),
            }
            for cell in cells
        ],
    }


def _cell_at(grid: Mapping[str, Any], row: int, col: int) -> dict[str, Any] | None:
    for cell in grid.get('cells') or []:
        if cell['row'] == row and cell['col'] == col:
            return cell
    return None


def _covering_cell(grid: Mapping[str, Any], row: int, col: int) -> dict[str, Any] | None:
    for cell in grid.get('cells') or []:
        if cell['row'] <= row < cell['row'] + cell['row_span'] and cell['col'] <= col < cell['col'] + cell['col_span']:
            return cell
    return None


def _require_unmerged_lines(before: Mapping[str, Any], *, rows: range | None = None, cols: range | None = None) -> None:
    """Every cell touching the given rows (or columns) must be a plain 1x1 cell.

    That excludes merged cells in the edited lines and cells spanning across
    them, which is what makes the post-edit layout exactly predictable.
    """
    points = (
        [(r, c) for r in rows for c in range(1, before['cols'] + 1)]
        if rows is not None
        else [(r, c) for c in cols for r in range(1, before['rows'] + 1)]
    )
    for r, c in points:
        cell = _covering_cell(before, r, c)
        if cell is None or cell['row_span'] != 1 or cell['col_span'] != 1:
            owner = cell_address(cell['row'], cell['col']) if cell else 'no cell'
            line = 'row' if rows is not None else 'column'
            raise TableStructureError(
                f'{line} edit touches merged cell {owner} at {cell_address(r, c)}; row/column edits only run on lines of unmerged cells'
            )


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
    if before.get('cells') is None:
        raise TableStructureError(f'{action} needs cell-level readback (RowAddr/ColAddr), which this table did not report')
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
        span = count if action.startswith('delete') else 1
        if 'row' in action:
            _require_unmerged_lines(before, rows=range(row, row + span))
        else:
            _require_unmerged_lines(before, cols=range(col, col + span))
        return plan
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
    if rows + split_rows - 1 > MAX_GRID or cols + split_cols - 1 > MAX_GRID:
        raise TableStructureError(f'split would exceed {MAX_GRID} rows or columns')
    plan.update({'split_rows': split_rows, 'split_cols': split_cols})
    return plan


def _shift(value: int, at: int, by: int) -> int:
    return value + by if value >= at else value


def expected_layout(plan: Mapping[str, Any], before: Mapping[str, Any]) -> dict[str, Any]:
    """The exact grid the plan must produce.

    Each expected cell carries ``text``: the text it must keep, '' for a new
    empty cell, or None when Hancom decides the content (merged or split
    cells). ``merged_texts`` lists texts the merged cell must still contain.
    """
    action = plan['action']
    rows, cols = before['rows'], before['cols']
    cells = [dict(cell) for cell in before['cells']]
    merged_texts: list[str] = []
    if action in ROW_COL_ACTIONS:
        n = plan['count']
        on_rows = 'row' in action
        index = plan['row'] if on_rows else plan['col']
        axis, span_axis = ('row', 'col') if on_rows else ('col', 'row')
        extent = cols if on_rows else rows
        if action.startswith('insert'):
            at = index if action in ('insert_row_above', 'insert_col_left') else index + 1
            for cell in cells:
                cell[axis] = _shift(cell[axis], at, n)
            for i in range(n):
                for other in range(1, extent + 1):
                    cells.append({axis: at + i, span_axis: other, 'row_span': 1, 'col_span': 1, 'text': ''})
        else:
            cells = [cell for cell in cells if not (index <= cell[axis] < index + n)]
            for cell in cells:
                if cell[axis] >= index + n:
                    cell[axis] -= n
            n = -n
        if on_rows:
            rows += n
        else:
            cols += n
    elif action == 'merge_cells':
        r, c, er, ec = plan['row'], plan['col'], plan['end_row'], plan['end_col']
        area = [cell for cell in cells if r <= cell['row'] <= er and c <= cell['col'] <= ec]
        merged_texts = [cell['text'] for cell in sorted(area, key=lambda item: (item['row'], item['col'])) if cell['text']]
        cells = [cell for cell in cells if cell not in area]
        cells.append({'row': r, 'col': c, 'row_span': er - r + 1, 'col_span': ec - c + 1, 'text': None})
    else:  # split_cell: a 1x1 cell gains (R-1) row lines and (C-1) column lines.
        r, c, dr, dc = plan['row'], plan['col'], plan['split_rows'] - 1, plan['split_cols'] - 1
        target = _cell_at(before, r, c)
        cells = []
        for cell in before['cells']:
            if cell is target:
                continue
            moved = dict(cell)
            if cell['row'] <= r < cell['row'] + cell['row_span']:
                moved['row_span'] += dr
            if cell['col'] <= c < cell['col'] + cell['col_span']:
                moved['col_span'] += dc
            moved['row'] = _shift(cell['row'], r + 1, dr)
            moved['col'] = _shift(cell['col'], c + 1, dc)
            cells.append(moved)
        for i in range(dr + 1):
            for j in range(dc + 1):
                cells.append({'row': r + i, 'col': c + j, 'row_span': 1, 'col_span': 1, 'text': None})
        rows, cols = rows + dr, cols + dc
    cells.sort(key=lambda item: (item['row'], item['col']))
    return {'rows': rows, 'cols': cols, 'cell_count': len(cells), 'cells': cells, 'merged_texts': merged_texts}


def evaluate_change(plan: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the observed grid with the exact expected layout.

    Checks dimensions, every cell's position and span, and every cell's text
    fingerprint, so deleting or inserting at the wrong line, or splitting on
    the wrong axis, cannot pass on counts alone. ``ok`` is True only when
    nothing differs; ``reasons`` lists up to ten differences.
    """
    expected = expected_layout(plan, before)
    reasons: list[str] = []
    for key in ('rows', 'cols'):
        if after[key] != expected[key]:
            reasons.append(f'{key} is {after[key]}, expected {expected[key]}')
    if after.get('cells') is None:
        reasons.append('post-edit readback has no cell-level data')
    else:
        observed = {(cell['row'], cell['col']): cell for cell in after['cells']}
        wanted = {(cell['row'], cell['col']): cell for cell in expected['cells']}
        for key in sorted(set(observed) | set(wanted)):
            where = cell_address(*key)
            got, want = observed.get(key), wanted.get(key)
            if got is None:
                reasons.append(f'expected a cell at {where}, found none')
            elif want is None:
                reasons.append(f'unexpected cell at {where}')
            elif (got['row_span'], got['col_span']) != (want['row_span'], want['col_span']):
                reasons.append(f'{where} spans {got["row_span"]}x{got["col_span"]}, expected {want["row_span"]}x{want["col_span"]}')
            elif want['text'] is not None and got['text'] != want['text']:
                reasons.append(f'{where} content changed (expected {len(want["text"])} chars, found {len(got["text"])})')
        if plan['action'] == 'merge_cells':
            merged = observed.get((plan['row'], plan['col']))
            missing = [text for text in expected['merged_texts'] if merged is None or text not in merged['text']]
            if missing:
                reasons.append(f'merged cell {plan["address"]} lost content from {len(missing)} source cell(s)')
    return {
        'ok': not reasons,
        'expected': {'rows': expected['rows'], 'cols': expected['cols'], 'cell_count': expected['cell_count']},
        'observed': {'rows': after['rows'], 'cols': after['cols'], 'cell_count': after.get('cell_count')},
        'reasons': reasons[:10],
    }
