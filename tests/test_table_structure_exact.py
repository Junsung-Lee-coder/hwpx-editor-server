from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import (
    LocalCliMutationError,
    LocalCliService,
    LocalCliServiceError,
    _record_bundle_undo_state,
)
from app.table_structure import (
    TableStructureError,
    cell_address,
    check_plan,
    evaluate_change,
    parse_cell_address,
    parse_cell_range,
    parse_table_grid_xml,
    public_grid,
)
from local_cli_v1.bundles import BundleError, build_named_bundle

# A cell spec: (row, col, row_span, col_span[, text[, controls]]), 1-based.
Cell = tuple
TARGET_INST = '1111111111'
OTHER_INST = '2222222222'


def _cell_xml(row: int, col: int, row_span: int, col_span: int, text: str = '', controls: tuple[str, ...] = ()) -> str:
    inner = (f'<CHAR>{text}</CHAR>' if text else '') + ''.join(f'<{tag} BinItem="1"/>' for tag in controls)
    body = f'<P><TEXT>{inner}</TEXT></P>' if inner else ''
    return f'<CELL RowAddr="{row - 1}" ColAddr="{col - 1}" RowSpan="{row_span}" ColSpan="{col_span}" Width="1000" Height="500"><PARALIST>{body}</PARALIST></CELL>'


def _hwpml(rows: int, cols: int, cells: list[Cell] | None = None, *, declaration: bool = False) -> str:
    """Default cells: an unmerged grid whose texts are 'R<r>C<c>'."""
    if cells is None:
        cells = [(r, c, 1, 1, f'R{r}C{c}') for r in range(1, rows + 1) for c in range(1, cols + 1)]
    by_row: dict[int, list[str]] = {}
    for cell in cells:
        by_row.setdefault(cell[0], []).append(_cell_xml(*cell))
    rows_xml = ''.join(f'<ROW>{"".join(items)}</ROW>' for _row, items in sorted(by_row.items()))
    prefix = '<?xml version="1.0" encoding="UTF-16" standalone="no"?>' if declaration else ''
    return f'{prefix}<HWPML><BODY><SECTION><P><TEXT><TABLE RowCount="{rows}" ColCount="{cols}">{rows_xml}</TABLE></TEXT></P></SECTION></BODY></HWPML>'


def _grid(rows: int, cols: int, cells: list[Cell] | None = None) -> dict[str, Any]:
    return parse_table_grid_xml(_hwpml(rows, cols, cells))


def _unmerged(rows: int, cols: int, text: Callable[[int, int], str]) -> dict[str, Any]:
    return _grid(rows, cols, [(r, c, 1, 1, text(r, c)) for r in range(1, rows + 1) for c in range(1, cols + 1)])


def _from_old_rows(rows: int, cols: int, old_row: dict[int, int]) -> dict[str, Any]:
    """New row r holds old row old_row[r]'s text, or is empty when r is not mapped."""
    return _unmerged(rows, cols, lambda r, c: f'R{old_row[r]}C{c}' if r in old_row else '')


def _step(**overrides: Any) -> dict[str, Any]:
    step = {'action': 'insert_row_below', 'row': 1, 'col': 1, 'expected_rows': 3, 'expected_cols': 2}
    step.update(overrides)
    return step


# 3x2 grid with A1:A2 merged vertically.
MERGED_COL_A = [(1, 1, 2, 1, 'A'), (1, 2, 1, 1, 'B1'), (2, 2, 1, 1, 'B2'), (3, 1, 1, 1, 'A3'), (3, 2, 1, 1, 'B3')]


class AddressTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        for row, col, text in ((1, 1, 'A1'), (3, 2, 'B3'), (10, 26, 'Z10'), (2, 27, 'AA2'), (5, 52, 'AZ5')):
            self.assertEqual(cell_address(row, col), text)
            self.assertEqual(parse_cell_address(text), (row, col))

    def test_rejects_non_addresses(self) -> None:
        for value in (None, '', 'A0', '1A', 'A-1', ('A', 1), 3):
            self.assertIsNone(parse_cell_address(value))

    def test_cell_ranges(self) -> None:
        self.assertEqual(parse_cell_range(['A1:B2']), {(1, 1), (1, 2), (2, 1), (2, 2)})
        self.assertEqual(parse_cell_range(['B2:A1']), {(1, 1), (1, 2), (2, 1), (2, 2)})
        self.assertEqual(parse_cell_range(['A1', 'B1']), {(1, 1), (1, 2)})
        self.assertEqual(parse_cell_range('C3'), {(3, 3)})
        for bad in (None, [], ['A1:B2:C3'], ['?'], [3]):
            self.assertIsNone(parse_cell_range(bad))


class GridReadbackTests(unittest.TestCase):
    def test_counts_rows_cols_cells_text_and_controls(self) -> None:
        grid = _grid(2, 3, [(1, 1, 1, 1, 'x', ('PICTURE',))] + [(r, c, 1, 1, 'y') for r, c in ((1, 2), (1, 3), (2, 1), (2, 2), (2, 3))])
        self.assertEqual((grid['rows'], grid['cols'], grid['cell_count']), (2, 3, 6))
        first = grid['cells'][0]
        self.assertEqual((first['row'], first['col'], first['text'], first['controls']), (1, 1, 'x', ['PICTURE']))

    def test_fingerprint_covers_controls_but_not_address_or_geometry(self) -> None:
        plain = parse_table_grid_xml(_hwpml(1, 1, [(1, 1, 1, 1, 'x')]))['cells'][0]['fingerprint']
        moved = parse_table_grid_xml(_hwpml(1, 1, [(1, 1, 1, 1, 'x')]).replace('RowAddr="0"', 'RowAddr="7"').replace('Width="1000"', 'Width="3"'))
        self.assertEqual(moved['cells'][0]['fingerprint'], plain)
        with_picture = parse_table_grid_xml(_hwpml(1, 1, [(1, 1, 1, 1, 'x', ('PICTURE',))]))['cells'][0]['fingerprint']
        self.assertNotEqual(with_picture, plain)

    def test_public_grid_hides_cell_text(self) -> None:
        cell = public_grid(_grid(1, 2))['cells'][0]
        self.assertNotIn('text', cell)
        self.assertEqual((cell['text_chars'], len(cell['fingerprint']), cell['controls']), (4, 16, []))

    def test_accepts_xml_declaration_in_str(self) -> None:
        self.assertEqual(parse_table_grid_xml(_hwpml(1, 2, declaration=True))['cell_count'], 2)

    def test_nested_tables_are_controls_not_grid_cells(self) -> None:
        nested = '<TABLE RowCount="5" ColCount="5"><ROW><CELL RowAddr="0" ColAddr="0" RowSpan="1" ColSpan="1"/></ROW></TABLE>'
        xml = _hwpml(1, 2).replace('</TEXT></P></PARALIST></CELL>', f'{nested}</TEXT></P></PARALIST></CELL>', 1)
        grid = parse_table_grid_xml(xml)
        self.assertEqual((grid['rows'], grid['cols'], grid['cell_count']), (1, 2, 2))
        self.assertEqual(grid['cells'][0]['controls'], ['TABLE'])

    def test_missing_cell_addresses_disable_cell_level_data(self) -> None:
        grid = parse_table_grid_xml('<HWPML><TABLE RowCount="2" ColCount="2"><ROW><CELL/></ROW></TABLE></HWPML>')
        self.assertIsNone(grid['cells'])
        self.assertIsNone(grid['cell_count'])

    def test_rejects_bad_readback(self) -> None:
        for xml in ('', '<HWPML/>', '<HWPML><TABLE RowCount="0" ColCount="2"/></HWPML>', '<HWPML><TABLE', None):
            with self.subTest(xml=xml), self.assertRaises(TableStructureError):
                parse_table_grid_xml(xml)


