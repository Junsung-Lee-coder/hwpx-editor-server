from __future__ import annotations

import unittest
from typing import Any

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliMutationError, LocalCliService, LocalCliServiceError
from app.table_structure import (
    TableStructureError,
    cell_address,
    check_plan,
    evaluate_change,
    parse_cell_address,
    parse_table_grid_xml,
)
from local_cli_v1.bundles import BundleError, build_named_bundle


def _hwpml(rows: int, cols: int, cells: list[tuple[int, int, int, int]] | None = None, *, declaration: bool = False) -> str:
    """cells: (row, col, row_span, col_span), 1-based; default is an unmerged grid."""
    if cells is None:
        cells = [(r, c, 1, 1) for r in range(1, rows + 1) for c in range(1, cols + 1)]
    by_row: dict[int, list[str]] = {}
    for row, col, row_span, col_span in cells:
        by_row.setdefault(row, []).append(
            f'<CELL RowAddr="{row - 1}" ColAddr="{col - 1}" RowSpan="{row_span}" ColSpan="{col_span}"><PARALIST/></CELL>'
        )
    body = ''.join(f'<ROW>{"".join(items)}</ROW>' for _row, items in sorted(by_row.items()))
    prefix = '<?xml version="1.0" encoding="UTF-16" standalone="no"?>' if declaration else ''
    return f'{prefix}<HWPML><BODY><SECTION><P><TEXT><TABLE RowCount="{rows}" ColCount="{cols}">{body}</TABLE></TEXT></P></SECTION></BODY></HWPML>'


def _grid(rows: int, cols: int, cells: list[tuple[int, int, int, int]] | None = None) -> dict[str, Any]:
    return parse_table_grid_xml(_hwpml(rows, cols, cells))


def _step(**overrides: Any) -> dict[str, Any]:
    step = {'action': 'insert_row_below', 'row': 1, 'col': 1, 'expected_rows': 3, 'expected_cols': 2}
    step.update(overrides)
    return step


class AddressTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        for row, col, text in ((1, 1, 'A1'), (3, 2, 'B3'), (10, 26, 'Z10'), (2, 27, 'AA2'), (5, 52, 'AZ5')):
            self.assertEqual(cell_address(row, col), text)
            self.assertEqual(parse_cell_address(text), (row, col))

    def test_rejects_non_addresses(self) -> None:
        for value in (None, '', 'A0', '1A', 'A-1', ('A', 1), 3):
            self.assertIsNone(parse_cell_address(value))


class GridReadbackTests(unittest.TestCase):
    def test_counts_rows_cols_and_cells(self) -> None:
        grid = _grid(2, 3)
        self.assertEqual((grid['rows'], grid['cols'], grid['cell_count']), (2, 3, 6))
        self.assertEqual(grid['cells'][0], {'row': 1, 'col': 1, 'row_span': 1, 'col_span': 1})

    def test_accepts_xml_declaration_in_str(self) -> None:
        self.assertEqual(parse_table_grid_xml(_hwpml(1, 2, declaration=True))['cell_count'], 2)

    def test_ignores_nested_tables(self) -> None:
        nested = '<TABLE RowCount="5" ColCount="5"><ROW><CELL RowAddr="0" ColAddr="0" RowSpan="1" ColSpan="1"/></ROW></TABLE>'
        xml = _hwpml(1, 2).replace('<PARALIST/></CELL>', f'<PARALIST><P><TEXT>{nested}</TEXT></P></PARALIST></CELL>', 1)
        grid = parse_table_grid_xml(xml)
        self.assertEqual((grid['rows'], grid['cols'], grid['cell_count']), (1, 2, 2))

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

    def test_merge_requires_unmerged_range_inside_table(self) -> None:
        plan = check_plan(_step(action='merge_cells', end_row=2, end_col=2), _grid(3, 2))
        self.assertEqual((plan['area'], plan['end_address']), (4, 'B2'))
        with self.assertRaisesRegex(TableStructureError, 'inside'):
            check_plan(_step(action='merge_cells', end_row=4, end_col=2), _grid(3, 2))
        with self.assertRaisesRegex(TableStructureError, 'at least two'):
            check_plan(_step(action='merge_cells', end_row=1, end_col=1), _grid(3, 2))
        merged = _grid(3, 2, [(1, 1, 2, 1), (1, 2, 1, 1), (2, 2, 1, 1), (3, 1, 1, 1), (3, 2, 1, 1)])
        with self.assertRaisesRegex(TableStructureError, 'already merged'):
            check_plan(_step(action='merge_cells', end_row=2, end_col=2), merged)

    def test_split_requires_unmerged_top_left_cell(self) -> None:
        merged = _grid(2, 2, [(1, 1, 1, 2), (2, 1, 1, 1), (2, 2, 1, 1)])
        with self.assertRaisesRegex(TableStructureError, 'covered by merged cell A1'):
            check_plan(_step(action='split_cell', row=1, col=2, split_cols=2, expected_rows=2), merged)
        with self.assertRaisesRegex(TableStructureError, 'is a merged cell'):
            check_plan(_step(action='split_cell', row=1, col=1, split_cols=2, expected_rows=2), merged)
        with self.assertRaisesRegex(TableStructureError, '>= 2'):
            check_plan(_step(action='split_cell', expected_rows=2, expected_cols=2), _grid(2, 2))

    def test_merge_and_split_need_cell_level_readback(self) -> None:
        grid = parse_table_grid_xml('<HWPML><TABLE RowCount="2" ColCount="2"><ROW><CELL/></ROW></TABLE></HWPML>')
        with self.assertRaisesRegex(TableStructureError, 'cell-level'):
            check_plan(_step(action='merge_cells', end_row=1, end_col=2, expected_rows=2), grid)


