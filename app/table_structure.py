"""Pure planning and verification for exact table-structure edits.

Nothing here touches Hancom. The service mixin reads the table grid through
HWPML (the same ``SelectCtrlFront`` + ``GetTextFile('HWPML2X', 'saveblock')``
path pyhwpx uses for ``get_row_num``/``get_col_num``), and these helpers
decide whether an edit may run and whether the observed grid proves it did
exactly what was asked.

Every cell carries a content ``fingerprint``: a hash of its canonical HWPML
(paragraphs, text, embedded controls and cell attributes), leaving out only
its address, its geometry and edit bookkeeping. Row/column edits are refused
unless the edited lines are distinguishable from their neighbours by those
fingerprints, so an edit that lands on the wrong line always changes the
readback somewhere and cannot pass verification.

Cell coordinates on the public surface are 1-based ``row``/``col``, matching
Hancom's ``A1`` addresses (``A1`` is row 1, col 1).
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
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

# Attributes that describe where a cell is or how it is laid out, not what it
# holds. Positions shift, spans are compared on their own, and geometry may be
# recomputed by Hancom after a structure edit, so they are excluded from the
# content fingerprint.
_VOLATILE_ATTRS = frozenset({'RowAddr', 'ColAddr', 'RowSpan', 'ColSpan', 'Width', 'Height', 'Dirty', 'InstId', 'InstID'})


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


def parse_cell_range(tokens: Any) -> set[tuple[int, int]] | None:
    """Expand ``['A1:B2']`` / ``['A1', 'B1']`` into a set of (row, col); None if unparseable."""
    if isinstance(tokens, str):
        tokens = [tokens]
    if not isinstance(tokens, (list, tuple)) or not tokens:
        return None
    cells: set[tuple[int, int]] = set()
    for token in tokens:
        if not isinstance(token, str):
            return None
        parts = token.split(':')
        if len(parts) == 1:
            single = parse_cell_address(parts[0])
            if single is None:
                return None
            cells.add(single)
        elif len(parts) == 2:
            start, end = parse_cell_address(parts[0]), parse_cell_address(parts[1])
            if start is None or end is None:
                return None
            for row in range(min(start[0], end[0]), max(start[0], end[0]) + 1):
                for col in range(min(start[1], end[1]), max(start[1], end[1]) + 1):
                    cells.add((row, col))
        else:
            return None
    return cells


def _int_attr(element: ET.Element, name: str) -> int | None:
    raw = element.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _canonical(element: ET.Element) -> str:
    attrs = ''.join(f' {key}={value!r}' for key, value in sorted(element.attrib.items()) if key not in _VOLATILE_ATTRS)
    text = (element.text or '').strip()
    children = ''.join(_canonical(child) for child in element)
    tail = (element.tail or '').strip()
    return f'<{element.tag}{attrs}>{text}{children}</{element.tag}>{tail}'


def _cell_controls(cell: ET.Element) -> list[str]:
    """Tags of embedded controls: every TEXT child that is not a plain CHAR run.

    In HWPML a paragraph's TEXT holds CHAR runs plus inline controls
    (TABLE, PICTURE, EQUATION, shapes, fields, footnotes, ...).
    """
    return sorted(child.tag for text in cell.iter('TEXT') for child in text if child.tag != 'CHAR')


def parse_table_grid_xml(xml_text: str) -> dict[str, Any]:
    """Read RowCount/ColCount and top-level cells of the first TABLE in HWPML.

    Only ``TABLE > ROW > CELL`` children of the outer table are counted, so
    tables nested inside cells do not leak into the grid (they still count
    as embedded controls of their cell). ``cells`` is None when the cell
    address/span attributes are missing, which makes every edit fail closed.
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
    cells: list[dict[str, Any]] | None = []
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
                'text': ' '.join(''.join(cell.itertext()).split()),
                'controls': _cell_controls(cell),
                'fingerprint': hashlib.sha256(_canonical(cell).encode('utf-8')).hexdigest(),
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
    """Grid for responses and logs: no cell text, only short hashes, lengths and control tags."""
    cells = grid.get('cells')
    return {
        'rows': grid['rows'],
        'cols': grid['cols'],
        'cell_count': grid.get('cell_count'),
        'cells': None if cells is None else [
            {
                **{key: cell[key] for key in ('row', 'col', 'row_span', 'col_span', 'controls')},
                'fingerprint': cell['fingerprint'][:16],
                'text_chars': len(cell['text']),
            }
            for cell in cells
        ],
    }


def _is_empty(cell: Mapping[str, Any]) -> bool:
    return not cell['text'] and not cell['controls']


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


def _line_cells(grid: Mapping[str, Any], on_rows: bool, index: int) -> list[dict[str, Any]]:
    """Cells of one row (or column), in order. Only valid for lines of unmerged cells."""
    key, other = ('row', 'col') if on_rows else ('col', 'row')
    return sorted((cell for cell in grid['cells'] if cell[key] == index), key=lambda item: item[other])