class PlanTests(unittest.TestCase):
    def test_expected_grid_must_match(self) -> None:
        with self.assertRaisesRegex(TableStructureError, 're-inventory'):
            check_plan(_step(expected_rows=4), _grid(3, 2))

    def test_target_must_be_inside_table(self) -> None:
        with self.assertRaisesRegex(TableStructureError, 'outside'):
            check_plan(_step(row=4), _grid(3, 2))

    def test_delete_bounds(self) -> None:
        with self.assertRaisesRegex(TableStructureError, 'past the last row'):
            check_plan(_step(action='delete_row', row=3, count=2), _grid(3, 2))
        with self.assertRaisesRegex(TableStructureError, 'every row'):
            check_plan(_step(action='delete_row', row=1, count=3), _grid(3, 2))
        with self.assertRaisesRegex(TableStructureError, 'every column'):
            check_plan(_step(action='delete_col', col=1, count=2), _grid(3, 2))

    def test_row_col_plan_carries_hancom_action(self) -> None:
        plan = check_plan(_step(action='insert_col_right', col=2, count=2), _grid(3, 2))
        self.assertEqual(plan['hancom_action'], 'TableInsertRightColumn')
        self.assertEqual((plan['count'], plan['address']), (2, 'B1'))

    def test_row_col_edits_refuse_lines_touching_merged_cells(self) -> None:
        merged = _grid(3, 2, MERGED_COL_A)
        for step in (
            _step(action='insert_row_below', row=1),  # A1:A2 spans across the new line
            _step(action='insert_row_above', row=2),
            _step(action='delete_row', row=2),
            _step(action='insert_col_right', col=1),  # column A holds the merged cell
            _step(action='delete_col', col=1),
        ):
            with self.subTest(step=step), self.assertRaisesRegex(TableStructureError, 'merged cell A1'):
                check_plan(step, merged)
        # Lines away from the merged cell are accepted.
        check_plan(_step(action='insert_row_below', row=3), merged)
        check_plan(_step(action='delete_col', col=2), merged)

    def test_row_col_edits_refuse_indistinguishable_lines(self) -> None:
        # Rows 2 and 3 are identical: deleting row 3 instead of row 2 would read back the same.
        twins = _grid(3, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b'), (2, 1, 1, 1, 'x'), (2, 2, 1, 1, 'y'), (3, 1, 1, 1, 'x'), (3, 2, 1, 1, 'y')])
        with self.assertRaisesRegex(TableStructureError, 'row 2 is identical to row 3'):
            check_plan(_step(action='delete_row', row=2), twins)
        with self.assertRaisesRegex(TableStructureError, 'row 3 is identical to row 2'):
            check_plan(_step(action='delete_row', row=3), twins)
        check_plan(_step(action='delete_row', row=1), twins)
        # Blank rows next to the insertion point would hide a misplaced empty row.
        blank_row_2 = _grid(3, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b'), (2, 1, 1, 1), (2, 2, 1, 1), (3, 1, 1, 1, 'c'), (3, 2, 1, 1, 'd')])
        for step in (_step(action='insert_row_below', row=1), _step(action='insert_row_above', row=2), _step(action='insert_row_below', row=2)):
            with self.subTest(step=step), self.assertRaisesRegex(TableStructureError, 'row 2 next to the insertion point is empty'):
                check_plan(step, blank_row_2)
        # Identical columns are refused the same way.
        twin_cols = _unmerged(2, 3, lambda r, c: f'R{r}' if c > 1 else 'k')
        with self.assertRaisesRegex(TableStructureError, 'column 2 is identical to column 3'):
            check_plan(_step(action='delete_col', col=2, expected_rows=2, expected_cols=3), twin_cols)

    def test_delete_refuses_lines_with_embedded_controls(self) -> None:
        grid = _grid(3, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b'), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd', ('PICTURE',)), (3, 1, 1, 1, 'e'), (3, 2, 1, 1, 'f')])
        with self.assertRaisesRegex(TableStructureError, 'B2 holds embedded control'):
            check_plan(_step(action='delete_row', row=2), grid)
        with self.assertRaisesRegex(TableStructureError, 'B2 holds embedded control'):
            check_plan(_step(action='delete_col', col=2), grid)
        check_plan(_step(action='delete_row', row=3), grid)

    def test_merge_requires_unmerged_range_inside_table(self) -> None:
        plan = check_plan(_step(action='merge_cells', end_row=2, end_col=2), _grid(3, 2))
        self.assertEqual((plan['area'], plan['end_address']), (4, 'B2'))
        self.assertEqual(plan['selection'], [(1, 1), (1, 2), (2, 1), (2, 2)])
        with self.assertRaisesRegex(TableStructureError, 'inside'):
            check_plan(_step(action='merge_cells', end_row=4, end_col=2), _grid(3, 2))
        with self.assertRaisesRegex(TableStructureError, 'at least two'):
            check_plan(_step(action='merge_cells', end_row=1, end_col=1), _grid(3, 2))
        with self.assertRaisesRegex(TableStructureError, 'already merged'):
            check_plan(_step(action='merge_cells', end_row=2, end_col=2), _grid(3, 2, MERGED_COL_A))

    def test_split_requires_unmerged_top_left_cell(self) -> None:
        merged = _grid(2, 2, [(1, 1, 1, 2), (2, 1, 1, 1), (2, 2, 1, 1)])
        with self.assertRaisesRegex(TableStructureError, 'covered by merged cell A1'):
            check_plan(_step(action='split_cell', row=1, col=2, split_cols=2, expected_rows=2), merged)
        with self.assertRaisesRegex(TableStructureError, 'is a merged cell'):
            check_plan(_step(action='split_cell', row=1, col=1, split_cols=2, expected_rows=2), merged)
        with self.assertRaisesRegex(TableStructureError, '>= 2'):
            check_plan(_step(action='split_cell', expected_rows=2, expected_cols=2), _grid(2, 2))

    def test_every_action_needs_cell_level_readback(self) -> None:
        grid = parse_table_grid_xml('<HWPML><TABLE RowCount="2" ColCount="2"><ROW><CELL/></ROW></TABLE></HWPML>')
        for step in (_step(expected_rows=2), _step(action='merge_cells', end_row=1, end_col=2, expected_rows=2)):
            with self.subTest(step=step), self.assertRaisesRegex(TableStructureError, 'cell-level'):
                check_plan(step, grid)


