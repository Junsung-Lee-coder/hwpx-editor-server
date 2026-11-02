"""What a table command tells its caller and what it refuses: width against the page, lines that print, deletes by hash, creation text and history.

Through the real bundle validation, package dispatch, service and census over tests.native_table_fakes. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from app import native_table as nt
from hwpx_mcp.schema import Command
from tests.native_table_fakes import SOLID, Cell, TableHwp, grid, para, sample_document
from tests.test_native_table import Flow, binding, edit, inventory, ok_result, refused, style
from tests.test_repair_runtime_safety import owned_doc

SESSION = 'a' * 32
FULL = [10488, 10488, 10488, 10486]  # A table as wide as Hancom makes it on an A4 page with its usual margins.
THIN_RED = {'line': 'solid', 'width_mm': 0.12, 'color': '#FF0000'}
THICK_RED = {'line': 'solid', 'width_mm': 1.0, 'color': '#FF0000'}
DASH_RED = {'line': 'dash', 'width_mm': 1.0, 'color': '#FF0000'}
NONE = {'line': 'none'}


def width_doc(*tables, **fields) -> TableHwp:
    return TableHwp(['앞 문단', *tables, '끝 문단'], **fields)


def full_table(inst='2001'):
    return grid(inst, [[f'{letter}{row}' for letter in 'ABCD'] for row in (1, 2)], widths=FULL)


def widen(hwp, inst='2001', **fields):
    return ok_result(unittest.TestCase(), edit(hwp, inst, 'insert_col_right', **{'col': 4, **fields}))


class Layout(unittest.TestCase):
    def test_every_table_says_how_wide_it_is_against_the_text_and_the_page(self) -> None:
        hwp = width_doc(full_table())
        layout = inventory(hwp)['tbl:2001']['layout']
        self.assertEqual(layout['status'], 'fits')
        self.assertEqual((layout['width_hu'], layout['text_width_hu'], layout['left_hu'], layout['right_edge_hu'], layout['page_width_hu']), (41950, 42520, 8787, 50737, 59528))
        self.assertEqual(layout['overflow_hu'], 0)

    def test_a_column_added_to_a_full_width_table_is_named_past_the_margin_and_off_the_page(self) -> None:
        hwp = width_doc(grid('2001', [[f'{letter}{row}' for letter in 'ABCD'] for row in (1, 2)], widths=[13650, 13650, 13650, 1000]))
        value = widen(hwp)
        layout = value['after']['layout']
        self.assertEqual(layout['width_hu'], 41950 + 1000)
        self.assertEqual(layout['status'], 'past_margin')
        self.assertEqual(value['before']['layout']['status'], 'fits')
        self.assertTrue(any('tbl:2001' in text and '여백' in text for text in value['warnings']))
        value = widen(hwp, col=5, count=10)
        self.assertEqual(value['after']['layout']['status'], 'off_page')
        self.assertEqual(value['after']['layout']['overflow_hu'], 8787 + 41950 + 1000 * 11 - 51024)
        self.assertTrue(any('쪽 밖' in text for text in value['warnings']))

    def test_landscape_paper_is_as_wide_as_its_stored_height(self) -> None:
        hwp = width_doc(grid('2001', [['가', '나']], widths=[30000, 30000]), )
        hwp.page_def = {'Landscape': 1}
        layout = inventory(hwp)['tbl:2001']['layout']
        self.assertEqual((layout['page_width_hu'], layout['text_width_hu'], layout['status']), (84189, 84189 - 8504 * 2, 'fits'))

    def test_geometry_that_cannot_be_proved_is_unverified_and_never_a_pass(self) -> None:
        cases = {
            'floating': lambda hwp, table: setattr(table, 'treat_as_char', False),
            'columns': lambda hwp, table: setattr(hwp, 'column_count', 2),
            'cell_spacing': lambda hwp, table: setattr(table, 'cell_spacing', 141),
            'gutter': lambda hwp, table: hwp.page_def.update(Gutter=1000),
            'relative_width': lambda hwp, table: setattr(table, 'width_rel', 'Paper'),
        }
        for reason, arrange in cases.items():
            with self.subTest(reason=reason):
                table = full_table()
                hwp = width_doc(table)
                arrange(hwp, table)
                layout = inventory(hwp)['tbl:2001']['layout']
                self.assertEqual(layout['status'], 'unverified')
                self.assertEqual(layout['reason'], reason)
                self.assertNotIn('overflow_hu', layout)

    def test_a_size_that_disagrees_with_the_cells_is_unverified(self) -> None:
        table = grid('2001', [['가', '나'], ['다', '라']], widths=[9000, 9000])
        table.named('A2').width = 5000
        hwp = width_doc(table)
        self.assertEqual(inventory(hwp)['tbl:2001']['layout']['status'], 'unverified')

    def test_a_nested_table_is_compared_with_the_cell_that_holds_it(self) -> None:
        inner = grid('2002', [['속']], widths=[30000])
        outer = grid('2003', [[{'paragraphs': [para('앞', inner)]}, '옆']], widths=[9000, 9000])
        layout = inventory(width_doc(outer))['tbl:2002']['layout']
        self.assertEqual(layout['status'], 'wider_than_parent')
        self.assertEqual(layout['parent_width_hu'], 9000 - 1020)


class Width(unittest.TestCase):
    def width(self, hwp, inst='2001', **fields):
        return Flow(hwp).step(op='table_width_exact', **binding(hwp, inst), **fields)

    def widths(self, hwp, inst='2001'):
        return {cell.address: cell.width for cell in hwp.by_inst(inst).ordered()}

    def test_fit_brings_a_widened_table_back_inside_the_text_proportionally_and_keeps_all_else(self) -> None:
        hwp = width_doc(full_table())
        widen(hwp)
        before = {cell.address: (cell.paragraphs, dict(cell.border), cell.fill) for cell in hwp.by_inst('2001').ordered()}
        calls = len(hwp.native_calls())
        value = ok_result(self, self.width(hwp, fit='text_width'))
        self.assertEqual(value['after']['layout']['status'], 'fits')
        self.assertEqual(value['after']['layout']['width_hu'], 42520 - 566)
        widths = self.widths(hwp)
        self.assertEqual(widths['A1'], widths['A2'])
        self.assertLessEqual(abs(widths['A1'] - round(10488 * 41954 / 52436)), 2)
        self.assertEqual(value['changes']['columns'][0], {'column': 'A', 'before_hu': 10488, 'after_hu': widths['A1']})
        self.assertEqual(len(hwp.native_calls()) - calls, 5)  # One action for each column, each proven by its block.
        self.assertEqual(value['undo']['native_editing_actions'], 5)
        for cell in hwp.by_inst('2001').ordered():
            self.assertEqual((cell.paragraphs, dict(cell.border), cell.fill), before[cell.address])

    def test_exact_widths_go_column_by_column_and_a_merged_cell_takes_the_sum(self) -> None:
        hwp = width_doc(grid('2001', [[{'paragraphs': [para('합')], 'cs': 2}, None, '오른쪽'], ['가', '나', '다']], widths=[13984, 13984, 13986]))
        value = ok_result(self, self.width(hwp, column_widths_mm=[20, 40, 30]))
        hu = [round(mm * 7200 / 25.4) for mm in (20, 40, 30)]
        self.assertEqual(self.widths(hwp), {'A1': hu[0] + hu[1], 'C1': hu[2], 'A2': hu[0], 'B2': hu[1], 'C2': hu[2]})
        self.assertEqual(value['undo']['native_editing_actions'], 5)  # Across a merge each cell is its own action, as measured.
        self.assertEqual(value['after']['layout']['width_hu'], sum(hu))

    def test_a_plain_table_changes_only_the_columns_that_differ(self) -> None:
        hwp = width_doc(grid('2001', [['가', '나'], ['다', '라']], widths=[9000, 9000]))
        value = ok_result(self, self.width(hwp, column_widths_mm=[9000 * 25.4 / 7200, 40]))
        self.assertEqual(value['undo']['native_editing_actions'], 1)
        self.assertEqual(self.widths(hwp)['A1'], 9000)

    def test_a_width_already_there_is_no_call(self) -> None:
        hwp = width_doc(grid('2001', [['가', '나']], widths=[20977, 20977]))
        value = ok_result(self, self.width(hwp, fit='text_width'))
        self.assertEqual((value['result']['changed'], value['result']['status'], hwp.native_calls()), (False, 'already_set', []))

    def test_what_would_not_fit_or_is_too_narrow_is_refused_before_any_edit(self) -> None:
        hwp = width_doc(full_table())
        refused(self, self.width(hwp, column_widths_mm=[60, 60, 60, 60]), 'table_would_overflow', hwp)
        refused(self, self.width(hwp, column_widths_mm=[30, 30, 30]), 'column_widths_mismatch', hwp)
        refused(self, self.width(hwp, column_widths_mm=[30, 30, 30, 1]), 'column_too_narrow', hwp)
        hwp = width_doc(grid('2001', [[str(number) for number in range(50)]], widths=[1000] * 50))
        refused(self, self.width(hwp, fit='text_width'), 'column_too_narrow', hwp)

    def test_fit_without_proved_geometry_is_refused_and_exact_widths_report_unverified(self) -> None:
        table = full_table()
        table.treat_as_char = False
        hwp = width_doc(table)
        refused(self, self.width(hwp, fit='text_width'), 'table_geometry_unknown', hwp)
        value = ok_result(self, self.width(hwp, column_widths_mm=[30, 30, 30, 30]))
        self.assertEqual(value['after']['layout']['status'], 'unverified')

    def test_fit_is_not_offered_for_a_nested_table(self) -> None:
        inner = grid('2002', [['속']], widths=[30000])
        hwp = width_doc(grid('2003', [[{'paragraphs': [para('앞', inner)]}, '옆']], widths=[9000, 9000]))
        refused(self, self.width(hwp, inst='2002', fit='text_width'), 'table_geometry_unknown', hwp)

    def test_a_stale_table_stops_before_any_edit(self) -> None:
        hwp = width_doc(full_table())
        fields = binding(hwp, '2001')
        ok_result(self, style(hwp, '2001', scope='cell', cell='A1', fill='#FFF2CC'))
        refused(self, Flow(hwp).step(op='table_width_exact', **fields, fit='text_width'), 'table_state_stale', hwp, baseline=1)

    def test_an_unexpected_result_after_the_action_is_unknown_and_not_retried(self) -> None:
        hwp = width_doc(full_table())
        def damage(native):
            native.by_inst('2001').named('A2').paragraphs = [para('바뀜')]
        hwp.corrupt_after_execute = damage
        outcome = self.width(hwp, column_widths_mm=[30, 30, 30, 30])
        self.assertFalse(outcome['ok'])
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'])
        self.assertEqual(len(hwp.native_calls()), 4)
        self.assertIn('A2', outcome['steps'][0]['outcome_detail'])

    def test_one_logical_undo_and_redo_reverse_every_counted_action(self) -> None:
        hwp = width_doc(grid('2001', [[{'paragraphs': [para('합')], 'cs': 2}, None, '오른쪽'], ['가', '나', '다']], widths=[13984, 13984, 13986]))
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        fields = binding(hwp, '2001')
        before = {cell.address: cell.width for cell in hwp.by_inst('2001').ordered()}
        self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_width_exact', 'label': 'w', **fields, 'column_widths_mm': [20, 40, 30]}])['ok'])
        after = {cell.address: cell.width for cell in hwp.by_inst('2001').ordered()}
        undone = owned.service.undo()
        self.assertEqual(undone['native_undo_steps'], 5)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual({cell.address: cell.width for cell in hwp.by_inst('2001').ordered()}, before)
        redone = owned.service.redo()
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual({cell.address: cell.width for cell in hwp.by_inst('2001').ordered()}, after)

    def test_the_schema_takes_exactly_one_way_of_giving_widths(self) -> None:
        bound = {'table_id': 'tbl:1', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 1, 'expected_cols': 2, 'confirm_layout': True}
        Command.model_validate({'session_id': SESSION, 'request': {'op': 'table_width_exact', **bound, 'fit': 'text_width'}})
        Command.model_validate({'session_id': SESSION, 'request': {'op': 'table_width_exact', **bound, 'column_widths_mm': [20, 30]}})
        for bad in ({}, {'fit': 'text_width', 'column_widths_mm': [20, 30]}, {'fit': 'page'}, {'column_widths_mm': []}, {'column_widths_mm': [0, 30]}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': SESSION, 'request': {'op': 'table_width_exact', **bound, **bad}})


class WiderNeighbour(unittest.TestCase):
    """Measured: a cell's side and the side of a wider cell facing it both print, one over the other."""

    def test_none_under_a_wider_line_is_refused_and_names_the_neighbour(self) -> None:
        hwp = sample_document()
        outcome = style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **NONE}])
        refused(self, outcome, 'edge_held_by_wider_neighbour', hwp)
        self.assertIn('A1', outcome['steps'][0]['refusal_message'])

    def test_a_line_the_wider_one_would_show_through_is_refused_unless_asked_for(self) -> None:
        for line in (THIN_RED, DASH_RED):
            with self.subTest(line=line):
                hwp = sample_document()
                refused(self, style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **line}]), 'edge_held_by_wider_neighbour', hwp)
                value = ok_result(self, style(hwp, '1015', scope='cell', cell='B2', over_wider_neighbour=True, borders=[{'edges': ['top'], **line}]))
                self.assertEqual(value['changes']['prints_over'], [{'cell': 'A1', 'side': 'bottom', 'under': 'B2', 'line': {'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}}])
                self.assertEqual(hwp.by_inst('1015').named('A1').border['bottom'], SOLID)

    def test_none_stays_refused_even_when_overprinting_is_asked_for(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1015', scope='cell', cell='B2', over_wider_neighbour=True, borders=[{'edges': ['top'], **NONE}]), 'edge_held_by_wider_neighbour', hwp)

    def test_a_solid_line_wider_than_the_neighbours_covers_it(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **THICK_RED}]))
        self.assertEqual(value['changes']['neighbours_not_written'], [{'cell': 'A1', 'side': 'bottom', 'reason': 'wider_than_edge'}])
        self.assertEqual(value['changes']['prints_over'], [])

    def test_a_wider_neighbour_with_no_line_holds_nothing_back(self) -> None:
        hwp = sample_document()
        hwp.by_inst('1015').named('A1').border['bottom'] = ('None', '0.12mm', 0)
        ok_result(self, style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **THIN_RED}]))
        self.assertEqual(hwp.by_inst('1015').named('B2').border['top'], ('Solid', '0.12mm', 0x0000FF))

    def test_the_wider_cell_itself_removes_the_whole_line(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='cell', cell='A1', borders=[{'edges': ['bottom'], **NONE}]))
        table = hwp.by_inst('1015')
        self.assertEqual([table.named(name).border[side][0] for name, side in (('A1', 'bottom'), ('A2', 'top'), ('B2', 'top'))], ['None'] * 3)

    def test_a_range_holding_the_wider_cells_whole_side_writes_it(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='range', cell='A2', end_cell='B3', borders=[{'edges': ['top'], **NONE}]))
        self.assertEqual(hwp.by_inst('1015').named('A1').border['bottom'][0], 'None')

    def test_a_range_partly_under_the_wider_cell_is_held_too(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1015', scope='range', cell='B2', end_cell='C2', borders=[{'edges': ['top'], **NONE}]), 'edge_held_by_wider_neighbour', hwp)

    def test_a_cell_beside_a_taller_merged_cell_is_held_the_same_way(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1015', scope='cell', cell='B3', borders=[{'edges': ['left'], **NONE}]), 'edge_held_by_wider_neighbour', hwp)

    def test_scope_table_with_inner_and_all_on_a_merged_table_reach_every_line(self) -> None:
        hwp = sample_document()
        value = ok_result(self, style(hwp, '1015', scope='table', borders=[{'edges': ['inner'], **NONE}]))
        table = hwp.by_inst('1015')
        self.assertEqual(table.named('A1').border['bottom'][0], 'None')
        self.assertEqual(table.named('C3').border['bottom'][0], 'Solid')
        self.assertTrue(value['changes']['edges_total'] > 0)
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='table', borders=[{'edges': ['all'], **THIN_RED}]))
        for cell in hwp.by_inst('1015').cells:
            self.assertEqual(set(cell.border.values()), {('Solid', '0.12mm', 0x0000FF)}, cell.address)

    def test_a_nested_table_takes_its_own_lines_and_the_outer_one_stays(self) -> None:
        hwp = sample_document()
        inner = grid('3001', [['속A', '속B'], ['속C', '속D']], widths=[3000, 3000])
        hwp.body.append(grid('3002', [[{'paragraphs': [para('앞', inner)]}, '옆']]))
        ok_result(self, style(hwp, '3001', scope='table', borders=[{'edges': ['inner'], **THICK_RED}]))
        self.assertEqual(hwp.by_inst('3001').named('A1').border['bottom'][1], '1.0mm')
        self.assertEqual(hwp.by_inst('3002').named('A1').border['bottom'], SOLID)


