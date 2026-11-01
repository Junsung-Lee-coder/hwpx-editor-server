"""Invisible controls beside a formatted range and the refusal for a range reaching a number mark.

Through the real bundle validation, service, census and a real manager where a hold or Undo is involved, over the fake built on
measured native behavior. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import native_format
from tests.native_format_fakes import bookmark, char, end_shape, footnote_like, FormatHwp, index_mark, new_number, numbered_document, page_hiding, page_number, paragraph, picture, shape, table, unmeasured
from tests.test_native_format import CHARACTER, character, para_format, refusal, result_of, styled
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

RED = {'bold': True, 'text_color': '#C00000'}
RED_CHAR = char(Bold=True, TextColor=0x0000C0)
BLUE_CHAR = char(TextColor=0xFF0000)


def title_document(*lead, title='표제 앞부분 끝부분', **options) -> FormatHwp:
    return FormatHwp([paragraph(*lead, title), paragraph('본문 첫째'), paragraph('본문 둘째')], **options)


def lead_shapes(hwp: FormatHwp, para: int = 0) -> list[dict]:
    return [atom['cs'] for atom in hwp.atoms_of(0, para) if 'ctrl' in atom]


class PageNumberBeforeTheTitle(unittest.TestCase):
    def test_the_whole_title_and_its_prefix_are_formatted_and_nothing_is_held(self) -> None:
        for match, tail in (('표제 앞부분 끝부분', ()), ('표제 앞부분', ((' 끝부분', char()),))):
            with self.subTest(match=match):
                hwp = title_document(page_number(), new_number())
                value = result_of(self, character(hwp, match=match, **RED))
                self.assertEqual(styled(hwp, 0, 0), [(match, RED_CHAR), *tail])
                self.assertEqual(lead_shapes(hwp), [RED_CHAR] * 6, 'the section and page controls share the run of the first text')
                self.assertEqual(hwp.executes()[0][3][:3], (0, 0, 48))
                self.assertEqual(value['result']['joined_controls'], ['secdef', 'coldef', 'header', 'footer', 'pagenum', 'newnum'])

    def test_controls_in_another_shape_keep_their_other_values_and_take_the_requested_ones(self) -> None:
        hwp = title_document(page_number(cs=BLUE_CHAR), new_number(cs=BLUE_CHAR))
        hwp.lists[0][0]['atoms'][:4] = [{**atom, 'cs': BLUE_CHAR} for atom in hwp.lists[0][0]['atoms'][:4]]
        result_of(self, character(hwp, match='표제 앞부분', bold=True))
        self.assertEqual(lead_shapes(hwp), [char(TextColor=0xFF0000, Bold=True)] * 6)
        self.assertEqual(styled(hwp, 0, 0), [('표제 앞부분', char(Bold=True)), (' 끝부분', char())])

    def test_a_range_later_in_the_title_leaves_the_controls_alone(self) -> None:
        hwp = title_document(page_number(), new_number())
        value = result_of(self, character(hwp, match='끝부분', **RED))
        self.assertEqual(lead_shapes(hwp), [char()] * 6)
        self.assertEqual(value['result']['joined_controls'], [])

    def test_a_title_and_body_paragraph_range_starts_after_the_page_controls(self) -> None:
        hwp = title_document(page_number(), new_number())
        value = result_of(self, para_format(hwp, match='표제 앞부분', through_match='본문 첫째', align='center'))
        self.assertEqual(hwp.executes()[0][3][:3], (0, 0, 48))
        self.assertEqual([item['shape']['Align'] for item in hwp.lists[0]], ['Center', 'Center', 'Justify'])
        self.assertEqual(lead_shapes(hwp), [char()] * 6)
        self.assertEqual(len(value['result']['after']), 2)

    def test_a_control_whose_data_moved_is_not_accepted(self) -> None:
        for field, name in ((lambda hwp: hwp.lists[0][0]['atoms'].__getitem__(4).update(xml='<PAGENUM FormatType="Digit" Pos="TopCenter" SideChar="-"/>'), 'page number'),
                            (lambda hwp: hwp.lists[0][0]['atoms'].__getitem__(5).update(xml='<BOOKMARK Name="바뀐이름"/>'), 'bookmark')):
            with self.subTest(name=name):
                lead = (page_number(), new_number()) if name == 'page number' else (page_number(), bookmark())
                hwp = title_document(*lead)
                hwp.after_execute = field
                owned = owned_doc(self, hwp)
                with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
                    outcome = owned.service.command_bundle(steps=[{'label': 'x', **CHARACTER, 'match': '표제 앞부분', 'bold': True}])
                self.assertTrue(outcome['native_custody_held'])

    def test_a_control_that_does_not_take_the_values_is_not_accepted(self) -> None:
        def undo_join(doc):
            doc.lists[0][0]['atoms'][4]['cs'] = char()
        hwp = title_document(page_number(), new_number())
        hwp.after_execute = undo_join
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            outcome = owned.service.command_bundle(steps=[{'label': 'x', **CHARACTER, 'match': '표제 앞부분', 'bold': True}])
        self.assertTrue(outcome['native_custody_held'])

    def test_text_that_really_changed_outside_the_range_is_still_held(self) -> None:
        def spill(doc):
            for atom in doc.lists[0][0]['atoms'][-3:]:
                atom['cs'] = char(Bold=True)
        hwp = title_document(page_number(), new_number())
        hwp.after_execute = spill
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            outcome = owned.service.command_bundle(steps=[{'label': 'x', **CHARACTER, 'match': '표제 앞부분', 'bold': True}])
        self.assertTrue(outcome['native_custody_held'])

    def test_undo_and_redo_are_one_step_each_and_reach_the_exact_states(self) -> None:
        hwp = title_document(page_number(), new_number())
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        patches = (patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}),
                   patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.assertTrue(owned.service.command_bundle(steps=[{'label': 'x', **CHARACTER, 'match': '표제 앞부분 끝부분', **RED}])['ok'])
        undone = owned.service.undo()
        self.assertTrue(undone['ok'], undone)
        self.assertEqual((undone['native_undo_steps'], undone['confirmation']['expected_state']), (1, {'compared': True, 'matches': True}))
        self.assertEqual(lead_shapes(hwp), [char()] * 6)
        redone = owned.service.redo()
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(lead_shapes(hwp), [RED_CHAR] * 6)


class OtherControlsBesideARange(unittest.TestCase):
    def test_each_measured_invisible_control_before_the_first_text_joins(self) -> None:
        for name, make in (('bookmark', bookmark), ('pagehiding', page_hiding), ('indexmark', index_mark)):
            with self.subTest(name=name):
                hwp = title_document(make())
                value = result_of(self, character(hwp, match='표제 앞부분', **RED))
                self.assertEqual(lead_shapes(hwp), [RED_CHAR] * 5)
                self.assertEqual(value['result']['joined_controls'][-1], name)

    def test_a_first_text_after_an_invisible_control_outside_the_first_paragraph_joins_too(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph(bookmark(), '본문 글자'), paragraph('끝')])
        result_of(self, character(hwp, match='본문', **RED))
        self.assertEqual(lead_shapes(hwp, 1), [RED_CHAR])

    def test_a_range_ending_right_before_invisible_controls_takes_them_along(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('앞글자', bookmark('가'), bookmark('나'), '뒤글자'), paragraph('끝')])
        value = result_of(self, character(hwp, match='앞글자', **RED))
        self.assertEqual(lead_shapes(hwp, 1), [RED_CHAR, RED_CHAR])
        self.assertEqual(styled(hwp, 0, 1), [('앞글자', RED_CHAR), ('뒤글자', char())])
        self.assertEqual(value['result']['joined_controls'], ['bookmark', 'bookmark'])

    def test_a_range_right_after_an_invisible_control_with_text_before_it_does_not(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('앞글자', bookmark(), '뒤글자'), paragraph('끝')])
        value = result_of(self, character(hwp, match='뒤글자', **RED))
        self.assertEqual(lead_shapes(hwp, 1), [char()])
        self.assertEqual(value['result']['joined_controls'], [])

    def test_a_visible_object_among_the_controls_stops_the_join(self) -> None:
        for lead in ((picture(), bookmark()), (bookmark(), picture())):
            with self.subTest(lead=[atom['ctrl'] for atom in lead]):
                hwp = FormatHwp([paragraph('제목'), paragraph(*lead, '본문 글자'), paragraph('끝')])
                value = result_of(self, character(hwp, match='본문', **RED))
                self.assertEqual(lead_shapes(hwp, 1), [char(), char()])
                self.assertEqual(value['result']['joined_controls'], [])

    def test_objects_fields_and_notes_keep_their_own_shape_beside_a_range(self) -> None:
        for name, make in (('picture', picture), ('field', footnote_like), ('table', lambda: table(paragraph('가'), paragraph('나'), paragraph('다'), paragraph('라')))):
            with self.subTest(name=name):
                hwp = FormatHwp([paragraph('제목'), paragraph(make(), '가운데 글자', make(), '뒤쪽 글'), paragraph('끝')])
                result_of(self, character(hwp, match='가운데 글자', **RED))
                self.assertEqual(lead_shapes(hwp, 1), [char(), char()])

    def test_a_control_nothing_measured_beside_the_range_is_refused_before_any_edit(self) -> None:
        cases = (('at first text', [paragraph('제목'), paragraph(unmeasured(), '본문 글자'), paragraph('끝')], '본문'),
                 ('right before', [paragraph('제목'), paragraph('본문', unmeasured(), ' 글자'), paragraph('끝')], '본문'),
                 ('right after', [paragraph('제목'), paragraph('앞글', unmeasured(), '본문 글자'), paragraph('끝')], '본문'),
                 ('behind a bookmark', [paragraph('제목'), paragraph('본문', bookmark(), unmeasured(), ' 글자'), paragraph('끝')], '본문'),
                 ('among the lead', [paragraph('제목'), paragraph(bookmark(), unmeasured(), '본문 글자'), paragraph('끝')], '본문'))
        for name, body, match in cases:
            with self.subTest(name=name):
                hwp = FormatHwp(body)
                outcome = character(hwp, match=match, **RED)
                refusal(self, outcome, 'target_beside_unmeasured_control', hwp)
                self.assertIn('ellipse', outcome['steps'][0]['refusal_message'])
                self.assertNotIn('table, picture', outcome['steps'][0]['refusal_message'])

    def test_a_control_nothing_measured_elsewhere_in_the_paragraph_does_not_matter(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('본문 글자 ', unmeasured(), ' 뒤쪽'), paragraph('끝')])
        result_of(self, character(hwp, match='본문', **RED))
        self.assertEqual(lead_shapes(hwp, 1), [char()])

    def test_a_range_crossing_an_invisible_control_says_what_it_crosses(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('앞글자', bookmark(), '뒤글자'), paragraph('끝')])
        outcome = character(hwp, match='글자뒤', bold=True)
        refusal(self, outcome, 'target_crosses_invisible_control', hwp)
        text = outcome['steps'][0]['refusal_message']
        self.assertIn('invisible', text)
        self.assertNotIn('table, picture', text)

    def test_a_range_crossing_a_picture_keeps_its_own_reason(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('앞글자', picture(), '뒤글자'), paragraph('끝')])
        outcome = character(hwp, match='글자뒤', bold=True)
        refusal(self, outcome, 'target_crosses_control', hwp)
        self.assertIn('table, picture', outcome['steps'][0]['refusal_message'])


class ParagraphRangeRefusals(unittest.TestCase):
    def reason(self, hwp: FormatHwp, code: str = 'target_has_controls', **fields) -> str:
        outcome = para_format(hwp, align='center', **fields)
        refusal(self, outcome, code, hwp)
        return outcome['steps'][0]['refusal_message']

    def test_a_visible_object_keeps_the_visible_reason(self) -> None:
        text = self.reason(title_document(picture()), match='표제 앞부분', through_match='본문 첫째')
        self.assertIn('table, picture', text)

    def test_a_visible_object_after_page_controls_is_refused_too(self) -> None:
        text = self.reason(title_document(page_number(), picture()), match='표제 앞부분', through_match='본문 첫째')
        self.assertIn('table, picture', text)

    def test_an_invisible_control_later_in_the_range_is_named_as_invisible(self) -> None:
        for name, body in (('in the next paragraph', [paragraph('제목 글'), paragraph(bookmark(), '본문 글'), paragraph('끝 글')]),
                           ('after the first text', [paragraph('제목 글', bookmark()), paragraph('본문 글'), paragraph('끝 글')])):
            with self.subTest(name=name):
                text = self.reason(FormatHwp(body), 'target_has_invisible_controls', match='제목 글', through_match='본문 글')
                self.assertIn('invisible', text)
                self.assertNotIn('table, picture', text)

    def test_a_control_nothing_measured_before_the_first_text_is_refused_as_an_object(self) -> None:
        text = self.reason(title_document(unmeasured()), match='표제 앞부분', through_match='본문 첫째')
        self.assertIn('other object', text)

    def test_the_review_lists_no_invisible_control_as_an_object(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph(bookmark(), page_hiding(), index_mark(), '글'), paragraph(picture(), '글자'), paragraph('끝')])
        model = native_format.parse_format_model(hwp.get_text_file('HWPML2X', ''))['paragraphs']
        self.assertEqual(native_format.readback_format(model[0])['objects'], [])
        self.assertEqual(native_format.readback_format(model[1])['objects'], [])
        self.assertEqual(native_format.readback_format(model[2])['objects'], ['picture'])


class RealFactsForTheReview(unittest.TestCase):
    def facts(self, hwp: FormatHwp, index: int) -> dict:
        return native_format.readback_format(native_format.parse_format_model(hwp.get_text_file('HWPML2X', ''))['paragraphs'][index])

    def test_a_new_number_moves_only_the_numbering_values_of_the_paragraph_shape(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('본문'), paragraph('끝')])
        before = self.facts(hwp, 1)
        hwp.lists[0][1]['shape'] = shape(HeadingType='Number')
        after = self.facts(hwp, 1)
        self.assertEqual(before['paragraph_rest'], after['paragraph_rest'])
        self.assertNotEqual(before['paragraph_heading'], after['paragraph_heading'])

    def test_another_paragraph_value_still_moves_the_remainder(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('본문'), paragraph('끝')])
        before = self.facts(hwp, 1)
        real = hwp.get_text_file
        hwp.get_text_file = lambda fmt='UNICODE', option='': real(fmt, option).replace('KeepLines="false"', 'KeepLines="true"')
        self.assertNotEqual(before['paragraph_rest'], self.facts(hwp, 1)['paragraph_rest'])

    def test_top_and_bottom_underlines_reach_the_review_as_known_values(self) -> None:
        from hwpx_mcp import review as rules
        top = FormatHwp([paragraph('제목'), paragraph(('본문', char(Underline='Top'))), paragraph('끝')])
        bottom = FormatHwp([paragraph('제목'), paragraph(('본문', char(Underline='Bottom'))), paragraph('끝')])
        rows = rules.format_changes({'format': self.facts(top, 1)}, {'format': self.facts(bottom, 1)})['ranges']
        self.assertEqual(rows[0]['changes'], {'underline_type': {'before': 'top', 'after': 'bottom'}})


class RangeReachingANumberMark(unittest.TestCase):
    def test_a_multi_paragraph_range_ending_exactly_at_the_last_numbered_end_is_refused(self) -> None:
        hwp = numbered_document()
        outcome = Flow(hwp).step(**CHARACTER, match='일반 앞글 꼬리글', through_match='위쪽 앞글 꼬리글', **RED)
        refusal(self, outcome, 'target_changes_number_mark', hwp)
        text = outcome['steps'][0]['refusal_message']
        self.assertIn("numbered paragraph's end", text)
        self.assertIn("the last paragraph's end included", text)

    def test_ending_a_character_earlier_is_not_refused(self) -> None:
        hwp = numbered_document()
        hwp.lists[0][4]['shape'] = shape()
        result_of(self, Flow(hwp).step(**CHARACTER, match='일반 앞글 꼬리글', through_match='위쪽 앞글 꼬리', **RED))
        self.assertEqual(end_shape(hwp.lists[0][5]), char(Underline='Top'))

    def test_running_through_a_numbered_end_into_the_next_paragraph_is_refused_with_the_same_reason(self) -> None:
        hwp = numbered_document()
        outcome = Flow(hwp).step(**CHARACTER, match='걸침 위글', through_match='걸침 아래글', **RED)
        refusal(self, outcome, 'target_changes_number_mark', hwp)
        self.assertIn("numbered paragraph's end", outcome['steps'][0]['refusal_message'])

    def test_a_single_numbered_paragraph_still_keeps_its_mark(self) -> None:
        hwp = numbered_document()
        value = result_of(self, Flow(hwp).step(**CHARACTER, match='번호 앞글 꼬리글', **RED))
        self.assertEqual(end_shape(hwp.lists[0][1]), char())
        self.assertEqual(value['result']['number_marks'][0]['kept_by'], 'paragraph_end_action')

    def test_follow_is_still_accepted_at_the_boundary(self) -> None:
        hwp = numbered_document()
        result_of(self, Flow(hwp).step(**CHARACTER, match='일반 앞글 꼬리글', through_match='위쪽 앞글 꼬리글', number_mark='follow', **RED))

    def test_text_before_a_final_bookmark_reaches_the_mark_and_keeps_it(self) -> None:
        hwp = FormatHwp([paragraph('제목'), paragraph('번호 글', bookmark(), para_shape=shape(HeadingType='Number')), paragraph('끝')])
        value = result_of(self, Flow(hwp).step(**CHARACTER, match='번호 글', **RED))
        self.assertEqual(end_shape(hwp.lists[0][1]), char())
        self.assertEqual(lead_shapes(hwp, 1), [RED_CHAR])
        self.assertEqual(len(hwp.executes()), 2)
        self.assertEqual(value['result']['number_marks'][0]['changed'], False)


if __name__ == '__main__':
    unittest.main()
