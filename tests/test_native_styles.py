"""Named styles: the inventory, applying one by name, creating one and changing one.

Through the real bundle validation, package dispatch, service, census and style model. The SDK is tests.native_styles_fakes,
which follows the official style action and parameter set references and Hancom's own forum examples for the create and change
actions (StyleAddNew, StyleDirectEdit); not Hancom evidence.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app.local_cli_service_support import LocalCliServiceError
from tests.native_format_fakes import bookmark, char, paragraph, shape, table
from tests.native_styles_fakes import StyleHwp, area, default_styles, style, styled, styles_document
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

APPLY = {'op': 'style_apply_exact', 'expected_page': 1, 'confirm_layout': True}
CREATE = {'op': 'style_create_exact', 'confirm_layout': True}
UPDATE = {'op': 'style_update_exact', 'confirm_layout': True}
BODY = default_styles()[1]


def run(hwp: StyleHwp, **fields) -> dict:
    try:
        return Flow(hwp).step(**fields)
    except LocalCliServiceError as exc:
        return {'ok': False, 'service_error': str(exc), 'steps': [{}]}


def ok(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def inventory(hwp: StyleHwp, **fields) -> dict:
    outcome = run(hwp, op='style_inventory', **fields)
    assert outcome['ok'], outcome
    return outcome['steps'][0]['result']


def row(hwp: StyleHwp, name: str) -> dict:
    return next(item for item in inventory(hwp, name=name)['styles'] if item['name'] == name)


def executes(hwp: StyleHwp) -> list[tuple]:
    return [call for call in hwp.style_calls() if call[0] == 'Execute']


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: StyleHwp, state: tuple | None = None) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(executes(hwp), [], 'nothing is written')
    if state is not None:
        test.assertEqual((hwp.cursor, hwp.selection), state)
    return step


def invalid(test: unittest.TestCase, outcome: dict, hwp: StyleHwp, text: str) -> None:
    test.assertFalse(outcome['ok'], outcome)
    test.assertIn(text, outcome.get('service_error') or outcome['steps'][0].get('error', ''))
    test.assertEqual(hwp.style_calls(), [])


def held(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertTrue(step['mutation_may_have_persisted'], step)
    test.assertNotIn('refusal_code', step)
    return step


def flat(item: dict) -> dict:
    """A paragraph without the paragraphs nested in its objects and areas, which are rows of their own."""
    return {**item, 'atoms': [{key: value for key, value in atom.items() if key not in {'cells', 'drawtext', 'caption', 'paras'}} for atom in item['atoms']]}


def plain_document() -> StyleHwp:
    """본문 used in two body paragraphs, a table cell and the header, every use showing exactly its definitions."""
    body = [
        paragraph('문서 제목'),
        styled('본문 첫 문단', 1, para_shape=BODY['para']),
        styled('본문 둘째 문단', 1, para_shape=BODY['para']),
        paragraph(table(styled('셀 본문', 1, para_shape=BODY['para']), paragraph('셀 둘'), paragraph('셀 셋'), paragraph('셀 넷'))),
        paragraph('마지막 문단'),
    ]
    return StyleHwp(body, areas=[area('header', styled('머리 본문', 1, para_shape=BODY['para'])), area('footer', paragraph('꼬리 문구'))])


def twin_document() -> StyleHwp:
    """A sixth style, 복제, with exactly 바탕글's definitions: only its index tells them apart."""
    styles = default_styles() + [style('복제', eng='Copy')]
    body = [paragraph('문서 제목'), styled('본문 첫 문단', 1, para_shape=BODY['para']), paragraph('바탕 둘째 문단'), paragraph('마지막 문단')]
    return StyleHwp(body, styles=styles, areas=[area('header', paragraph('머리 문구'))])