class EffectiveEdges(unittest.TestCase):
    def test_all_and_inner_mean_the_lines_the_target_has(self) -> None:
        cases = (
            ({'scope': 'cell', 'cell': 'B2'}, ['all'], 1, {'BorderTypeTop', 'BorderTypeLeft'}, {'TypeHorz', 'TypeVert'}),
            ({'scope': 'range', 'cell': 'A1', 'end_cell': 'D1'}, ['all'], 4, {'TypeVert'}, {'TypeHorz'}),
            ({'scope': 'range', 'cell': 'A1', 'end_cell': 'D1'}, ['inner'], 3, {'TypeVert'}, {'TypeHorz', 'BorderTypeTop'}),
            ({'scope': 'range', 'cell': 'B1', 'end_cell': 'B5'}, ['all'], 5, {'TypeHorz'}, {'TypeVert'}),
            ({'scope': 'range', 'cell': 'B1', 'end_cell': 'B5'}, ['inner'], 4, {'TypeHorz'}, {'TypeVert', 'BorderTypeLeft'}),
        )
        for target, edges, cells, sent, unsent in cases:
            with self.subTest(target=target, edges=edges):
                hwp = sample_document()
                value = ok_result(self, style(hwp, '1012', **target, borders=[{'edges': edges, **THICK_RED}]))
                items = hwp.native_calls()[0][2]
                self.assertTrue(sent <= set(items), items)
                self.assertFalse(unsent & set(items), items)
                self.assertEqual(value['target']['cells'], cells if edges != ['inner'] else cells + 1)

    def test_a_merged_cell_alone_takes_all_as_its_outline(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1015', scope='cell', cell='A2', borders=[{'edges': ['all'], **THICK_RED}]))
        self.assertEqual(set(hwp.by_inst('1015').named('A2').border.values()), {('Solid', '1.0mm', 0x0000FF)})

    def test_an_effective_set_that_is_empty_is_refused(self) -> None:
        hwp = sample_document()
        refused(self, style(hwp, '1012', scope='cell', cell='B2', borders=[{'edges': ['inner'], **THICK_RED}]), 'edge_not_in_target', hwp)
        refused(self, style(hwp, '1012', scope='range', cell='A1', end_cell='D1', borders=[{'edges': ['inner_horizontal'], **THICK_RED}]), 'edge_not_in_target', hwp)
        refused(self, style(hwp, '1012', scope='range', cell='B1', end_cell='B5', borders=[{'edges': ['inner_vertical'], **THICK_RED}]), 'edge_not_in_target', hwp)
        refused(self, style(hwp, '1012', scope='range', cell='A1', end_cell='D1', borders=[{'edges': ['outline'], **THIN_RED}, {'edges': ['inner_horizontal'], **THICK_RED}]), 'edge_not_in_target', hwp)


class DeleteByHash(unittest.TestCase):
    LONG = '긴셀 ' + '가나다 ABC  ' * 260

    def read(self, hwp, inst):
        return {cell['address']: cell for cell in ok_result(self, Flow(hwp).step(op='table_read', table_id=f'tbl:{inst}'))['cells']}

    def test_a_cell_too_long_to_read_whole_goes_by_its_hash(self) -> None:
        hwp = width_doc(grid('2001', [[self.LONG, '옆'], ['남는', '칸']]))
        cell = self.read(hwp, '2001')['A1']
        self.assertTrue(cell['text_truncated'])
        value = ok_result(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=[{'address': 'A1', 'content_sha256': cell['content_sha256']}, {'address': 'B1', 'text': '옆'}]))
        self.assertEqual(hwp.by_inst('2001').rows, 1)
        removed = value['changes']['removed_cells'][0]
        self.assertEqual(removed['address'], 'A1')
        self.assertEqual(removed['text_truncated'], True)
        self.assertLessEqual(len(removed['text']), nt.READ_TEXT)

    def test_many_paragraphs_with_blanks_go_by_their_hash(self) -> None:
        body = [para('둘째줄 앞'), para(), para(self.LONG[:2100])]
        hwp = width_doc(grid('2001', [[{'paragraphs': body}, '옆'], ['남는', '칸']]))
        cell = self.read(hwp, '2001')['A1']
        ok_result(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=[{'address': 'A1', 'content_sha256': cell['content_sha256']}, {'address': 'B1', 'text': '옆'}]))

    def test_the_hash_answers_for_whitespace_and_objects_stay_named(self) -> None:
        hwp = width_doc(grid('2001', [[{'paragraphs': [para('picture', self.LONG)]}, '옆'], ['남는', '칸']]))
        cell = self.read(hwp, '2001')['A1']
        self.assertEqual(cell['objects'], ['picture'])
        stated = {'address': 'A1', 'content_sha256': cell['content_sha256']}
        refused(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=[stated, {'address': 'B1', 'text': '옆'}]), 'removed_content_mismatch', hwp)
        ok_result(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=[{**stated, 'objects': ['picture']}, {'address': 'B1', 'text': '옆'}]))

    def test_a_hash_of_other_content_or_another_cell_is_refused(self) -> None:
        hwp = width_doc(grid('2001', [[self.LONG, self.LONG + ' '], ['남는', '칸']]))
        cells = self.read(hwp, '2001')
        self.assertNotEqual(cells['A1']['content_sha256'], cells['B1']['content_sha256'])
        swapped = [{'address': 'A1', 'content_sha256': cells['B1']['content_sha256']}, {'address': 'B1', 'content_sha256': cells['A1']['content_sha256']}]
        refused(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=swapped), 'removed_content_mismatch', hwp)

    def test_a_preview_is_not_accepted_for_a_long_cell(self) -> None:
        hwp = width_doc(grid('2001', [[self.LONG, '옆'], ['남는', '칸']]))
        preview = self.read(hwp, '2001')['A1']['text']
        refused(self, edit(hwp, '2001', 'delete_row', row=1, expected_removed_cells=[{'address': 'A1', 'text': preview}, {'address': 'B1', 'text': '옆'}]), 'removed_content_mismatch', hwp)

    def test_each_cell_gives_its_text_or_its_hash(self) -> None:
        for bad in ({'address': 'A1'}, {'address': 'A1', 'text': '가', 'content_sha256': 'a' * 24}, {'address': 'A1', 'content_sha256': 'short'}):
            with self.subTest(bad=bad), self.assertRaises(nt.NativeNumberingInputError):
                nt._validated_removed([bad])  # noqa: SLF001
        bound = {'table_id': 'tbl:1', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 1, 'expected_cols': 2, 'confirm_layout': True}
        request = {'op': 'table_edit_exact', **bound, 'action': 'delete_row', 'row': 1}
        Command.model_validate({'session_id': SESSION, 'request': {**request, 'expected_removed_cells': [{'address': 'A1', 'content_sha256': 'a' * 24}]}})
        with self.assertRaises(ValidationError):
            Command.model_validate({'session_id': SESSION, 'request': {**request, 'expected_removed_cells': [{'address': 'A1'}]}})