class EvaluateChangeTests(unittest.TestCase):
    def _plan(self, before: dict[str, Any], **step: Any) -> dict[str, Any]:
        return check_plan(_step(expected_rows=before['rows'], expected_cols=before['cols'], **step), before)

    def test_row_and_column_counts(self) -> None:
        before = _grid(3, 2)
        cases = (
            ({'action': 'insert_row_above', 'count': 2}, _grid(5, 2), True),
            ({'action': 'insert_row_below'}, _grid(3, 2), False),
            ({'action': 'insert_col_left'}, _grid(3, 3), True),
            ({'action': 'delete_row', 'row': 2}, _grid(2, 2), True),
            ({'action': 'delete_col', 'col': 2}, _grid(3, 1), True),
            ({'action': 'delete_col', 'col': 2}, _grid(2, 1), False),
        )
        for step, after, ok in cases:
            with self.subTest(step=step):
                self.assertEqual(evaluate_change(self._plan(before, **step), before, after)['ok'], ok)

    def test_merge_checks_span_and_cell_count(self) -> None:
        before = _grid(2, 2)
        plan = self._plan(before, action='merge_cells', end_row=1, end_col=2)
        good = _grid(2, 2, [(1, 1, 1, 2), (2, 1, 1, 1), (2, 2, 1, 1)])
        self.assertTrue(evaluate_change(plan, before, good)['ok'])
        wrong_span = _grid(2, 2, [(1, 1, 2, 1), (1, 2, 1, 1), (2, 2, 1, 1)])
        result = evaluate_change(plan, before, wrong_span)
        self.assertFalse(result['ok'])
        self.assertTrue(any('does not span' in reason for reason in result['reasons']))
        self.assertFalse(evaluate_change(plan, before, before)['ok'])

    def test_split_checks_cell_count(self) -> None:
        before = _grid(2, 2)
        plan = self._plan(before, action='split_cell', split_rows=2, split_cols=2)
        after = _grid(4, 4, [(r, c, 1, 1) for r in range(1, 5) for c in range(1, 5)][:7])
        self.assertTrue(evaluate_change(plan, before, after)['ok'])
        self.assertFalse(evaluate_change(plan, before, before)['ok'])