def _line_signature(grid: Mapping[str, Any], on_rows: bool, index: int) -> tuple[str, ...]:
    return tuple(cell['fingerprint'] for cell in _line_cells(grid, on_rows, index))


def _line_is_empty(grid: Mapping[str, Any], on_rows: bool, index: int) -> bool:
    return all(_is_empty(cell) for cell in _line_cells(grid, on_rows, index))


def _require_unmerged_lines(before: Mapping[str, Any], on_rows: bool, lines: range) -> None:
    """Every cell touching the given rows (or columns) must be a plain 1x1 cell.

    That excludes merged cells in the edited lines and cells spanning across
    them, which is what makes the post-edit layout exactly predictable.
    """
    extent = before['cols'] if on_rows else before['rows']
    for line in lines:
        for other in range(1, extent + 1):
            r, c = (line, other) if on_rows else (other, line)
            cell = _covering_cell(before, r, c)
            if cell is None or cell['row_span'] != 1 or cell['col_span'] != 1:
                owner = cell_address(cell['row'], cell['col']) if cell else 'no cell'
                raise TableStructureError(
                    f'{"row" if on_rows else "column"} edit touches merged cell {owner} at {cell_address(r, c)}; '
                    'row/column edits only run on lines of unmerged cells'
                )


def _require_distinguishable_lines(before: Mapping[str, Any], action: str, index: int, count: int) -> None:
    """Refuse edits whose wrong-line outcome would read back identically.

    Deleting lines [i, i+n) can only be confused with deleting a shifted
    window if a deleted line equals the line that would replace it at a
    window edge; checking the two edge pairs covers every shift. An
    inserted line is empty, so the lines on both sides of the insertion
    boundary must hold content for a misplaced insert to be visible.
    """
    on_rows = 'row' in action
    total = before['rows'] if on_rows else before['cols']
    name = 'row' if on_rows else 'column'
    if action.startswith('delete'):
        first, last = index, index + count - 1
        for deleted, neighbour in ((first, last + 1), (last, first - 1)):
            if 1 <= neighbour <= total and _line_signature(before, on_rows, deleted) == _line_signature(before, on_rows, neighbour):
                raise TableStructureError(
                    f'{name} {deleted} is identical to {name} {neighbour}; a delete that hit the wrong {name} would be '
                    'indistinguishable, so this edit is refused'
                )
        return
    boundary = index if action in ('insert_row_above', 'insert_col_left') else index + 1
    for side in (boundary - 1, boundary):
        if 1 <= side <= total and _line_is_empty(before, on_rows, side):
            raise TableStructureError(
                f'{name} {side} next to the insertion point is empty, so a misplaced empty {name} would be '
                'indistinguishable; this edit is refused'
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
        on_rows = 'row' in action
        index = row if on_rows else col
        plan['count'] = count
        plan['hancom_action'] = ROW_COL_ACTIONS[action]
        if action.startswith('insert') and (rows if on_rows else cols) + count > MAX_GRID:
            raise TableStructureError(f'inserting {count} line(s) would exceed {MAX_GRID}')
        if action.startswith('delete'):
            total, name = (rows, 'row') if on_rows else (cols, 'column')
            if index + count - 1 > total:
                raise TableStructureError(f'deleting {count} {name}(s) from {name} {index} runs past the last {name} {total}')
            if total - count < 1:
                raise TableStructureError(f'refusing to delete every {name}; delete the table as a control instead')
        span = count if action.startswith('delete') else 1
        _require_unmerged_lines(before, on_rows, range(index, index + span))
        if action.startswith('delete'):
            for line in range(index, index + count):
                for cell in _line_cells(before, on_rows, line):
                    if cell['controls']:
                        raise TableStructureError(
                            f'{cell_address(cell["row"], cell["col"])} holds embedded control(s) {", ".join(cell["controls"])}; '
                            'remove them explicitly before deleting its line'
                        )
        else:
            # The neighbour across the insertion boundary must be plain too,
            # or a spanning cell would absorb the new line.
            boundary_neighbour = index - 1 if action in ('insert_row_above', 'insert_col_left') else index + 1
            if 1 <= boundary_neighbour <= (rows if on_rows else cols):
                _require_unmerged_lines(before, on_rows, range(boundary_neighbour, boundary_neighbour + 1))
        _require_distinguishable_lines(before, action, index, count)
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
        plan.update({
            'end_row': end_row,
            'end_col': end_col,
            'end_address': cell_address(end_row, end_col),
            'area': area,
            'selection': sorted((r, c) for r in range(row, end_row + 1) for c in range(col, end_col + 1)),
        })
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

    Each expected cell has ``expect``: ``'same'`` (fingerprint must equal
    ``fingerprint``), ``'empty'`` (a new cell: no text, no controls) or
    ``'derived'`` (merged/split cells whose content is checked as a group).
    """
    action = plan['action']
    rows, cols = before['rows'], before['cols']
    kept = [{**cell, 'expect': 'same'} for cell in before['cells']]
    groups: dict[str, Any] = {}
    if action in ROW_COL_ACTIONS:
        n = plan['count']
        on_rows = 'row' in action
        index = plan['row'] if on_rows else plan['col']
        axis, other_axis = ('row', 'col') if on_rows else ('col', 'row')
        extent = cols if on_rows else rows
        if action.startswith('insert'):
            at = index if action in ('insert_row_above', 'insert_col_left') else index + 1
            for cell in kept:
                cell[axis] = _shift(cell[axis], at, n)
            for i in range(n):
                for other in range(1, extent + 1):
                    kept.append({axis: at + i, other_axis: other, 'row_span': 1, 'col_span': 1, 'expect': 'empty'})
        else:
            kept = [cell for cell in kept if not (index <= cell[axis] < index + n)]
            for cell in kept:
                if cell[axis] >= index + n:
                    cell[axis] -= n
            n = -n
        if on_rows:
            rows += n
        else:
            cols += n
    elif action == 'merge_cells':
        r, c, er, ec = plan['row'], plan['col'], plan['end_row'], plan['end_col']
        area = [cell for cell in kept if r <= cell['row'] <= er and c <= cell['col'] <= ec]
        groups['merge_sources'] = area
        kept = [cell for cell in kept if not (r <= cell['row'] <= er and c <= cell['col'] <= ec)]
        kept.append({'row': r, 'col': c, 'row_span': er - r + 1, 'col_span': ec - c + 1, 'expect': 'derived'})
    else:  # split_cell: a 1x1 cell gains (R-1) row lines and (C-1) column lines.
        r, c, dr, dc = plan['row'], plan['col'], plan['split_rows'] - 1, plan['split_cols'] - 1
        groups['split_source'] = _cell_at(before, r, c)
        moved = []
        for cell in kept:
            if (cell['row'], cell['col']) == (r, c):
                continue
            if cell['row'] <= r < cell['row'] + cell['row_span']:
                cell['row_span'] += dr
            if cell['col'] <= c < cell['col'] + cell['col_span']:
                cell['col_span'] += dc
            cell['row'] = _shift(cell['row'], r + 1, dr)
            cell['col'] = _shift(cell['col'], c + 1, dc)
            moved.append(cell)
        kept = moved
        for i in range(dr + 1):
            for j in range(dc + 1):
                kept.append({'row': r + i, 'col': c + j, 'row_span': 1, 'col_span': 1, 'expect': 'derived'})
        rows, cols = rows + dr, cols + dc
    kept.sort(key=lambda item: (item['row'], item['col']))
    return {'rows': rows, 'cols': cols, 'cell_count': len(kept), 'cells': kept, **groups}


def evaluate_change(plan: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the observed grid with the exact expected layout.

    Checks dimensions, every cell's position and span, every kept cell's
    content fingerprint (text, embedded controls, cell attributes), that new
    cells are empty, and that merged/split cells keep all source text and
    controls. ``ok`` is True only when nothing differs; ``reasons`` lists up
    to ten differences.
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
            elif want['expect'] == 'same' and got['fingerprint'] != want['fingerprint']:
                reasons.append(f'{where} content changed (it is not the cell that belongs there)')
            elif want['expect'] == 'empty' and not _is_empty(got):
                reasons.append(f'{where} should be a new empty cell but holds content')
        if 'merge_sources' in expected:
            merged = observed.get((plan['row'], plan['col']))
            sources = expected['merge_sources']
            missing = [cell for cell in sources if cell['text'] and (merged is None or cell['text'] not in merged['text'])]
            if missing:
                reasons.append(f'merged cell {plan["address"]} lost text from {len(missing)} source cell(s)')
            want_controls = Counter(tag for cell in sources for tag in cell['controls'])
            if merged is None or Counter(merged['controls']) != want_controls:
                reasons.append(f'merged cell {plan["address"]} does not hold exactly the source cells\' embedded controls')
        if 'split_source' in expected:
            source = expected['split_source']
            parts = [observed.get((cell['row'], cell['col'])) for cell in expected['cells'] if cell['expect'] == 'derived']
            if all(part is not None for part in parts):
                text = ' '.join(part['text'] for part in parts if part['text'])
                if text != source['text']:
                    reasons.append(f'split cells of {plan["address"]} do not hold exactly the original text')
                if Counter(tag for part in parts for tag in part['controls']) != Counter(source['controls']):
                    reasons.append(f'split cells of {plan["address"]} do not hold exactly the original embedded controls')
    return {
        'ok': not reasons,
        'expected': {'rows': expected['rows'], 'cols': expected['cols'], 'cell_count': expected['cell_count']},
        'observed': {'rows': after['rows'], 'cols': after['cols'], 'cell_count': after.get('cell_count')},
        'reasons': reasons[:10],
    }