class NestedDelete(unittest.TestCase):
    def document(self, *, keep_picture: bool):
        inner = grid('2002', [[{'paragraphs': [para('picture', '속그림')]}, '속글']], widths=[3000, 3000])
        outer = grid('2003', [[{'paragraphs': [para('바깥앞', inner)]}, '오른쪽'], ['아래왼쪽', '아래오른쪽']])
        return TableHwp(['앞', outer, *([para('picture', '유지그림')] if keep_picture else []), '끝'])

    def test_reading_a_cell_names_what_its_nested_tables_hold(self) -> None:
        hwp = self.document(keep_picture=True)
        cell = {item['address']: item for item in ok_result(self, Flow(hwp).step(op='table_read', table_id='tbl:2003'))['cells']}['A1']
        self.assertEqual(sorted(cell['objects']), ['picture', 'table'])

    def test_the_picture_in_a_nested_table_must_be_stated_and_then_goes_without_a_hold(self) -> None:
        for keep in (True, False):
            with self.subTest(keep_picture=keep):
                hwp = self.document(keep_picture=keep)
                refused(self, edit(hwp, '2003', 'delete_row', row=1, expected_removed_cells=[{'address': 'A1', 'text': '바깥앞', 'objects': ['table']}, {'address': 'B1', 'text': '오른쪽'}]), 'removed_content_mismatch', hwp)
                value = ok_result(self, edit(hwp, '2003', 'delete_row', row=1, expected_removed_cells=[{'address': 'A1', 'text': '바깥앞', 'objects': ['table', 'picture']}, {'address': 'B1', 'text': '오른쪽'}]))
                self.assertEqual(sorted(value['changes']['removed_cells'][0]['objects']), ['picture', 'table'])
                self.assertEqual([table.inst for table in hwp.tables()], ['2003'])

    def test_a_kept_picture_that_lost_its_data_is_not_a_success(self) -> None:
        hwp = self.document(keep_picture=True)
        fields = {'expected_removed_cells': [{'address': 'A1', 'text': '바깥앞', 'objects': ['table', 'picture']}, {'address': 'B1', 'text': '오른쪽'}]}
        def damage(native):
            native.media_payload_override = 'other'
        hwp.corrupt_after_execute = damage
        outcome = edit(hwp, '2003', 'delete_row', row=1, **fields)
        self.assertFalse(outcome['ok'])
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'])