class Inventory(unittest.TestCase):
    def test_lists_every_style_with_identity_kind_definitions_and_hash_without_writing(self) -> None:
        hwp = styles_document()
        hwp.cursor = (0, 3, 2)
        result = inventory(hwp)
        self.assertTrue(result['read_only'])
        self.assertEqual((result['count'], result['paragraph_styles'], result['character_styles']), (5, 3, 2))
        self.assertRegex(result['styles_hash'], r'^sha256:[0-9a-f]{24}$')
        rows = result['styles']
        self.assertEqual([(item['index'], item['name'], item['english_name'], item['kind']) for item in rows],
                         [(0, '바탕글', 'Normal', 'paragraph'), (1, '본문', 'Body', 'paragraph'), (2, '개요 1', 'Outline 1', 'paragraph'),
                          (3, '강조', 'Emphasis', 'character'), (4, '쪽 번호', 'Page Number', 'character')])
        outline = rows[2]
        self.assertEqual(outline['character'], {'font_family': '함초롬바탕', 'size_pt': 12.0, 'bold': True, 'italic': False, 'underline': False, 'text_color': '#000000'})
        self.assertEqual(outline['paragraph'], {'align': 'justify', 'left_indent_pt': 0.0, 'right_indent_pt': 0.0, 'first_line_indent_pt': 0.0,
                                                'line_spacing_type': 'percent', 'line_spacing_value': 160})
        self.assertEqual(outline['numbering'], {'kind': 'outline', 'level': 1})
        self.assertEqual(rows[1]['paragraph']['left_indent_pt'], 15.0)
        self.assertEqual(outline['next_style'], {'index': 2, 'name': '개요 1'})
        self.assertFalse(outline['lock_form'])
        emphasis = rows[3]
        self.assertIsNone(emphasis['paragraph'])
        self.assertEqual((emphasis['character']['bold'], emphasis['character']['text_color']), (True, '#FF0000'))
        self.assertEqual(emphasis['uses'], {'recorded': False}, 'the document text records no use of a character style')
        for item in rows:
            self.assertRegex(item['hash'], r'^sha256:[0-9a-f]{24}$')
        self.assertEqual(len({item['hash'] for item in rows}), 5)
        self.assertEqual(hwp.style_calls(), [])
        self.assertEqual(executes(hwp), [])
        self.assertEqual(hwp.cursor, (0, 3, 2))
        self.assertEqual(inventory(hwp)['styles_hash'], result['styles_hash'], 'the same document gives the same hashes')

    def test_paragraph_style_use_is_counted_by_context_with_plain_uses_and_a_hash(self) -> None:
        rows = {item['name']: item for item in inventory(styles_document())['styles']}
        body = rows['본문']['uses']
        self.assertEqual((body['recorded'], body['total'], body['plain']), (True, 4, 3))
        self.assertEqual(body['by_context'], {'body': 2, 'table_cell': 1, 'other': 0, 'header': 1, 'footer': 0})
        self.assertEqual([(use['context'], use['paragraph_index'], use['area'], use['text'], use['plain']) for use in body['rows']],
                         [('header', None, 'header:s0:both', '머리 본문', True), ('body', 1, None, '본문 첫 문단', True),
                          ('body', 2, None, '본문 둘째 굵은말 끝', False), ('table_cell', 5, None, '셀 본문', True)])
        self.assertTrue(body['rows_complete'])
        self.assertRegex(body['hash'], r'^sha256:[0-9a-f]{24}$')
        self.assertEqual(rows['개요 1']['uses']['total'], 0)
        normal = rows['바탕글']['uses']
        self.assertEqual(normal['by_context']['footer'], 1)
        self.assertEqual(normal['total'], 13 - 3 + 1, 'every other paragraph, the footer one included')

    def test_a_name_filter_lists_one_style_with_every_use(self) -> None:
        result = inventory(styles_document(), name='본문')
        self.assertEqual([item['name'] for item in result['styles']], ['본문'])
        self.assertEqual(result['count'], 5, 'the count is of the whole list')
        self.assertEqual(inventory(styles_document(), name='없는 스타일')['styles'], [])

    def test_the_use_hash_changes_with_the_uses_and_the_style_hash_with_the_definition(self) -> None:
        hwp = styles_document()
        before = row(hwp, '본문')
        hwp.lists[0][3]['style'] = 1
        after = row(hwp, '본문')
        self.assertEqual(after['hash'], before['hash'], 'a new use is not a new definition')
        self.assertNotEqual(after['uses']['hash'], before['uses']['hash'])
        hwp.styles[1]['char'] = char(Height=1100)
        self.assertNotEqual(row(hwp, '본문')['hash'], before['hash'])

    def test_duplicate_names_are_reported(self) -> None:
        styles = default_styles() + [style('본문', eng='Body copy')]
        result = inventory(StyleHwp([paragraph('하나')], styles=styles))
        self.assertEqual(result['duplicate_names'], ['본문'])

    def test_a_style_list_whose_ids_are_not_its_order_is_refused(self) -> None:
        hwp = styles_document()
        real = hwp._style_xml  # noqa: SLF001
        hwp._style_xml = lambda chars, paras: real(chars, paras).replace('Id="1"', 'Id="7"', 1)  # noqa: SLF001
        outcome = run(hwp, op='style_inventory')
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'style_model_unusable')

    def test_unknown_keys_are_refused_before_any_read(self) -> None:
        hwp = styles_document()
        invalid(self, run(hwp, op='style_inventory', kind='paragraph'), hwp, 'kind')


