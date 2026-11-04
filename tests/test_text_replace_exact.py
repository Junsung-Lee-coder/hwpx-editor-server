"""Replacing one exact range of existing text through the real bundle validation, package dispatch, service and census.

The SDK is tests.native_text_fakes, built on measured native behavior. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from hwpx_mcp.schema import Command
from tests.native_format_fakes import char
from tests.native_text_fakes import TextHwp, plan_document
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

REPLACE = {'op': 'text_replace_exact', 'expected_page': 1, 'confirm_layout': True}
TITLE = char(Height=1600, Bold=True)
BOLD = char(Bold=True)


def replace(hwp: TextHwp, **fields):
    return Flow(hwp).step(**REPLACE, **fields)


def ok(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: TextHwp) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(hwp.executes(), [])
    test.assertNotIn(('Run', 'Delete'), hwp.calls)
    return step


def text_of(hwp: TextHwp, list_id: int, para: int) -> str:
    return ''.join(c for c, _props in hwp.text_props(list_id, para))


def shapes_of(hwp: TextHwp, list_id: int, para: int, low: int, high: int) -> list[dict]:
    return [props for _c, props in hwp.text_props(list_id, para)[low:high]]


class Replace(unittest.TestCase):
    def test_title_year_in_context_keeps_the_title_format_and_the_body_year(self) -> None:
        hwp = plan_document()
        result = ok(self, replace(hwp, match='Annual Plan 2025', old_text='2025', new_text='2026'))
        self.assertEqual(text_of(hwp, 0, 0), 'Annual Plan 2026')
        self.assertEqual(shapes_of(hwp, 0, 0, 0, 16), [TITLE] * 16)
        self.assertIn('annual plan for 2025.', text_of(hwp, 0, 1))
        self.assertEqual(result['target']['paragraph_index'], 0)
        self.assertEqual(result['target']['context'], 'body')
        self.assertEqual((result['target']['occurrence'], result['target']['occurrences']), (1, 1))
        self.assertEqual(result['result']['paragraph_before'], 'Annual Plan 2025')
        self.assertEqual(result['result']['paragraph_after'], 'Annual Plan 2026')
        self.assertEqual(result['result']['format']['bold'], True)
        self.assertEqual(result['result']['format']['size_pt'], 16.0)
        self.assertEqual(result['result']['native_actions'], ['Execute:InsertText', 'Run:Delete'])
        self.assertEqual(result['undo']['native_editing_actions'], 2)
        self.assertRegex(result['undo']['pre_state_sha256'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(result['undo']['pre_state_sha256'], result['undo']['post_state_sha256'])
        inserts = [call for call in hwp.executes() if call[1] == 'InsertText']
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][2], {'Text': '2026'})
        self.assertIsNone(inserts[0][3], 'the new text goes in at a bare caret, never over a block')
        self.assertEqual(hwp.selection, None)

    def test_new_text_takes_the_format_of_the_replaced_bold_run(self) -> None:
        hwp = plan_document()
        ok(self, replace(hwp, old_text='budget', new_text='costs'))
        props = dict(hwp.text_props(0, 1))
        text = text_of(hwp, 0, 1)
        start = text.index('costs')
        self.assertEqual(shapes_of(hwp, 0, 1, start, start + 5), [BOLD] * 5)
        self.assertEqual(shapes_of(hwp, 0, 1, start - 1, start), [char()])
        self.assertEqual(shapes_of(hwp, 0, 1, start + 5, start + 6), [char()])
        self.assertTrue(props)

    def test_the_first_text_after_the_section_controls_keeps_its_format_and_the_controls(self) -> None:
        hwp = plan_document()
        controls = [atom.get('ctrl') for atom in hwp.atoms_of(0, 0) if 'ctrl' in atom]
        ok(self, replace(hwp, old_text='Annual', new_text='Yearly'))
        self.assertEqual(text_of(hwp, 0, 0), 'Yearly Plan 2025')
        self.assertEqual(shapes_of(hwp, 0, 0, 0, 16), [TITLE] * 16)
        self.assertEqual([atom.get('ctrl') for atom in hwp.atoms_of(0, 0) if 'ctrl' in atom], controls)

    def test_a_repeated_phrase_needs_an_occurrence_and_its_count(self) -> None:
        hwp = plan_document()
        step = refused(self, replace(hwp, old_text='Status: draft', new_text='Status: final'), 'target_ambiguous', hwp)
        self.assertIn('3', step['refusal_message'])
        hwp = plan_document()
        refused(self, replace(hwp, old_text='Status: draft', new_text='Status: final', occurrence=2, expected_occurrences=2), 'target_count_mismatch', hwp)
        hwp = plan_document()
        result = ok(self, replace(hwp, old_text='Status: draft', new_text='Status: final', occurrence=2, expected_occurrences=3))
        self.assertEqual(text_of(hwp, 0, 3), 'Status: draft. Owner: team. Status: final.')
        self.assertEqual(text_of(hwp, 0, 4), 'Status: draft')
        self.assertEqual((result['target']['occurrence'], result['target']['occurrences']), (2, 3))

    def test_a_paragraph_named_by_index_and_text(self) -> None:
        hwp = plan_document()
        ok(self, replace(hwp, paragraph_index=8, expected_text='Status: draft', old_text='draft', new_text='final'))
        self.assertEqual(text_of(hwp, 0, 4), 'Status: final')
        self.assertEqual(text_of(hwp, 0, 3), 'Status: draft. Owner: team. Status: draft.')
        hwp = plan_document()
        refused(self, replace(hwp, paragraph_index=8, expected_text='Status: final', old_text='draft', new_text='final'), 'target_text_mismatch', hwp)
        hwp = plan_document()
        refused(self, replace(hwp, paragraph_index=7, expected_text='Status: draft. Owner: team. Status: draft.', old_text='draft', new_text='final'), 'target_ambiguous', hwp)
        hwp = plan_document()
        ok(self, replace(hwp, paragraph_index=7, expected_text='Status: draft. Owner: team. Status: draft.', old_text='draft', new_text='final', occurrence=1, expected_occurrences=2))
        self.assertEqual(text_of(hwp, 0, 3), 'Status: final. Owner: team. Status: draft.')

    def test_a_table_cell_paragraph(self) -> None:
        hwp = plan_document()
        cell_list = next(number for number, paragraphs in hwp.lists.items() if number and paragraphs[0]['atoms'] and text_of(hwp, number, 0) == '1,200')
        result = ok(self, replace(hwp, old_text='1,200', new_text='1,350'))
        self.assertEqual(text_of(hwp, cell_list, 0), '1,350')
        self.assertEqual(result['target']['context'], 'table_cell')
        self.assertEqual([text_of(hwp, number, 0) for number in sorted(hwp.lists) if number and hwp.lists[number][0]['atoms']][:3], ['Materials', '1,350', 'Planned'])

    def test_mixed_formats_are_refused_unless_the_last_character_is_chosen(self) -> None:
        hwp = plan_document()
        step = refused(self, replace(hwp, old_text='RedBlue', new_text='Green'), 'target_mixed_format', hwp)
        self.assertIn('format_from', step['refusal_message'])
        hwp = plan_document()
        result = ok(self, replace(hwp, old_text='RedBlue', new_text='Green', format_from='last_character'))
        self.assertEqual(text_of(hwp, 0, 5), 'Mixed Green tail')
        self.assertEqual(shapes_of(hwp, 0, 5, 6, 11), [char(Italic=True)] * 5)
        self.assertEqual(result['requested']['format_from'], 'last_character')

    def test_text_right_after_an_inline_picture(self) -> None:
        hwp = plan_document()
        pictures = [atom for atom in hwp.atoms_of(0, 6) if 'ctrl' in atom]
        ok(self, replace(hwp, old_text='Text after', new_text='Words after'))
        self.assertEqual(text_of(hwp, 0, 6), 'BeforeWords after the picture')
        self.assertEqual([atom for atom in hwp.atoms_of(0, 6) if 'ctrl' in atom], pictures)

    def test_a_range_ending_right_before_an_invisible_control_keeps_the_control(self) -> None:
        hwp = plan_document()
        ok(self, replace(hwp, old_text='mark', new_text='point'))
        atoms = hwp.atoms_of(0, 7)
        self.assertEqual(text_of(hwp, 0, 7), 'Before point after')
        self.assertEqual([atom.get('ctrl') for atom in atoms if 'ctrl' in atom], ['bokm'])
        self.assertEqual(atoms[len('Before point')].get('ctrl'), 'bokm')

    def test_a_range_starting_right_after_an_invisible_control_is_refused(self) -> None:
        hwp = plan_document()
        refused(self, replace(hwp, match='mark after', old_text=' after', new_text=' later'), 'target_beside_unproven_control', hwp)

    def test_numbered_paragraph_end(self) -> None:
        hwp = plan_document()
        ok(self, replace(hwp, old_text='item', new_text='entry'))
        self.assertEqual(text_of(hwp, 0, 8), 'First numbered entry')

    def test_places_that_cannot_be_edited_are_refused_before_any_edit(self) -> None:
        for fields, code in (
            ({'old_text': 'Inside box', 'new_text': 'Outside'}, 'target_unsupported_context'),
            ({'old_text': 'SAMPLE', 'new_text': 'DRAFT'}, 'target_in_header_footer'),
            ({'old_text': 'Nowhere', 'new_text': 'Somewhere'}, 'target_not_found'),
            ({'old_text': 'Before', 'new_text': 'After'}, 'target_ambiguous'),
            ({'match': 'Before mark after', 'old_text': 'mark after', 'new_text': 'x'}, 'target_crosses_invisible_control'),
        ):
            with self.subTest(fields=fields):
                hwp = plan_document()
                refused(self, replace(hwp, **fields), code, hwp)

    def test_wrong_page_says_where_the_target_is(self) -> None:
        hwp = plan_document(pages={1: 2})
        flow = Flow(hwp)
        flow.service._bundle_page_evidence = lambda doc, **kwargs: {'page': doc.current_page}
        step = refused(self, flow.step(**REPLACE, old_text='budget', new_text='costs'), 'target_wrong_page', hwp)
        self.assertEqual(step['found_page'], 2)
        self.assertTrue(flow.step(**{**REPLACE, 'expected_page': 2}, old_text='budget', new_text='costs')['ok'])

    def test_a_clean_refusal_gives_the_caret_back(self) -> None:
        hwp = plan_document()
        hwp.set_pos(0, 10, 3)
        hwp.calls.clear()
        refused(self, replace(hwp, old_text='Status: draft', new_text='x'), 'target_ambiguous', hwp)
        self.assertEqual((hwp.cursor, hwp.selection), ((0, 10, 3), None))

    def test_requests_are_validated_before_any_native_call(self) -> None:
        for fields in (
            {'old_text': 'budget'},
            {'new_text': 'costs'},
            {'old_text': '', 'new_text': 'costs'},
            {'old_text': 'budget', 'new_text': ''},
            {'old_text': 'budget', 'new_text': 'budget'},
            {'old_text': 'budget', 'new_text': 'two\nlines'},
            {'old_text': 'budget', 'new_text': 'tab\there'},
            {'old_text': 'budget', 'new_text': 'costs', 'occurrence': 1},
            {'old_text': 'budget', 'new_text': 'costs', 'expected_occurrences': 1},
            {'old_text': 'budget', 'new_text': 'costs', 'occurrence': 2, 'expected_occurrences': 1},
            {'old_text': 'budget', 'new_text': 'costs', 'match': 'The schedule'},
            {'old_text': 'a', 'new_text': 'b', 'match': 'banana'},
            {'old_text': 'budget', 'new_text': 'costs', 'paragraph_index': 1},
            {'old_text': 'budget', 'new_text': 'costs', 'paragraph_index': 1, 'expected_text': 'x', 'match': 'budget'},
            {'old_text': 'budget', 'new_text': 'costs', 'format_from': 'first_character'},
            {'old_text': 'budget', 'new_text': 'costs', 'colour': 'red'},
            {'old_text': 'budget', 'new_text': 'costs', 'confirm_layout': False},
        ):
            with self.subTest(fields=fields):
                hwp = plan_document()
                with self.assertRaises(Exception):
                    Flow(hwp).step(**{**REPLACE, **fields})
                self.assertEqual(hwp.executes(), [])
                self.assertNotIn(('Run', 'Delete'), hwp.calls)

    def test_mcp_schema_takes_the_same_fields(self) -> None:
        for good in ({'match': 'Annual Plan 2025', 'old_text': '2025', 'new_text': '2026'},
                     {'old_text': 'draft', 'new_text': 'final', 'occurrence': 2, 'expected_occurrences': 3, 'format_from': 'last_character'},
                     {'paragraph_index': 4, 'expected_text': 'Status: draft', 'old_text': 'draft', 'new_text': 'final'}):
            Command.model_validate({'session_id': 'a' * 32, 'request': {**REPLACE, **good}})
        for bad in ({'old_text': 'x'}, {'old_text': 'x', 'new_text': 'y', 'format_from': 'other'}, {'old_text': 'x', 'new_text': 'y', 'surprise': 1},
                    {'old_text': 'x', 'new_text': 'y', 'confirm_layout': False}, {'old_text': 'x', 'new_text': 'a\nb'}, {'old_text': 'x', 'new_text': 'y', 'occurrence': 1},
                    {'old_text': 'x', 'new_text': 'y', 'paragraph_index': 1}, {'old_text': 'x', 'new_text': 'y', 'paragraph_index': 1, 'expected_text': 'x', 'match': 'x'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': 'a' * 32, 'request': {**REPLACE, **bad}})


class UnknownOutcome(unittest.TestCase):
    """A real manager and owning thread: once InsertText may have run, the session is held and nothing else is tried."""

    def run_step(self, hwp: TextHwp, **fields):
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned, owned.service.command_bundle(steps=[{'label': 'x', **REPLACE, **fields}])

    def assert_held(self, owned, outcome: dict, *, deletes: int) -> None:
        self.assertFalse(outcome['ok'], outcome)
        step = outcome['steps'][0]
        self.assertTrue(outcome['native_custody_held'], outcome)
        self.assertTrue(step['mutation_may_have_persisted'], step)
        self.assertNotIn('refusal_code', step)
        self.assertTrue(owned.session._native_custody_held)
        self.assertEqual(len([call for call in owned.hwp.executes() if call[1] == 'InsertText']), 1, 'no second insertion')
        self.assertEqual([call for call in owned.hwp.calls if call[0] == 'Run' and call[1] != 'Delete'], [], 'no Undo or other recovery follows')
        self.assertEqual(owned.hwp.calls.count(('Run', 'Delete')), deletes)

    def test_insert_text_not_returning_true(self) -> None:
        hwp = plan_document()
        hwp.insert_result = False
        owned, outcome = self.run_step(hwp, old_text='budget', new_text='costs')
        self.assert_held(owned, outcome, deletes=0)

    def test_delete_not_returning_true(self) -> None:
        hwp = plan_document()
        hwp.delete_result = False
        owned, outcome = self.run_step(hwp, old_text='budget', new_text='costs')
        self.assert_held(owned, outcome, deletes=1)

    def test_new_text_with_another_format_is_not_a_success(self) -> None:
        hwp = plan_document()
        hwp.insert_shape = char(Italic=True)
        owned, outcome = self.run_step(hwp, old_text='budget', new_text='costs')
        self.assert_held(owned, outcome, deletes=1)

    def test_a_delete_taking_more_than_the_range_is_not_a_success(self) -> None:
        hwp = plan_document()
        hwp.delete_extra = 1
        owned, outcome = self.run_step(hwp, old_text='budget', new_text='costs')
        self.assert_held(owned, outcome, deletes=1)

    def test_a_change_elsewhere_is_not_a_success(self) -> None:
        hwp = plan_document()

        def damage(native: TextHwp) -> None:
            if native.atoms_of(0, 10)[0]['c'] == 'L':
                native.atoms_of(0, 10)[0]['c'] = 'l'
        hwp.after_execute = damage
        owned, outcome = self.run_step(hwp, old_text='budget', new_text='costs')
        self.assert_held(owned, outcome, deletes=1)


class UndoRedo(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = plan_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def test_one_replacement_is_two_native_steps_undone_and_redone_together(self) -> None:
        self.assertTrue(self.step(**REPLACE, match='Annual Plan 2025', old_text='2025', new_text='2026')['ok'])
        undone = self.history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 2)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(text_of(self.hwp, 0, 0), 'Annual Plan 2025')
        redone = self.history('redo')
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(text_of(self.hwp, 0, 0), 'Annual Plan 2026')

    def test_undo_keeps_an_earlier_edit(self) -> None:
        self.assertTrue(self.step(op='character_format_exact', expected_page=1, confirm_layout=True, match='schedule', italic=True)['ok'])
        formatted = self.hwp.text_props(0, 1)
        self.assertTrue(self.step(**REPLACE, old_text='budget', new_text='costs')['ok'])
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.hwp.text_props(0, 1), formatted)
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.hwp.text_props(0, 1), plan_document().text_props(0, 1))
        refusal = None
        try:
            self.history('undo')
        except Exception as exc:  # noqa: BLE001 - the service raises its refusal type
            refusal = exc
        self.assertEqual(getattr(refusal, 'code', None), 'no_undo_available')


if __name__ == '__main__':
    unittest.main()