class Creation(unittest.TestCase):
    def create(self, hwp, cells, rows=None, cols=None):
        return Flow(hwp).step(op='table_create_exact', match='맺음 문단', expected_page=1, rows=rows or len(cells), cols=cols or len(cells[0]), cells=cells, confirm_layout=True)

    def new_table(self, hwp):
        return next(table for table in hwp.tables() if int(table.inst) >= 500 and table.inst not in {'1011', '1012', '1013', '1014', '1015', '1016', '1017'})

    def text_of(self, cell):
        return [''.join(run[0] for run in item['runs']) for item in cell.paragraphs]

    def test_a_linebreak_makes_paragraphs_and_reads_back_exactly(self) -> None:
        hwp = sample_document()
        value = ok_result(self, self.create(hwp, [['첫줄\n둘째줄', '끝'], ['', '가\r\n나']]))
        table = self.new_table(hwp)
        self.assertEqual(self.text_of(table.named('A1')), ['첫줄', '둘째줄'])
        self.assertEqual(self.text_of(table.named('B2')), ['가', '나'])
        self.assertEqual(self.text_of(table.named('A2')), [''])
        self.assertEqual(value['changes']['cell_text_filled'], True)
        self.assertEqual(hwp.calls[[call[0] for call in hwp.calls].index('put_field_text')][2], ['첫줄\r\n둘째줄', '끝', '', '가\r\n나'])

    def test_partly_blank_and_mostly_blank_arrays_are_ordinary(self) -> None:
        for cells in ([['가', ''], ['', '라']], [['', ''], ['', '라']]):
            with self.subTest(cells=cells):
                hwp = sample_document()
                ok_result(self, self.create(hwp, cells))

    def test_other_control_characters_are_refused_before_the_edit(self) -> None:
        for text in ('가\r나', '가\x01나', '가\x0b나', '\x0c'):
            with self.subTest(text=text):
                hwp = sample_document()
                refused(self, self.create(hwp, [[text, '끝']]), 'cells_text_unsupported', hwp)

    def test_tab_and_spaces_stay(self) -> None:
        hwp = sample_document()
        ok_result(self, self.create(hwp, [['가\t나  다', '끝']]))
        self.assertEqual(self.text_of(self.new_table(hwp).named('A1')), ['가\t나  다'])

    def test_text_is_still_not_counted_as_one_undo(self) -> None:
        hwp = sample_document()
        value = ok_result(self, self.create(hwp, [['가', '나']]))
        self.assertIsNone(value['undo']['native_editing_actions'])