class ApplyParagraphStyle(unittest.TestCase):
    def test_a_paragraph_style_by_name_on_one_paragraph(self) -> None:
        hwp = styles_document()
        hwp.cursor = (0, 0, 0)
        target = row(hwp, '개요 1')
        before = copy.deepcopy(hwp.every_paragraph())
        result = ok(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=target['hash'], paragraph_index=3, expected_text='바탕 둘째 문단'))
        para = hwp.lists[0][3]
        self.assertEqual(para['style'], 2)
        self.assertEqual(para['shape'], hwp.styles[2]['para'])
        self.assertTrue(all(atom['cs'] == hwp.styles[2]['char'] for atom in para['atoms']))
        self.assertEqual([call[:2] for call in hwp.style_calls()], [('GetDefault', 'Style'), ('Execute', 'Style')])
        self.assertEqual(executes(hwp)[0][2], {'Apply': 2})
        self.assertIsNotNone(executes(hwp)[0][3], 'the style is applied over a selected block of the paragraph')
        after = hwp.every_paragraph()
        self.assertEqual([item for number, item in enumerate(after) if item is not para],
                         [item for number, item in enumerate(before) if number != 3 + 2], 'every other paragraph stays')
        self.assertEqual(result['result']['status'], 'applied')
        self.assertEqual(result['style'], {'index': 2, 'name': '개요 1', 'kind': 'paragraph', 'hash': target['hash']})
        changed = result['result']['paragraphs'][0]
        self.assertEqual({key: changed[key] for key in ('paragraph_index', 'context', 'text', 'style_before', 'style_after', 'characters')},
                         {'paragraph_index': 3, 'context': 'body', 'text': '바탕 둘째 문단', 'style_before': '바탕글', 'style_after': '개요 1', 'characters': 'style'})
        self.assertEqual(changed['after']['paragraph'], target['paragraph'])
        self.assertEqual(result['undo']['native_editing_actions'], 1)
        self.assertEqual(hwp.cursor, (0, 0, 0), 'the caret is given back')
        self.assertIsNone(hwp.selection)
        self.assertEqual(row(hwp, '개요 1')['uses']['total'], 1)

    def test_a_range_of_paragraphs_and_a_table_cell(self) -> None:
        hwp = styles_document()
        result = ok(self, run(hwp, **APPLY, style_name='본문', expected_style_hash=row(hwp, '본문')['hash'], match='범위 첫째', through_match='범위 둘째'))
        self.assertEqual([item['paragraph_index'] for item in result['result']['paragraphs']], [9, 10])
        self.assertEqual([hwp.lists[0][number]['style'] for number in (5, 6)], [1, 1])
        hwp = styles_document()
        result = ok(self, run(hwp, **APPLY, style_name='본문', expected_style_hash=row(hwp, '본문')['hash'], paragraph_index=6, expected_text='셀 둘'))
        self.assertEqual(result['result']['paragraphs'][0]['context'], 'table_cell')
        cell = next(para for para in hwp.every_paragraph() if ''.join(atom.get('c', '') for atom in para['atoms']) == '셀 둘')
        self.assertEqual((cell['style'], cell['shape']), (1, BODY['para']))

    def test_characters_that_keep_their_own_format_are_one_accepted_outcome(self) -> None:
        for model, expected in (('reset', 'style'), ('kept_direct', 'kept_direct')):
            with self.subTest(model=model):
                hwp = styles_document()
                hwp.apply_model = model
                result = ok(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=row(hwp, '개요 1')['hash'], paragraph_index=2,
                                      expected_text='본문 둘째 굵은말 끝'))
                self.assertEqual(result['result']['paragraphs'][0]['characters'], expected)
                bold = [atom['cs'] for atom in hwp.lists[0][2]['atoms'] if atom.get('c') == '굵']
                self.assertEqual(bold, [hwp.styles[2]['char'] if model == 'reset' else char(Bold=True)])

    def test_a_paragraph_already_in_the_style_with_its_definitions_issues_nothing(self) -> None:
        hwp = styles_document()
        result = ok(self, run(hwp, **APPLY, style_name='본문', expected_style_hash=row(hwp, '본문')['hash'], paragraph_index=1, expected_text='본문 첫 문단'))
        self.assertEqual((result['result']['status'], result['result']['changed']), ('already_set', False))
        self.assertEqual(executes(hwp), [])


class ApplyCharacterStyle(unittest.TestCase):
    def test_a_character_style_on_an_exact_range(self) -> None:
        hwp = styles_document()
        emphasis = row(hwp, '강조')
        result = ok(self, run(hwp, **APPLY, style_name='강조', expected_style_hash=emphasis['hash'], match='첫 문단'))
        props = [(atom['c'], atom['cs']) for atom in hwp.lists[0][1]['atoms'] if 'c' in atom]
        self.assertEqual([text for text, cs in props if cs == hwp.styles[3]['char']], list('첫 문단'))
        self.assertEqual([text for text, cs in props if cs == char()], list('본문 '))
        self.assertEqual(hwp.lists[0][1]['style'], 1, 'the paragraph keeps its own style')
        self.assertEqual(hwp.lists[0][1]['shape'], BODY['para'])
        self.assertEqual(executes(hwp)[0][2], {'Apply': 3})
        segments = result['result']['paragraphs'][0]['after']['segments']
        self.assertEqual([(item['text'], item['bold']) for item in segments], [('첫 문단', True)])
        self.assertEqual(result['style']['kind'], 'character')

    def test_a_range_reaching_a_number_mark_it_would_change_is_refused(self) -> None:
        hwp = styles_document()
        refused(self, run(hwp, **APPLY, style_name='강조', expected_style_hash=row(hwp, '강조')['hash'], match='꼬리'), 'target_changes_number_mark', hwp)


