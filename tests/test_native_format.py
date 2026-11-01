"""Exact-range character and paragraph formatting through the real bundle validation, package dispatch, service and census.

The SDK is tests.native_format_fakes, built on measured native behavior. Source behavior only; the native run is recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import local_cli_runtime as rt
from hwpx_mcp.schema import Command
from tests.native_format_fakes import char, paragraph, sample_document, shape, FormatHwp
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

CHARACTER = {'op': 'character_format_exact', 'expected_page': 1, 'confirm_layout': True}
PARAGRAPH = {'op': 'paragraph_format_exact', 'expected_page': 1, 'confirm_layout': True}


def character(hwp: FormatHwp, **fields):
    return Flow(hwp).step(**CHARACTER, **fields)


def para_format(hwp: FormatHwp, **fields):
    return Flow(hwp).step(**PARAGRAPH, **fields)


def result_of(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def refusal(test: unittest.TestCase, outcome: dict, code: str, hwp: FormatHwp, state=None) -> None:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(hwp.executes(), [])
    if state is not None:
        test.assertEqual((hwp.cursor, hwp.selection), state)


def styled(hwp: FormatHwp, list_id: int, para: int) -> dict[str, dict]:
    """Text by uniform character properties, in order."""
    groups: list[tuple[str, dict]] = []
    for text, props in hwp.text_props(list_id, para):
        if groups and groups[-1][1] == props:
            groups[-1] = (groups[-1][0] + text, props)
        else:
            groups.append((text, props))
    return groups


class CharacterFormat(unittest.TestCase):
    def test_each_property_alone_sets_only_its_native_items_in_one_action(self) -> None:
        cases = [
            ({'bold': True}, {'Bold': 1}, {'Bold': True}),
            ({'italic': True}, {'Italic': 1}, {'Italic': True}),
            ({'underline': True}, {'UnderlineType': 1}, {'Underline': 'Bottom'}),
            ({'size_pt': 16.5}, {'Height': 1650}, {'Height': 1650}),
            ({'text_color': '#C03020'}, {'TextColor': 0x2030C0}, {'TextColor': 0x2030C0}),
        ]
        for fields, items, expected in cases:
            with self.subTest(fields=fields):
                hwp = sample_document()
                value = result_of(self, character(hwp, match='English', **fields))
                self.assertEqual([call[1:3] for call in hwp.executes()], [('CharShape', items)])
                self.assertEqual(hwp.executes()[0][3], (0, 3, 4, 0, 3, 11))
                self.assertEqual(styled(hwp, 0, 3), [('한글과 ', char()), ('English', char(**expected)), (' 혼합 target phrase 끝', char())])
                self.assertEqual(value['undo']['native_editing_actions'], 1)
                self.assertEqual(value['result']['native_actions'], ['Execute:CharShape'])

    def test_font_family_sets_every_language_and_font_type(self) -> None:
        hwp = sample_document()
        with patch('app.native_format.installed_font_families', return_value={'굴림'}):
            result_of(self, character(hwp, match='English', font_family='굴림'))
        items = hwp.executes()[0][2]
        for lang in ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User'):
            self.assertEqual((items[f'FaceName{lang}'], items[f'FontType{lang}']), ('굴림', 1))
        self.assertEqual(styled(hwp, 0, 3)[1], ('English', char(Font='굴림')))

    def test_combined_properties_are_one_native_action(self) -> None:
        hwp = sample_document()
        with patch('app.native_format.installed_font_families', return_value={'굴림'}):
            value = result_of(self, character(hwp, match='target phrase', bold=True, italic=True, underline=True, size_pt=13.5, text_color='#0000FF', font_family='굴림'))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual(styled(hwp, 0, 3)[1], ('target phrase', char(Bold=True, Italic=True, Underline='Bottom', Height=1350, TextColor=0xFF0000, Font='굴림')))
        self.assertEqual(value['result']['after'][0]['segments'], [
            {'text': 'target phrase', 'font_family': '굴림', 'size_pt': 13.5, 'bold': True, 'italic': True, 'underline': True, 'text_color': '#0000FF'}])
        self.assertEqual(value['result']['before'][0]['segments'], [
            {'text': 'target phrase', 'font_family': '함초롬바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000'}])

    def test_range_across_mixed_runs_keeps_each_runs_other_properties(self) -> None:
        hwp = sample_document()
        value = result_of(self, character(hwp, match='일반 빨강구간 Bold 파랑구간 뒤', italic=True))
        groups = styled(hwp, 0, 2)
        self.assertEqual([text for text, _props in groups], ['혼합 앞 ', '일반 ', '빨강구간 Bold ', '파랑구간 ', '뒤', ' 일반'])
        self.assertEqual(groups[1][1], char(Italic=True))
        self.assertEqual(groups[2][1], char(Height=1400, TextColor=192, Bold=True, Italic=True))
        self.assertEqual(groups[3][1], char(Height=1200, TextColor=12582912, Italic=True))
        self.assertEqual(groups[0][1], char())
        self.assertEqual(groups[5][1], char())
        self.assertEqual(len(value['result']['before'][0]['segments']), 4)

    def test_turning_a_property_off_applies_only_inside_the_range(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='굵은빨강', bold=False))
        self.assertEqual(styled(hwp, 0, 1), [('굵은빨강', char(Height=1400, TextColor=192)), (' 문단', char(Height=1400, TextColor=192, Bold=True))])

    def test_significant_whitespace_and_tabs_are_matched_exactly(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='두칸  문구', bold=True))
        self.assertEqual(styled(hwp, 0, 6)[1][0], '두칸  문구')
        hwp = sample_document()
        result_of(self, character(hwp, match='글자\t탭뒤', bold=True))
        self.assertEqual(hwp.executes()[0][3], (0, 7, 2, 0, 7, 14))
        self.assertEqual(styled(hwp, 0, 7)[1][0], '글자\t탭뒤')
        hwp = sample_document()
        result_of(self, character(hwp, match='탭뒤글자\n줄뒤 묶음 고정', underline=True))
        self.assertEqual(styled(hwp, 0, 7)[1][0], '탭뒤글자\n줄뒤 묶음 고정')

    def test_leading_and_trailing_spaces_are_part_of_the_range(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match=' target ', bold=True))
        self.assertEqual(styled(hwp, 0, 3)[1][0], ' target ')

    def test_text_beside_an_inline_picture_positions_after_the_control(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='그림뒤', bold=True))
        self.assertEqual(hwp.executes()[0][3], (0, 9, 11, 0, 9, 14))
        self.assertEqual(styled(hwp, 0, 9), [('그림앞', char()), ('그림뒤', char(Bold=True)), (' 문구', char())])

    def test_text_after_the_section_controls_of_the_first_paragraph(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='제목', bold=True))
        self.assertEqual(hwp.executes()[0][3], (0, 0, 32, 0, 0, 34))

    def test_text_of_a_paragraph_holding_a_table_leaves_the_cells_alone(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='표뒤', bold=True))
        self.assertEqual(hwp.executes()[0][3], (0, 10, 8, 0, 10, 10))
        table_atom = hwp.atoms_of(0, 10)[0]
        self.assertEqual({atom['cs']['Bold'] for cell in table_atom['cells'] for atom in cell['atoms']}, {False})

    def test_phrase_in_a_table_cell_uses_the_cell_list(self) -> None:
        hwp = sample_document()
        value = result_of(self, character(hwp, match='표적 문구', bold=True))
        list_id = hwp.executes()[0][3][0]
        self.assertNotEqual(list_id, 0)
        self.assertEqual(styled(hwp, list_id, 0), [('셀 ', char()), ('표적 문구', char(Bold=True)), (' 뒤', char())])
        self.assertEqual(value['target']['paragraphs'][0]['context'], 'table_cell')

    def test_range_through_a_later_paragraph(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='범위 첫째', through_match='범위 둘째', italic=True))
        self.assertEqual(hwp.executes()[0][3], (0, 14, 0, 0, 15, 5))
        self.assertEqual(styled(hwp, 0, 14), [('범위 첫째', char(Italic=True))])
        self.assertEqual(styled(hwp, 0, 15), [('범위 둘째', char(Italic=True))])
        self.assertEqual(styled(hwp, 0, 16), [('범위 셋째', char())])

    def test_explicit_paragraph_applies_to_its_whole_text(self) -> None:
        hwp = sample_document()
        census_index = next(i for i, p in enumerate(_census_texts(hwp)) if p == '중복문구 둘째')
        result_of(self, character(hwp, paragraph_index=census_index, expected_text='중복문구 둘째', bold=True))
        self.assertEqual(styled(hwp, 0, 5), [('중복문구 둘째', char(Bold=True))])
        self.assertEqual(styled(hwp, 0, 4), [('중복문구 첫째', char())])

    def test_repeated_commands_on_one_target_each_reselect_it(self) -> None:
        hwp = sample_document()
        flow = Flow(hwp)
        for fields in ({'bold': True}, {'italic': True}, {'size_pt': 12.0}):
            self.assertTrue(flow.step(**CHARACTER, match='target phrase', **fields)['ok'])
        self.assertEqual(len(hwp.executes()), 3)
        self.assertEqual({call[3] for call in hwp.executes()}, {(0, 3, 15, 0, 3, 28)})
        self.assertEqual(styled(hwp, 0, 3)[1], ('target phrase', char(Bold=True, Italic=True, Height=1200)))
        self.assertEqual(sum(1 for call in hwp.calls if call[0] == 'select_text'), 3)

    def test_success_ends_the_selection_and_restores_the_caret(self) -> None:
        hwp = sample_document()
        start = hwp.cursor
        result_of(self, character(hwp, match='English', bold=True))
        self.assertIsNone(hwp.selection)
        self.assertEqual(hwp.cursor, start)

    def test_a_value_already_present_is_a_no_op_without_a_native_call(self) -> None:
        hwp = sample_document()
        outcome = character(hwp, match='굵은빨강', bold=True, size_pt=14.0)
        value = result_of(self, outcome)
        self.assertIs(value['result']['changed'], False)
        self.assertEqual(hwp.executes(), [])
        self.assertFalse(outcome['steps'][0]['dirty'])
        self.assertEqual(value['undo']['native_editing_actions'], 0)


def _census_texts(hwp: FormatHwp) -> list[str]:
    from app.native_numbering_census import parse_hwpml2x_census
    return [p['text'] for p in parse_hwpml2x_census(hwp.get_text_file('HWPML2X', ''))['paragraphs']]


class Refusals(unittest.TestCase):
    def test_targets_that_cannot_be_exact_change_nothing_and_stay_usable(self) -> None:
        cases = [
            ({'match': '없는 문구'}, 'target_not_found'),
            ({'match': '중복문구'}, 'target_ambiguous'),
            ({'match': '셀중복'}, 'target_ambiguous'),
            ({'match': '그림앞그림뒤'}, 'target_crosses_control'),
            ({'match': '머리말 문구'}, 'target_in_header_footer'),
            ({'match': '꼬리말 문구'}, 'target_in_header_footer'),
            ({'match': '글상자 안'}, 'target_unsupported_context'),
            ({'match': '중복문구 첫째', 'expected_page': 2}, 'target_wrong_page'),
            ({'match': '범위 첫째', 'through_match': '앞 이웃'}, 'target_range_unsupported'),
            ({'match': '셀 표적', 'through_match': '문단 대상'}, 'target_range_unsupported'),
        ]
        for fields, code in cases:
            with self.subTest(fields=fields):
                hwp = sample_document()
                state = (hwp.cursor, hwp.selection)
                refusal(self, Flow(hwp).step(**{**CHARACTER, 'bold': True, **fields}), code, hwp, state)

    def test_wrong_page_says_where_the_target_is(self) -> None:
        hwp = sample_document(pages={3: 2})
        flow = Flow(hwp)
        flow.service._bundle_page_evidence = lambda doc, **kwargs: {'page': doc.current_page}
        outcome = flow.step(**CHARACTER, match='English', bold=True)
        self.assertEqual(outcome['steps'][0]['refusal_code'], 'target_wrong_page')
        self.assertEqual(outcome['steps'][0]['found_page'], 2)
        self.assertTrue(flow.step(**{**CHARACTER, 'expected_page': 2}, match='English', bold=True)['ok'])

    def test_a_stale_paragraph_identity_is_refused(self) -> None:
        hwp = sample_document()
        refusal(self, character(hwp, paragraph_index=4, expected_text='바뀐 글', bold=True), 'target_text_mismatch', hwp)
        hwp = sample_document()
        refusal(self, character(hwp, paragraph_index=999, expected_text='x', bold=True), 'target_not_found', hwp)

    def test_empty_paragraph_has_no_character_range(self) -> None:
        hwp = sample_document()
        index = next(i for i, text in enumerate(_census_texts(hwp)) if text == '' and i > 15)
        refusal(self, character(hwp, paragraph_index=index, expected_text='', bold=True), 'target_empty', hwp)

    def test_range_crossing_a_control_paragraph_is_refused_for_paragraph_format(self) -> None:
        hwp = sample_document()
        refusal(self, para_format(hwp, match='그림뒤 문구', through_match='앞 이웃', align='center'), 'target_has_controls', hwp)

    def test_font_not_available_is_refused_before_any_edit(self) -> None:
        hwp = sample_document()
        with patch('app.native_format.installed_font_families', return_value={'굴림'}):
            refusal(self, character(hwp, match='English', font_family='없는글꼴'), 'font_not_available', hwp)

    def test_font_availability_unknown_is_refused(self) -> None:
        hwp = sample_document()
        with patch('app.native_format.installed_font_families', side_effect=OSError('no GDI')):
            refusal(self, character(hwp, match='English', font_family='굴림'), 'font_availability_unknown', hwp)

    def test_a_font_already_in_the_document_needs_no_enumeration(self) -> None:
        hwp = sample_document()
        with patch('app.native_format.installed_font_families', side_effect=OSError('no GDI')):
            result_of(self, character(hwp, match='English', font_family='함초롬돋움'))

    def test_a_built_in_font_used_by_the_document_still_needs_the_session_list(self) -> None:
        from app import native_format
        hwp = sample_document()
        text = hwp.get_text_file('HWPML2X', '').replace('Type="ttf"', 'Type="hft"')
        self.assertEqual(native_format.parse_format_model(text)['fonts'], set())
        real = hwp.get_text_file
        hwp.get_text_file = lambda fmt='UNICODE', option='': real(fmt, option).replace('Type="ttf"', 'Type="hft"')
        with patch('app.native_format.installed_font_families', return_value=set()):
            refusal(self, character(hwp, match='English', font_family='함초롬돋움'), 'font_not_available', hwp)

    def test_selection_that_does_not_hold_the_exact_text_stops_before_the_action(self) -> None:
        hwp = sample_document()
        hwp.position_drift = 1
        state = (hwp.cursor, hwp.selection)
        refusal(self, character(hwp, match='English', bold=True), 'selection_unverified', hwp, state)

    def test_a_selection_collapsed_by_the_proof_read_never_formats_the_caret(self) -> None:
        hwp = sample_document()
        hwp.collapse_on_block_read = True
        state = (hwp.cursor, hwp.selection)
        refusal(self, character(hwp, match='English', bold=True), 'selection_unverified', hwp, state)

    def test_an_active_selection_found_before_a_refusal_is_given_back(self) -> None:
        hwp = sample_document()
        hwp.select_text(4, 0, 4, 2, 0)
        state = (hwp.cursor, hwp.selection)
        refusal(self, character(hwp, match='없는 문구', bold=True), 'target_not_found', hwp, state)

    def test_oversized_document_is_refused_as_unprovable(self) -> None:
        hwp = sample_document()
        real = hwp.get_text_file
        hwp.get_text_file = lambda fmt='UNICODE', option='': real(fmt, option) + ('x' * (32 * 1024 * 1024))
        outcome = character(hwp, match='English', bold=True)
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['steps'][0]['refusal_code'], 'preservation_unprovable')
        self.assertEqual(hwp.executes(), [])

    def test_no_forbidden_native_path_is_ever_used(self) -> None:
        hwp = sample_document()
        result_of(self, character(hwp, match='English', bold=True))
        names = {call[0] for call in hwp.calls}
        self.assertFalse(names & {'SelectAll', 'Run', 'pycall', 'hwp_action'}, names)

    def test_schema_refuses_unknown_and_malformed_requests(self) -> None:
        good = {'op': 'character_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'bold': True}
        Command.model_validate({'session_id': 'a' * 32, 'request': good})
        bad = [
            {**good, 'method': 'SelectAll'}, {**good, 'confirm_layout': False}, {**good, 'text_color': 'red'}, {**good, 'text_color': '#12345'},
            {**good, 'size_pt': 0}, {**good, 'size_pt': 5000.0}, {**good, 'bold': 'yes'}, {**good, 'match': ''}, {**good, 'match': 'a\rb'}, {**good, 'font_family': ''},
            {**good, 'font_family': '@굴림'}, {k: v for k, v in good.items() if k != 'bold'}, {k: v for k, v in good.items() if k != 'match'},
            {**good, 'paragraph_index': 1}, {**good, 'paragraph_index': 1, 'expected_text': 'x'},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'align': 'distribute'},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'line_spacing_type': 'percent'},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'line_spacing_value': 160},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'line_spacing_type': 'percent', 'line_spacing_value': 160.5},
            {'op': 'paragraph_format_exact', 'match': 'x', 'expected_page': 1, 'confirm_layout': True, 'left_indent_pt': -1.0},
        ]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': 'a' * 32, 'request': request})

    def test_values_that_are_not_finite_numbers_are_refused_as_input(self) -> None:
        from app import native_format
        base = {'match': 'x', 'expected_page': 1, 'confirm_layout': True}
        for value in (float('nan'), float('inf')):
            for fields in ({'line_spacing_type': 'percent', 'line_spacing_value': value}, {'line_spacing_type': 'fixed', 'line_spacing_value': value}, {'left_indent_pt': value}):
                with self.subTest(fields=fields), self.assertRaises(native_format.NativeNumberingInputError):
                    native_format.validate_paragraph_step({**base, **fields})
            with self.subTest(size=value), self.assertRaises(native_format.NativeNumberingInputError):
                native_format.validate_character_step({**base, 'size_pt': value})
            with self.subTest(schema=value), self.assertRaises(ValidationError):
                Command.model_validate({'session_id': 'a' * 32, 'request': {'op': 'paragraph_format_exact', **base, 'line_spacing_type': 'percent', 'line_spacing_value': value}})

    def test_a_model_read_declaring_entities_is_unusable(self) -> None:
        from app import native_format
        hwp = sample_document()
        text = hwp.get_text_file('HWPML2X', '')
        with self.assertRaises(native_format.CensusError):
            native_format.parse_format_model('<!DOCTYPE x [<!ENTITY a "b">]>' + text[text.index('<HWPML'):])

    def test_the_backend_validates_independently_of_the_adapter(self) -> None:
        hwp = sample_document()
        with self.assertRaises(Exception):
            Flow(hwp).step(**CHARACTER, match='x')
        with self.assertRaises(Exception):
            Flow(hwp).step(**CHARACTER, match='x', bold=True, method='SelectAll')
        with self.assertRaises(Exception):
            Flow(hwp).step(**PARAGRAPH, match='x', left_indent_pt=0.25)
        self.assertEqual(hwp.executes(), [])


class ParagraphFormat(unittest.TestCase):
    def test_alignment_values_are_the_measured_native_enums(self) -> None:
        for name, native, stored in (('justify', 0, 'Justify'), ('left', 1, 'Left'), ('right', 2, 'Right'), ('center', 3, 'Center')):
            with self.subTest(name=name):
                hwp = sample_document()
                if name == 'justify':
                    hwp.lists[0][12]['shape'] = shape(Align='Center')
                result_of(self, para_format(hwp, match='문단 대상', align=name))
                self.assertEqual(hwp.executes()[0][2], {'AlignType': native})
                self.assertEqual(hwp.lists[0][12]['shape']['Align'], stored)

    def test_indents_and_line_spacing_use_the_measured_units(self) -> None:
        hwp = sample_document()
        value = result_of(self, para_format(hwp, match='문단 대상', left_indent_pt=20.0, right_indent_pt=10.5, first_line_indent_pt=-5.0, line_spacing_type='fixed', line_spacing_value=18.0))
        self.assertEqual(hwp.executes()[0][2], {'LeftMargin': 4000, 'RightMargin': 2100, 'Indentation': -1000, 'LineSpacingType': 1, 'LineSpacing': 3600})
        self.assertEqual(value['result']['after'][0]['paragraph'], {'align': 'justify', 'left_indent_pt': 20.0, 'right_indent_pt': 10.5, 'first_line_indent_pt': -5.0, 'line_spacing_type': 'fixed', 'line_spacing_value': 18.0})
        self.assertEqual(value['result']['before'][0]['paragraph']['line_spacing_value'], 160)

    def test_line_spacing_kinds(self) -> None:
        for kind, native, value, stored, stored_type in (('percent', 0, 200, 200, 'Percent'), ('fixed', 1, 20, 4000, 'Fixed'), ('between_lines', 2, 3, 600, 'BetweenLines'), ('at_least', 3, 11, 2200, 'AtLeast')):
            with self.subTest(kind=kind):
                hwp = sample_document()
                result_of(self, para_format(hwp, match='문단 대상', line_spacing_type=kind, line_spacing_value=value))
                self.assertEqual(hwp.executes()[0][2], {'LineSpacingType': native, 'LineSpacing': stored})
                self.assertEqual((hwp.lists[0][12]['shape']['LineSpacing'], hwp.lists[0][12]['shape']['LineSpacingType']), (stored, stored_type))

    def test_a_phrase_targets_only_its_own_paragraph_through_a_block(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='대상', align='center'))
        self.assertIsNotNone(hwp.executes()[0][3], 'paragraph formatting goes through a block so each command is its own Undo step')
        self.assertEqual([p['shape']['Align'] for p in hwp.lists[0][11:14]], ['Justify', 'Center', 'Justify'])

    def test_range_changes_exactly_its_paragraphs_in_one_action(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='범위 첫째', through_match='범위 셋째', align='right'))
        self.assertEqual(len(hwp.executes()), 1)
        self.assertEqual([p['shape']['Align'] for p in hwp.lists[0][13:18]], ['Justify', 'Right', 'Right', 'Right', 'Justify'])

    def test_other_paragraph_properties_and_numbering_stay(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='번호 문단', align='center'))
        self.assertEqual(hwp.lists[0][19]['shape'], shape(HeadingType='Number', Align='Center'))
        hwp = sample_document()
        hwp.lists[0][12]['shape'] = shape(Left=2000, LineSpacing=200)
        result_of(self, para_format(hwp, match='문단 대상', align='center'))
        self.assertEqual(hwp.lists[0][12]['shape'], shape(Left=2000, LineSpacing=200, Align='Center'))

    def test_blank_paragraph_by_identity_through_its_mark(self) -> None:
        hwp = sample_document()
        index = next(i for i, text in enumerate(_census_texts(hwp)) if text == '' and i > 15)
        result_of(self, para_format(hwp, paragraph_index=index, expected_text='', align='center'))
        self.assertEqual(hwp.executes()[0][3], (0, 17, 0, 0, 18, 0))
        self.assertEqual([p['shape']['Align'] for p in hwp.lists[0][16:19]], ['Justify', 'Center', 'Justify'])

    def test_paragraph_holding_only_a_table_through_its_mark_leaves_cells_alone(self) -> None:
        hwp = sample_document()
        index = _census_texts(hwp).index('전용표 A1') - 1
        result_of(self, para_format(hwp, paragraph_index=index, expected_text='', align='right'))
        self.assertEqual(hwp.executes()[0][3], (0, 21, 8, 0, 22, 0))
        self.assertEqual(hwp.lists[0][21]['shape']['Align'], 'Right')
        self.assertEqual(hwp.lists[0][22]['shape']['Align'], 'Justify')
        self.assertEqual({cell['shape']['Align'] for cell in hwp.atoms_of(0, 21)[0]['cells']}, {'Justify'})

    def test_paragraph_with_inline_picture_selects_text_not_the_picture(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='그림뒤', align='center'))
        self.assertEqual(hwp.executes()[0][3], (0, 9, 0, 0, 9, 3))

    def test_first_paragraph_selects_after_its_section_controls(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='제목', align='center'))
        self.assertEqual(hwp.executes()[0][3], (0, 0, 32, 0, 0, 37))

    def test_cell_paragraph(self) -> None:
        hwp = sample_document()
        result_of(self, para_format(hwp, match='표적 문구', align='center'))
        list_id = hwp.executes()[0][3][0]
        self.assertEqual(hwp.lists[list_id][0]['shape']['Align'], 'Center')
        self.assertEqual(hwp.lists[list_id + 1][0]['shape']['Align'], 'Justify')

    def test_a_last_blank_paragraph_with_no_following_mark_is_refused(self) -> None:
        body = [paragraph('첫 문단'), paragraph('')]
        hwp = FormatHwp(body)
        refusal(self, para_format(hwp, paragraph_index=1, expected_text='', align='center'), 'target_unselectable', hwp)

    def test_same_value_is_a_no_op(self) -> None:
        hwp = sample_document()
        outcome = para_format(hwp, match='문단 대상', align='justify', line_spacing_type='percent', line_spacing_value=160)
        self.assertIs(result_of(self, outcome)['result']['changed'], False)
        self.assertEqual(hwp.executes(), [])


class UnknownOutcome(unittest.TestCase):
    """A real manager and owning thread: once the action may have run, the session is held and nothing else is tried."""

    def run_step(self, owned, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def assert_held(self, owned, outcome):
        self.assertFalse(outcome['ok'], outcome)
        self.assertTrue(outcome['native_custody_held'])
        self.assertTrue(outcome['steps'][0]['mutation_may_have_persisted'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertEqual([c for c in owned.hwp.calls if c[0] == 'Run'], [], 'no Undo or other recovery follows')
        self.assertEqual(len(owned.hwp.executes()), 1, 'no fallback or second action')

    def test_action_returning_false(self) -> None:
        hwp = sample_document()
        hwp.execute_result = False
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='English', bold=True))

    def test_action_raising(self) -> None:
        hwp = sample_document()
        hwp.execute_raises = OSError('synthetic native failure')
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **PARAGRAPH, match='문단 대상', align='center'))

    def test_a_change_outside_the_range_is_not_a_success(self) -> None:
        def corrupt(doc):
            doc.lists[0][4]['atoms'][0]['cs'] = char(Bold=True)
        hwp = sample_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='English', bold=True))

    def test_an_unrequested_property_change_inside_the_range_is_not_a_success(self) -> None:
        def corrupt(doc):
            for atom in doc.lists[0][3]['atoms']:
                if atom['cs']['Bold']:
                    atom['cs'] = {**atom['cs'], 'Ratio': 90}
        hwp = sample_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='English', bold=True))

    def test_a_requested_value_not_persisted_is_not_a_success(self) -> None:
        def corrupt(doc):
            for atom in doc.lists[0][3]['atoms']:
                atom['cs'] = char()
        hwp = sample_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **CHARACTER, match='English', bold=True))

    def test_an_adjacent_paragraph_change_is_not_a_success(self) -> None:
        def corrupt(doc):
            doc.lists[0][13]['shape'] = shape(Align='Center')
        hwp = sample_document()
        hwp.after_execute = corrupt
        owned = owned_doc(self, hwp)
        self.assert_held(owned, self.run_step(owned, **PARAGRAPH, match='문단 대상', align='center'))

    def test_a_clean_refusal_leaves_the_session_usable_and_closable(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        outcome = self.run_step(owned, **CHARACTER, match='없는 문구', bold=True)
        self.assertEqual(outcome['steps'][0]['refusal_code'], 'target_not_found')
        self.assertFalse(owned.session._native_custody_held)
        self.assertFalse(owned.session._lifetime_edit_possible)
        self.assertTrue(self.run_step(owned, **CHARACTER, match='English', bold=True)['ok'])


class UndoRedo(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = sample_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])
        self.originals = {}

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def test_each_command_is_one_native_undo_and_redo_restoring_the_exact_state(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='English', bold=True)['ok'])
        after_first = self.hwp.text_props(0, 3)
        self.assertTrue(self.step(**CHARACTER, match='English', italic=True)['ok'])
        after_second = self.hwp.text_props(0, 3)
        undone = self.history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 1)
        self.assertEqual(self.hwp.text_props(0, 3), after_first)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(undone['confirmation']['changes'][0]['kind'], 'format_changed')
        redone = self.history('redo')
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(self.hwp.text_props(0, 3), after_second)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertEqual(self.history('undo')['confirmation']['expected_state']['matches'], True)
        self.assertEqual(self.history('undo')['confirmation']['expected_state']['matches'], True)
        self.assertEqual(self.hwp.text_props(0, 3), sample_document().text_props(0, 3))

    def test_repeated_paragraph_commands_on_one_paragraph_stay_separate_undo_steps(self) -> None:
        self.assertTrue(self.step(**PARAGRAPH, match='문단 대상', align='center')['ok'])
        self.assertTrue(self.step(**PARAGRAPH, match='문단 대상', left_indent_pt=10.0)['ok'])
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.hwp.lists[0][12]['shape'], shape(Align='Center'))
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.hwp.lists[0][12]['shape'], shape())

    def test_undo_keeps_earlier_edits_and_ends_the_restored_selection(self) -> None:
        self.assertTrue(self.step(**PARAGRAPH, match='앞 이웃', align='right')['ok'])
        self.assertTrue(self.step(**CHARACTER, match='English', bold=True)['ok'])
        self.assertTrue(self.history('undo')['ok'])
        self.assertIsNone(self.hwp.selection, 'a block given back by Undo must not linger for a later command')
        self.assertEqual(self.hwp.lists[0][11]['shape'], shape(Align='Right'))
        self.assertEqual(styled(self.hwp, 0, 3), [('한글과 English 혼합 target phrase 끝', char())])

    def test_a_state_other_than_the_recorded_one_after_undo_is_reported_and_stops_history(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='English', bold=True)['ok'])
        real = self.hwp.history_step

        def overshoot(*args, **kwargs):
            result = real(*args, **kwargs)
            self.hwp.lists[0][4]['atoms'][0]['cs'] = char(Italic=True)
            return result

        self.hwp.history_step = overshoot
        outcome = self.history('undo')
        self.assertIs(outcome['ok'], False)
        self.assertEqual(outcome['confirmation']['expected_state'], {'compared': True, 'matches': False})
        self.assertIn('undo_state_mismatch', outcome['detail'])
        self.hwp.history_step = real
        with self.assertRaises(Exception) as caught:
            self.history('undo')
        self.assertEqual(getattr(caught.exception, 'code', None), 'undo_not_counted')

    def test_a_no_op_adds_no_undo_step(self) -> None:
        self.assertTrue(self.step(**CHARACTER, match='굵은빨강', bold=True)['ok'])
        with self.assertRaises(Exception) as caught:
            self.history('undo')
        self.assertEqual(getattr(caught.exception, 'code', None), 'no_undo_available')


class ReadbackCarriesFormat(unittest.TestCase):
    def test_every_paragraph_reports_its_runs_and_paragraph_values(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            value = owned.service.numbering_readback()
        rows = value['paragraphs']
        mixed = next(row for row in rows if row['text'].startswith('혼합'))
        self.assertEqual(rows.index(mixed), mixed['index'])
        self.assertEqual(mixed['context'], 'body')
        self.assertEqual([run['text'] for run in mixed['format']['runs']], ['혼합 앞 일반 ', '빨강구간 Bold ', '파랑구간 ', '뒤 일반'])
        named = {key: value for key, value in mixed['format']['runs'][1].items() if key != 'rest'}
        self.assertEqual(named, {'text': '빨강구간 Bold ', 'length': 10, 'font_family': '함초롬바탕', 'size_pt': 14.0, 'bold': True, 'italic': False, 'underline': False, 'text_color': '#C00000'})
        self.assertEqual(mixed['format']['paragraph']['align'], 'justify')
        self.assertEqual(next(row for row in rows if row['text'].startswith('셀 표적'))['context'], 'table_cell')
        self.assertEqual(next(row for row in rows if row['text'].startswith('글상자'))['context'], 'other')


class PublicResult(unittest.TestCase):
    """A caller reads the range and values from the public step, which otherwise redacts text and cuts nesting."""

    def public_step(self, **fields):
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            raw = owned.service._execute_live(binding=owned.store, command_name='command-bundle', task_label='x', handler=lambda handle: owned.service._execute_command_bundle_step(handle, {'label': 'x', **fields}, binding=owned.store)[0], timeout=5)
        return owned.service._public_bundle_steps(session_id=owned.sid, steps=[{'op': fields['op'], 'ok': True, 'result': raw}])[0]['result']

    def test_character_range_and_values_reach_the_caller(self) -> None:
        value = self.public_step(**CHARACTER, match='English', bold=True, text_color='#C03020')
        self.assertEqual(value['target']['paragraphs'][0]['text'], '한글과 English 혼합 target phrase 끝')
        self.assertEqual(value['target']['paragraphs'][0]['context'], 'body')
        self.assertEqual(value['requested'], {'bold': True, 'text_color': '#C03020'})
        self.assertEqual(value['result']['before'][0]['segments'][0]['text'], 'English')
        self.assertEqual(value['result']['after'][0]['segments'][0], {'text': 'English', 'font_family': '함초롬바탕', 'size_pt': 10.0, 'bold': True, 'italic': False, 'underline': False, 'text_color': '#C03020'})

    def test_paragraph_values_reach_the_caller(self) -> None:
        value = self.public_step(**PARAGRAPH, match='문단 대상', align='center', first_line_indent_pt=-5.0)
        self.assertEqual(value['result']['after'][0]['paragraph']['align'], 'center')
        self.assertEqual(value['result']['after'][0]['paragraph']['first_line_indent_pt'], -5.0)
        self.assertEqual(value['result']['before'][0]['text'], '문단 대상')

    def test_a_very_long_range_is_cut_and_says_so(self) -> None:
        from app import native_format
        step = {'target': {'kind': 'phrase', 'native_block': [0] * 6, 'paragraphs': [{'paragraph_index': n, 'context': 'body', 'text': 'x' * 500} for n in range(80)]},
                'requested': {}, 'result': {'changed': True, 'status': 'formatted', 'native_actions': [], 'before': [], 'after': [{'paragraph_index': 0, 'segments': [{'text': 'y' * 500}] * 80}]}}
        value = native_format.public_report(step)
        self.assertEqual(len(value['target']['paragraphs']), native_format.REPORT_ITEMS)
        self.assertTrue(value['target']['paragraphs_cut'])
        self.assertEqual(len(value['result']['after'][0]['segments']), native_format.REPORT_ITEMS)
        self.assertTrue(value['result']['after'][0]['segments_cut'])
        self.assertEqual(len(value['result']['after'][0]['segments'][0]['text']), native_format.REPORT_TEXT)


class StatusWording(unittest.TestCase):
    def test_a_held_session_is_not_attributed_to_a_numbering_command(self) -> None:
        import inspect
        from app import local_cli_service
        self.assertNotIn('held after a numbering command', inspect.getsource(local_cli_service))


if __name__ == '__main__':
    unittest.main()