class HistoryDifferences(unittest.TestCase):
    def run_edit(self, hwp, **fields):
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        target = binding(hwp, '1015')
        result = owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'a', **target, **fields}])
        self.assertTrue(result['ok'], result)
        return owned, result

    def test_a_mismatch_names_table_cell_field_and_both_values(self) -> None:
        hwp = sample_document()
        owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        def damage(native):
            native.by_inst('1015').named('A2').height = 7777
        hwp.history_damage = damage
        undone = owned.service.undo()
        self.assertFalse(undone['ok'])
        expected = undone['confirmation']['expected_state']
        self.assertEqual(expected['matches'], False)
        self.assertIn({'table_id': 'tbl:1015', 'cell': 'A2', 'field': 'height', 'recorded': 3600, 'now': 7777}, expected['differences'])
        self.assertEqual(expected['differences_total'], len(expected['differences']))
        self.assertIn('A2', undone['detail'])

    def test_a_frame_difference_is_named_too(self) -> None:
        hwp = sample_document()
        owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        def damage(native):
            native.by_inst('1015').zorder = 9
        hwp.history_damage = damage
        expected = owned.service.undo()['confirmation']['expected_state']
        self.assertIn({'table_id': 'tbl:1015', 'cell': None, 'field': 'zorder', 'recorded': '1', 'now': '9'}, expected['differences'])

    def test_naming_it_takes_no_further_native_call_after_the_history_read(self) -> None:
        hwp = sample_document()
        owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        hwp.history_damage = lambda native: setattr(native.by_inst('1015').named('A2'), 'height', 7777)
        without = len([call for call in hwp.calls if call[0] == 'get_text_file'])
        owned.service.undo()
        self.assertEqual(len([call for call in hwp.calls if call[0] == 'get_text_file']) - without, 2)  # Before and after the Undo, and no more.

    def test_values_a_hash_leaves_out_are_named_when_they_are_what_moved(self) -> None:
        for damage, row in ((lambda t: setattr(t.named('A2'), 'dirty', 'true'), {'table_id': 'tbl:1015', 'cell': 'A2', 'field': 'dirty', 'recorded': 'false', 'now': 'true'}),
                            (lambda t: setattr(t.named('A2'), 'link_list', 7), {'table_id': 'tbl:1015', 'cell': 'A2', 'field': 'linklistid', 'recorded': '0', 'now': '7'}),
                            (lambda t: setattr(t, 'size_height', 7777), {'table_id': 'tbl:1015', 'cell': None, 'field': 'size.height', 'recorded': '9000', 'now': '7777'})):
            with self.subTest(row=row):
                hwp = sample_document()
                owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
                hwp.history_damage = lambda native, change=damage: change(native.by_inst('1015'))
                self.assertIn(row, owned.service.undo()['confirmation']['expected_state']['differences'])

    def test_a_difference_only_in_the_compared_evidence_is_named_too(self) -> None:
        evidence = {'paragraphs': [{'text': '비밀글', 'heading': 'NONE', 'level': 0, 'content_sha256': 'aaaa', 'end_charshape_sha256': 'bbbb'}], 'controls': ['tbl'], 'digests': {'styles_sha256': 's', 'media_sha256': 'm'}}
        table = {'tables': {}, 'paragraphs': []}
        def changed(**fields):
            value = {'paragraphs': [dict(evidence['paragraphs'][0])], 'controls': list(evidence['controls']), 'digests': dict(evidence['digests'])}
            if 'content' in fields:
                value['paragraphs'][0]['content_sha256'] = fields['content']
            if 'end' in fields:
                value['paragraphs'][0]['end_charshape_sha256'] = fields['end']
            if 'styles' in fields:
                value['digests']['styles_sha256'] = fields['styles']
            if 'controls' in fields:
                value['controls'] = fields['controls']
            return value
        old = nt.evidence_detail(evidence)
        self.assertEqual(nt.detail_differences({**table, 'evidence': old}, {**table, 'evidence': old}), [])
        rows = nt.detail_differences({**table, 'evidence': old}, {**table, 'evidence': nt.evidence_detail(changed(content='cccc'))})
        self.assertEqual(rows, [{'table_id': None, 'cell': None, 'field': 'body paragraph[0] content', 'recorded': 'aaaa', 'now': 'cccc'}])
        self.assertEqual(nt.detail_differences({**table, 'evidence': old}, {**table, 'evidence': nt.evidence_detail(changed(end='dddd'))})[0]['field'], 'body paragraph[0] end_charshape')
        self.assertEqual(nt.detail_differences({**table, 'evidence': old}, {**table, 'evidence': nt.evidence_detail(changed(styles='t'))})[0]['field'], 'styles_sha256')
        self.assertEqual(nt.detail_differences({**table, 'evidence': old}, {**table, 'evidence': nt.evidence_detail(changed(controls=[]))})[0]['field'], 'controls')
        self.assertNotIn('비밀글', str(rows))

    def test_a_match_carries_no_difference_rows(self) -> None:
        hwp = sample_document()
        owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        self.assertEqual(owned.service.undo()['confirmation']['expected_state'], {'compared': True, 'matches': True})

    def test_the_recorded_detail_stays_out_of_the_public_reply(self) -> None:
        hwp = sample_document()
        _owned, result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        undo = result['steps'][0]['result']['undo']
        self.assertEqual(set(undo), {'native_editing_actions', 'pre_state_sha256', 'post_state_sha256'})

    def test_after_a_mismatch_the_history_stops_and_the_document_stays_usable(self) -> None:
        hwp = sample_document()
        owned, _result = self.run_edit(hwp, scope='cell', cell='B2', fill='#FFF2CC')
        hwp.history_damage = lambda native: setattr(native.by_inst('1015').named('A2'), 'height', 7777)
        self.assertFalse(owned.service.undo()['ok'])
        hwp.history_damage = None
        with self.assertRaises(Exception) as caught:
            owned.service.undo()
        self.assertEqual(getattr(caught.exception, 'code', None), 'undo_not_counted')
        fields = {'table_id': 'tbl:1012', **{key: value for key, value in binding(hwp, '1012').items() if key != 'table_id'}}
        self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'b', **fields, 'scope': 'cell', 'cell': 'A1', 'fill': '#FFF2CC'}])['ok'])


