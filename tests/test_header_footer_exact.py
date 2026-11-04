"""Existing headers and footers: inventory, a binding by section and page scope, exact text and format edits, Undo and Redo.

Through the real bundle validation, package dispatch, service and census. The SDK is tests.native_header_footer_fakes,
built on measured native behavior. Source behavior only; the native runs are recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import native_header_footer as hf
from hwpx_mcp.schema import Command
from tests.native_format_fakes import char, paragraph
from tests.native_header_footer_fakes import HeaderFooterHwp, area, page_field, report_document, sections_document
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

TEXT = {'op': 'header_footer_text_exact', 'confirm_layout': True}
FORMAT = {'op': 'header_footer_format_exact', 'confirm_layout': True}


def inventory(hwp: HeaderFooterHwp) -> dict:
    outcome = Flow(hwp).step(op='header_footer_inventory')
    assert outcome['ok'], outcome
    return outcome['steps'][0]['result']


def bound(hwp: HeaderFooterHwp, area_id: str) -> dict:
    found = {item['area_id']: item for item in inventory(hwp)['areas']}[area_id]
    return {'kind': found['kind'], 'section': found['section'], 'apply_to': found['apply_to'], 'expected_area_hash': found['hash'], 'expected_affected_sections': found['shown_in_sections']}


def ok(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: HeaderFooterHwp, caret: tuple) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(hwp.executes(), [])
    test.assertNotIn(('Run', 'Delete'), hwp.calls)
    test.assertEqual(hwp.cursor, caret, 'a refusal gives the caret back')
    test.assertIsNone(hwp.selection)
    return step


class Inventory(unittest.TestCase):
    def test_lists_each_area_with_scope_text_fields_and_a_hash_without_moving_anything(self) -> None:
        hwp = report_document()
        hwp.cursor = (0, 1, 5)
        result = inventory(hwp)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['count'], 2)
        header, footer = result['areas']
        self.assertEqual({key: header[key] for key in ('area_id', 'kind', 'section', 'apply_to', 'text', 'shown_in_sections')},
                         {'area_id': 'header:s0:both', 'kind': 'header', 'section': 0, 'apply_to': 'both', 'text': 'SAMPLE REPORT', 'shown_in_sections': [0]})
        self.assertRegex(header['hash'], r'^sha256:[0-9a-f]{24}$')
        self.assertEqual(header['fields'], [])
        self.assertEqual(footer['area_id'], 'footer:s0:both')
        self.assertEqual(footer['text'], 'Sample document ')
        self.assertEqual(footer['fields'], [{'kind': 'page_number', 'paragraph_index': 0}])
        self.assertEqual(header['paragraphs'][0]['paragraph']['align'], 'justify')
        self.assertEqual(header['paragraphs'][0]['segments'][0]['text'], 'SAMPLE REPORT')
        self.assertEqual(hwp.executes(), [])
        self.assertEqual(hwp.cursor, (0, 1, 5))
        self.assertEqual(inventory(hwp)['areas'][0]['hash'], header['hash'], 'the same document gives the same hash')

    def test_sections_say_which_scopes_they_define_and_which_they_inherit(self) -> None:
        result = inventory(sections_document())
        self.assertEqual([item['area_id'] for item in result['areas']],
                         ['header:s0:both', 'footer:s0:both', 'header:s1:odd', 'header:s1:even', 'footer:s1:both'])
        sections = result['sections']
        self.assertEqual(len(sections), 3)
        self.assertEqual(sections[0]['header'], {'own': ['both'], 'inherited_from': None})
        self.assertEqual(sections[1]['header'], {'own': ['odd', 'even'], 'inherited_from': None})
        self.assertEqual(sections[2]['header'], {'own': [], 'inherited_from': 1})
        self.assertEqual(sections[2]['footer'], {'own': [], 'inherited_from': 1})
        odd = result['areas'][2]
        self.assertEqual(odd['shown_in_sections'], [1, 2])
        self.assertEqual(result['areas'][0]['shown_in_sections'], [0])

    def test_hashes_differ_between_areas_of_the_same_text(self) -> None:
        hwp = HeaderFooterHwp([paragraph('Body'), paragraph('Two')], areas=[area('header', paragraph('Same')), area('header', paragraph('Same'), apply='Odd', section=1)], sections=(1,))
        first, second = inventory(hwp)['areas']
        self.assertNotEqual(first['hash'], second['hash'])


class TextEdit(unittest.TestCase):
    def test_header_word_replaced_in_its_format_with_body_and_footer_untouched(self) -> None:
        hwp = report_document()
        hwp.cursor = (0, 1, 5)
        binding = bound(hwp, 'header:s0:both')
        body_before = hwp.text_props(0, 1)
        footer_before = hwp.area_props(1)
        result = ok(self, Flow(hwp).step(**TEXT, **binding, old_text='SAMPLE', new_text='UPDATED'))
        self.assertEqual(hwp.area_text(0), ['UPDATED REPORT'])
        self.assertEqual(hwp.text_props(0, 1), body_before, 'the same words in the body stay')
        self.assertEqual(hwp.area_props(1), footer_before)
        self.assertEqual(hwp.area_controls(1), ['atno'])
        self.assertEqual(result['area']['area_id'], 'header:s0:both')
        self.assertEqual(result['area']['hash_before'], binding['expected_area_hash'])
        self.assertNotEqual(result['area']['hash_after'], binding['expected_area_hash'])
        self.assertEqual(result['area']['hash_after'], inventory(hwp)['areas'][0]['hash'], 'the next binding')
        self.assertEqual(result['result']['paragraph_before'], 'SAMPLE REPORT')
        self.assertEqual(result['result']['paragraph_after'], 'UPDATED REPORT')
        self.assertEqual(result['result']['native_actions'], ['Execute:InsertText', 'Run:Delete'])
        self.assertEqual(result['undo']['native_editing_actions'], 2)
        self.assertRegex(result['undo']['pre_state_sha256'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(result['undo']['pre_state_sha256'], result['undo']['post_state_sha256'])
        inserts = [call for call in hwp.executes() if call[1] == 'InsertText']
        self.assertEqual([call[2] for call in inserts], [{'Text': 'UPDATED'}])
        self.assertIsNone(inserts[0][3], 'the new text goes in at a bare caret')
        self.assertEqual(hwp.cursor, (0, 1, 5), 'the caret is given back where the caller had it')
        self.assertIsNone(hwp.selection)
        self.assertEqual(result['caret'], [0, 1, 5])

    def test_footer_text_before_the_page_number_keeps_the_field(self) -> None:
        hwp = report_document()
        result = ok(self, Flow(hwp).step(**TEXT, **bound(hwp, 'footer:s0:both'), old_text='Sample document', new_text='Updated footer'))
        self.assertEqual(hwp.area_text(1), ['Updated footer '])
        self.assertEqual(hwp.area_controls(1), ['atno'])
        self.assertEqual(hwp.area_text(0), ['SAMPLE REPORT'])
        self.assertEqual(result['result']['fields_kept'], [{'kind': 'page_number', 'paragraph_index': 0}])

    def test_the_new_text_keeps_the_format_of_the_replaced_text(self) -> None:
        bold = char(Bold=True, TextColor=192)
        hwp = HeaderFooterHwp([paragraph('Body')], areas=[area('header', paragraph('Plan ', ('2025', bold), ' draft'))])
        ok(self, Flow(hwp).step(**TEXT, **bound(hwp, 'header:s0:both'), old_text='2025', new_text='2026'))
        self.assertEqual(hwp.area_props(0)[5:9], [(c, bold) for c in '2026'])
        self.assertEqual(hwp.area_props(0)[4], (' ', char()))
        self.assertEqual(hwp.area_props(0)[9], (' ', char()))

    def test_odd_header_with_explicit_shared_section_scope(self) -> None:
        hwp = sections_document()
        result = ok(self, Flow(hwp).step(**TEXT, **bound(hwp, 'header:s1:odd'), old_text='2025', new_text='2026'))
        self.assertEqual([hwp.area_text(number) for number in range(5)],
                         [['SAMPLE REPORT'], ['Sample document '], ['ODD HEADER 2026'], ['EVEN HEADER 2025'], ['Section two footer ']])
        self.assertEqual(''.join(c for c, _props in hwp.text_props(0, 0)), 'Section one body 2025')
        self.assertEqual(''.join(c for c, _props in hwp.text_props(0, 3)), 'Section three body inherits 2025')
        self.assertEqual(result['area']['shown_in_sections'], [1, 2])
        self.assertEqual(result['area']['area_id'], 'header:s1:odd')

    def test_occurrence_and_paragraph_inside_the_area(self) -> None:
        hwp = HeaderFooterHwp([paragraph('Body')], areas=[area('footer', paragraph('Draft A'), paragraph('Draft B Draft'))])
        binding = bound(hwp, 'footer:s0:both')
        step = Flow(hwp).step(**TEXT, **binding, old_text='Draft', new_text='Final')
        self.assertEqual(step['steps'][0]['refusal_code'], 'target_ambiguous')
        ok(self, Flow(hwp).step(**TEXT, **binding, old_text='Draft', new_text='Final', occurrence=3, expected_occurrences=3))
        self.assertEqual(hwp.area_text(0), ['Draft A', 'Draft B Final'])
        binding = bound(hwp, 'footer:s0:both')
        ok(self, Flow(hwp).step(**TEXT, **binding, paragraph_index=1, expected_text='Draft B Final', old_text='Draft', new_text='Last'))
        self.assertEqual(hwp.area_text(0), ['Draft A', 'Last B Final'])


class Refusals(unittest.TestCase):
    def check(self, hwp: HeaderFooterHwp, fields: dict, code: str, op: dict = TEXT) -> dict:
        hwp.cursor = (0, 0, 3)
        calls = len(hwp.calls)
        before = hwp.get_text_file('HWPML2X', '')
        step = refused(self, Flow(hwp).step(**op, **fields), code, hwp, (0, 0, 3))
        self.assertEqual(hwp.get_text_file('HWPML2X', ''), before)
        self.assertGreaterEqual(len(hwp.calls), calls)
        return step

    def test_a_section_without_its_own_header_names_where_its_header_comes_from(self) -> None:
        hwp = sections_document()
        step = self.check(hwp, {'kind': 'header', 'section': 2, 'apply_to': 'odd', 'expected_area_hash': 'sha256:' + 'a' * 24, 'old_text': '2025', 'new_text': '2026'},
                          'area_inherited')
        self.assertIn('section 1', step['refusal_message'])

    def test_a_scope_the_section_does_not_define(self) -> None:
        hwp = sections_document()
        step = self.check(hwp, {'kind': 'header', 'section': 1, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'old_text': '2025', 'new_text': '2026'},
                          'area_not_found')
        self.assertIn('odd', step['refusal_message'])
        self.assertIn('even', step['refusal_message'])
        self.check(sections_document(), {'kind': 'footer', 'section': 7, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'old_text': 'x', 'new_text': 'y'},
                   'area_not_found')
        none = HeaderFooterHwp([paragraph('Body')], areas=[])
        self.check(none, {'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'old_text': 'x', 'new_text': 'y'}, 'area_not_found')

    def test_two_areas_of_one_scope_are_refused_rather_than_guessed(self) -> None:
        hwp = HeaderFooterHwp([paragraph('Body')], areas=[area('header', paragraph('One')), area('header', paragraph('Two'))])
        self.check(hwp, {'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'old_text': 'One', 'new_text': 'Uno'}, 'area_ambiguous')

    def test_a_stale_hash(self) -> None:
        hwp = report_document()
        binding = bound(hwp, 'header:s0:both')
        self.check(hwp, {**binding, 'expected_area_hash': 'sha256:' + '0' * 24, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'}, 'area_hash_mismatch')

    def test_text_only_in_the_body_or_another_area(self) -> None:
        hwp = report_document()
        self.check(hwp, {**bound(hwp, 'header:s0:both'), 'old_text': 'annual plan', 'new_text': 'yearly plan'}, 'target_not_found')
        self.check(hwp, {**bound(hwp, 'footer:s0:both'), 'old_text': 'SAMPLE', 'new_text': 'UPDATED'}, 'target_not_found')

    def test_a_range_over_the_page_number_field(self) -> None:
        hwp = HeaderFooterHwp([paragraph('Body')], areas=[area('footer', paragraph('Page ', page_field(), ' end'))])
        self.check(hwp, {**bound(hwp, 'footer:s0:both'), 'paragraph_index': 0, 'expected_text': 'Page  end', 'old_text': 'Page  end', 'new_text': 'x'}, 'target_crosses_control')

    def test_an_area_the_native_lists_do_not_prove(self) -> None:
        hwp = report_document()
        binding = bound(hwp, 'header:s0:both')
        hwp.holders[2] = 'tbl'
        self.check(hwp, {**binding, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'}, 'area_unlocatable')
        hwp = report_document()
        binding = bound(hwp, 'header:s0:both')
        hwp.lists[2][0]['atoms'][0]['c'] = 'X'  # The native list holds other words than the document text says.
        hwp.get_text_file = lambda fmt, option='', real=HeaderFooterHwp.get_text_file, native=hwp: real(native, fmt, option).replace('XAMPLE', 'SAMPLE') if 'saveblock' not in option else real(native, fmt, option)
        self.check(hwp, {**binding, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'}, 'area_unlocatable')

    def test_format_refusals(self) -> None:
        hwp = sections_document()
        self.check(hwp, {'kind': 'footer', 'section': 2, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'match': 'Section', 'bold': True}, 'area_inherited', FORMAT)
        hwp = report_document()
        self.check(hwp, {**bound(hwp, 'header:s0:both'), 'match': 'annual', 'bold': True}, 'target_not_found', FORMAT)


class Held(unittest.TestCase):
    def test_a_change_outside_the_area_is_an_unknown_outcome(self) -> None:
        hwp = report_document()
        binding = bound(hwp, 'header:s0:both')

        def damage(native: HeaderFooterHwp) -> None:
            native.lists[0][2]['atoms'][0]['c'] = 'l'
        hwp.after_execute = damage
        outcome = Flow(hwp).step(**TEXT, **binding, old_text='SAMPLE', new_text='UPDATED')
        self.assertFalse(outcome['ok'])
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'])

    def test_a_change_to_another_area_is_an_unknown_outcome(self) -> None:
        hwp = report_document()
        binding = bound(hwp, 'header:s0:both')

        def damage(native: HeaderFooterHwp) -> None:
            native.lists[3][0]['atoms'][0]['c'] = 's'
        hwp.after_execute = damage
        outcome = Flow(hwp).step(**TEXT, **binding, old_text='SAMPLE', new_text='UPDATED')
        self.assertFalse(outcome['ok'])
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'])


class Format(unittest.TestCase):
    def test_character_and_paragraph_format_of_a_header_word(self) -> None:
        hwp = report_document()
        hwp.cursor = (0, 2, 4)
        result = ok(self, Flow(hwp).step(**FORMAT, **bound(hwp, 'header:s0:both'), match='REPORT', bold=True, text_color='#C00000', align='center'))
        props = hwp.area_props(0)
        self.assertEqual([p['Bold'] for _c, p in props], [False] * 7 + [True] * 6)
        self.assertEqual({p['TextColor'] for _c, p in props[7:]}, {0x0000C0})
        self.assertEqual(hwp.lists[2][0]['shape']['Align'], 'Center')
        self.assertEqual([p['Bold'] for _c, p in hwp.text_props(0, 1)], [False] * len(hwp.text_props(0, 1)), 'the body mention stays')
        self.assertEqual(result['result']['native_actions'], ['Execute:CharShape', 'Execute:ParagraphShape'])
        self.assertEqual(result['undo']['native_editing_actions'], 2)
        self.assertEqual(result['result']['after']['paragraph']['align'], 'center')
        self.assertEqual(result['result']['after']['segments'][-1]['bold'], True)
        self.assertEqual(hwp.cursor, (0, 2, 4))
        self.assertEqual(result['area']['hash_after'], inventory(hwp)['areas'][0]['hash'])

    def test_whole_paragraph_by_index_and_paragraph_format_only(self) -> None:
        hwp = sections_document()
        result = ok(self, Flow(hwp).step(**FORMAT, **bound(hwp, 'header:s1:even'), paragraph_index=0, expected_text='EVEN HEADER 2025', align='right'))
        self.assertEqual(hwp.lists[5][0]['shape']['Align'], 'Right')
        self.assertEqual(hwp.lists[4][0]['shape']['Align'], 'Justify')
        self.assertEqual(result['result']['native_actions'], ['Execute:ParagraphShape'])
        self.assertEqual(result['undo']['native_editing_actions'], 1)

    def test_a_value_already_present_is_a_proven_no_op(self) -> None:
        hwp = report_document()
        result = ok(self, Flow(hwp).step(**FORMAT, **bound(hwp, 'header:s0:both'), match='SAMPLE', bold=False, align='justify'))
        self.assertEqual(result['result']['status'], 'already_set')
        self.assertFalse(result['result']['changed'])
        self.assertEqual(hwp.executes(), [])


class UndoRedo(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = sections_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def binding(self, area_id: str) -> dict:
        listed = self.step(op='header_footer_inventory')['steps'][0]['result']['areas']
        found = {item['area_id']: item for item in listed}[area_id]
        return {'kind': found['kind'], 'section': found['section'], 'apply_to': found['apply_to'], 'expected_area_hash': found['hash'], 'expected_affected_sections': found['shown_in_sections']}

    def test_a_header_replacement_is_one_logical_undo_of_two_native_steps(self) -> None:
        self.assertTrue(self.step(**TEXT, **self.binding('header:s1:odd'), old_text='2025', new_text='2026')['ok'])
        undone = self.history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 2)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(self.hwp.area_text(2), ['ODD HEADER 2025'])
        redone = self.history('redo')
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(self.hwp.area_text(2), ['ODD HEADER 2026'])

    def test_a_format_then_a_text_edit_undo_one_at_a_time(self) -> None:
        self.assertTrue(self.step(**FORMAT, **self.binding('header:s1:even'), match='EVEN', italic=True)['ok'])
        formatted = self.hwp.area_props(3)
        self.assertTrue(self.step(**TEXT, **self.binding('footer:s1:both'), old_text='two', new_text='2')['ok'])
        self.assertEqual(self.hwp.area_text(4), ['Section 2 footer '])
        undone = self.history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(self.hwp.area_text(4), ['Section two footer '])
        self.assertEqual(self.hwp.area_props(3), formatted)
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.hwp.area_props(3), sections_document().area_props(3))


class Requests(unittest.TestCase):
    GOOD = {'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'a' * 24, 'confirm_layout': True}

    def test_text_request_validation(self) -> None:
        good = {**self.GOOD, 'old_text': 'A', 'new_text': 'B'}
        self.assertEqual(hf.validate_text_step(good)['apply_to'], 'both')
        for bad in ({**good, 'kind': 'body'}, {**good, 'apply_to': 'first'}, {**good, 'section': -1}, {**good, 'section': True},
                    {**good, 'expected_area_hash': 'abc'}, {key: value for key, value in good.items() if key != 'expected_area_hash'},
                    {**good, 'confirm_layout': False}, {**good, 'new_text': 'A'}, {**good, 'expected_page': 1}, {**good, 'extra': 1},
                    {**good, 'new_text': 'a\nb'}):
            with self.subTest(bad=bad), self.assertRaises(hf.NativeNumberingInputError):
                hf.validate_text_step(bad)

    def test_format_request_validation(self) -> None:
        good = {**self.GOOD, 'match': 'A', 'bold': True}
        self.assertEqual(hf.validate_format_step(good)['character'], {'bold': True})
        self.assertEqual(hf.validate_format_step({**self.GOOD, 'paragraph_index': 0, 'expected_text': 'A', 'align': 'center'})['paragraph'], {'align': 'center'})
        for bad in ({**self.GOOD, 'match': 'A'}, {**good, 'through_match': 'B'}, {**good, 'expected_page': 1}, {**good, 'number_mark': 'follow'},
                    {**good, 'size_pt': 0.05}, {**self.GOOD, 'bold': True}, {**good, 'paragraph_index': 0}):
            with self.subTest(bad=bad), self.assertRaises(hf.NativeNumberingInputError):
                hf.validate_format_step(bad)

    def test_mcp_schema_takes_the_three_commands(self) -> None:
        def command(request: dict) -> None:
            Command.model_validate({'session_id': 'a' * 32, 'request': request})
        command({'op': 'header_footer_inventory'})
        command({'op': 'header_footer_text_exact', **self.GOOD, 'old_text': 'A', 'new_text': 'B'})
        command({'op': 'header_footer_format_exact', **self.GOOD, 'match': 'A', 'bold': True, 'align': 'center'})
        command({'op': 'header_footer_format_exact', **self.GOOD, 'paragraph_index': 0, 'expected_text': 'A', 'line_spacing_type': 'percent', 'line_spacing_value': 150})
        for bad in ({'op': 'header_footer_text_exact', **self.GOOD, 'old_text': 'A', 'new_text': 'B', 'apply_to': 'first'},
                    {'op': 'header_footer_format_exact', **self.GOOD, 'match': 'A'},
                    {'op': 'header_footer_text_exact', **self.GOOD, 'old_text': 'A', 'new_text': 'B', 'expected_page': 1},
                    {'op': 'header_footer_format_exact', **self.GOOD, 'match': 'A', 'size_pt': 10.05},
                    {'op': 'header_footer_format_exact', **self.GOOD, 'match': 'A', 'line_spacing_type': 'percent'},
                    {'op': 'header_footer_format_exact', **self.GOOD, 'match': 'A', 'paragraph_index': 0, 'expected_text': 'A', 'bold': True},
                    {'op': 'header_footer_text_exact', **self.GOOD, 'expected_area_hash': 'sha256:' + 'A' * 24, 'old_text': 'A', 'new_text': 'B'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                command(bad)


if __name__ == '__main__':
    unittest.main()