class ApplyRefusals(unittest.TestCase):
    def test_requests_that_cannot_be_bound_are_refused_before_any_write(self) -> None:
        cases = [
            ({'style_name': '없는 스타일'}, 'style_not_found'),
            ({'style_name': '본문', 'expected_style_hash': 'sha256:' + '0' * 24}, 'style_hash_mismatch'),
            ({'style_name': '본문', 'match': '없는 문구'}, 'target_not_found'),
            ({'style_name': '본문', 'match': '머리 본문'}, 'target_in_header_footer'),
            ({'style_name': '본문', 'paragraph_index': 3, 'expected_text': '다른 글'}, 'target_text_mismatch'),
        ]
        for fields, code in cases:
            with self.subTest(code=code):
                hwp = styles_document()
                hwp.cursor = (0, 0, 1)
                bound = row(hwp, '본문')['hash'] if fields['style_name'] == '본문' else 'sha256:' + 'a' * 24
                request = {'expected_style_hash': bound, 'match': '바탕 둘째', **fields}
                if 'paragraph_index' in fields:
                    request.pop('match')
                refused(self, run(hwp, **APPLY, **request), code, hwp, ((0, 0, 1), None))

    def test_a_name_held_by_two_styles_is_refused(self) -> None:
        hwp = StyleHwp([paragraph('하나')], styles=default_styles() + [style('본문', eng='Body copy')])
        refused(self, run(hwp, **APPLY, style_name='본문', expected_style_hash='sha256:' + 'a' * 24, match='하나'), 'style_name_ambiguous', hwp)

    def test_defaults_that_do_not_read_are_a_clean_refusal(self) -> None:
        hwp = styles_document()
        bound = row(hwp, '본문')['hash']
        hwp.defaults_result = False
        refused(self, run(hwp, **APPLY, style_name='본문', expected_style_hash=bound, match='바탕 둘째'), 'style_action_unavailable', hwp)

    def test_malformed_requests_are_input_errors(self) -> None:
        cases = [
            ({'style_name': '본문', 'expected_style_hash': 'sha256:xyz', 'match': '바탕'}, 'expected_style_hash'),
            ({'style_name': '', 'expected_style_hash': 'sha256:' + 'a' * 24, 'match': '바탕'}, 'style_name'),
            ({'style_name': '본문', 'expected_style_hash': 'sha256:' + 'a' * 24}, 'match'),
            ({'style_name': '본문', 'expected_style_hash': 'sha256:' + 'a' * 24, 'match': '바탕', 'bold': True}, 'bold'),
        ]
        for fields, text in cases:
            with self.subTest(text=text):
                hwp = styles_document()
                invalid(self, run(hwp, **APPLY, **fields), hwp, text)
        hwp = styles_document()
        invalid(self, run(hwp, op='style_apply_exact', expected_page=1, style_name='본문', expected_style_hash='sha256:' + 'a' * 24, match='바탕'), hwp, 'confirm_layout')


class ApplyAdversaries(unittest.TestCase):
    def test_anything_beyond_the_requested_style_is_an_unknown_outcome(self) -> None:
        def header(owner):
            return next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == 'head')['paras'][0]

        adversaries = {
            'other_text': lambda owner: owner.lists[0][0]['atoms'].append({'c': '!', 'cs': char()}),
            'twin_style_index': lambda owner: owner.lists[0][0].update(style=5),
            'other_style_definition': lambda owner: owner.styles[1].update(char=char(Height=1100)),
            'other_style_name': lambda owner: owner.styles[1].update(name='본문 바뀜'),
            'target_other_format': lambda owner: owner.lists[0][2]['atoms'][0].update(cs=char(Italic=True)),
            'target_text': lambda owner: owner.lists[0][2]['atoms'].append({'c': '!', 'cs': char()}),
            'other_instid': lambda owner: owner.lists[0][3].update(p_inst='5'),
            'header_text': lambda owner: header(owner)['atoms'].append({'c': '!', 'cs': char()}),
            'control_added': lambda owner: owner.lists[0][3]['atoms'].append({'cs': char(), **bookmark('새것')}),
            'page_break': lambda owner: owner.lists[0][2].update(p_page_break=True),
        }
        for name, adversary in adversaries.items():
            with self.subTest(adversary=name):
                hwp = twin_document()
                bound = row(hwp, '개요 1')['hash']
                hwp.after_execute = adversary
                step = held(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=2, expected_text='바탕 둘째 문단'))
                self.assertEqual(len(executes(hwp)), 1, 'the action ran, so the proof itself caught the change')
                self.assertIn('not as requested', step['outcome_detail'])

    def test_a_character_style_beyond_its_range_or_a_header_frame_change_is_an_unknown_outcome(self) -> None:
        def area_atom(owner):
            return next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == 'head')

        for name, adversary in (('beyond_the_range', lambda owner: [atom.update(cs=dict(owner.styles[3]['char'])) for atom in owner.lists[0][1]['atoms']]),
                                ('other_paragraph', lambda owner: [atom.update(cs=dict(owner.styles[3]['char'])) for atom in owner.lists[0][3]['atoms']]),
                                ('header_frame', lambda owner: area_atom(owner).update(apply='Odd')),
                                ('header_series', lambda owner: area_atom(owner).update(series=7))):
            with self.subTest(adversary=name):
                hwp = styles_document()
                bound = row(hwp, '강조')['hash']
                hwp.after_execute = adversary
                step = held(self, run(hwp, **APPLY, style_name='강조', expected_style_hash=bound, match='첫 문단'))
                self.assertIn('not as requested', step['outcome_detail'])

    def test_an_action_that_does_not_confirm_or_raises_is_an_unknown_outcome(self) -> None:
        for name, change in (('false', {'execute_result': False}), ('none', {'execute_result': None}), ('raises', {'execute_raises': RuntimeError('COM')})):
            with self.subTest(result=name):
                hwp = styles_document()
                bound = row(hwp, '개요 1')['hash']
                for key, value in change.items():
                    setattr(hwp, key, value)
                held(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=3, expected_text='바탕 둘째 문단'))