class HeaderAndFooterTables(unittest.TestCase):
    def document(self):
        hwp = sample_document()
        hwp.header_table = grid('4001', [['머리표가', '머리표나']], widths=[9000, 9000])
        hwp.footer_table = grid('4002', [['꼬리표가', '꼬리표나']], widths=[9000, 9000])
        return hwp

    def test_they_are_read_apart_from_the_body(self) -> None:
        model = nt.parse_table_model(self.document().get_text_file('HWPML2X', ''))
        self.assertNotIn('tbl:4001', [table['table_id'] for table in model['tables']])
        self.assertEqual([(table['table_id'], table['scope']) for table in model['margin_tables']], [('tbl:4001', 'header'), ('tbl:4002', 'footer')])
        self.assertFalse(any('머리표가' == row['text'] for row in model['paragraphs']))

    def test_the_inventory_is_of_the_body_and_a_margin_table_is_refused_by_name(self) -> None:
        hwp = self.document()
        self.assertNotIn('tbl:4001', inventory(hwp))
        outcome = Flow(hwp).step(op='table_read', table_id='tbl:4001')
        self.assertEqual(outcome['steps'][0]['refusal_code'], 'target_in_header_footer')

    def test_the_review_carries_them_with_their_scope_and_no_visit_to_their_anchor(self) -> None:
        hwp = self.document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        facts = owned.service.numbering_readback()
        rows = {row['table_id']: row for row in facts['tables']}
        self.assertEqual((rows['tbl:4001']['scope'], rows['tbl:4002']['scope'], rows['tbl:1012']['scope']), ('header', 'footer', 'body'))
        self.assertIsNone(rows['tbl:4001']['page'])
        self.assertEqual(rows['tbl:4001']['cells'][0]['text'], '머리표가')