class EvaluateChangeTests(unittest.TestCase):
    def _check(self, before: dict[str, Any], after: dict[str, Any], **step: Any) -> dict[str, Any]:
        plan = check_plan(_step(expected_rows=before['rows'], expected_cols=before['cols'], **step), before)
        return evaluate_change(plan, before, after)

    def test_insert_and_delete_check_every_cell(self) -> None:
        before = _grid(3, 2)
        cases = (
            # Two rows above row 2: new empty rows 2-3, old rows 2-3 move to 4-5.
            ({'action': 'insert_row_above', 'row': 2, 'count': 2}, _from_old_rows(5, 2, {1: 1, 4: 2, 5: 3}), True),
            # Same counts, but the new rows landed at the bottom.
            ({'action': 'insert_row_above', 'row': 2, 'count': 2}, _from_old_rows(5, 2, {1: 1, 2: 2, 3: 3}), False),
            ({'action': 'delete_row', 'row': 2}, _from_old_rows(2, 2, {1: 1, 2: 3}), True),
            # Wrong row deleted (row 1 instead of row 2): counts match, content does not.
            ({'action': 'delete_row', 'row': 2}, _from_old_rows(2, 2, {1: 2, 2: 3}), False),
            ({'action': 'delete_col', 'col': 2}, _unmerged(3, 1, lambda r, c: f'R{r}C1'), True),
            ({'action': 'delete_col', 'col': 2}, _unmerged(3, 1, lambda r, c: f'R{r}C2'), False),
            ({'action': 'insert_col_left', 'col': 1}, _unmerged(3, 3, lambda r, c: '' if c == 1 else f'R{r}C{c - 1}'), True),
            ({'action': 'insert_row_below', 'row': 1}, _grid(3, 2), False),
        )
        for step, after, ok in cases:
            with self.subTest(step=step, ok=ok):
                result = self._check(before, after, **step)
                self.assertEqual(result['ok'], ok, result['reasons'])

    def test_kept_cells_must_keep_their_controls(self) -> None:
        before = _grid(3, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b', ('PICTURE',)), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd'), (3, 1, 1, 1, 'e'), (3, 2, 1, 1, 'f')])
        dropped_picture = _grid(2, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b'), (2, 1, 1, 1, 'e'), (2, 2, 1, 1, 'f')])
        result = self._check(before, dropped_picture, action='delete_row', row=2)
        self.assertIn('B1 content changed (it is not the cell that belongs there)', result['reasons'])

    def test_new_cells_must_be_empty(self) -> None:
        before = _grid(3, 2)
        after = _from_old_rows(4, 2, {1: 1, 3: 2, 4: 3})
        after = _grid(4, 2, [(c['row'], c['col'], 1, 1, c['text'] or ('ghost' if c['row'] == 2 and c['col'] == 1 else '')) for c in after['cells']])
        result = self._check(before, after, action='insert_row_below', row=1)
        self.assertIn('A2 should be a new empty cell but holds content', result['reasons'])

    def test_merge_checks_layout_and_keeps_content(self) -> None:
        before = _grid(2, 2)
        step = {'action': 'merge_cells', 'end_row': 1, 'end_col': 2}
        good = _grid(2, 2, [(1, 1, 1, 2, 'R1C1 R1C2'), (2, 1, 1, 1, 'R2C1'), (2, 2, 1, 1, 'R2C2')])
        self.assertTrue(self._check(before, good, **step)['ok'])
        lost = _grid(2, 2, [(1, 1, 1, 2, 'R1C1'), (2, 1, 1, 1, 'R2C1'), (2, 2, 1, 1, 'R2C2')])
        self.assertTrue(any('lost text' in reason for reason in self._check(before, lost, **step)['reasons']))
        wrong_span = _grid(2, 2, [(1, 1, 2, 1, 'R1C1 R2C1'), (1, 2, 1, 1, 'R1C2'), (2, 2, 1, 1, 'R2C2')])
        self.assertFalse(self._check(before, wrong_span, **step)['ok'])
        self.assertFalse(self._check(before, before, **step)['ok'])

    def test_merge_keeps_embedded_controls(self) -> None:
        before = _grid(2, 2, [(1, 1, 1, 1, 'a'), (1, 2, 1, 1, 'b', ('PICTURE',)), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd')])
        step = {'action': 'merge_cells', 'end_row': 1, 'end_col': 2}
        kept = _grid(2, 2, [(1, 1, 1, 2, 'a b', ('PICTURE',)), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd')])
        self.assertTrue(self._check(before, kept, **step)['ok'])
        lost = _grid(2, 2, [(1, 1, 1, 2, 'a b'), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd')])
        self.assertTrue(any('embedded controls' in reason for reason in self._check(before, lost, **step)['reasons']))

    def test_split_checks_axis_neighbour_spans_and_content(self) -> None:
        before = _grid(2, 2)
        step = {'action': 'split_cell', 'row': 1, 'col': 1, 'split_cols': 2}
        # A1 becomes A1|B1; old column B shifts to C; A2 now spans A2:B2.
        good = _grid(2, 3, [(1, 1, 1, 1, 'R1C1'), (1, 2, 1, 1, ''), (1, 3, 1, 1, 'R1C2'), (2, 1, 1, 2, 'R2C1'), (2, 3, 1, 1, 'R2C2')])
        result = self._check(before, good, **step)
        self.assertTrue(result['ok'], result['reasons'])
        # Same cell count (5), split on the wrong axis.
        wrong_axis = _grid(3, 2, [(1, 1, 1, 1, 'R1C1'), (1, 2, 2, 1, 'R1C2'), (2, 1, 1, 1, ''), (3, 1, 1, 1, 'R2C1'), (3, 2, 1, 1, 'R2C2')])
        result = self._check(before, wrong_axis, **step)
        self.assertFalse(result['ok'])
        self.assertIn('rows is 3, expected 2', result['reasons'])
        lost_text = _grid(2, 3, [(1, 1, 1, 1, ''), (1, 2, 1, 1, ''), (1, 3, 1, 1, 'R1C2'), (2, 1, 1, 2, 'R2C1'), (2, 3, 1, 1, 'R2C2')])
        self.assertTrue(any('original text' in reason for reason in self._check(before, lost_text, **step)['reasons']))
        self.assertFalse(self._check(before, before, **step)['ok'])

    def test_split_keeps_embedded_controls(self) -> None:
        before = _grid(2, 2, [(1, 1, 1, 1, 'x', ('PICTURE',)), (1, 2, 1, 1, 'b'), (2, 1, 1, 1, 'c'), (2, 2, 1, 1, 'd')])
        step = {'action': 'split_cell', 'row': 1, 'col': 1, 'split_rows': 2}
        base = [(1, 2, 2, 1, 'b'), (3, 1, 1, 1, 'c'), (3, 2, 1, 1, 'd')]
        kept = _grid(3, 2, [(1, 1, 1, 1, 'x', ('PICTURE',)), (2, 1, 1, 1)] + base)
        self.assertTrue(self._check(before, kept, **step)['ok'], self._check(before, kept, **step)['reasons'])
        lost = _grid(3, 2, [(1, 1, 1, 1, 'x'), (2, 1, 1, 1)] + base)
        self.assertTrue(any('original embedded controls' in reason for reason in self._check(before, lost, **step)['reasons']))


class _Ctrl:
    def __init__(self, inst: str) -> None:
        self.CtrlInstID = inst


class _Grid:
    """Tiny table model: 1-based (row, col) -> [row_span, col_span, text, controls]."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.cells = {(r, c): [1, 1, f'R{r}C{c}', ()] for r in range(1, rows + 1) for c in range(1, cols + 1)}

    def xml(self) -> str:
        return _hwpml(self.rows, self.cols, [(r, c, *cell) for (r, c), cell in sorted(self.cells.items())])

    def insert_row(self, at: int) -> None:
        self.cells = {((r + 1) if r >= at else r, c): cell for (r, c), cell in self.cells.items()}
        self.cells.update({(at, c): [1, 1, '', ()] for c in range(1, self.cols + 1)})
        self.rows += 1

    def insert_col(self, at: int) -> None:
        self.cells = {(r, (c + 1) if c >= at else c): cell for (r, c), cell in self.cells.items()}
        self.cells.update({(r, at): [1, 1, '', ()] for r in range(1, self.rows + 1)})
        self.cols += 1

    def delete_row(self, at: int) -> None:
        self.cells = {((r - 1) if r > at else r, c): cell for (r, c), cell in self.cells.items() if r != at}
        self.rows -= 1

    def delete_col(self, at: int) -> None:
        self.cells = {(r, (c - 1) if c > at else c): cell for (r, c), cell in self.cells.items() if c != at}
        self.cols -= 1

    def merge(self, top_left: tuple[int, int], bottom_right: tuple[int, int]) -> None:
        (r1, c1), (r2, c2) = top_left, bottom_right
        parts = [self.cells.pop((r, c)) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
        text = ' '.join(part[2] for part in parts if part[2])
        controls = tuple(tag for part in parts for tag in part[3])
        self.cells[(r1, c1)] = [r2 - r1 + 1, c2 - c1 + 1, text, controls]

    def split(self, row: int, col: int, rows: int, cols: int) -> None:
        """Insert (rows-1) row lines and (cols-1) column lines through the target cell."""
        dr, dc = rows - 1, cols - 1
        moved: dict[tuple[int, int], list[Any]] = {}
        for (r, c), (rs, cs, text, controls) in self.cells.items():
            if (r, c) == (row, col):
                continue
            if r <= row < r + rs:
                rs += dr
            if c <= col < c + cs:
                cs += dc
            moved[((r + dr) if r > row else r, (c + dc) if c > col else c)] = [rs, cs, text, controls]
        _rs, _cs, text, controls = self.cells[(row, col)]
        for i in range(rows):
            for j in range(cols):
                moved[(row + i, col + j)] = [1, 1, text, controls] if (i, j) == (0, 0) else [1, 1, '', ()]
        self.cells, self.rows, self.cols = moved, self.rows + dr, self.cols + dc


class _FakeHAction:
    def __init__(self, hwp: _FakeTableHwp) -> None:
        self.hwp = hwp

    def Run(self, name: str) -> bool:  # noqa: N802 - Hancom API name
        if name in self.hwp.raising:
            self.hwp.log.append(f'{name}!raised')
            raise RuntimeError(f'COM error in {name}')
        return self.hwp.run(name)


class _FakeTableHwp:
    """Fake pyhwpx Hwp over _Grid.

    Fault knobs: ``noop`` actions report success but change nothing;
    ``raising`` actions raise from HAction.Run; ``wrong_line`` makes row/col
    deletes hit the neighbouring line; ``split_swap`` swaps the split axes;
    ``caret_after_delete='previous'`` leaves the caret on the previous row
    after TableDeleteRow; ``sticky_selection`` ignores Cancel;
    ``goto_leaves_selection`` leaves an uncancellable block after goto_addr;
    ``parent_inst`` / ``parent_after_first_action`` change which table the
    caret reports; ``select_wrong`` makes every control selection land on
    another control; ``front_after_first_action`` makes SelectCtrlFront pick
    that control once a mutating action ran (an embedded picture at the
    caret); ``no_select_ctrl`` removes SelectCtrl;
    ``snapshot_unknown`` makes the selection state unreadable;
    ``range_off`` makes get_selected_range report a wider block.
    """

    MUTATING = {'TableInsertUpperRow', 'TableInsertLowerRow', 'TableInsertLeftColumn', 'TableInsertRightColumn',
                'TableDeleteRow', 'TableDeleteColumn', 'TableMergeCell'}

    def __init__(self, rows: int, cols: int, **faults: Any) -> None:
        self.grid = _Grid(rows, cols)
        self.caret = (1, 1)
        self.block: tuple[int, int] | None = None
        self.selected = False
        self.log: list[str] = []
        self.plain_run_calls: list[str] = []
        self.noop: set[str] = set(faults.get('noop', ()))
        self.raising: set[str] = set(faults.get('raising', ()))
        self.wrong_line = bool(faults.get('wrong_line'))
        self.split_swap = bool(faults.get('split_swap'))
        self.caret_after_delete = faults.get('caret_after_delete', 'same')
        self.sticky_selection = bool(faults.get('sticky_selection'))
        self.goto_leaves_selection = bool(faults.get('goto_leaves_selection'))
        self.parent_inst = faults.get('parent_inst', TARGET_INST)
        self.parent_after_first_action = faults.get('parent_after_first_action')
        self.select_wrong = bool(faults.get('select_wrong'))
        self.front_inst = TARGET_INST
        self.front_after_first_action = faults.get('front_after_first_action')
        self.current_selected: str | None = None
        if faults.get('no_select_ctrl'):
            self.SelectCtrl = None  # type: ignore[assignment]
        self.snapshot_unknown = bool(faults.get('snapshot_unknown'))
        self.has_selection_unknown = bool(faults.get('has_selection_unknown'))
        self.range_off = bool(faults.get('range_off'))
        self.stuck_block = False
        self.HAction = _FakeHAction(self)

    @property
    def ParentCtrl(self) -> _Ctrl:  # noqa: N802
        return _Ctrl(self.parent_inst)

    @property
    def CurSelectedCtrl(self) -> _Ctrl | None:  # noqa: N802
        return _Ctrl(self.current_selected) if self.selected and self.current_selected else None

    def SelectCtrl(self, inst: str, option: int = 1) -> bool:  # noqa: N802
        self.selected = True
        self.current_selected = OTHER_INST if self.select_wrong else inst
        return True

    def get_pos(self) -> tuple[int, int, int]:
        return (1, 0, 0)

    def set_pos(self, *_args: Any) -> bool:
        if not self.sticky_selection:
            self.selected = False
        return True

    def get_cell_addr(self, as_: str = 'str') -> Any:
        row, col = self.caret
        if as_ == 'tuple':
            return (row - 1, col - 1)  # pyhwpx 1.7.2 order: (row, col)
        return cell_address(row, col)

    def goto_addr(self, addr: str) -> bool:
        parsed = parse_cell_address(addr)
        if parsed is None or parsed not in self.grid.cells:
            return False
        self.caret = parsed
        self.stuck_block = self.goto_leaves_selection
        return True

    def get_selected_range(self) -> list[str]:
        if self.block is None:
            return [cell_address(*self.caret)]
        end = (self.caret[0] + 1, self.caret[1]) if self.range_off else self.caret
        return [f'{cell_address(*self.block)}:{cell_address(*end)}']

    def SelectCtrlFront(self) -> bool:  # noqa: N802
        self.selected = True
        self.current_selected = OTHER_INST if self.select_wrong else self.front_inst
        return True

    def GetTextFile(self, fmt: str, option: str) -> str:  # noqa: N802
        assert (fmt, option) == ('HWPML2X', 'saveblock') and self.selected
        return self.grid.xml()

    def Run(self, name: str) -> bool:  # noqa: N802 - plain hwp.Run entry point
        self.plain_run_calls.append(name)
        return self.run(name)

    def TableSplitCell(self, Rows: int = 2, Cols: int = 0, DistributeHeight: int = 0, Merge: int = 0) -> bool:  # noqa: N802,N803
        self.log.append(f'TableSplitCell(Rows={Rows}, Cols={Cols})')
        if 'TableSplitCell' in self.noop:
            return True
        rows, cols = max(Rows, 1), max(Cols, 1)
        if self.split_swap:
            rows, cols = cols, rows
        self.grid.split(*self.caret, rows, cols)
        return True

    def run(self, name: str) -> bool:
        self.log.append(name)
        if name in self.MUTATING and self.parent_after_first_action is not None:
            self.parent_inst = self.parent_after_first_action
        if name in self.MUTATING and self.front_after_first_action is not None:
            self.front_inst = self.front_after_first_action
        if name in self.noop:
            return True
        row, col = self.caret
        shift = -1 if self.wrong_line else 0
        if name == 'TableInsertUpperRow':
            self.grid.insert_row(row)
            self.caret = (row + 1, col)
        elif name == 'TableInsertLowerRow':
            self.grid.insert_row(row + 1)
        elif name == 'TableInsertLeftColumn':
            self.grid.insert_col(col)
            self.caret = (row, col + 1)
        elif name == 'TableInsertRightColumn':
            self.grid.insert_col(col + 1)
        elif name == 'TableDeleteRow':
            self.grid.delete_row(max(row + shift, 1))
            previous = self.caret_after_delete == 'previous'
            self.caret = (max(1, min(row - 1 if previous else row, self.grid.rows)), col)
        elif name == 'TableDeleteColumn':
            self.grid.delete_col(max(col + shift, 1))
            self.caret = (row, min(col, self.grid.cols))
        elif name == 'TableCellBlock':
            self.block = self.caret
        elif name in ('TableRightCell', 'TableLowerCell'):
            self.caret = (row, col + 1) if name == 'TableRightCell' else (row + 1, col)
        elif name == 'TableMergeCell' and self.block is not None:
            self.grid.merge(self.block, self.caret)
            self.caret = self.block
        elif name == 'Cancel':
            if self.block is not None:
                self.caret = self.block
            self.block = None
            if not self.sticky_selection:
                self.selected = False
        return True


def _service(hwp: _FakeTableHwp) -> LocalCliService:
    service = object.__new__(LocalCliService)
    service._bundle_resolve_control_target = lambda _hwp, step, *, op_name, require_table=False: {  # type: ignore[method-assign]
        'target_id': step['target_id'],
        'expected_hash': step['expected_hash'],
        'expected_page': step['expected_page'],
        'page_from': 1,
        'page_to': 1,
        'section_anchor': None,
        'around': None,
        'enumeration_mode': 'fake',
        'target_ctrl': _Ctrl(TARGET_INST),
        'before_item': {'type': 'tbl'},
        'target_anchor_pos': (0, 0, 0),
    }
    service._bundle_enter_table_cell_for_ctrl = lambda _hwp, _ctrl: (  # type: ignore[method-assign]
        setattr(hwp, 'caret', (1, 1)) or {'is_cell': True, 'normal_edit_state': True, 'cell_addr': 'A1'}
    )

    def snapshot(_hwp: Any) -> dict[str, Any]:
        if hwp.snapshot_unknown:
            return {'is_cell': True, 'selection_mode': None}
        if hwp.has_selection_unknown:
            return {'is_cell': True, 'selection_mode': 0}  # selected-pos probe failed
        return {'is_cell': True, 'has_selection': bool(hwp.selected or hwp.block is not None or hwp.stuck_block), 'selection_mode': 0}

    service._bundle_compact_snapshot = snapshot  # type: ignore[method-assign]
    return service


def _exec_step(rows: int, cols: int, **overrides: Any) -> dict[str, Any]:
    step = {
        'op': 'table_structure_exact',
        'target_id': 'ctrl/7',
        'expected_hash': 'sha256:abc',
        'expected_page': 1,
        'expected_rows': rows,
        'expected_cols': cols,
        'row': 1,
        'col': 1,
        'confirm_layout': True,
    }
    step.update(overrides)
    return step


def _run(hwp: _FakeTableHwp, **overrides: Any) -> dict[str, Any]:
    return _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(hwp.grid.rows, hwp.grid.cols, **overrides))


class ServiceFlowTests(unittest.TestCase):
    def test_each_action_runs_and_verifies(self) -> None:
        merge_keys = ['TableCellBlock', 'TableCellBlockExtend', 'TableRightCell', 'TableLowerCell', 'TableMergeCell', 'Cancel']
        cases = (
            ({'action': 'insert_row_above', 'row': 2, 'count': 2}, (5, 2), ['TableInsertUpperRow'] * 2),
            ({'action': 'insert_row_below', 'row': 3}, (4, 2), ['TableInsertLowerRow']),
            ({'action': 'insert_col_left', 'col': 2}, (3, 3), ['TableInsertLeftColumn']),
            ({'action': 'insert_col_right', 'col': 2, 'count': 3}, (3, 5), ['TableInsertRightColumn'] * 3),
            ({'action': 'delete_row', 'row': 2, 'count': 2}, (1, 2), ['TableDeleteRow'] * 2),
            ({'action': 'delete_col', 'col': 1}, (3, 1), ['TableDeleteColumn']),
            ({'action': 'merge_cells', 'row': 2, 'col': 1, 'end_row': 3, 'end_col': 2}, (3, 2), merge_keys),
            ({'action': 'split_cell', 'row': 2, 'col': 2, 'split_cols': 2}, (3, 3), ['TableSplitCell(Rows=0, Cols=2)']),
            ({'action': 'split_cell', 'row': 1, 'col': 1, 'split_rows': 2, 'split_cols': 3}, (4, 4), ['TableSplitCell(Rows=2, Cols=3)']),
        )
        for overrides, (rows, cols), native in cases:
            with self.subTest(action=overrides['action']):
                hwp = _FakeTableHwp(3, 2)
                result = _run(hwp, **overrides)
                self.assertTrue(result['succeeded'])
                self.assertTrue(result['verification']['ok'], result['verification'])
                self.assertEqual((result['after_grid']['rows'], result['after_grid']['cols']), (rows, cols))
                self.assertEqual(result['target_proof']['ctrl_inst_id'], TARGET_INST)
                self.assertNotIn('text', result['after_grid']['cells'][0])
                self.assertEqual(hwp.log, native)
                self.assertEqual(hwp.plain_run_calls, [])

    def test_repeats_restart_from_target_cell(self) -> None:
        # Hancom may leave the caret on the previous row after a delete; the
        # second delete must still hit the planned row, not the one above.
        hwp = _FakeTableHwp(4, 2, caret_after_delete='previous')
        result = _run(hwp, action='delete_row', row=2, count=2)
        self.assertTrue(result['verification']['ok'])
        self.assertEqual(sorted(cell[2] for cell in hwp.grid.cells.values()), ['R1C1', 'R1C2', 'R4C1', 'R4C2'])

    def test_plan_refusal_happens_before_any_native_action(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        with self.assertRaises(LocalCliRuntimeError) as caught:
            _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(4, 2, action='insert_row_below'))
        self.assertNotIsInstance(caught.exception, LocalCliMutationError)
        self.assertIn('re-inventory', str(caught.exception))
        self.assertEqual(hwp.log, [])

    def test_unverified_or_misplaced_changes_report_possible_mutation(self) -> None:
        cases = (
            ({'noop': {'TableInsertLowerRow'}}, {'action': 'insert_row_below'}, 'rows is 3, expected 4'),
            ({'wrong_line': True}, {'action': 'delete_row', 'row': 2}, 'content changed'),
            ({'wrong_line': True}, {'action': 'delete_col', 'col': 2}, 'content changed'),
            ({'split_swap': True}, {'action': 'split_cell', 'row': 1, 'col': 1, 'split_cols': 2}, 'rows is 4, expected 3'),
        )
        for faults, overrides, reason in cases:
            with self.subTest(faults=faults, action=overrides['action']):
                hwp = _FakeTableHwp(3, 2, **faults)
                with self.assertRaises(LocalCliMutationError) as caught:
                    _run(hwp, **overrides)
                self.assertTrue(caught.exception.mutation_may_have_persisted)
                self.assertIn('refused to mark success', str(caught.exception))
                self.assertIn(reason, str(caught.exception))

    def test_raising_native_action_is_not_retried_through_plain_run(self) -> None:
        hwp = _FakeTableHwp(3, 2, raising={'TableDeleteRow'})
        with self.assertRaises(LocalCliMutationError) as caught:
            _run(hwp, action='delete_row', row=2)
        self.assertTrue(caught.exception.mutation_may_have_persisted)
        self.assertIn('outcome_unknown', str(caught.exception))
        self.assertEqual(hwp.log, ['TableDeleteRow!raised'])
        self.assertEqual(hwp.plain_run_calls, [])
        self.assertEqual(hwp.grid.rows, 3)

    def test_unproven_edit_state_is_refused_before_mutation(self) -> None:
        cases = (
            ({'sticky_selection': True}, 'after grid readback'),
            # Readback is clean; only the check right before mutation sees the block.
            ({'goto_leaves_selection': True}, 'before mutation: editor is not provably in normal edit state'),
            # An unreadable selection state is not treated as "no selection".
            ({'snapshot_unknown': True}, 'normal edit state'),
            ({'has_selection_unknown': True}, 'normal edit state'),
        )
        for faults, message in cases:
            with self.subTest(faults=faults):
                hwp = _FakeTableHwp(3, 2, **faults)
                with self.assertRaises(LocalCliRuntimeError) as caught:
                    _run(hwp, action='insert_row_below')
                self.assertNotIsInstance(caught.exception, LocalCliMutationError)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn('TableInsertLowerRow', hwp.log)

    def test_caret_in_another_table_is_refused_before_mutation(self) -> None:
        for faults, message in (
            ({'parent_inst': OTHER_INST}, 'grid readback is not bound to the target table'),
            ({'select_wrong': True}, 'could not select the target table'),
        ):
            with self.subTest(faults=faults):
                hwp = _FakeTableHwp(3, 2, **faults)
                with self.assertRaises(LocalCliRuntimeError) as caught:
                    _run(hwp, action='insert_row_below')
                self.assertNotIsInstance(caught.exception, LocalCliMutationError)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(hwp.log, [])

    def test_picture_at_caret_does_not_break_post_edit_readback(self) -> None:
        # Native finding on 768647e: with a picture in A2, SelectCtrlFront picked
        # the picture during post-edit readback and a correct edit was reported
        # as possibly-persisted failure. Exact SelectCtrl(CtrlInstID) avoids it.
        hwp = _FakeTableHwp(3, 2, front_after_first_action='gso-picture')
        hwp.grid.cells[(2, 1)][3] = ('PICTURE',)
        result = _run(hwp, action='insert_row_below', row=1)
        self.assertTrue(result['verification']['ok'], result['verification'])
        self.assertEqual(hwp.grid.cells[(3, 1)][3], ('PICTURE',))

    def test_readback_falls_back_to_select_ctrl_front_only_when_it_selects_the_target(self) -> None:
        hwp = _FakeTableHwp(3, 2, no_select_ctrl=True)
        self.assertTrue(_run(hwp, action='insert_row_below', row=1)['verification']['ok'])
        hwp = _FakeTableHwp(3, 2, no_select_ctrl=True, front_after_first_action='gso-picture')
        with self.assertRaises(LocalCliMutationError) as caught:
            _run(hwp, action='insert_row_below', row=1)
        self.assertIn('could not select the target table', str(caught.exception))

    def test_table_switch_between_repeats_stops_with_possible_mutation(self) -> None:
        # After the first native delete the caret reports a different table:
        # the second delete must not run, and the step reports possible mutation.
        hwp = _FakeTableHwp(4, 2, parent_after_first_action=OTHER_INST)
        with self.assertRaises(LocalCliMutationError) as caught:
            _run(hwp, action='delete_row', row=2, count=2)
        self.assertIn('caret is not inside the target table', str(caught.exception))
        self.assertEqual(hwp.log.count('TableDeleteRow'), 1)

    def test_state_lost_between_repeats_stops_with_possible_mutation(self) -> None:
        hwp = _FakeTableHwp(4, 2)
        original_run = hwp.run

        def run_then_lose_state(name: str) -> bool:
            result = original_run(name)
            if name == 'TableDeleteRow':
                hwp.snapshot_unknown = True
            return result

        hwp.run = run_then_lose_state  # type: ignore[method-assign]
        with self.assertRaises(LocalCliMutationError) as caught:
            _run(hwp, action='delete_row', row=2, count=2)
        self.assertIn('normal edit state', str(caught.exception))
        self.assertEqual(hwp.log.count('TableDeleteRow'), 1)

    def test_merge_checks_selected_range_before_merging(self) -> None:
        hwp = _FakeTableHwp(3, 2, range_off=True)
        with self.assertRaises(LocalCliRuntimeError) as caught:
            _run(hwp, action='merge_cells', row=1, col=1, end_row=1, end_col=2)
        self.assertNotIsInstance(caught.exception, LocalCliMutationError)
        self.assertIn('refused before TableMergeCell', str(caught.exception))
        self.assertNotIn('TableMergeCell', hwp.log)
        self.assertEqual(hwp.log[-1], 'Cancel')
        self.assertEqual(len(hwp.grid.cells), 6)

    def test_merge_over_already_merged_range_is_refused_without_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        _run(hwp, action='merge_cells', end_row=2, end_col=1)
        hwp.log.clear()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'already merged'):
            _run(hwp, action='merge_cells', end_row=2, end_col=2)
        self.assertEqual(hwp.log, [])

    def test_row_edit_across_merged_cell_is_refused_without_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        _run(hwp, action='merge_cells', end_row=2, end_col=1)
        hwp.log.clear()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'merged cell A1'):
            _run(hwp, action='insert_row_below', row=1)
        self.assertEqual(hwp.log, [])

    def test_unreachable_cell_is_refused_without_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        hwp.goto_addr = lambda _addr: False  # type: ignore[method-assign]
        hwp.run = lambda name: hwp.log.append(name) or True  # type: ignore[method-assign]
        with self.assertRaisesRegex(LocalCliRuntimeError, 'could not place the caret'):
            _run(hwp, action='delete_row', row=3)
        self.assertNotIn('TableDeleteRow', hwp.log)

    def test_target_without_ctrl_inst_id_is_refused(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        service = _service(hwp)
        resolve = service._bundle_resolve_control_target
        service._bundle_resolve_control_target = lambda *args, **kwargs: {**resolve(*args, **kwargs), 'target_ctrl': object()}  # type: ignore[method-assign]
        with self.assertRaisesRegex(LocalCliRuntimeError, 'exposes no CtrlInstID'):
            service._bundle_table_structure_exact(hwp, _exec_step(3, 2, action='insert_row_below'))
        self.assertEqual(hwp.log, [])


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(LocalCliService)
        self.service.command_packages = get_command_package_registry()

    def _validate(self, **overrides: Any) -> dict[str, Any]:
        step = {'op': 'table_structure_exact', 'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1,
                'action': 'insert_row_below', 'row': 1, 'col': 1, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True}
        step.update(overrides)
        step = {key: value for key, value in step.items() if value is not None}
        return self.service._validate_command_bundle_steps([step])[0]

    def test_valid_steps(self) -> None:
        self.assertEqual(self._validate()['action'], 'insert_row_below')
        self.assertEqual(self._validate(action='merge_cells', end_row=2, end_col=2)['end_col'], 2)
        self.assertEqual(self._validate(action='split_cell', split_rows=3)['split_rows'], 3)

    def test_rejections(self) -> None:
        cases = (
            ({'action': 'drop_table'}, 'action must be one of'),
            ({'confirm_layout': None}, 'confirm_layout'),
            ({'row': None}, 'requires row'),
            ({'expected_cols': None}, 'requires expected_cols'),
            ({'count': 51}, 'count must be 1..50'),
            ({'end_row': 2}, 'does not take: end_row'),
            ({'action': 'merge_cells', 'end_row': 2}, 'requires end_row and end_col'),
            ({'action': 'split_cell'}, 'product >= 2'),
            ({'action': 'split_cell', 'split_rows': 21}, 'product >= 2'),
            ({'action': 'delete_row', 'split_cols': 2}, 'does not take: split_cols'),
            ({'row': 0}, 'positive integer'),
            ({'target_id': 'tbl/1'}, 'exact target_id'),
            ({'unexpected': 1}, 'unsupported fields'),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(LocalCliServiceError, message):
                self._validate(**overrides)


class CliBundleTests(unittest.TestCase):
    BASE = ['--page-from', '2', '--target-id', 'ctrl/5', '--expected-hash', 'sha256:x', '--expected-page', '2',
            '--row', '1', '--col', '1', '--expected-rows', '3', '--expected-cols', '3', '--confirm-layout']

    def test_bundle_payload_passes_server_validation(self) -> None:
        service = object.__new__(LocalCliService)
        service.command_packages = get_command_package_registry()
        for extra in (['--action', 'insert_col_right', '--count', '2'], ['--action', 'merge_cells', '--end-row', '2', '--end-col', '3'],
                      ['--action', 'split_cell', '--split-rows', '2']):
            with self.subTest(extra=extra):
                steps = build_named_bundle('table-structure-exact', self.BASE + extra).server_payload()['steps']
                self.assertEqual([step['op'] for step in steps], ['where', 'table_structure_exact', 'where'])
                service._validate_command_bundle_steps(steps)

    def test_bundle_rejects_options_for_other_actions(self) -> None:
        for extra, message in ((['--action', 'delete_row', '--end-row', '2'], 'does not apply'),
                               (['--action', 'merge_cells', '--end-row', '2'], 'requires --end-row and --end-col'),
                               (['--action', 'split_cell'], 'at least two cells')):
            with self.subTest(extra=extra), self.assertRaisesRegex(BundleError, message):
                build_named_bundle('table-structure-exact', self.BASE + extra)


class BundleUndoTests(unittest.TestCase):
    """`command_bundle` end to end with the real step dispatch, then `hwpx undo`."""

    def _bundle(self, hwp: _FakeTableHwp, store: dict[str, Any], **overrides: Any) -> tuple[LocalCliService, dict[str, Any]]:
        service = _service(hwp)
        service.command_packages = get_command_package_registry()

        def execute_live(*, handler: Any, command_name: str = '', **kwargs: Any) -> Any:
            if command_name == 'undo':
                store['undo_dispatched'] = True
                return {'snapshot': {}, 'context': {}, 'location': {}}
            return handler(SimpleNamespace(hwp=hwp, source_filename='a.hwpx', session_id='s'))

        def save(binding: dict[str, Any]) -> dict[str, Any]:
            store['binding'] = binding
            return binding

        service._load_active_binding = lambda session_id=None: store['binding']  # type: ignore[method-assign]
        service._save_binding = save  # type: ignore[method-assign]
        service._update_live_binding = lambda current, **kwargs: current  # type: ignore[method-assign]
        service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service._execute_live = execute_live  # type: ignore[method-assign]
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            result = service.command_bundle(steps=[_exec_step(hwp.grid.rows, hwp.grid.cols, page_from=1, page_to=1, **overrides)])
        return service, result

    def _store(self) -> dict[str, Any]:
        return {'binding': {'session_id': 's', 'command_generation': 0, 'native_command_sequence': 0, 'pending_logical_undo_count': 1}}

    def test_multi_row_insert_refuses_undo(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        store = self._store()
        service, result = self._bundle(hwp, store, action='insert_row_below', row=3, count=3)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['steps'][0]['result']['undo']['native_editing_actions'], 3)
        self.assertEqual(result['steps'][0]['result']['undo']['hwpx_undo'], 'refused')
        self.assertIsNone(store['binding']['pending_logical_undo_count'])
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()
        self.assertNotIn('undo_dispatched', store)

    def test_failure_after_mutation_refuses_undo(self) -> None:
        hwp = _FakeTableHwp(3, 2, noop={'TableInsertLowerRow'})
        store = self._store()
        service, result = self._bundle(hwp, store, action='insert_row_below', row=3, count=2)
        self.assertFalse(result['ok'])
        self.assertTrue(result['steps'][0]['mutation_may_have_persisted'])
        self.assertEqual(result['steps'][0]['rollback']['undo']['native_editing_actions'], 2)
        self.assertEqual(result['steps'][0]['rollback']['undo']['hwpx_undo'], 'refused')
        self.assertIn('without saving and reopen', result['steps'][0]['rollback']['hint'])
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()

    def test_single_native_edit_keeps_one_undo(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        store = self._store()
        _service_obj, result = self._bundle(hwp, store, action='insert_row_below', row=3)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['steps'][0]['result']['undo']['hwpx_undo'], 'one_step')
        self.assertEqual(store['binding']['pending_logical_undo_count'], 1)
        self.assertIsNone(store['binding'].get('logical_undo_unverified'))
        older = self._store()
        older['binding'].update(pending_logical_undo_count=None, logical_undo_unverified='an older multi-edit')
        _service_obj, result = self._bundle(_FakeTableHwp(3, 2), older, action='insert_row_below', row=3)
        self.assertEqual((older['binding']['pending_logical_undo_count'], older['binding']['logical_undo_unverified']), (1, 'an older multi-edit'))

    def test_policy_table_decides_not_the_step_report(self) -> None:
        single = {'native_editing_actions': 1, 'hwpx_undo': 'one_step', 'single_undo_expected': True}
        for op, report, ok, expected in (
            ('table_structure_exact', single, True, 1),
            ('table_structure_exact', single, False, None),
            ('table_structure_exact', {**single, 'native_editing_actions': 3}, True, None),
            ('object_insert_exact', single, True, None),
            ('layout_exact', single, True, None),
        ):
            with self.subTest(op=op, ok=ok, actions=report['native_editing_actions']):
                binding: dict[str, Any] = {}
                _record_bundle_undo_state(binding, [{'op': op, 'dirty': True, 'ok': ok, 'result': {'undo': report}}], {})
                self.assertEqual(binding['pending_logical_undo_count'], expected)

    def test_refused_before_mutation_restores_prior_undo_state(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        store = self._store()
        _service_obj, result = self._bundle(hwp, store, action='insert_row_below', row=3, expected_rows=9)
        self.assertFalse(result['ok'])
        self.assertEqual(store['binding']['pending_logical_undo_count'], 1)
        self.assertIsNone(store['binding'].get('logical_undo_unverified'))


if __name__ == '__main__':
    unittest.main()
