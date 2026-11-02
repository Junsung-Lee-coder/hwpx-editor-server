"""Table inventory, reading, structure, borders, fill and creation through the real bundle validation, package dispatch, service and census.

The SDK is tests.native_table_fakes, built on measured native behavior. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import local_cli_runtime as rt
from hwpx_mcp.schema import Command
from tests.native_table_fakes import SOLID, Cell, FakeTableCtrl, TableHwp, grid, para, sample_document
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc


def ok_result(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def inventory(hwp: TableHwp) -> dict[str, dict]:
    result = ok_result(unittest.TestCase(), Flow(hwp).step(op='table_inventory'))
    return {item['table_id']: item for item in result['tables']}


def binding(hwp: TableHwp, inst: str) -> dict:
    item = inventory(hwp)[f'tbl:{inst}']
    return {'table_id': item['table_id'], 'expected_hash': item['expected_hash'], 'expected_page': item['page'], 'expected_rows': item['rows'], 'expected_cols': item['cols'], 'confirm_layout': True}


def edit(hwp: TableHwp, inst: str, action: str, **fields) -> dict:
    return Flow(hwp).step(op='table_edit_exact', action=action, **binding(hwp, inst), **fields)


def style(hwp: TableHwp, inst: str, **fields) -> dict:
    return Flow(hwp).step(op='table_style_exact', **binding(hwp, inst), **fields)


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: TableHwp, *, baseline: int = 0) -> None:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(len(hwp.native_calls()), baseline)
    test.assertEqual(hwp.SelectionMode, 0)


def texts(hwp: TableHwp, inst: str) -> dict[str, str]:
    table = hwp.by_inst(inst)
    return {cell.address: ''.join(run[0] for item in cell.paragraphs for run in item['runs'] if isinstance(run, tuple)) for cell in table.cells}


def spans(hwp: TableHwp, inst: str) -> dict[str, tuple[int, int]]:
    return {cell.address: (cell.rs, cell.cs) for cell in hwp.by_inst(inst).cells}


RED = '#FF0000'
BLUE = '#0000FF'
LINE = {'line': 'dash', 'width_mm': 0.3, 'color': RED}


class Inventory(unittest.TestCase):
    def test_similar_tables_are_told_apart_and_the_caret_stays(self) -> None:
        hwp = sample_document()
        hwp.set_pos(0, 3, 2)
        tables = inventory(hwp)
        self.assertEqual(len(tables), 7)
        first, second = tables['tbl:1011'], tables['tbl:1012']
        self.assertEqual((first['rows'], first['cols'], second['rows'], second['cols']), (5, 4, 5, 4))
        self.assertNotEqual(first['expected_hash'], second['expected_hash'])
        self.assertEqual(first['before_text'], '표일 앞 문단')
        self.assertEqual(second['before_text'], '표이 앞 문단')
        self.assertEqual(first['page'], 1)
        self.assertEqual(tables['tbl:1016']['page'], 2)
        self.assertEqual(tables['tbl:1015']['merged_cells'], 2)
        self.assertEqual(hwp.get_pos(), (0, 3, 2))
        self.assertEqual(hwp.native_calls(), [])

    def test_two_identical_tables_have_different_identities_and_the_same_contents_hash_is_not_their_identity(self) -> None:
        hwp = TableHwp(['글', grid('1', [['같음']]), grid('2', [['같음']])])
        tables = inventory(hwp)
        self.assertEqual(set(tables), {'tbl:1', 'tbl:2'})

    def test_reading_a_table_gives_exact_text_spans_styles_borders_and_fill(self) -> None:
        hwp = sample_document()
        value = ok_result(self, Flow(hwp).step(op='table_read', table_id='tbl:1011'))
        cells = {cell['address']: cell for cell in value['cells']}
        self.assertEqual((value['rows'], value['cols'], value['expected_hash']), (5, 4, inventory(hwp)['tbl:1011']['expected_hash']))
        self.assertEqual(cells['A2']['text'], '가나 ABC  두칸')
        self.assertEqual(cells['B3']['text'], 'B3앞\tB3뒤')
        self.assertEqual(cells['D4']['text'], 'D4첫문단\nD4둘째문단')
        self.assertEqual(cells['D4']['paragraph_count'], 2)
        self.assertEqual(cells['A5']['objects'], ['picture'])
        self.assertEqual([run['text'] for run in cells['C3']['paragraphs'][0]['runs']], ['C3굵게', 'C3보통'])
        self.assertEqual(cells['A1']['border']['top'], {'line': 'solid', 'width_mm': 0.12, 'color': '#000000'})
        self.assertEqual(cells['A1']['fill'], {'kind': 'none'})
        merged = {cell['address']: cell for cell in ok_result(self, Flow(hwp).step(op='table_read', table_id='tbl:1015'))['cells']}
        self.assertEqual((merged['A1']['row_span'], merged['A1']['col_span'], merged['A2']['row_span']), (1, 2, 2))
        self.assertEqual(merged['C2']['fill'], {'kind': 'solid', 'color': '#FFF2CC'})

    def test_the_reading_is_bounded_and_says_so(self) -> None:
        hwp = sample_document()
        value = ok_result(self, Flow(hwp).step(op='table_read', table_id='tbl:1011', max_cells=3))
        self.assertEqual(len(value['cells']), 3)
        self.assertIs(value['cells_truncated'], True)

    def test_an_unknown_table_is_refused(self) -> None:
        hwp = sample_document()
        outcome = Flow(hwp).step(op='table_read', table_id='tbl:9')
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'table_not_found')


class Targeting(unittest.TestCase):
    def test_a_stale_hash_rows_or_page_stops_before_any_edit_with_the_caret_back(self) -> None:
        hwp = sample_document()
        hwp.set_pos(0, 3, 1)
        good = binding(hwp, '1011')
        cases = [({**good, 'expected_hash': 'sha256:' + '0' * 24}, 'table_state_stale'), ({**good, 'expected_rows': 4}, 'table_grid_mismatch'),
                 ({**good, 'expected_page': 2}, 'target_wrong_page'), ({**good, 'table_id': 'tbl:9'}, 'table_not_found')]
        for fields, code in cases:
            with self.subTest(code=code):
                refused(self, Flow(hwp).step(op='table_edit_exact', action='insert_row_below', row=1, **fields), code, hwp)
                self.assertEqual(hwp.get_pos(), (0, 3, 1))

    def test_after_any_edit_the_old_hash_is_stale(self) -> None:
        hwp = sample_document()
        before = binding(hwp, '1011')
        ok_result(self, Flow(hwp).step(op='table_style_exact', **before, scope='cell', cell='A1', fill='#FF0000'))
        refused(self, Flow(hwp).step(op='table_style_exact', **before, scope='cell', cell='B1', fill='#FF0000'), 'table_state_stale', hwp, baseline=1)

    def test_a_neighbouring_identical_table_is_never_the_one_edited(self) -> None:
        hwp = sample_document()
        other_before = texts(hwp, '1011')
        ok_result(self, edit(hwp, '1012', 'insert_row_below', row=2))
        self.assertEqual(hwp.by_inst('1012').rows, 6)
        self.assertEqual(hwp.by_inst('1011').rows, 5)
        self.assertEqual(texts(hwp, '1011'), other_before)

    def test_the_caret_moved_by_other_reads_is_found_again_before_each_edit(self) -> None:
        hwp = sample_document()
        ok_result(self, edit(hwp, '1012', 'insert_row_below', row=1))
        hwp.set_pos(0, 0, 0)
        Flow(hwp).step(op='table_read', table_id='tbl:1011')
        ok_result(self, style(hwp, '1012', scope='cell', cell='A1', fill=BLUE))
        hwp.set_pos(*hwp.get_pos())
        value = ok_result(self, edit(hwp, '1012', 'insert_col_right', col=4))
        self.assertEqual(value['after']['cols'], 5)

    def test_a_caret_that_lands_in_another_table_stops_before_the_edit(self) -> None:
        hwp = sample_document()
        target = binding(hwp, '1012')
        real = FakeTableCtrl.GetCtrlInstID
        original_parent = type(hwp).ParentCtrl
        other = FakeTableCtrl(hwp.by_inst('1011'))
        with patch.object(type(hwp), 'ParentCtrl', property(lambda self: other)):
            refused(self, Flow(hwp).step(op='table_edit_exact', action='insert_row_below', row=1, **target), 'target_table_not_entered', hwp)
        self.assertIs(FakeTableCtrl.GetCtrlInstID, real)
        self.assertIs(type(hwp).ParentCtrl, original_parent)


class Lines(unittest.TestCase):
    def test_each_insert_is_one_native_action_with_the_right_side_and_count(self) -> None:
        cases = [('insert_row_above', {'row': 2}, 2, 2, (7, 4)), ('insert_row_below', {'row': 2}, 3, 2, (7, 4)), ('insert_col_left', {'col': 2}, 0, 2, (5, 6)), ('insert_col_right', {'col': 2}, 1, 2, (5, 6))]
        for action, where, side, count, shape in cases:
            with self.subTest(action=action):
                hwp = sample_document()
                value = ok_result(self, edit(hwp, '1012', action, count=count, **where))
                calls = hwp.native_calls()
                self.assertEqual([(call[1], call[2]) for call in calls], [('TableInsertRowColumn', {'Side': side, 'Count': count})])
                self.assertEqual((hwp.by_inst('1012').rows, hwp.by_inst('1012').cols), shape)
                self.assertEqual(value['undo']['native_editing_actions'], 1)
                self.assertEqual(value['changes']['inserted_lines'], count)

    def test_blank_and_repeated_rows_can_be_edited_and_their_new_lines_are_blank(self) -> None:
        hwp = sample_document()
        ok_result(self, edit(hwp, '1013', 'insert_row_below', row=1))
        self.assertEqual(hwp.by_inst('1013').rows, 4)
        ok_result(self, edit(hwp, '1014', 'insert_row_above', row=2))
        self.assertEqual(texts(hwp, '1014')['A2'], '')
        self.assertEqual(texts(hwp, '1014')['A3'], '같은글')
        ok_result(self, edit(hwp, '1014', 'delete_row', row=2))
        ok_result(self, edit(hwp, '1014', 'delete_row', row=2, expected_removed_cells=[{'address': 'A2', 'text': '같은글'}, {'address': 'B2', 'text': '같은글'}]))
        self.assertEqual(hwp.by_inst('1014').rows, 3)

    def test_deleting_needs_the_removed_contents_stated_exactly(self) -> None:
        hwp = sample_document()
        refused(self, edit(hwp, '1012', 'delete_row', row=2), 'removed_content_mismatch', hwp)
        refused(self, edit(hwp, '1012', 'delete_row', row=2, expected_removed_cells=[{'address': 'A2', 'text': 'A2'}, {'address': 'B2', 'text': 'B2'}, {'address': 'C2', 'text': 'C2'}]), 'removed_content_mismatch', hwp)
        removed = [{'address': f'{col}2', 'text': f'{col}2'} for col in 'ABCD']
        value = ok_result(self, edit(hwp, '1012', 'delete_row', row=2, expected_removed_cells=removed))
        self.assertEqual(value['changes']['removed_cells'], removed)
        self.assertEqual([(call[1]) for call in hwp.native_calls()], ['TableDeleteRow'])
        self.assertEqual(texts(hwp, '1012')['A2'], 'A3')

    def test_a_removed_object_must_be_named_too(self) -> None:
        hwp = sample_document()
        cells = [{'address': 'A5', 'text': 'A5그림뒤'}, {'address': 'B5', 'text': 'B5'}, {'address': 'C5', 'text': 'C5'}, {'address': 'D5', 'text': 'D5'}]
        refused(self, edit(hwp, '1011', 'delete_row', row=5, expected_removed_cells=cells), 'removed_content_mismatch', hwp)
        cells[0]['objects'] = ['picture']
        ok_result(self, edit(hwp, '1011', 'delete_row', row=5, expected_removed_cells=cells))
        self.assertEqual(hwp.by_inst('1011').rows, 4)

    def test_several_lines_are_one_native_delete_on_a_whole_line_block(self) -> None:
        hwp = sample_document()
        removed = [{'address': f'{col}{row}', 'text': f'{col}{row}'} for row in (2, 3) for col in 'ABCD']
        value = ok_result(self, edit(hwp, '1012', 'delete_row', row=2, count=2, expected_removed_cells=removed))
        calls = hwp.native_calls()
        self.assertEqual([call[1] for call in calls], ['TableDeleteCell'])
        self.assertEqual(calls[0][3], [f'{col}{row}' for row in (2, 3) for col in 'ABCD'])
        self.assertEqual(value['undo']['native_editing_actions'], 1)
        self.assertEqual(texts(hwp, '1012')['A2'], 'A4')

    def test_every_line_cannot_be_deleted(self) -> None:
        hwp = sample_document()
        removed = [{'address': f'{col}{row}', 'text': f'{col}{row}'} for row in range(1, 6) for col in 'ABCD']
        refused(self, edit(hwp, '1012', 'delete_row', row=1, count=5, expected_removed_cells=removed), 'table_would_be_empty', hwp)

    def test_lines_outside_the_table_are_refused(self) -> None:
        hwp = sample_document()
        refused(self, edit(hwp, '1012', 'delete_row', row=9, expected_removed_cells=[]), 'cell_out_of_range', hwp)
        refused(self, edit(hwp, '1012', 'insert_col_left', col=9), 'cell_out_of_range', hwp)

    def test_a_new_line_beside_merged_cells_widens_what_crosses_and_mirrors_the_source_line(self) -> None:
        hwp = sample_document()
        ok_result(self, edit(hwp, '1015', 'insert_row_below', row=2))
        self.assertEqual(spans(hwp, '1015')['A2'], (3, 1))
        self.assertEqual(texts(hwp, '1015')['B4'], '셋째가운데')
        self.assertIsNotNone(hwp.by_inst('1015').named('C3').fill, 'a new cell takes the fill of its source line')
        hwp = sample_document()
        ok_result(self, edit(hwp, '1015', 'insert_row_below', row=1))
        self.assertEqual(spans(hwp, '1015')['A2'], (1, 2), 'the new line repeats the merged shape of its source line')
        hwp = sample_document()
        ok_result(self, edit(hwp, '1015', 'insert_col_right', col=1))
        self.assertEqual(spans(hwp, '1015')['A1'], (1, 3))
        self.assertEqual(spans(hwp, '1015')['B2'], (2, 1))

    def test_a_deleted_line_through_a_merged_cell_shortens_it_and_keeps_its_text(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1015', 'delete_row', row=2, expected_removed_cells=[{'address': 'B2', 'text': '둘째가운데'}, {'address': 'C2', 'text': '채움셀'}]))
        self.assertEqual(texts(hwp, '1015')['A2'], '세로병합')
        self.assertEqual(spans(hwp, '1015')['A2'], (1, 1))
        self.assertEqual(value['changes']['shortened_cells'], ['A2'])

    def test_a_block_of_lines_that_cuts_a_merged_cell_is_refused_with_a_way_forward(self) -> None:
        hwp = sample_document()
        refused(self, edit(hwp, '1015', 'delete_row', row=1, count=2, expected_removed_cells=[]), 'merged_cell_crosses_lines', hwp)


class MergeAndSplit(unittest.TestCase):
    def test_merging_text_keeps_every_paragraph_in_reading_order_with_its_styles(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1011', 'merge_cells', cell='C3', end_cell='D4'))
        cell = hwp.by_inst('1011').named('C3')
        self.assertEqual((cell.rs, cell.cs), (2, 2))
        self.assertEqual([[run[0] for run in item['runs']] for item in cell.paragraphs], [['C3굵게', 'C3보통'], ['D3'], ['C4'], ['D4첫문단'], ['D4둘째문단']])
        self.assertEqual(cell.paragraphs[0]['runs'][0][1], 1)
        self.assertEqual(value['changes']['merged'], {'cell': 'C3', 'from': ['C3', 'D3', 'C4', 'D4'], 'paragraphs': 5, 'dropped_blank_cells': []})
        self.assertEqual([call[1] for call in hwp.native_calls()], ['TableMergeCell'])
        self.assertEqual(hwp.native_calls()[0][3], ['C3', 'D3', 'C4', 'D4'])

    def test_a_blank_cell_adds_no_paragraph_and_a_blank_paragraph_of_a_fuller_cell_stays(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1017', 'merge_cells', cell='A1', end_cell='B1'))
        self.assertEqual(texts(hwp, '1017')['A1'], '구갑')
        self.assertEqual(value['changes']['merged']['dropped_blank_cells'], ['B1'])
        hwp = sample_document()
        ok_result(self, edit(hwp, '1017', 'merge_cells', cell='B2', end_cell='C2'))
        self.assertEqual(len(hwp.by_inst('1017').named('B2').paragraphs), 3)

    def test_merging_a_whole_line_collapses_it_and_the_change_says_so(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1014', 'merge_cells', cell='A1', end_cell='B2'))
        self.assertEqual(hwp.by_inst('1014').rows, 3)
        self.assertEqual(value['after']['rows'], 3)
        self.assertEqual(texts(hwp, '1014')['A2'], '같은글')

    def test_a_range_including_a_whole_merged_cell_can_be_merged_and_one_cutting_it_cannot(self) -> None:
        hwp = sample_document()
        ok_result(self, edit(hwp, '1015', 'merge_cells', cell='A2', end_cell='B3'))
        self.assertEqual(texts(hwp, '1015')['A2'], '세로병합둘째가운데셋째가운데')
        hwp = sample_document()
        refused(self, edit(hwp, '1015', 'merge_cells', cell='B1', end_cell='C2'), 'range_cuts_merged_cell', hwp)
        refused(self, edit(hwp, '1015', 'merge_cells', cell='B2', end_cell='B2'), 'range_too_small', hwp)

    def test_splitting_a_plain_cell_keeps_its_text_in_the_first_part_and_widens_its_lines(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1012', 'split_cell', cell='B1', split_cols=2))
        self.assertEqual(hwp.by_inst('1012').cols, 5)
        self.assertEqual(texts(hwp, '1012')['B1'], 'B1')
        self.assertEqual(texts(hwp, '1012')['C1'], '')
        self.assertEqual(spans(hwp, '1012')['B2'], (1, 2))
        self.assertEqual(hwp.native_calls()[0][2], {'Rows': 0, 'Cols': 2})
        self.assertEqual(value['changes']['split'], {'cell': 'B1', 'parts': ['B1', 'C1'], 'content_in': 'B1'})
        hwp = sample_document()
        ok_result(self, edit(hwp, '1012', 'split_cell', cell='C3', split_rows=2, split_cols=2))
        self.assertEqual((hwp.by_inst('1012').rows, hwp.by_inst('1012').cols), (6, 5))
        self.assertEqual(spans(hwp, '1012')['A3'], (2, 1))

    def test_unmerging_returns_a_merged_cell_to_single_cells_with_all_content_in_the_first(self) -> None:
        hwp = sample_document()
        value = ok_result(self, edit(hwp, '1015', 'unmerge_cell', cell='A1'))
        self.assertEqual((hwp.by_inst('1015').rows, hwp.by_inst('1015').cols), (3, 3))
        self.assertEqual(texts(hwp, '1015')['A1'], '병합머리')
        self.assertEqual(texts(hwp, '1015')['B1'], '')
        self.assertEqual(value['changes']['split'], {'cell': 'A1', 'parts': ['A1', 'B1'], 'content_in': 'A1'})
        self.assertEqual(hwp.native_calls()[0][2], {'Rows': 0, 'Cols': 2})
        hwp = sample_document()
        ok_result(self, edit(hwp, '1015', 'unmerge_cell', cell='A2'))
        self.assertEqual(hwp.native_calls()[0][2], {'Rows': 2, 'Cols': 0})
        self.assertEqual(spans(hwp, '1015')['A3'], (1, 1))

    def test_unmerging_columns_of_different_widths_adds_a_line_as_hancom_does_and_says_so(self) -> None:
        hwp = sample_document()
        ok_result(self, edit(hwp, '1016', 'merge_cells', cell='A1', end_cell='B1'))
        value = ok_result(self, edit(hwp, '1016', 'unmerge_cell', cell='A1'))
        self.assertEqual(hwp.by_inst('1016').cols, 4)
        self.assertEqual(value['changes']['added_grid_lines'], {'rows': 0, 'cols': 1})
        self.assertEqual(spans(hwp, '1016')['A1'], (1, 2))
        self.assertEqual(spans(hwp, '1016')['B2'], (1, 2))

    def test_unmerge_and_split_refusals(self) -> None:
        hwp = sample_document()
        refused(self, edit(hwp, '1012', 'unmerge_cell', cell='B1'), 'cell_not_merged', hwp)
        refused(self, edit(hwp, '1015', 'split_cell', cell='A2', split_rows=3), 'merged_split_rows_unsupported', hwp)
        refused(self, edit(hwp, '1015', 'unmerge_cell', cell='B1'), 'cell_covered_by_merge', hwp)
        refused(self, edit(hwp, '1012', 'split_cell', cell='Z9', split_cols=2), 'cell_out_of_range', hwp)


class Borders(unittest.TestCase):
    def test_a_single_edge_writes_only_its_items_and_the_shared_side(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], **LINE}]))
        call = hwp.native_calls()[0]
        self.assertEqual(call[1:3], ('CellBorder', {'BorderTypeTop': 2, 'BorderWidthTop': 5, 'BorderColorTop': 0x0000FF}))
        table = hwp.by_inst('1012')
        wanted = ('Dash', '0.3mm', 0x0000FF)
        self.assertEqual(table.named('B2').border['top'], wanted)
        self.assertEqual(table.named('B1').border['bottom'], wanted)
        self.assertEqual(table.named('B2').border['left'], SOLID)
        changes = value['changes']['edges']
        self.assertEqual(sorted((row['cell'], row['side'], row['role']) for row in changes), [('B1', 'bottom', 'shared_neighbour'), ('B2', 'top', 'requested')])
        self.assertEqual(next(row for row in changes if row['cell'] == 'B2')['before'], {'line': 'solid', 'width_mm': 0.12, 'color': '#000000'})
        self.assertEqual(next(row for row in changes if row['cell'] == 'B2')['after'], {'line': 'dash', 'width_mm': 0.3, 'color': '#FF0000'})

    def test_the_left_color_uses_the_native_item_name_that_works(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['left'], 'line': 'solid', 'width_mm': 0.5, 'color': BLUE}]))
        self.assertEqual(hwp.native_calls()[0][2], {'BorderTypeLeft': 1, 'BorderWidthLeft': 7, 'BorderCorlorLeft': 0xFF0000})
        self.assertEqual(hwp.by_inst('1012').named('A2').border['right'], ('Solid', '0.5mm', 0xFF0000))

    def test_a_range_outline_and_inner_lines_cover_exactly_their_edges(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1012', scope='range', cell='B2', end_cell='C3', borders=[
            {'edges': ['outline'], 'line': 'double', 'width_mm': 0.5, 'color': '#112233'},
            {'edges': ['inner_horizontal'], 'line': 'dot', 'width_mm': 0.2, 'color': '#445566'},
            {'edges': ['inner_vertical'], 'line': 'solid', 'width_mm': 0.4, 'color': '#778899'}]))
        self.assertEqual(len(hwp.native_calls()), 1)
        table = hwp.by_inst('1012')
        outline, horizontal, vertical = ('DoubleSlim', '0.5mm', 0x332211), ('Dot', '0.2mm', 0x665544), ('Solid', '0.4mm', 0x998877)
        self.assertEqual(table.named('B2').border, {'left': outline, 'top': outline, 'right': vertical, 'bottom': horizontal})
        self.assertEqual(table.named('C3').border, {'left': vertical, 'top': horizontal, 'right': outline, 'bottom': outline})
        self.assertEqual(table.named('A2').border['right'], outline)
        self.assertEqual(table.named('D3').border['left'], outline)
        self.assertEqual(table.named('B1').border['bottom'], outline)
        self.assertEqual(table.named('C4').border['top'], outline)
        self.assertEqual(table.named('D1').border['bottom'], SOLID)

    def test_the_whole_table_and_the_table_edge_have_no_neighbours(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1012', scope='table', borders=[{'edges': ['outline'], 'line': 'solid', 'width_mm': 1.0, 'color': '#0A0B0C'}]))
        table = hwp.by_inst('1012')
        wanted = ('Solid', '1.0mm', 0x0C0B0A)
        self.assertEqual(table.named('A1').border['top'], wanted)
        self.assertEqual(table.named('D5').border['right'], wanted)
        self.assertEqual(table.named('B2').border['top'], SOLID)
        self.assertEqual(value['target']['cells'], 20)
        self.assertEqual({row['role'] for row in value['changes']['edges']}, {'requested'})

    def test_clearing_keeps_the_stored_width_and_color_as_hancom_does(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], 'line': 'dot', 'width_mm': 0.5, 'color': RED}]))
        value = ok_result(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], 'line': 'none'}]))
        self.assertEqual(hwp.native_calls()[-1][2], {'BorderTypeTop': 0})
        self.assertEqual(hwp.by_inst('1012').named('B2').border['top'][0], 'None')
        self.assertEqual(hwp.by_inst('1012').named('B1').border['bottom'][0], 'None')
        self.assertEqual(next(row for row in value['changes']['edges'] if row['cell'] == 'B2')['after']['line'], 'none')

    def test_a_neighbour_only_partly_on_the_edge_keeps_its_side_and_the_reply_says_so(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **LINE}]))
        self.assertEqual(hwp.by_inst('1015').named('A1').border['bottom'], SOLID)
        self.assertEqual(value['changes']['neighbours_not_written'], [{'cell': 'A1', 'side': 'bottom', 'reason': 'wider_than_edge'}])
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='range', cell='A2', end_cell='B3', borders=[{'edges': ['top'], **LINE}]))
        self.assertEqual(hwp.by_inst('1015').named('A1').border['bottom'][0], 'Dash')

    def test_a_merged_cell_as_a_target_writes_all_its_neighbours(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='cell', cell='A2', borders=[{'edges': ['right'], **LINE}]))
        table = hwp.by_inst('1015')
        self.assertEqual((table.named('B2').border['left'][0], table.named('B3').border['left'][0]), ('Dash', 'Dash'))

    def test_a_range_cutting_a_merged_cell_stops_before_it_is_selected(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1015', scope='range', cell='B1', end_cell='C2', fill=RED), 'range_cuts_merged_cell', hwp)

    def test_an_inner_edge_needs_an_inner_line(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['inner_horizontal'], **LINE}]), 'edge_not_in_target', hwp)
        refused(self, style(hwp, '1012', scope='range', cell='B2', end_cell='C2', borders=[{'edges': ['inner_horizontal'], **LINE}]), 'edge_not_in_target', hwp)

    def test_every_measured_line_and_width_goes_by_its_native_number(self) -> None:
        for name, number in (('solid', 1), ('dash', 2), ('dot', 3), ('dash_dot', 4), ('dash_dot_dot', 5), ('long_dash', 6), ('circle', 7), ('double', 8), ('slim_thick', 9), ('thick_slim', 10), ('slim_thick_slim', 11)):
            with self.subTest(line=name):
                hwp = sample_document()
                ok_result(self, style(hwp, '1012', scope='cell', cell='A1', borders=[{'edges': ['top'], 'line': name, 'width_mm': 0.12, 'color': '#000000'}]))
                self.assertEqual(hwp.native_calls()[0][2], {'BorderTypeTop': number, 'BorderWidthTop': 1, 'BorderColorTop': 0})
        for width, number in ((0.1, 0), (0.12, 1), (0.15, 2), (0.2, 3), (0.25, 4), (0.3, 5), (0.4, 6), (0.5, 7), (0.6, 8), (0.7, 9), (1.0, 10), (1.5, 11), (2.0, 12), (3.0, 13), (4.0, 14), (5.0, 15)):
            with self.subTest(width=width):
                hwp = sample_document()
                ok_result(self, style(hwp, '1012', scope='cell', cell='A1', borders=[{'edges': ['top'], 'line': 'solid', 'width_mm': width, 'color': '#000000'}]))
                self.assertEqual(hwp.native_calls()[0][2]['BorderWidthTop'], number)

    def test_a_value_already_there_is_no_native_call_and_no_new_dirty(self) -> None:
        hwp = sample_document()
        outcome = style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], 'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}])
        value = ok_result(self, outcome)
        self.assertEqual(value['result']['status'], 'already_set')
        self.assertFalse(outcome['steps'][0]['dirty'])
        self.assertEqual(hwp.native_calls(), [])


class Fill(unittest.TestCase):
    def test_one_cell_a_range_and_the_table_with_native_color_packing(self) -> None:
        cases = [({'scope': 'cell', 'cell': 'B2'}, ['B2']), ({'scope': 'range', 'cell': 'B2', 'end_cell': 'C3'}, ['B2', 'C2', 'B3', 'C3']), ({'scope': 'table'}, [f'{col}{row}' for row in range(1, 6) for col in 'ABCD'])]
        for where, cells in cases:
            with self.subTest(where=where):
                hwp = sample_document()
                value = ok_result(self, style(hwp, '1012', fill='#C0D0E0', **where))
                call = hwp.native_calls()[0]
                self.assertEqual(call[1], 'CellFill')
                self.assertEqual(call[2]['face'], 0xE0D0C0)
                self.assertEqual(call[3], cells)
                table = hwp.by_inst('1012')
                self.assertEqual(sorted(cell.address for cell in table.cells if cell.fill), sorted(cells))
                self.assertEqual(sorted(row['cell'] for row in value['changes']['fills']), sorted(cells))

    def test_white_is_a_fill_and_none_is_no_fill(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1015', scope='cell', cell='C2', fill='#FFFFFF'))
        self.assertEqual(value['changes']['fills'], [{'cell': 'C2', 'before': {'kind': 'solid', 'color': '#FFF2CC'}, 'after': {'kind': 'solid', 'color': '#FFFFFF'}}])
        self.assertEqual(ok_result(self, Flow(hwp).step(op='table_read', table_id='tbl:1015'))['cells'][4]['fill'], {'kind': 'solid', 'color': '#FFFFFF'})
        value = ok_result(self, style(hwp, '1015', scope='cell', cell='C2', fill='none'))
        self.assertEqual(value['changes']['fills'], [{'cell': 'C2', 'before': {'kind': 'solid', 'color': '#FFFFFF'}, 'after': {'kind': 'none'}}])
        self.assertIsNone(hwp.by_inst('1015').named('C2').fill)

    def test_a_fill_already_there_is_no_call(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1012', scope='cell', cell='B2', fill='none'))
        self.assertEqual(value['result']['status'], 'already_set')
        self.assertEqual(hwp.native_calls(), [])

    def test_a_pattern_or_gradation_fill_is_refused_rather_than_guessed(self) -> None:
        hwp = sample_document()
        hwp.by_inst('1012').named('B2').fill = {'kind': 'gradation'}
        refused(self, style(hwp, '1012', scope='cell', cell='B2', fill=RED), 'fill_kind_unsupported', hwp)

    def test_text_borders_and_other_cells_stay(self) -> None:
        hwp = sample_document()
        before = texts(hwp, '1011')
        ok_result(self, style(hwp, '1011', scope='range', cell='B3', end_cell='C3', fill=RED))
        self.assertEqual(texts(hwp, '1011'), before)
        self.assertEqual(hwp.by_inst('1011').named('C3').border['top'], SOLID)
        self.assertEqual(hwp.by_inst('1011').named('C3').paragraphs[0]['runs'][0][1], 1)

    def test_border_and_fill_together_are_two_counted_native_actions(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], **LINE}], fill=RED))
        self.assertEqual([call[1] for call in hwp.native_calls()], ['CellBorder', 'CellFill'])
        self.assertEqual(value['undo']['native_editing_actions'], 2)
        self.assertEqual(value['result']['native_actions'], ['Execute:CellBorder', 'Execute:CellFill'])


class Unknown(unittest.TestCase):
    def assert_held(self, outcome: dict) -> None:
        self.assertFalse(outcome['ok'], outcome)
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'], outcome)

    def test_a_change_outside_the_request_is_not_a_success(self) -> None:
        def stray(hwp: TableHwp) -> None:
            hwp.by_inst('1011').named('A1').paragraphs = [para('바뀜')]

        for fields in ({'op': 'style'}, {'op': 'edit'}):
            with self.subTest(fields=fields):
                hwp = sample_document()
                hwp.corrupt_after_execute = stray
                outcome = style(hwp, '1012', scope='cell', cell='B2', fill=RED) if fields['op'] == 'style' else edit(hwp, '1012', 'insert_row_below', row=1)
                self.assert_held(outcome)

    def test_an_unrequested_neighbour_edge_is_not_hidden(self) -> None:
        def stray(hwp: TableHwp) -> None:
            hwp.by_inst('1012').named('D5').border['bottom'] = ('Dash', '0.5mm', 0)

        hwp = sample_document()
        hwp.corrupt_after_execute = stray
        self.assert_held(style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], **LINE}]))

    def test_a_kept_cell_losing_whitespace_is_not_a_success(self) -> None:
        def stray(hwp: TableHwp) -> None:
            hwp.by_inst('1011').named('A3').paragraphs = [para('가나 ABC 두칸')]

        hwp = sample_document()
        hwp.corrupt_after_execute = stray
        self.assert_held(edit(hwp, '1011', 'insert_row_below', row=1))

    def test_a_false_native_return_and_a_raising_call_are_unknown_and_not_retried(self) -> None:
        hwp = sample_document()
        hwp.execute_result = False
        self.assert_held(style(hwp, '1012', scope='cell', cell='B2', fill=RED))
        self.assertEqual(len(hwp.native_calls()), 1)
        hwp = sample_document()
        hwp.execute_raises = RuntimeError('native')
        self.assert_held(edit(hwp, '1012', 'insert_row_below', row=1))
        self.assertEqual(len(hwp.native_calls()), 1)

    def test_a_second_action_failing_after_the_first_is_unknown(self) -> None:
        hwp = sample_document()
        hwp.fail_action = ('CellFill', 0)
        self.assert_held(style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['top'], **LINE}], fill=RED))
        self.assertEqual([call[1] for call in hwp.native_calls()], ['CellBorder', 'CellFill'])

    def test_a_block_that_is_not_the_requested_cells_stops_before_the_edit(self) -> None:
        hwp = sample_document()
        target = binding(hwp, '1012')
        hwp.selection_off_by_one = True
        refused(self, Flow(hwp).step(op='table_style_exact', **target, scope='range', cell='B2', end_cell='C3', fill=RED), 'selection_unverified', hwp)


class History(unittest.TestCase):
    def test_each_command_is_one_undo_and_redo_back_to_its_recorded_state(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        target = {item['table_id']: item for item in owned.service.command_bundle(steps=[{'op': 'table_inventory', 'label': 't'}])['steps'][0]['result']['tables']}['tbl:1012']
        fields = {'table_id': target['table_id'], 'expected_hash': target['expected_hash'], 'expected_page': target['page'], 'expected_rows': 5, 'expected_cols': 4, 'confirm_layout': True}
        self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'a', **fields, 'scope': 'cell', 'cell': 'B2', 'borders': [{'edges': ['top'], **LINE}], 'fill': RED}])['ok'])
        undone = owned.service.undo()
        self.assertEqual(undone['native_undo_steps'], 2)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertIsNone(hwp.by_inst('1012').named('B2').fill)
        self.assertEqual(hwp.by_inst('1012').named('B2').border['top'], SOLID)
        redone = owned.service.redo()
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertIsNotNone(hwp.by_inst('1012').named('B2').fill)

    def test_a_structure_edit_undoes_with_one_step_whatever_its_count(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        target = {item['table_id']: item for item in owned.service.command_bundle(steps=[{'op': 'table_inventory', 'label': 't'}])['steps'][0]['result']['tables']}['tbl:1012']
        fields = {'table_id': target['table_id'], 'expected_hash': target['expected_hash'], 'expected_page': target['page'], 'expected_rows': 5, 'expected_cols': 4, 'confirm_layout': True}
        self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_edit_exact', 'label': 'a', 'action': 'insert_row_below', 'row': 1, 'count': 3, **fields}])['ok'])
        undone = owned.service.undo()
        self.assertEqual(undone['native_undo_steps'], 1)
        self.assertEqual(hwp.by_inst('1012').rows, 5)

    def test_an_edit_reaching_no_native_call_leaves_the_history_alone(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        target = {item['table_id']: item for item in owned.service.command_bundle(steps=[{'op': 'table_inventory', 'label': 't'}])['steps'][0]['result']['tables']}['tbl:1012']
        fields = {'table_id': target['table_id'], 'expected_hash': target['expected_hash'], 'expected_page': target['page'], 'expected_rows': 5, 'expected_cols': 4, 'confirm_layout': True}
        self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'a', 'scope': 'cell', 'cell': 'B2', 'fill': 'none', **fields}])['ok'])
        with self.assertRaises(Exception) as caught:
            owned.service.undo()
        self.assertEqual(getattr(caught.exception, 'code', None), 'no_undo_available')

    def test_a_refused_edit_keeps_earlier_unsaved_work(self) -> None:
        hwp = sample_document()
        first = style(hwp, '1012', scope='cell', cell='B2', fill=RED)
        ok_result(self, first)
        self.assertEqual(hwp.undo_stack.__len__(), 1)
        outcome = edit(hwp, '1012', 'merge_cells', cell='B2', end_cell='B2')
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'range_too_small')
        self.assertIsNotNone(hwp.by_inst('1012').named('B2').fill)
        self.assertEqual(hwp.undo_stack.__len__(), 1)


class Creation(unittest.TestCase):
    def test_a_new_table_goes_after_the_named_paragraph_as_one_undo(self) -> None:
        hwp = sample_document()
        value = ok_result(self, Flow(hwp).step(op='table_create_exact', match='표이 앞 문단', expected_page=1, rows=2, cols=3, confirm_layout=True))
        self.assertEqual(value['table']['rows'], 2)
        self.assertEqual(value['table']['cols'], 3)
        self.assertTrue(value['table']['table_id'].startswith('tbl:'))
        self.assertEqual(value['undo']['native_editing_actions'], 1)
        self.assertEqual(hwp.native_calls()[0][2], {'Rows': 2, 'Cols': 3, 'header': False})
        self.assertIsInstance(hwp.body[4], type(hwp.by_inst('1012')))
        self.assertEqual(hwp.body[3], '표이 앞 문단')

    def test_cell_text_can_be_given_and_then_undo_is_refused_rather_than_counted(self) -> None:
        hwp = sample_document()
        self.assertEqual(Flow(hwp).step(op='table_create_exact', match='맺음 문단', expected_page=1, rows=1, cols=2, cells=[['가']], confirm_layout=True)['steps'][0].get('refusal_code'), 'cells_shape_mismatch')

    def test_the_anchor_must_be_one_plain_body_paragraph(self) -> None:
        hwp = sample_document()
        hwp.body.insert(1, '표이 앞 문단')
        cases = [('표이 앞 문단', 'target_ambiguous'), ('없는 문단', 'target_not_found'), ('C3굵게', 'target_not_in_body')]
        for match, code in cases:
            with self.subTest(match=match):
                outcome = Flow(hwp).step(op='table_create_exact', match=match, expected_page=1, rows=2, cols=2, confirm_layout=True)
                self.assertEqual(outcome['steps'][0].get('refusal_code'), code, outcome)
                self.assertEqual(hwp.native_calls(), [])


class Validation(unittest.TestCase):
    def test_the_closed_schema_refuses_shapes_a_caller_could_get_wrong(self) -> None:
        sid = 'a' * 32
        good = {'table_id': 'tbl:1', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True}
        accepted = [
            {'op': 'table_inventory'},
            {'op': 'table_read', 'table_id': 'tbl:1'},
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': 1, **good},
            {'op': 'table_edit_exact', 'action': 'merge_cells', 'cell': 'A1', 'end_cell': 'B1', **good},
            {'op': 'table_edit_exact', 'action': 'unmerge_cell', 'cell': 'A1', **good},
            {'op': 'table_style_exact', 'scope': 'table', 'fill': '#FFFFFF', **good},
            {'op': 'table_style_exact', 'scope': 'cell', 'cell': 'A1', 'fill': 'none', **good},
            {'op': 'table_style_exact', 'scope': 'range', 'cell': 'A1', 'end_cell': 'B2', 'borders': [{'edges': ['outline'], 'line': 'none'}], **good},
            {'op': 'table_create_exact', 'match': '문단', 'expected_page': 1, 'rows': 2, 'cols': 2, 'confirm_layout': True},
        ]
        for request in accepted:
            with self.subTest(request=request):
                Command.model_validate({'session_id': sid, 'request': request})
        rejected = [
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': True, **good},
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': 1, 'count': 0, **good},
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': 1, 'count': 51, **good},
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'cell': 'A1', **good},
            {'op': 'table_edit_exact', 'action': 'merge_cells', 'cell': 'A1', **good},
            {'op': 'table_edit_exact', 'action': 'split_cell', 'cell': 'A1', 'split_rows': 1, 'split_cols': 1, **good},
            {'op': 'table_edit_exact', 'action': 'unmerge_cell', 'cell': 'A1', 'split_rows': 2, **good},
            {'op': 'table_edit_exact', 'action': 'run_anything', 'cell': 'A1', **good},
            {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': 1, **{**good, 'confirm_layout': False}},
            {'op': 'table_style_exact', 'scope': 'table', **good},
            {'op': 'table_style_exact', 'scope': 'table', 'cell': 'A1', 'fill': 'none', **good},
            {'op': 'table_style_exact', 'scope': 'cell', 'fill': 'none', **good},
            {'op': 'table_style_exact', 'scope': 'cell', 'cell': 'A1', 'end_cell': 'B1', 'fill': 'none', **good},
            {'op': 'table_style_exact', 'scope': 'range', 'cell': 'A1', 'fill': 'none', **good},
            {'op': 'table_style_exact', 'scope': 'table', 'fill': 'red', **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['outline'], 'line': 'solid'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['outline'], 'line': 'none', 'color': '#000000'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['outline'], 'line': 'solid', 'width_mm': 0.11, 'color': '#000000'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['diagonal'], 'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['top', 'outline'], 'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'borders': [{'edges': ['top'], 'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}, {'edges': ['top'], 'line': 'none'}], **good},
            {'op': 'table_style_exact', 'scope': 'table', 'fill': 'none', 'action': 'CellFill', **good},
            {'op': 'table_create_exact', 'match': '문단', 'expected_page': 1, 'rows': 0, 'cols': 2, 'confirm_layout': True},
            {'op': 'table_create_exact', 'match': '문단', 'expected_page': 1, 'rows': 2, 'cols': 51, 'confirm_layout': True},
        ]
        for request in rejected:
            with self.subTest(request=request):
                with self.assertRaises(ValidationError):
                    Command.model_validate({'session_id': sid, 'request': request})

    def test_the_backend_validates_a_step_the_adapter_never_sent(self) -> None:
        hwp = sample_document()
        target = binding(hwp, '1012')
        for fields in ({'scope': 'cell', 'cell': 'B2', 'fill': 'red'}, {'scope': 'cell', 'cell': 'B2', 'fill': RED, 'borders': [{'edges': ['top'], 'line': 'wave', 'width_mm': 0.12, 'color': '#000000'}]}, {'scope': 'cell', 'cell': 'B2'}):
            with self.subTest(fields=fields):
                with self.assertRaises(Exception) as caught:
                    Flow(hwp).step(op='table_style_exact', **target, **fields)
                self.assertNotIsInstance(caught.exception, AssertionError)
                self.assertEqual(hwp.native_calls(), [])


if __name__ == '__main__':
    unittest.main()