class ReviewRows(unittest.TestCase):
    def rows(self, hwp):
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        return owned.service.numbering_readback()

    def test_frame_and_cell_properties_are_in_the_row_by_name(self) -> None:
        hwp = sample_document()
        table = hwp.by_inst('1012')
        table.named('A1').vert_align = 'Top'
        table.named('A1').margin = (100, 200, 300, 400)
        rows = {row['table_id']: row for row in self.rows(hwp)['tables']}
        row = rows['tbl:1012']
        self.assertEqual(row['frame_values']['zorder'], '1')
        self.assertEqual(row['frame_values']['page_break'], 'Cell')
        self.assertTrue(row['frame'])
        cell = next(item for item in row['cells'] if item['address'] == 'A1')
        self.assertEqual(cell['attr_values']['vertical_align'], 'Top')
        self.assertEqual(cell['attr_values']['margin'], {'left': 200, 'right': 300, 'top': 400, 'bottom': 100})
        self.assertTrue(cell['attrs'])
        self.assertEqual(row['layout']['status'], 'fits')

    def test_text_longer_than_a_row_holds_says_it_was_cut(self) -> None:
        hwp = width_doc(grid('2001', [['가' * 400, '짧음']]))
        cells = {cell['address']: cell for cell in self.rows(hwp)['tables'][0]['cells']}
        self.assertEqual((cells['A1']['text_truncated'], len(cells['A1']['text'])), (True, 300))
        self.assertEqual(cells['B1']['text_truncated'], False)

    def test_a_paragraph_row_says_which_table_and_cell_it_belongs_to(self) -> None:
        hwp = sample_document()
        facts = self.rows(hwp)
        by_text = {row['text']: row for row in facts['paragraphs']}
        self.assertEqual((by_text['병합머리']['table_id'], by_text['병합머리']['cell']), ('tbl:1015', 'A1'))
        self.assertEqual(by_text['맺음 문단'].get('table_id'), None)
        holders = [row for row in facts['paragraphs'] if row.get('holds_tables')]
        self.assertIn(['tbl:1015'], [row['holds_tables'] for row in holders])

    def test_a_non_solid_fill_reads_as_its_family_and_meaning(self) -> None:
        hwp = width_doc(grid('2001', [[{'paragraphs': [para('무늬')], 'fill': {'kind': 'pattern', 'face': 16777215, 'hatch': 16711680, 'style': 'Slash'}}, {'paragraphs': [para('그러')], 'fill': {'kind': 'gradation'}}]]))
        cells = {cell['address']: cell for cell in self.rows(hwp)['tables'][0]['cells']}
        self.assertEqual(cells['A1']['fill']['family'], 'pattern')
        self.assertEqual(cells['A1']['fill']['meaning'], {'style': 'slash', 'face': '#FFFFFF', 'hatch': '#0000FF', 'alpha': 0})
        self.assertEqual(cells['B1']['fill']['family'], 'gradation')
        self.assertEqual(cells['B1']['fill']['meaning'], {'type': 'linear', 'angle': 90, 'center_x': 0, 'center_y': 0, 'step': 255, 'step_center': 50, 'colors': ['#FF0000', '#0000FF'], 'alpha': 0})

    def test_reading_or_saving_beside_such_a_fill_is_ordinary_and_replacing_it_is_refused(self) -> None:
        hwp = width_doc(grid('2001', [[{'paragraphs': [para('무늬')], 'fill': {'kind': 'pattern', 'face': 16777215, 'hatch': 16711680, 'style': 'Slash'}}, '옆']]))
        ok_result(self, style(hwp, '2001', scope='cell', cell='B1', fill='#FFF2CC'))
        refused(self, style(hwp, '2001', scope='cell', cell='A1', fill='#FFF2CC'), 'fill_kind_unsupported', hwp, baseline=1)