def created(hwp: StyleHwp, **fields) -> dict:
    listing = inventory(hwp)
    based = next(item for item in listing['styles'] if item['name'] == fields.get('based_on', '본문'))
    request = {'based_on': '본문', 'expected_based_on_hash': based['hash'], 'expected_styles_hash': listing['styles_hash'], **fields}
    return run(hwp, **CREATE, **request)


class Create(unittest.TestCase):
    def test_a_paragraph_style_from_an_existing_one_with_requested_properties(self) -> None:
        hwp = styles_document()
        hwp.cursor = (0, 3, 2)
        before = copy.deepcopy(hwp.every_paragraph())
        result = ok(self, created(hwp, name='강조 본문', kind='paragraph', size_pt=14, bold=True))
        new = hwp.styles[2]
        self.assertEqual(len(hwp.styles), 6)
        # In slot 2, right after the base 본문 (the fake does what C04 measured); with next_style omitted it names that slot, itself.
        self.assertEqual((new['name'], new['eng'], new['type'], new['next'], new['lock']), ('강조 본문', '강조 본문', 'Para', 2, 0))
        self.assertEqual(new['char'], char(Height=1400, Bold=True))
        self.assertEqual(new['para'], shape(Left=3000), "the base's paragraph shape")
        self.assertEqual(hwp.every_paragraph(), before, 'no paragraph changes, the one the caret stood in included')
        self.assertEqual([call[:2] for call in hwp.style_calls()], [('GetDefault', 'StyleAddNew'), ('Execute', 'StyleAddNew')])
        self.assertEqual(hwp.style_calls()[0][2], 'legacy', 'the defaults are read into the set the action itself created')
        self.assertEqual(executes(hwp)[0][2], {'Type': 0, 'NameLocal': '강조 본문', 'NameEng': '강조 본문', 'Next': 2, 'LockForm': 0},
                         'the names and next style written over the base style\'s own defaults')
        self.assertEqual(result['result']['native_actions'], ['Execute:StyleAddNew'])
        stand = [call for call in hwp.calls if call[0] == 'set_pos']
        self.assertIn(('set_pos', 0, 1, 0), stand, 'the caret stood in a plain use of the base style')
        self.assertEqual(hwp.cursor, (0, 3, 2), 'and is given back')
        self.assertEqual(result['style']['index'], 2)
        listed = row(hwp, '강조 본문')
        self.assertEqual(result['style']['hash'], listed['hash'])
        self.assertEqual(listed['uses']['total'], 0)
        self.assertEqual(listed['character']['size_pt'], 14.0)
        self.assertEqual(result['result']['status'], 'created')
        self.assertEqual(result['based_on']['name'], '본문')

    def test_a_character_style_is_refused_and_an_explicit_next_style_passes(self) -> None:
        """StyleAddNew's documented example creates a style without a kind; that it can create a character style is not shown."""
        hwp = styles_document()
        step = refused(self, created(hwp, name='빨강', english_name='Red', kind='character', based_on='바탕글', text_color='#FF0000', italic=True),
                       'style_kind_unsupported', hwp)
        self.assertIn('character style', step['refusal_message'])
        self.assertEqual((len(hwp.styles), hwp.style_calls()), (5, []))
        hwp = styles_document()
        ok(self, created(hwp, name='소제목', kind='paragraph', next_style='본문'))
        self.assertEqual((hwp.styles[2]['name'], hwp.styles[2]['next'], hwp.styles[2]['char'], hwp.styles[2]['para']), ('소제목', 1, BODY['char'], BODY['para']))

    def test_requests_that_cannot_be_bound_are_refused_before_any_write(self) -> None:
        cases = [
            ({'name': '본문', 'kind': 'paragraph'}, 'style_name_taken'),
            ({'name': '새것', 'kind': 'paragraph', 'based_on': '없는 스타일'}, 'style_not_found'),
            ({'name': '새것', 'kind': 'paragraph', 'based_on': '강조'}, 'style_kind_unsupported'),
            ({'name': '새것', 'kind': 'paragraph', 'expected_based_on_hash': 'sha256:' + '0' * 24}, 'style_hash_mismatch'),
            ({'name': '새것', 'kind': 'paragraph', 'expected_styles_hash': 'sha256:' + '0' * 24}, 'styles_hash_mismatch'),
            ({'name': '새것', 'kind': 'paragraph', 'based_on': '개요 1'}, 'style_without_plain_body_use'),
            ({'name': '새것', 'kind': 'paragraph', 'next_style': '없는 스타일'}, 'style_not_found'),
        ]
        for fields, code in cases:
            with self.subTest(code=code):
                hwp = styles_document()
                hwp.cursor = (0, 0, 1)
                listing = inventory(hwp)
                rows = {item['name']: item for item in listing['styles']}
                based = rows.get(fields.get('based_on', '본문'), {'hash': 'sha256:' + 'a' * 24})
                request = {'based_on': '본문', 'expected_based_on_hash': based['hash'], 'expected_styles_hash': listing['styles_hash'], **fields}
                refused(self, run(hwp, **CREATE, **request), code, hwp, ((0, 0, 1), None))
                self.assertEqual(len(hwp.styles), 5)

    def test_defaults_without_a_character_shape_refuse_character_properties_cleanly(self) -> None:
        hwp = styles_document()
        hwp.cursor = (0, 3, 2)
        hwp.legacy_subsets = False  # GetDefault leaves no CharShape sub-set: the base's character shape is not there to change.
        step = refused(self, created(hwp, name='새것', kind='paragraph', size_pt=14), 'style_variant_unsupported', hwp, ((0, 3, 2), None))
        self.assertEqual([call[:2] for call in hwp.style_calls()], [('GetDefault', 'StyleAddNew')])
        self.assertIn('character shape', step['refusal_message'])
        self.assertEqual(len(hwp.styles), 5)

    def test_paragraph_properties_are_refused_before_any_native_call(self) -> None:
        hwp = styles_document()
        step = refused(self, created(hwp, name='새것', kind='paragraph', align='center'), 'style_variant_unsupported', hwp)
        self.assertIn('paragraph properties', step['refusal_message'])
        self.assertEqual(hwp.style_calls(), [])

    def test_malformed_requests_are_input_errors(self) -> None:
        bad = 'sha256:' + 'a' * 24
        base = {'based_on': '본문', 'expected_based_on_hash': bad, 'expected_styles_hash': bad}
        cases = [
            ({'name': '새것', 'kind': 'character', 'align': 'center'}, 'align'),
            ({'name': ' 새것', 'kind': 'paragraph'}, 'name'),
            ({'name': '새\x01것', 'kind': 'paragraph'}, 'name'),
            ({'name': 'x' * 65, 'kind': 'paragraph'}, 'name'),
            ({'name': '새것\ud800', 'kind': 'paragraph'}, 'name'),
            ({'name': '새것', 'kind': 'table'}, 'kind'),
            ({'name': '새것', 'kind': 'character', 'next_style': '본문'}, 'next_style'),
            ({'name': '새것', 'kind': 'paragraph', 'size_pt': 0}, 'size_pt'),
        ]
        for fields, text in cases:
            with self.subTest(text=text, fields=fields):
                hwp = styles_document()
                invalid(self, run(hwp, **CREATE, **base, **fields), hwp, text)

    def test_anything_beyond_one_new_style_is_an_unknown_outcome(self) -> None:
        adversaries = {
            # Inserted at 1, before the base 본문, not in the slot after it the set's Next was planned for: Next 2 then names 본문.
            'inserted_before_the_base': {'add_at': 1},
            'caret_paragraph_restyled': {'add_restyles_caret': True},
            'current_style_renamed_instead': {'add_renames_current': True},
            'other_style_changed': {'after_execute': lambda owner: owner.styles[0].update(char=char(Height=1100))},
            'new_style_other_definition': {'after_execute': lambda owner: next(item for item in owner.styles if item['name'] == '새것').update(char=char(Height=1300))},
            'paragraph_changed': {'after_execute': lambda owner: owner.lists[0][3]['atoms'].append({'c': '!', 'cs': char()})},
        }
        for name, change in adversaries.items():
            with self.subTest(adversary=name):
                hwp = styles_document()
                for key, value in change.items():
                    setattr(hwp, key, value)
                held(self, created(hwp, name='새것', kind='paragraph', bold=True))
                self.assertEqual(len(executes(hwp)), 1)