class _Grid:
    """Tiny table model: 1-based cells with spans, enough to fake Hancom actions."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.cells = {(r, c): [1, 1] for r in range(1, rows + 1) for c in range(1, cols + 1)}

    def xml(self) -> str:
        return _hwpml(self.rows, self.cols, [(r, c, span[0], span[1]) for (r, c), span in sorted(self.cells.items())])

    def insert_row(self, at: int) -> None:
        self.cells = {((r + 1) if r >= at else r, c): span for (r, c), span in self.cells.items()}
        self.cells.update({(at, c): [1, 1] for c in range(1, self.cols + 1)})
        self.rows += 1

    def insert_col(self, at: int) -> None:
        self.cells = {(r, (c + 1) if c >= at else c): span for (r, c), span in self.cells.items()}
        self.cells.update({(r, at): [1, 1] for r in range(1, self.rows + 1)})
        self.cols += 1

    def delete_row(self, at: int) -> None:
        self.cells = {((r - 1) if r > at else r, c): span for (r, c), span in self.cells.items() if r != at}
        self.rows -= 1

    def delete_col(self, at: int) -> None:
        self.cells = {(r, (c - 1) if c > at else c): span for (r, c), span in self.cells.items() if c != at}
        self.cols -= 1


class _FakeHAction:
    def __init__(self, hwp: _FakeTableHwp) -> None:
        self.hwp = hwp

    def Run(self, name: str) -> bool:  # noqa: N802 - Hancom API name
        return self.hwp.run(name)


class _FakeTableHwp:
    def __init__(self, rows: int, cols: int, *, broken_actions: set[str] | None = None) -> None:
        self.grid = _Grid(rows, cols)
        self.caret = (1, 1)
        self.block: tuple[int, int] | None = None
        self.selected = False
        self.log: list[str] = []
        self.broken = broken_actions or set()
        self.HAction = _FakeHAction(self)

    # position / selection
    def get_pos(self) -> tuple[int, int, int]:
        return (1, 0, 0)

    def set_pos(self, *_args: Any) -> bool:
        self.selected = False
        return True

    def get_cell_addr(self, as_: str = 'str') -> Any:
        row, col = self.caret
        if as_ == 'tuple':
            return (row - 1, col - 1)  # pyhwpx order: (row, col)
        return cell_address(row, col)

    def goto_addr(self, addr: str) -> bool:
        parsed = parse_cell_address(addr)
        if parsed is None or parsed not in self.grid.cells:
            return False
        self.caret = parsed
        return True

    def SelectCtrlFront(self) -> bool:  # noqa: N802
        self.selected = True
        return True

    def GetTextFile(self, fmt: str, option: str) -> str:  # noqa: N802
        assert (fmt, option) == ('HWPML2X', 'saveblock') and self.selected
        return self.grid.xml()

    def TableSplitCell(self, Rows: int = 2, Cols: int = 0, DistributeHeight: int = 0, Merge: int = 0) -> bool:  # noqa: N802,N803
        self.log.append(f'TableSplitCell(Rows={Rows}, Cols={Cols})')
        if 'TableSplitCell' in self.broken:
            return True
        rows, cols = max(Rows, 1), max(Cols, 1)
        row, col = self.caret
        for _ in range(cols - 1):
            self.grid.insert_col(col + 1)
        for _ in range(rows - 1):
            self.grid.insert_row(row + 1)
        # Neighbours in the widened row/column become merged cells.
        for (r, c), span in list(self.grid.cells.items()):
            if (r, c) == (row, col):
                continue
            if r == row and not (col <= c < col + cols) and (r, c) in self.grid.cells:
                span[0] = rows
                for extra in range(1, rows):
                    self.grid.cells.pop((r + extra, c), None)
            if c == col and not (row <= r < row + rows) and (r, c) in self.grid.cells:
                span[1] = cols
                for extra in range(1, cols):
                    self.grid.cells.pop((r, c + extra), None)
        return True

    def run(self, name: str) -> bool:
        self.log.append(name)
        if name in self.broken:
            return True  # reports success but changes nothing
        row, col = self.caret
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
            self.grid.delete_row(row)
            self.caret = (min(row, self.grid.rows), col)
        elif name == 'TableDeleteColumn':
            self.grid.delete_col(col)
            self.caret = (row, min(col, self.grid.cols))
        elif name == 'TableCellBlock':
            self.block = self.caret
        elif name in ('TableRightCell', 'TableLowerCell'):
            self.caret = (row, col + 1) if name == 'TableRightCell' else (row + 1, col)
        elif name == 'TableMergeCell' and self.block is not None:
            (r1, c1), (r2, c2) = self.block, self.caret
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    if (r, c) != (r1, c1):
                        self.grid.cells.pop((r, c), None)
            self.grid.cells[(r1, c1)] = [r2 - r1 + 1, c2 - c1 + 1]
            self.caret = (r1, c1)
        elif name == 'Cancel':
            self.block = None
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
        'target_ctrl': object(),
        'before_item': {'type': 'tbl'},
        'target_anchor_pos': (0, 0, 0),
    }
    service._bundle_enter_table_cell_for_ctrl = lambda _hwp, _ctrl: (  # type: ignore[method-assign]
        setattr(hwp, 'caret', (1, 1)) or {'is_cell': True, 'normal_edit_state': True, 'cell_addr': 'A1'}
    )
    service._bundle_compact_snapshot = lambda _hwp: {  # type: ignore[method-assign]
        'is_cell': True,
        'has_selection': hwp.selected or hwp.block is not None,
        'selection_mode': 0,
    }
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


class ServiceFlowTests(unittest.TestCase):
    def test_each_action_runs_and_verifies(self) -> None:
        cases = (
            ({'action': 'insert_row_above', 'row': 2, 'count': 2}, (5, 2), ['TableInsertUpperRow'] * 2),
            ({'action': 'insert_row_below', 'row': 3}, (4, 2), ['TableInsertLowerRow']),
            ({'action': 'insert_col_left', 'col': 2}, (3, 3), ['TableInsertLeftColumn']),
            ({'action': 'insert_col_right', 'col': 2, 'count': 3}, (3, 5), ['TableInsertRightColumn'] * 3),
            ({'action': 'delete_row', 'row': 2, 'count': 2}, (1, 2), ['TableDeleteRow'] * 2),
            ({'action': 'delete_col', 'col': 1}, (3, 1), ['TableDeleteColumn']),
            ({'action': 'merge_cells', 'row': 2, 'col': 1, 'end_row': 3, 'end_col': 2}, (3, 2),
             ['TableCellBlock', 'TableCellBlockExtend', 'TableRightCell', 'TableLowerCell', 'TableMergeCell', 'Cancel']),
            ({'action': 'split_cell', 'row': 2, 'col': 2, 'split_cols': 2}, (3, 3), ['TableSplitCell(Rows=0, Cols=2)']),
        )
        for overrides, (rows, cols), native in cases:
            with self.subTest(action=overrides['action']):
                hwp = _FakeTableHwp(3, 2)
                result = _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(3, 2, **overrides))
                self.assertTrue(result['succeeded'])
                self.assertTrue(result['verification']['ok'], result['verification'])
                self.assertEqual((result['after_grid']['rows'], result['after_grid']['cols']), (rows, cols))
                mutating = [entry for entry in hwp.log if entry not in ('Cancel',) or overrides['action'] == 'merge_cells']
                self.assertEqual(mutating, native)

    def test_plan_refusal_happens_before_any_native_action(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        with self.assertRaises(LocalCliRuntimeError) as caught:
            _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(4, 2, action='insert_row_below'))
        self.assertNotIsInstance(caught.exception, LocalCliMutationError)
        self.assertIn('re-inventory', str(caught.exception))
        self.assertEqual(hwp.log, [])

    def test_unverified_change_reports_possible_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2, broken_actions={'TableInsertLowerRow'})
        with self.assertRaises(LocalCliMutationError) as caught:
            _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(3, 2, action='insert_row_below'))
        self.assertTrue(caught.exception.mutation_may_have_persisted)
        self.assertIn('refused to mark success', str(caught.exception))

    def test_merge_over_already_merged_range_is_refused_without_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        service = _service(hwp)
        service._bundle_table_structure_exact(hwp, _exec_step(3, 2, action='merge_cells', end_row=2, end_col=1))
        hwp.log.clear()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'already merged'):
            service._bundle_table_structure_exact(hwp, _exec_step(3, 2, action='merge_cells', end_row=2, end_col=2))
        self.assertEqual(hwp.log, [])

    def test_unreachable_cell_is_refused_without_mutation(self) -> None:
        hwp = _FakeTableHwp(3, 2)
        hwp.goto_addr = lambda _addr: False  # type: ignore[method-assign]
        hwp.run = lambda name: hwp.log.append(name) or True  # type: ignore[method-assign]
        with self.assertRaisesRegex(LocalCliRuntimeError, 'could not place the caret'):
            _service(hwp)._bundle_table_structure_exact(hwp, _exec_step(3, 2, action='delete_row', row=3))
        self.assertNotIn('TableDeleteRow', hwp.log)


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


if __name__ == '__main__':
    unittest.main()