class ReadFailures(unittest.TestCase):
    """A read that returned and cannot be used is a fact about the document. A read that raised has no next SDK call."""

    def test_an_unusable_control_walk_leaves_the_review_readable_with_pages_unverified(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        hwp.control_id_override = 7  # What the walk returned is not usable; nothing raised.
        facts = owned.service.numbering_readback()
        self.assertEqual({row['page'] for row in facts['tables']}, {None})
        self.assertEqual(facts['tables_pages_reason'], 'control_enumeration_unavailable')
        self.assertTrue(facts['tables'])

    def test_a_raising_walk_makes_no_further_sdk_call(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        hwp.head_ctrl_error = OSError('native read failed')
        count = None
        with self.assertRaises(Exception):
            count = len(hwp.calls)
            owned.service.numbering_readback()
        last = [index for index, call in enumerate(hwp.calls) if call == ('HeadCtrl',)]
        self.assertEqual(last[-1], len(hwp.calls) - 1, hwp.calls[last[-1]:])
        self.assertIsNotNone(count)

    def test_a_refused_walk_in_an_inventory_gives_the_caret_back_and_keeps_earlier_work(self) -> None:
        hwp = sample_document()
        ok_result(self, style(hwp, '1012', scope='cell', cell='B2', fill='#FFF2CC'))
        dirty = len(hwp.undo_stack)
        hwp.set_pos(0, 2, 0)
        hwp.control_id_override = 7
        outcome = Flow(hwp).step(op='table_inventory')
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'control_enumeration_unavailable')
        self.assertFalse(outcome.get('native_custody_held', False))
        self.assertEqual(hwp.get_pos(), (0, 2, 0))
        self.assertEqual(len(hwp.undo_stack), dirty)
        hwp.control_id_override = None
        self.assertTrue(Flow(hwp).step(op='table_inventory')['ok'])

    def test_a_caret_that_cannot_be_given_back_after_the_walk_is_not_called_healthy(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        original = hwp.set_pos_by_set
        def moved(anchor):
            original(anchor)
            hwp.set_pos = lambda *args: False  # Reads moved it; putting it back no longer works.
            return True
        hwp.set_pos_by_set = moved
        with self.assertRaises(Exception):
            owned.service.numbering_readback()


class ReplyAndValidation(unittest.TestCase):
    def test_a_refusal_code_of_this_batch_reaches_the_caller_by_name(self) -> None:
        hwp = sample_document()
        self.assertEqual(style(hwp, '1015', scope='cell', cell='B2', borders=[{'edges': ['top'], **NONE}])['steps'][0]['refusal_code'], 'edge_held_by_wider_neighbour')

    def test_over_wider_neighbour_needs_borders(self) -> None:
        bound = {'table_id': 'tbl:1', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 1, 'expected_cols': 2, 'confirm_layout': True}
        request = {'op': 'table_style_exact', **bound, 'scope': 'cell', 'cell': 'A1', 'over_wider_neighbour': True}
        with self.assertRaises(ValidationError):
            Command.model_validate({'session_id': SESSION, 'request': {**request, 'fill': 'none'}})
        Command.model_validate({'session_id': SESSION, 'request': {**request, 'borders': [{'edges': ['top'], 'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}]}})


if __name__ == '__main__':
    unittest.main()
