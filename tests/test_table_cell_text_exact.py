"""Writing the text of existing table cells through the real bundle validation, package dispatch, service and census.

The SDK is tests.native_text_fakes.CellTextHwp, the table fake with a text caret in cells; source behavior only.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from hwpx_mcp.schema import Command
from tests.native_text_fakes import CellTextHwp, plan_tables
from tests.test_native_table import Flow, binding, inventory
from tests.test_repair_runtime_safety import owned_doc

OP = {'op': 'table_cell_text_exact'}


def fill(hwp: CellTextHwp, inst: str, cells: list[dict], **overrides):
    return Flow(hwp).step(**OP, **binding(hwp, inst), cells=cells, **overrides)


def ok(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: CellTextHwp) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual([call for call in hwp.calls if call[:2] in {('native', 'InsertText'), ('Run', 'Delete')}], [])
    return step


def texts(hwp: CellTextHwp, inst: str) -> dict[str, list[str]]:
    return {cell.address: [''.join(run[0] for run in item['runs'] if isinstance(run, tuple)) for item in cell.paragraphs] for cell in hwp.by_inst(inst).ordered()}


class Fill(unittest.TestCase):
    def test_two_cells_in_one_command_with_the_next_binding(self) -> None:
        hwp = plan_tables()
        before = texts(hwp, '1011')
        result = ok(self, fill(hwp, '1011', [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'},
                                            {'address': 'C2', 'expected_text': 'Planned', 'text': 'Revised'}]))
        after = texts(hwp, '1011')
        self.assertEqual(after['B2'], ['1,350'])
        self.assertEqual(after['C2'], ['Revised'])
        self.assertEqual({key: value for key, value in after.items() if key not in {'B2', 'C2'}}, {key: value for key, value in before.items() if key not in {'B2', 'C2'}})
        self.assertEqual(result['changes'], [{'address': 'B2', 'before': '1,200', 'after': '1,350'}, {'address': 'C2', 'before': 'Planned', 'after': 'Revised'}])
        self.assertEqual(result['after']['expected_hash'], inventory(hwp)['tbl:1011']['expected_hash'])
        self.assertNotEqual(result['after']['expected_hash'], result['before']['expected_hash'])
        self.assertEqual(result['undo']['native_editing_actions'], 4)
        self.assertEqual(result['result']['native_actions'], ['B2:Execute:InsertText', 'B2:Run:Delete', 'C2:Execute:InsertText', 'C2:Run:Delete'])
        cell = hwp.by_inst('1011').named('B2')
        self.assertEqual(cell.paragraphs[0]['runs'], [('1,350', 0)])
        self.assertEqual(hwp.SelectionMode, 0)

    def test_a_blank_cell_takes_the_text_in_one_action(self) -> None:
        hwp = plan_tables()
        result = ok(self, fill(hwp, '1012', [{'address': 'C1', 'expected_text': '', 'text': 'Filled'}]))
        self.assertEqual(texts(hwp, '1012')['C1'], ['Filled'])
        self.assertEqual(result['undo']['native_editing_actions'], 1)
        self.assertEqual(result['changes'], [{'address': 'C1', 'before': '', 'after': 'Filled'}])

    def test_the_new_text_keeps_the_cell_format(self) -> None:
        hwp = plan_tables()
        hwp.by_inst('1011').named('A3').paragraphs[0]['runs'] = [('Printing', 1)]
        ok(self, fill(hwp, '1011', [{'address': 'A3', 'expected_text': 'Printing', 'text': 'Binding'}]))
        self.assertEqual(hwp.by_inst('1011').named('A3').paragraphs[0]['runs'], [('Binding', 1)])

    def test_cells_that_cannot_be_written_whole_are_refused_before_any_edit(self) -> None:
        for inst, cell, code in (
            ('1012', {'address': 'B1', 'expected_text': '합친머리', 'text': 'x'}, 'cell_covered_by_merge'),
            ('1012', {'address': 'B2', 'expected_text': '두\n문단', 'text': 'x'}, 'cell_multi_paragraph'),
            ('1012', {'address': 'C2', 'expected_text': '굵게보통', 'text': 'x'}, 'cell_mixed_format'),
            ('1012', {'address': 'A3', 'expected_text': '그림뒤', 'text': 'x'}, 'cell_has_objects'),
            ('1011', {'address': 'B2', 'expected_text': '1,300', 'text': '1,350'}, 'cell_text_mismatch'),
            ('1011', {'address': 'D2', 'expected_text': '', 'text': 'x'}, 'cell_out_of_range'),
        ):
            with self.subTest(cell=cell):
                hwp = plan_tables()
                before = texts(hwp, inst)
                refused(self, fill(hwp, inst, [{'address': 'A1', 'expected_text': texts(hwp, inst)['A1'][0], 'text': 'first'}, cell] if inst == '1011' else [cell]), code, hwp)
                self.assertEqual(texts(hwp, inst), before)

    def test_a_stale_binding_is_refused(self) -> None:
        hwp = plan_tables()
        stale = binding(hwp, '1011')
        hwp.by_inst('1011').named('A1').paragraphs[0]['runs'] = [('Thing', 0)]
        outcome = Flow(hwp).step(**OP, **stale, cells=[{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}])
        refused(self, outcome, 'table_state_stale', hwp)

    def test_requests_are_validated_before_any_native_call(self) -> None:
        for cells in (
            [],
            [{'address': 'B2', 'expected_text': '1,200'}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': ''}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': '1,200'}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': 'a\nb'}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': 'a\tb'}],
            [{'address': 'b2', 'expected_text': '1,200', 'text': 'x'}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': 'x'}, {'address': 'B2', 'expected_text': '1,200', 'text': 'y'}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': 'x', 'bold': True}],
            [{'address': 'B2', 'expected_text': '1,200', 'text': 'x'}] * 101,
        ):
            with self.subTest(cells=cells[:2]):
                hwp = plan_tables()
                with self.assertRaises(Exception):
                    fill(hwp, '1011', cells)
                self.assertEqual([call for call in hwp.calls if call[:2] in {('native', 'InsertText'), ('Run', 'Delete')}], [])

    def test_mcp_schema_takes_the_same_fields(self) -> None:
        bound = {'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 3, 'expected_cols': 3, 'confirm_layout': True}
        Command.model_validate({'session_id': 'a' * 32, 'request': {**OP, **bound, 'cells': [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}]}})
        for bad in ({'cells': []}, {'cells': [{'address': 'B2', 'text': 'x'}]}, {'cells': [{'address': 'B2', 'expected_text': '', 'text': 'x', 'extra': 1}]},
                    {'cells': [{'address': 'B2', 'expected_text': '', 'text': 'x'}], 'confirm_layout': False}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': 'a' * 32, 'request': {**OP, **bound, **bad}})


class UnknownOutcome(unittest.TestCase):
    """A real manager and owning thread: once InsertText may have run, the session is held and nothing else is tried."""

    def run_step(self, hwp: CellTextHwp, cells: list[dict]):
        bound = binding(hwp, '1011')
        owned = owned_doc(self, hwp)
        owned.service._bundle_page_evidence = lambda native, **kwargs: {'page': native.current_page}
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned, owned.service.command_bundle(steps=[{'label': 'x', **OP, **bound, 'cells': cells}])

    def assert_held(self, owned, outcome: dict, *, inserts: int) -> None:
        self.assertFalse(outcome['ok'], outcome)
        step = outcome['steps'][0]
        self.assertTrue(outcome['native_custody_held'], outcome)
        self.assertTrue(step['mutation_may_have_persisted'], step)
        self.assertNotIn('refusal_code', step)
        self.assertTrue(owned.session._native_custody_held)
        self.assertEqual(len([call for call in owned.hwp.calls if call[:2] == ('native', 'InsertText')]), inserts, 'no further insertion')
        self.assertNotIn(('Run', 'Undo'), owned.hwp.calls)

    def test_insert_not_returning_true(self) -> None:
        hwp = plan_tables()
        hwp.insert_result = False
        owned, outcome = self.run_step(hwp, [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}, {'address': 'C2', 'expected_text': 'Planned', 'text': 'Revised'}])
        self.assert_held(owned, outcome, inserts=1)

    def test_delete_failing_on_the_second_cell(self) -> None:
        hwp = plan_tables()
        original = hwp._delete
        calls = []

        def second_fails():
            calls.append(1)
            return False if len(calls) == 2 else original()
        hwp._delete = second_fails
        owned, outcome = self.run_step(hwp, [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}, {'address': 'C2', 'expected_text': 'Planned', 'text': 'Revised'}])
        self.assert_held(owned, outcome, inserts=2)
        self.assertEqual(len(calls), 2)

    def test_new_text_in_another_format_is_not_a_success(self) -> None:
        hwp = plan_tables()
        hwp.insert_shape = 1
        owned, outcome = self.run_step(hwp, [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}])
        self.assert_held(owned, outcome, inserts=1)


class UndoRedo(unittest.TestCase):
    def test_one_fill_is_undone_and_redone_as_one_request(self) -> None:
        hwp = plan_tables()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        owned.service._bundle_page_evidence = lambda native, **kwargs: {'page': native.current_page}

        def run(**fields):
            with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
                return owned.service.command_bundle(steps=[{'label': 'x', **fields}])

        def history(name):
            with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                    patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
                return getattr(owned.service, name)()
        original = texts(hwp, '1011')
        self.assertTrue(run(**OP, **binding(hwp, '1011'), cells=[{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}, {'address': 'C2', 'expected_text': 'Planned', 'text': 'Revised'}])['ok'])
        filled = texts(hwp, '1011')
        undone = history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 4)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(texts(hwp, '1011'), original)
        redone = history('redo')
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(texts(hwp, '1011'), filled)


if __name__ == '__main__':
    unittest.main()