def updated(hwp: StyleHwp, name: str = '본문', **fields) -> dict:
    found = row(hwp, name)
    request = {'style_name': name, 'expected_style_hash': found['hash'], 'expected_uses': found['uses'].get('total', 0),
               'expected_uses_hash': found['uses'].get('hash', 'sha256:' + '0' * 24), 'all_uses': True, **fields}
    return run(hwp, **UPDATE, **request)


class Update(unittest.TestCase):
    def test_every_use_takes_the_changed_definition_and_nothing_else_changes(self) -> None:
        hwp = plain_document()
        hwp.cursor = (0, 4, 1)
        others = copy.deepcopy([flat(item) for item in hwp.every_paragraph() if item.get('style', 0) != 1])
        result = ok(self, updated(hwp, size_pt=11))
        self.assertEqual(hwp.styles[1]['char'], char(Height=1100))
        self.assertEqual(hwp.styles[1]['para'], shape(Left=3000))
        self.assertEqual((hwp.styles[1]['name'], hwp.styles[1]['eng']), ('본문', 'Body'))
        self.assertEqual(hwp.styles[0], default_styles()[0] | {'next': 0})
        uses = [item for item in hwp.every_paragraph() if item.get('style', 0) == 1]
        self.assertEqual(len(uses), 4)
        for item in uses:
            self.assertEqual(item['shape'], hwp.styles[1]['para'])
            self.assertTrue(all(atom['cs'] == hwp.styles[1]['char'] for atom in item['atoms']))
        self.assertEqual([flat(item) for item in hwp.every_paragraph() if item.get('style', 0) != 1], others)
        self.assertEqual([call[:2] for call in hwp.style_calls()], [('GetDefault', 'StyleDirectEdit'), ('Execute', 'StyleDirectEdit')])
        self.assertEqual(executes(hwp)[0][2]['NameLocal'], '본문', 'NameLocal selects the style to change')
        self.assertEqual(result['result']['native_actions'], ['Execute:StyleDirectEdit'])
        self.assertEqual(hwp.cursor, (0, 4, 1))
        self.assertEqual(result['result']['status'], 'updated')
        self.assertEqual(result['result']['affected']['total'], 4)
        self.assertEqual(sorted(item['context'] for item in result['result']['affected']['rows']), ['body', 'body', 'header', 'table_cell'])
        self.assertEqual(result['style_before']['character']['size_pt'], 10.0)
        self.assertEqual(result['style_after']['character']['size_pt'], 11.0)
        self.assertEqual(result['style_after']['hash'], row(hwp, '본문')['hash'])

    def test_a_rename_is_refused_before_any_native_call(self) -> None:
        """StyleDirectEdit's NameLocal selects the style to change; no documented mechanism renames a style."""
        for fields in ({'new_name': '본문 새이름'}, {'new_english_name': 'Body New'}, {'new_name': '바탕글', 'size_pt': 11}):
            with self.subTest(fields=fields):
                hwp = plain_document()
                hwp.cursor = (0, 0, 1)
                step = refused(self, updated(hwp, **fields), 'style_rename_unsupported', hwp, ((0, 0, 1), None))
                self.assertIn('rename', step['refusal_message'])
                self.assertEqual((hwp.styles[1]['name'], hwp.styles[1]['eng'], hwp.style_calls()), ('본문', 'Body', []))

    def test_paragraph_properties_are_refused_before_any_native_call(self) -> None:
        hwp = plain_document()
        step = refused(self, updated(hwp, size_pt=11, line_spacing_type='percent', line_spacing_value=180), 'style_variant_unsupported', hwp)
        self.assertIn('paragraph properties', step['refusal_message'])
        self.assertEqual(hwp.style_calls(), [])

    def test_requests_that_cannot_be_bound_are_refused_before_any_write(self) -> None:
        cases = [
            (plain_document, {'size_pt': 11, 'all_uses': False}, 'style_uses_unconfirmed'),
            (plain_document, {'size_pt': 11, 'expected_uses': 3}, 'style_uses_mismatch'),
            (plain_document, {'size_pt': 11, 'expected_uses_hash': 'sha256:' + '0' * 24}, 'style_uses_mismatch'),
            (plain_document, {'size_pt': 11, 'expected_style_hash': 'sha256:' + '0' * 24}, 'style_hash_mismatch'),
            (styles_document, {'size_pt': 11}, 'style_use_has_own_format'),
        ]
        for build, fields, code in cases:
            with self.subTest(code=code, fields=fields):
                hwp = build()
                hwp.cursor = (0, 0, 1)
                refused(self, updated(hwp, **fields), code, hwp, ((0, 0, 1), None))
        hwp = styles_document()
        refused(self, updated(hwp, '강조', bold=False), 'style_kind_unsupported', hwp)
        hwp = styles_document()
        refused(self, updated(hwp, '개요 1', size_pt=13), 'style_without_plain_body_use', hwp)
        header_only = StyleHwp([paragraph('하나')], areas=[area('header', styled('머리', 1, para_shape=BODY['para']))])
        refused(self, updated(header_only, size_pt=11), 'style_without_plain_body_use', header_only)

    def test_malformed_requests_are_input_errors(self) -> None:
        bad = 'sha256:' + 'a' * 24
        base = {'style_name': '본문', 'expected_style_hash': bad, 'expected_uses': 1, 'expected_uses_hash': bad, 'all_uses': True}
        cases = [
            ({}, 'at least one'),
            ({'size_pt': 11, 'expected_uses': -1}, 'expected_uses'),
            ({'size_pt': 11, 'all_uses': 'yes'}, 'all_uses'),
            ({'new_name': '새\x00이름'}, 'new_name'),
            ({'size_pt': 11, 'based_on': '바탕글'}, 'based_on'),
        ]
        for fields, text in cases:
            with self.subTest(text=text):
                hwp = plain_document()
                invalid(self, run(hwp, **UPDATE, **{**base, **fields}), hwp, text)

    def test_anything_beyond_the_bound_uses_and_the_definition_is_an_unknown_outcome(self) -> None:
        def twin_plain() -> StyleHwp:
            styles = default_styles() + [style('본문 쌍둥이', eng='Body twin', para=BODY['para'])]
            body = [paragraph('제목'), styled('본문 첫 문단', 1, para_shape=BODY['para']), styled('쌍둥이 문단', 5, para_shape=BODY['para']), paragraph('끝')]
            return StyleHwp(body, styles=styles)

        adversaries = {
            'shared_definition_changed_in_place': (twin_plain, {'change_in_place_shared': True}),
            'another_style_changed_instead': (plain_document, {'change_targets': 0}),
            'uses_not_changed': (plain_document, {'change_skips_uses': True}),
            'unrelated_text': (plain_document, {'after_execute': lambda owner: owner.lists[0][0]['atoms'].append({'c': '!', 'cs': char()})}),
            'style_order': (plain_document, {'after_execute': lambda owner: owner.styles.reverse()}),
            'other_style': (plain_document, {'after_execute': lambda owner: owner.styles[2].update(name='개요 하나')}),
            'next_style': (plain_document, {'after_execute': lambda owner: owner.styles[1].update(next=0)}),
            'use_text': (plain_document, {'after_execute': lambda owner: owner.lists[0][1]['atoms'].append({'c': '!', 'cs': owner.styles[1]['char']})}),
        }
        for name, (build, change) in adversaries.items():
            with self.subTest(adversary=name):
                hwp = build()
                bound = row(hwp, '본문')
                for key, value in change.items():
                    setattr(hwp, key, value)
                found = {'style_name': '본문', 'expected_style_hash': bound['hash'], 'expected_uses': bound['uses']['total'],
                         'expected_uses_hash': bound['uses']['hash'], 'all_uses': True}
                held(self, run(hwp, **UPDATE, **found, size_pt=11))
                self.assertEqual(len(executes(hwp)), 1)


