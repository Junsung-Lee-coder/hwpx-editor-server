"""Number marks, top underline, direct-caller input, the section-control range and the full-shape comparison.

Through the real bundle validation, service, census and a real manager where a hold or Undo is involved, over the fake built on
measured native behavior. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import native_format
from hwpx_mcp.schema import Command
from tests.native_format_fakes import char, end_shape, numbered_document, paragraph, shape, FormatHwp
from tests.test_native_format import CHARACTER, PARAGRAPH, character, para_format, refusal, result_of, styled
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

RED = {'bold': True, 'text_color': '#C00000'}
RED_CHAR = char(Bold=True, TextColor=0x0000C0)


def actions(hwp: FormatHwp) -> list[tuple]:
    return [call[1:] for call in hwp.executes()]


class NumberMarkDefault(unittest.TestCase):
    def test_a_range_ending_before_the_paragraph_end_is_one_action_and_leaves_the_mark(self) -> None:
        hwp = numbered_document()
        value = result_of(self, character(hwp, match='번호 앞글', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(end_shape(hwp.lists[0][1]), char())
        self.assertEqual(value['result']['number_marks'][0]['changed'], False)

    def test_a_range_reaching_the_end_keeps_the_mark_with_a_second_action_at_the_paragraph_end(self) -> None:
        for name, para in (('번호', 1), ('개요', 2), ('글머', 3)):
            with self.subTest(kind=name):
                hwp = numbered_document()
                outcome = Flow(hwp).step(**CHARACTER, match=name + ' 앞글 꼬리글', **RED)
                value = result_of(self, outcome)
                self.assertEqual(styled(hwp, 0, para), [(name + ' 앞글 꼬리글', RED_CHAR)])
                self.assertEqual(end_shape(hwp.lists[0][para]), char(), 'the printed mark keeps its old shape')
                first, second = hwp.executes()
                self.assertEqual(first[2], {'Bold': 1, 'TextColor': 0x0000C0})
                self.assertIsNotNone(first[3])
                self.assertEqual(second[2], {'Bold': 0, 'TextColor': 0}, 'only what the first action changed, back to the old values')
                self.assertIsNone(second[3], 'a bare caret, so no text can be touched')
                self.assertEqual(value['undo']['native_editing_actions'], 2)
                self.assertEqual(value['result']['native_actions'], ['Execute:CharShape', 'Execute:CharShape:paragraph-end'])
                mark = value['result']['number_marks'][0]
                self.assertEqual((mark['paragraph_index'], mark['source'], mark['changed'], mark['kept_by']), (para, 'paragraph_end', False, 'paragraph_end_action'))
                self.assertEqual(mark['before'], mark['after'])

    def test_a_plain_paragraph_needs_no_second_action(self) -> None:
        hwp = numbered_document()
        value = result_of(self, character(hwp, match='일반 앞글 꼬리글', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(value['result']['number_marks'], [])

    def test_a_definition_naming_its_own_mark_shape_is_not_affected(self) -> None:
        hwp = numbered_document()
        hwp.explicit_number_mark = True
        value = result_of(self, character(hwp, match='번호 앞글 꼬리글', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(value['result']['number_marks'][0]['source'], 'definition')
        self.assertIs(value['result']['number_marks'][0]['changed'], False)

    def test_a_renumbered_shape_named_by_a_definition_is_not_a_changed_definition(self) -> None:
        for match in ('번호 앞글', '번호 앞글 꼬리글'):
            with self.subTest(match=match):
                hwp = numbered_document()
                hwp.explicit_number_mark = char(Height=1200, TextColor=0xC00000)
                value = result_of(self, character(hwp, match=match, **RED))
                self.assertEqual(len(hwp.executes()), 1)
                mark = value['result']['number_marks'][0]
                self.assertEqual((mark['source'], mark['changed'], mark['after']['size_pt'], mark['after']['text_color']), ('definition', False, 12.0, '#0000C0'))

    def test_a_definition_that_really_names_another_shape_is_still_a_change(self) -> None:
        from app.native_numbering_census import parse_hwpml2x_census
        hwp = numbered_document()
        hwp.explicit_number_mark = char(Height=1200)
        first = parse_hwpml2x_census(hwp.get_text_file('HWPML2X', ''))
        hwp.explicit_number_mark = char(Height=1300)
        second = parse_hwpml2x_census(hwp.get_text_file('HWPML2X', ''))
        self.assertNotEqual(first['numberings_sha256'], second['numberings_sha256'])

    def test_values_the_mark_already_has_need_no_restoring(self) -> None:
        hwp = numbered_document()
        result_of(self, Flow(hwp).step(**CHARACTER, match='굵끝 앞글 꼬리글', bold=True))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(end_shape(hwp.lists[0][8]), char(Bold=True))

    def test_every_property_is_restored_exactly(self) -> None:
        hwp = numbered_document()
        with patch('app.native_format.installed_font_families', return_value={'굴림'}):
            result_of(self, Flow(hwp).step(**CHARACTER, match='위쪽 앞글 꼬리글', font_family='굴림', size_pt=13.5, bold=True, italic=True, underline=True, text_color='#0055AA'))
        items = hwp.executes()[1][2]
        self.assertEqual({key: items[key] for key in ('Height', 'Bold', 'Italic', 'UnderlineType', 'TextColor')}, {'Height': 1000, 'Bold': 0, 'Italic': 0, 'UnderlineType': 3, 'TextColor': 0})
        self.assertEqual(items['FaceNameLatin'], '함초롬바탕')
        self.assertEqual(end_shape(hwp.lists[0][5]), char(Underline='Top'))

    def test_formatting_the_tail_again_restores_again(self) -> None:
        hwp = numbered_document()
        flow = Flow(hwp)
        self.assertTrue(flow.step(**CHARACTER, match='번호 앞글 꼬리글', text_color='#C00000')['ok'])
        self.assertTrue(flow.step(**CHARACTER, match='번호 앞글 꼬리글', italic=True)['ok'])
        self.assertEqual(end_shape(hwp.lists[0][1]), char())
        self.assertEqual(styled(hwp, 0, 1), [('번호 앞글 꼬리글', char(TextColor=0x0000C0, Italic=True))])

    def test_a_table_cell_paragraph_restores_in_its_own_list(self) -> None:
        hwp = numbered_document()
        result_of(self, character(hwp, match='셀번 앞글 꼬리글', **RED))
        list_id = hwp.executes()[0][3][0]
        self.assertNotEqual(list_id, 0)
        self.assertEqual(end_shape(hwp.lists[list_id][0]), char())
        self.assertEqual(hwp.cursor, (0, 0, 32))

    def test_text_before_an_object_at_the_paragraph_end_does_not_reach_it(self) -> None:
        hwp = numbered_document()
        result_of(self, character(hwp, match='그림 앞글', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(hwp.atoms_of(0, 9)[-1]['cs'], char())

    def test_a_range_across_numbered_paragraphs_is_refused_before_any_edit(self) -> None:
        hwp = numbered_document()
        state = (hwp.cursor, hwp.selection)
        refusal(self, Flow(hwp).step(**CHARACTER, match='걸침 위글', through_match='걸침 아래글', **RED), 'target_changes_number_mark', hwp, state)

    def test_a_refusal_keeps_earlier_edits_and_the_session_usable(self) -> None:
        hwp = numbered_document()
        flow = Flow(hwp)
        self.assertTrue(flow.step(**CHARACTER, match='일반 앞글', bold=True)['ok'])
        outcome = flow.step(**CHARACTER, match='걸침 위글', through_match='걸침 아래글', **RED)
        self.assertEqual(outcome['steps'][0]['refusal_code'], 'target_changes_number_mark')
        self.assertEqual(styled(hwp, 0, 4)[0], ('일반 앞글', char(Bold=True)))
        self.assertTrue(flow.step(**CHARACTER, match='걸침 위글', **RED)['ok'])

    def test_a_built_in_font_on_the_mark_cannot_be_restored_so_it_is_refused(self) -> None:
        hwp = numbered_document()
        real = hwp.get_text_file
        hwp.get_text_file = lambda fmt='UNICODE', option='': real(fmt, option).replace('Type="ttf"', 'Type="hft"')
        with patch('app.native_format.installed_font_families', return_value={'굴림'}):
            refusal(self, Flow(hwp).step(**CHARACTER, match='번호 앞글 꼬리글', font_family='굴림'), 'target_changes_number_mark', hwp)


class NumberMarkFollow(unittest.TestCase):
    def test_the_explicit_scope_lets_the_mark_follow_in_one_action_and_says_so(self) -> None:
        hwp = numbered_document()
        value = result_of(self, Flow(hwp).step(**CHARACTER, match='번호 앞글 꼬리글', number_mark='follow', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(end_shape(hwp.lists[0][1]), RED_CHAR)
        mark = value['result']['number_marks'][0]
        self.assertIs(mark['changed'], True)
        self.assertEqual((mark['before']['bold'], mark['after']['bold'], mark['after']['text_color']), (False, True, '#C00000'))
        self.assertEqual(value['undo']['native_editing_actions'], 1)

    def test_the_explicit_scope_allows_a_range_across_numbered_paragraphs(self) -> None:
        hwp = numbered_document()
        value = result_of(self, Flow(hwp).step(**CHARACTER, match='걸침 위글', through_match='걸침 아래글', number_mark='follow', **RED))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(end_shape(hwp.lists[0][6]), RED_CHAR)
        self.assertEqual(end_shape(hwp.lists[0][7]), char())
        self.assertEqual([(m['paragraph_index'], m['changed']) for m in value['result']['number_marks']], [(6, True), (7, False)])

    def test_it_is_offered_only_for_character_formatting_and_only_with_known_values(self) -> None:
        base = {'match': 'x', 'expected_page': 1, 'confirm_layout': True}
        Command.model_validate({'session_id': 'a' * 32, 'request': {'op': 'character_format_exact', **base, 'bold': True, 'number_mark': 'follow'}})
        for request in ({'op': 'character_format_exact', **base, 'bold': True, 'number_mark': 'independent'}, {'op': 'paragraph_format_exact', **base, 'align': 'left', 'number_mark': 'follow'}):
            with self.subTest(request=request), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': 'a' * 32, 'request': request})
        for bad in ('independent', ['follow'], 1):
            with self.subTest(bad=bad), self.assertRaises(native_format.NativeNumberingInputError):
                native_format.validate_character_step({**base, 'bold': True, 'number_mark': bad})


class NumberMarkUnknown(unittest.TestCase):
    def run_step(self, owned, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def assert_held(self, owned, outcome, *, calls):
        self.assertFalse(outcome['ok'], outcome)
        self.assertTrue(outcome['native_custody_held'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertEqual([c for c in owned.hwp.calls if c[0] == 'Run'], [])
        self.assertEqual(len(owned.hwp.executes()), calls)

    def test_a_failed_second_action_holds_with_no_further_call(self) -> None:
        hwp = numbered_document()
        real = hwp.execute

        def second_fails(name, items):
            if hwp.selection is None:
                hwp.calls.append(('Execute', name, dict(items), None))
                return False
            return real(name, items)

        hwp.execute = second_fails
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='번호 앞글 꼬리글', **RED), calls=2)

    def test_a_mark_not_restored_is_not_a_success(self) -> None:
        hwp = numbered_document()
        real = hwp.execute

        def ignored(name, items):
            if hwp.selection is None:
                hwp.calls.append(('Execute', name, dict(items), None))
                return True
            return real(name, items)

        hwp.execute = ignored
        hwp.lists[0][1]['end'] = char(Italic=True)
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='번호 앞글 꼬리글', **RED), calls=2)

    def test_a_mark_changed_by_a_range_ending_earlier_is_not_a_success(self) -> None:
        def corrupt(doc):
            doc.lists[0][1]['end'] = char(Italic=True)
        hwp = numbered_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='번호 앞글', **RED), calls=1)

    def test_paragraph_formatting_leaves_every_mark(self) -> None:
        def corrupt(doc):
            doc.lists[0][2]['end'] = char(Italic=True)
        hwp = numbered_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **PARAGRAPH, match='번호 앞글', align='center'), calls=1)


class NumberMarkHistory(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = numbered_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def test_one_mcp_undo_and_redo_cover_both_native_actions(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='일반 앞글', italic=True)['ok'])
        self.assertTrue(self.step(**CHARACTER, match='번호 앞글 꼬리글', **RED)['ok'])
        undone = self.history('undo')
        self.assertEqual(undone['native_undo_steps'], 2)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(styled(self.hwp, 0, 1), [('번호 앞글 꼬리글', char())])
        self.assertEqual(end_shape(self.hwp.lists[0][1]), char())
        self.assertEqual(styled(self.hwp, 0, 4)[0], ('일반 앞글', char(Italic=True)))
        self.assertEqual(self.history('redo')['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(styled(self.hwp, 0, 1), [('번호 앞글 꼬리글', RED_CHAR)])
        self.assertEqual(end_shape(self.hwp.lists[0][1]), char())

    def test_undo_is_compared_with_the_mark_too(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='번호 앞글 꼬리글', number_mark='follow', **RED)['ok'])
        real = self.hwp.history_step

        def text_back_mark_not(*args, **kwargs):
            result = real(*args, **kwargs)
            self.hwp.lists[0][1]['end'] = RED_CHAR
            return result

        self.hwp.history_step = text_back_mark_not
        outcome = self.history('undo')
        self.assertIs(outcome['ok'], False)
        self.assertEqual(outcome['confirmation']['expected_state'], {'compared': True, 'matches': False})

    def test_the_reply_says_when_a_history_step_moved_a_mark(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='번호 앞글 꼬리글', number_mark='follow', **RED)['ok'])
        undone = self.history('undo')
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual({item['kind'] for item in undone['confirmation']['changes']}, {'format_changed', 'number_mark_changed'})


class Readback(unittest.TestCase):
    def test_a_numbered_paragraph_reports_its_mark_and_objects(self) -> None:
        hwp = numbered_document()
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            rows = owned.service.numbering_readback()['paragraphs']
        by = {row['text'][:2]: row for row in rows}
        self.assertEqual({key: by['번호']['format']['mark'][key] for key in ('kind', 'source', 'bold', 'size_pt')}, {'kind': 'number', 'source': 'paragraph_end', 'bold': False, 'size_pt': 10.0})
        self.assertEqual(by['개요']['format']['mark']['kind'], 'outline')
        self.assertEqual(by['글머']['format']['mark']['kind'], 'bullet')
        self.assertTrue(by['굵끝']['format']['mark']['bold'])
        self.assertIsNone(by['일반']['format'].get('mark'))
        self.assertEqual(by['그림']['format']['objects'], ['picture'])
        self.assertEqual(rows[0]['format']['objects'], [], 'section controls are not objects a reader sees')

    def test_an_unreadable_definition_is_not_called_inherited(self) -> None:
        hwp = numbered_document()
        real = hwp.get_text_file
        hwp.get_text_file = lambda fmt='UNICODE', option='': real(fmt, option).replace('Heading="1"', 'Heading="77"')
        rows = native_format.parse_format_model(hwp.get_text_file('HWPML2X', ''))['paragraphs']
        self.assertEqual(rows[1]['mark']['source'], 'unknown')


class TopUnderline(unittest.TestCase):
    def test_a_top_underline_is_not_already_a_bottom_one(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('위밑줄 ', ('표적', char(Underline='Top')), ' 끝')])
        value = result_of(self, character(hwp, match='표적', underline=True))
        self.assertIs(value['result']['changed'], True)
        self.assertEqual(hwp.executes()[0][2], {'UnderlineType': 1})
        self.assertEqual(styled(hwp, 0, 1)[1], ('표적', char(Underline='Bottom')))
        self.assertEqual(value['result']['before'][0]['segments'][0]['underline_type'], 'top')
        self.assertNotIn('underline_type', value['result']['after'][0]['segments'][0])

    def test_a_bottom_underline_is_already_set(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('밑줄 ', ('표적', char(Underline='Bottom')), ' 끝')])
        self.assertIs(result_of(self, character(hwp, match='표적', underline=True))['result']['changed'], False)
        self.assertEqual(hwp.executes(), [])

    def test_turning_off_takes_off_any_underline(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('위밑줄 ', ('표적', char(Underline='Top')), ' 끝')])
        result_of(self, character(hwp, match='표적', underline=False))
        self.assertEqual(styled(hwp, 0, 1)[0], ('위밑줄 표적 끝', char()))


class DirectBundleInput(unittest.TestCase):
    base = {'match': 'x', 'expected_page': 1, 'confirm_layout': True}

    def test_unhashable_and_wrong_type_enums_are_input_errors(self) -> None:
        for fields in ({'align': ['left']}, {'align': {'a': 1}}, {'align': 1}, {'line_spacing_type': ['percent'], 'line_spacing_value': 100}, {'line_spacing_type': {}, 'line_spacing_value': 100}):
            with self.subTest(fields=fields), self.assertRaises(native_format.NativeNumberingInputError):
                native_format.validate_paragraph_step({**self.base, **fields})

    def test_integers_too_large_for_a_float_are_input_errors(self) -> None:
        huge = 10 ** 400
        for fields in ({'left_indent_pt': huge}, {'first_line_indent_pt': -huge}, {'line_spacing_type': 'percent', 'line_spacing_value': huge}, {'line_spacing_type': 'fixed', 'line_spacing_value': huge}):
            with self.subTest(fields=str(fields)[:40]), self.assertRaises(native_format.NativeNumberingInputError):
                native_format.validate_paragraph_step({**self.base, **fields})
        with self.assertRaises(native_format.NativeNumberingInputError):
            native_format.validate_character_step({**self.base, 'size_pt': huge})

    def test_they_reach_a_direct_caller_as_a_clear_validation_error(self) -> None:
        hwp = numbered_document()
        for fields, words in (({'align': ['left']}, 'align'), ({'left_indent_pt': 10 ** 400}, 'left_indent_pt')):
            with self.subTest(fields=str(fields)[:30]), self.assertRaises(Exception) as caught:
                Flow(hwp).step(**PARAGRAPH, match='번호 앞글', **fields)
            self.assertIn(words, str(caught.exception))
            self.assertNotIsInstance(caught.exception, (TypeError, OverflowError))
        self.assertEqual(hwp.executes(), [])


class TitleAndBodyRange(unittest.TestCase):
    def test_a_range_from_the_first_paragraph_starts_after_its_section_controls(self) -> None:
        hwp = FormatHwp([paragraph('제목 문단'), paragraph('본문 문단'), paragraph('끝 문단')])
        value = result_of(self, para_format(hwp, match='제목 문단', through_match='본문 문단', align='center'))
        self.assertEqual(hwp.executes()[0][3], (0, 0, 32, 0, 1, 5))
        self.assertEqual([p['shape']['Align'] for p in hwp.lists[0]], ['Center', 'Center', 'Justify'])
        self.assertEqual(len(value['result']['after']), 2)

    def test_a_visible_object_in_the_first_paragraph_is_still_refused(self) -> None:
        from tests.native_format_fakes import picture
        hwp = FormatHwp([paragraph(picture(), '제목 문단'), paragraph('본문 문단')])
        refusal(self, para_format(hwp, match='제목 문단', through_match='본문 문단', align='center'), 'target_has_controls', hwp)

    def test_a_table_later_in_the_range_is_still_refused(self) -> None:
        from tests.native_format_fakes import table
        hwp = FormatHwp([paragraph('제목 문단'), paragraph('글', table(paragraph('가'), paragraph('나'), paragraph('다'), paragraph('라'))), paragraph('본문 문단')])
        refusal(self, para_format(hwp, match='제목 문단', through_match='본문 문단', align='center'), 'target_has_controls', hwp)


class FullShapeFacts(unittest.TestCase):
    def rows(self, hwp):
        text = hwp.get_text_file('HWPML2X', '')
        return [native_format.readback_format(row) for row in native_format.parse_format_model(text)['paragraphs']]

    def test_a_change_outside_the_named_values_moves_the_remainder(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph(('본문', char(Ratio=100)))])
        same = self.rows(hwp)[1]
        hwp.lists[0][1]['atoms'][0]['cs'] = char(Ratio=90)
        hwp.lists[0][1]['atoms'][1]['cs'] = char(Ratio=90)
        changed = self.rows(hwp)[1]
        self.assertNotEqual(same['runs'][0]['rest'], changed['runs'][0]['rest'])

    def test_named_values_do_not_move_the_remainder(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('본문')])
        same = self.rows(hwp)[1]
        for atom in hwp.lists[0][1]['atoms']:
            atom['cs'] = char(Bold=True, Italic=True, Underline='Bottom', Height=1400, TextColor=255, Font='함초롬돋움')
        hwp.lists[0][1]['shape'] = shape(Align='Center', Left=200, Right=200, Indent=200, LineSpacing=200)
        changed = self.rows(hwp)[1]
        self.assertEqual(same['runs'][0]['rest'], changed['runs'][0]['rest'])
        self.assertEqual(same['paragraph_rest'], changed['paragraph_rest'])

    def test_an_underline_changed_while_it_stays_on_is_not_a_named_value(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph(('본문', char(Underline='Bottom')))])
        same = self.rows(hwp)[1]
        for atom in hwp.lists[0][1]['atoms']:
            atom['cs'] = char(Underline='Bottom', UnderlineColor=255)
        changed = self.rows(hwp)[1]
        self.assertNotEqual(same['runs'][0]['underline_style'], changed['runs'][0]['underline_style'])
        self.assertNotIn('underline_style', self.rows(FormatHwp([paragraph('제목'), paragraph('본문')]))[1]['runs'][0])

    def test_a_renumbered_definition_with_the_same_content_does_not_move_it(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('본문')])
        before = self.rows(hwp)[1]
        text = hwp.get_text_file('HWPML2X', '')
        self.assertRegex(text, r'<BORDERFILL [^>]*\bId="2"')
        renumbered = text.replace('BorderFillId="2"', 'BorderFillId="92"').replace('BorderFill="2"', 'BorderFill="92"')
        renumbered = re.sub(r'(<BORDERFILL [^>]*\bId=)"2"', r'\1"92"', renumbered)
        moved = native_format.readback_format(native_format.parse_format_model(renumbered)['paragraphs'][1])
        self.assertEqual(before['runs'][0]['rest'], moved['runs'][0]['rest'])
        self.assertEqual(before['paragraph_rest'], moved['paragraph_rest'])

    def test_an_object_change_moves_its_digest(self) -> None:
        from tests.native_format_fakes import picture
        hwp = FormatHwp([paragraph('제목'), paragraph('앞', picture(), '뒤')])
        text = hwp.get_text_file('HWPML2X', '')
        before = native_format.readback_format(native_format.parse_format_model(text)['paragraphs'][1])
        after = native_format.readback_format(native_format.parse_format_model(text.replace('<SIZE Height="7200"', '<SIZE Height="7300"', 1))['paragraphs'][1])
        self.assertNotEqual(before['objects_sha256'], after['objects_sha256'])


class LargeDocument(unittest.TestCase):
    def test_a_budget_keeps_the_readback_within_the_response_limit_and_says_so(self) -> None:
        from app import native_paragraph_edit as edit
        hwp = FormatHwp([paragraph('제목')] + [paragraph(f'문단 {n} 글') for n in range(40)])
        model = native_format.parse_format_model(hwp.get_text_file('HWPML2X', ''))
        result = {'paragraphs': [{'text': row['text'][:300]} for row in model['paragraphs']]}
        with patch.object(edit, '_FORMAT_READBACK_BYTES', 4000):
            edit._add_format(result, [{'text': row['text']} for row in model['paragraphs']], lambda: model, native_format.readback_format)
        self.assertTrue(result['paragraphs'][0]['format']['runs'])
        self.assertFalse(result['paragraphs'][0]['format']['runs_truncated'])
        self.assertEqual(result['paragraphs'][-1]['format']['runs'], [])
        self.assertTrue(result['paragraphs'][-1]['format']['runs_truncated'])
        self.assertEqual(result['paragraphs'][-1]['format']['paragraph']['align'], 'justify')


if __name__ == '__main__':
    unittest.main()