class History(unittest.TestCase):
    def step(self, owned, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def test_undo_after_a_style_edit_is_refused_before_any_native_history_call(self) -> None:
        from app.local_cli_service import LocalCliUndoRefused
        hwp = styles_document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        listed = self.step(owned, op='style_inventory', name='개요 1')['steps'][0]['result']['styles'][0]
        outcome = self.step(owned, **APPLY, style_name='개요 1', expected_style_hash=listed['hash'], paragraph_index=3, expected_text='바탕 둘째 문단')
        self.assertTrue(outcome['ok'], outcome)
        self.assertTrue(outcome['dirty'])
        self.assertTrue(owned.session._normal_close_requires_save)  # noqa: SLF001
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}), \
                self.assertRaises(LocalCliUndoRefused) as refused_undo:
            owned.service.undo()
        self.assertEqual(refused_undo.exception.code, 'undo_not_counted')
        self.assertNotIn(('Run', 'Undo'), hwp.calls)

    def test_undo_after_a_create_or_a_change_is_refused_before_any_native_history_call(self) -> None:
        from app.local_cli_service import LocalCliUndoRefused
        for op in ('create', 'update'):
            with self.subTest(op=op):
                hwp = styles_document() if op == 'create' else plain_document()
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                listing = self.step(owned, op='style_inventory')['steps'][0]['result']
                rows = {item['name']: item for item in listing['styles']}
                if op == 'create':
                    request = {**CREATE, 'name': '새것', 'kind': 'paragraph', 'based_on': '본문', 'expected_based_on_hash': rows['본문']['hash'],
                               'expected_styles_hash': listing['styles_hash'], 'size_pt': 12}
                else:
                    found = rows['본문']
                    request = {**UPDATE, 'style_name': '본문', 'expected_style_hash': found['hash'], 'expected_uses': found['uses']['total'],
                               'expected_uses_hash': found['uses']['hash'], 'all_uses': True, 'size_pt': 12}
                outcome = self.step(owned, **request)
                self.assertTrue(outcome['ok'] and outcome['dirty'], outcome)
                with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                        patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}), \
                        self.assertRaises(LocalCliUndoRefused) as refused_undo:
                    owned.service.undo()
                self.assertEqual(refused_undo.exception.code, 'undo_not_counted')
                self.assertNotIn(('Run', 'Undo'), hwp.calls)

    def test_the_inventory_is_not_an_edit_and_a_raising_read_holds_the_session(self) -> None:
        hwp = styles_document()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        listed = self.step(owned, op='style_inventory')
        self.assertTrue(listed['ok'], listed)
        self.assertFalse(listed['dirty'])
        bound = listed['steps'][0]['result']['styles'][2]['hash']

        def broken(fmt='UNICODE', option=''):
            hwp.calls.append(('get_text_file', fmt, option))
            raise RuntimeError('GetTextFile failed')
        hwp.get_text_file = broken
        outcome = self.step(owned, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=3, expected_text='바탕 둘째 문단')
        step = outcome['steps'][0]
        self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
        self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(executes(hwp), [])


if __name__ == '__main__':
    unittest.main()
