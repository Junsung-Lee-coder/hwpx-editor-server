"""Named styles: the style list is proven by identity, so a legitimate renumbering of style ids is no change, and a create's
next style is planned for where StyleAddNew puts the new style.

Before Source07 the proof of a create expected the new style at the end of the list and compared the old styles index by index,
with each style's raw NextStyle index among its settings and each paragraph's raw P/@Style index. C02 on Source06 held after
StyleAddNew's Execute answered True: the proof read the document once and listed styles 1 to 6 as changed (the first six of its
problems); neither that text nor its model was kept, so nothing here is C02's after-state.

C04 on Source07 measured it with the identity proof: based on 바탕글 (index 0), the new style was at index 1, all 23 old styles kept
their identity, next styles included, and the set's Next 1, 본문's index before the action, named the new style itself. tests.native_styles_fakes now does the same by default: the new style right after the style the caret
paragraph shows, the old references renumbered and the new Next kept as written, an index of the list after the insertion. That is
one native observation on one host with the base at index 0; the other bases below are the fake's, not Hancom's.

A style is identified by its name and kind, and each must be unique before the edit; everything else of it (English name, lock,
language, the other settings, its character and paragraph definitions by their whole content, fonts with TYPEINFO included, and its
next style by name) must be as before. Paragraphs name a style by identity too. Through the real owned service and its public
projection, the runtime journal and the CLI normaliser. Not Hancom evidence.
"""
from __future__ import annotations

import copy
import json
import re
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt, local_cli_service as svc, style_rules
from app.command_packages.runtime import get_command_package_registry
from local_cli_v1.output_parser import normalize_command_bundle
from tests import test_cli_failure_recovery as recovery
from tests.native_format_fakes import bookmark, char, control, paragraph, picture, shape, table
from tests.native_styles_fakes import StyleHwp, area, style, styled
from tests.test_native_styles import APPLY, CREATE, UPDATE, executes, flat, held, inventory, ok, refused, run
from tests.test_native_styles_copy import FIELD_BEGIN, FIELD_END
from tests.test_native_styles_repairs import DOTUM, TYPEINFO

NAME = '검토용 문단'
FOOTNOTE = char(Height=900, TextColor=255)
BODY_TEXTS = ('문서 제목', '본문 첫 문단', '굵은 말', '개요 문단', '기존성명', '셀 본문', '돋움 글자', '각주 같은 문단', '그림 앞', '마지막 문단', '캡션 문단',
              '머리 문구', '꼬리 문구')


def seed_styles() -> list[dict]:
    """The 23 built-in styles of the native seed by name, English name and kind, with a Next graph that is not only self loops:
    본문 and 개요 1 name each other, 차례 1 to 3 form a cycle, 캡션 names 본문 and the character styles 바탕글. 각주 and 미주 have
    the very same definitions, so only their names tell them apart."""
    def outline(level: int) -> dict:
        return shape(HeadingType='Outline', Level=min(level - 1, 6), Left=1000 * level)

    styles = [style('바탕글', eng='Normal'), style('본문', eng='Body', para=shape(Left=3000))]
    styles += [style(f'개요 {n}', eng=f'Outline {n}', chars=char(Font=DOTUM) if n == 1 else None, para=outline(n)) for n in range(1, 11)]
    styles += [style('쪽 번호', eng='Page Number', kind='Char', chars=char(Height=900)),
               style('줄 번호', eng='Line Number', kind='Char', chars=char(Height=800, Italic=True)),
               style('머리말', eng='Header', chars=char(Height=900), para=shape(Left=500)),
               style('각주', eng='Footnote', chars=FOOTNOTE, para=shape(Indent=-1000)),
               style('미주', eng='Endnote', chars=FOOTNOTE, para=shape(Indent=-1000)),
               style('메모', eng='Memo', chars=char(Height=900, Italic=True), para=shape(Left=200)),
               style('차례 제목', eng='TOC Heading', chars=char(Height=1600, Bold=True), para=shape(Align='Center')),
               style('차례 1', eng='TOC 1', para=shape(Left=1000, Right=100)),
               style('차례 2', eng='TOC 2', para=shape(Left=2000, Right=100)),
               style('차례 3', eng='TOC 3', para=shape(Left=3000, Right=100)),
               style('캡션', eng='Caption', para=shape(Align='Center', Right=200))]
    at = {item['name']: number for number, item in enumerate(styles)}
    graph = {'본문': '개요 1', '개요 1': '본문', '캡션': '본문', '차례 1': '차례 2', '차례 2': '차례 3', '차례 3': '차례 1', '쪽 번호': '바탕글', '줄 번호': '바탕글'}
    for item in styles:
        item['next'] = at[graph.get(item['name'], item['name'])]
    return styles


def seed_document(styles: list[dict] | None = None) -> StyleHwp:
    """Styles used in the body, a table cell and the header (머리말, which names itself next); a word and a run with formats of their
    own; a click-here field and a bookmark; a picture; and a font with TYPEINFO that 개요 1 and direct-format text use."""
    styles = styles if styles is not None else seed_styles()
    at = {item['name']: number for number, item in enumerate(styles)}

    def use(runs, name: str) -> dict:
        return styled(runs, at[name], para_shape=styles[at[name]]['para'])

    body = [
        paragraph('문서 제목'),
        use('본문 첫 문단', '본문'),
        use(('본문 ', ('굵은 말', char(Bold=True)), ' 끝'), '본문'),
        use((('개요 문단', char(Font=DOTUM)),), '개요 1'),
        use(('성명: ', control(FIELD_BEGIN, '%clk'), '기존성명', control(FIELD_END, ''), ' ', bookmark('표시')), '본문'),
        paragraph(table(use('셀 본문', '본문'), paragraph('셀 둘'), paragraph('셀 셋'), paragraph('셀 넷'))),
        paragraph('앞 ', ('돋움 글자', char(Font=DOTUM)), ' 뒤'),
        use((('각주 같은 문단', FOOTNOTE),), '각주'),
        paragraph('그림 앞 ', picture(), ' 뒤'),
        paragraph('마지막 문단'),
        use('캡션 문단', '캡션'),
    ]
    areas = [area('header', use((('머리 문구', char(Height=900)),), '머리말')), area('footer', paragraph('꼬리 문구'))]
    hwp = EditedText(body, styles=styles, areas=areas)
    hwp.font_info = {DOTUM: dict(TYPEINFO), '함초롬바탕': dict(TYPEINFO)}
    return hwp


class EditedText(StyleHwp):
    """The fake whose HWPML2X text an adversary may still edit after the action: ``text_edits`` are (pattern, replacement) pairs."""

    text_edits: list[tuple[str, str]] = []

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        text = super().get_text_file(fmt, option)
        if fmt == 'HWPML2X':
            for pattern, replacement in self.text_edits:
                text, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
                assert count == 1, pattern
        return text


def index_of(owner: StyleHwp, name: str) -> int:
    return owner.style_names().index(name)


def renumber(owner: StyleHwp, order: list[int]) -> None:
    """A consistent renumbering: the styles in ``order`` (old indexes), every P/@Style and NextStyle following them."""
    position = {old: new for new, old in enumerate(order)}
    owner.styles[:] = [owner.styles[old] for old in order]
    for item in owner.styles:
        item['next'] = position[item['next']]
    for para in owner.every_paragraph():
        para['style'] = position[para.get('style', 0)]


def delete_style(owner: StyleHwp, name: str) -> None:
    """Remove one unused style, renumbering the references after it."""
    gone = index_of(owner, name)
    del owner.styles[gone]
    for item in owner.styles:
        item['next'] -= item['next'] > gone
    for para in owner.every_paragraph():
        para['style'] = para.get('style', 0) - (para.get('style', 0) > gone)


def created(hwp: StyleHwp, *, at: int | None = None, name: str = NAME, based_on: str = '바탕글', next_style: str | None = '본문', **fields) -> dict:
    hwp.add_at = at
    listing = inventory(hwp)
    rows = {item['name']: item for item in listing['styles']}
    request = {'name': name, 'kind': 'paragraph', 'based_on': based_on, 'expected_based_on_hash': rows[based_on]['hash'],
               'expected_styles_hash': listing['styles_hash'], **fields}
    if next_style is not None:
        request['next_style'] = next_style
    return run(hwp, **CREATE, **request)


def by_name(listing: dict) -> dict[str, dict]:
    """Each listed style without its index, its index-bound hash and its next style's index: what a renumbering may not change."""
    rows = {}
    for item in listing['styles']:
        rows[item['name']] = {**{key: value for key, value in item.items() if key not in {'index', 'hash'}}, 'next_style': item['next_style']['name']}
    return rows


def paragraphs_by_name(hwp: StyleHwp) -> list[dict]:
    names = hwp.style_names()
    return [{**flat(item), 'style': names[item.get('style', 0)]} for item in hwp.every_paragraph()]


def problems(step: dict) -> list[dict]:
    return step.get('style_proof', {}).get('problems', [])


def predicates(step: dict) -> set[str]:
    return {item['predicate'] for item in problems(step)}


class CreateAfterTheBase(unittest.TestCase):
    """The set's Next for the list as StyleAddNew leaves it: the new style in the slot right after the base, the old styles from that
    slot on one further. Every expected value below is worked out by hand from the seed's list (바탕글 0, 본문 1, 개요 1 2, 각주 15,
    미주 16, 캡션 22, the last)."""

    def assert_created(self, based_on: str, next_style: str | None, slot: int, written: int, named: str) -> None:
        hwp = seed_document()
        hwp.cursor = (0, 9, 2)
        listed, document = by_name(inventory(hwp)), paragraphs_by_name(hwp)
        outcome = created(hwp, based_on=based_on, next_style=next_style)
        result = ok(self, outcome)
        after = inventory(hwp)
        self.assertEqual(after['count'], 24)
        rows = by_name(after)
        self.assertEqual({name: item for name, item in rows.items() if name != NAME}, listed,
                         'every old style by name: English name, kind, lock, definitions, numbering, next style by name and uses')
        new = rows[NAME]
        self.assertEqual((new['kind'], new['english_name'], new['next_style'], new['lock_form'], new['uses']['total']), ('paragraph', NAME, named, False, 0))
        self.assertEqual((new['character'], new['paragraph']), (listed[based_on]['character'], listed[based_on]['paragraph']), "the base's definitions")
        self.assertEqual(paragraphs_by_name(hwp), document, 'every paragraph names the same style by name, with its text, field, place marker, picture and formats')
        self.assertEqual(result['style']['index'], slot)
        self.assertEqual(result['style']['next_style'], {'index': index_of(hwp, named), 'name': named})
        new_hash = next(item['hash'] for item in after['styles'] if item['name'] == NAME)
        self.assertEqual(result['next_binding'], {'style_name': NAME, 'expected_style_hash': new_hash, 'expected_styles_hash': after['styles_hash']})
        self.assertEqual(executes(hwp)[0][2]['Next'], written, "the set's Next, an index of the list after the insertion")
        self.assertEqual(hwp.styles[slot]['next'], written, 'kept as written')
        self.assertEqual([call[:2] for call in hwp.style_calls()], [('GetDefault', 'StyleAddNew'), ('Execute', 'StyleAddNew')])
        self.assertEqual(hwp.cursor, (0, 9, 2), 'the caret is given back')
        self.assertEqual(hwp.font_info[DOTUM], TYPEINFO)

    def test_c04s_request_names_본문_in_its_slot_after_the_insertion(self) -> None:
        """C04's own request: 본문, index 1 before, is index 2 once the new style is in slot 1."""
        self.assert_created('바탕글', '본문', 1, 2, '본문')

    def test_without_next_style_the_new_style_names_its_own_slot(self) -> None:
        self.assert_created('바탕글', None, 1, 1, NAME)
        self.assert_created('본문', None, 2, 2, NAME)
        self.assert_created('각주', None, 16, 16, NAME)

    def test_a_next_style_before_the_slot_keeps_its_index(self) -> None:
        self.assert_created('바탕글', '바탕글', 1, 0, '바탕글')
        self.assert_created('본문', '바탕글', 2, 0, '바탕글')
        self.assert_created('본문', '본문', 2, 1, '본문')
        self.assert_created('각주', '본문', 16, 1, '본문')

    def test_a_next_style_at_or_after_the_slot_moves_one_further(self) -> None:
        self.assert_created('본문', '개요 1', 2, 3, '개요 1')
        self.assert_created('개요 1', '개요 2', 3, 4, '개요 2')
        self.assert_created('각주', '미주', 16, 17, '미주')
        self.assert_created('바탕글', '캡션', 1, 23, '캡션')

    def test_a_base_at_the_end_puts_the_new_style_at_the_end(self) -> None:
        self.assert_created('캡션', None, 23, 23, NAME)
        self.assert_created('캡션', '캡션', 23, 22, '캡션')
        self.assert_created('캡션', '본문', 23, 1, '본문')

    def test_a_consistent_renumbering_of_the_old_styles_as_well_passes(self) -> None:
        def shuffle(owner: StyleHwp) -> None:
            order = list(range(len(owner.styles)))
            order[3], order[4] = order[4], order[3]
            order.append(order.pop(index_of(owner, '캡션')))
            renumber(owner, order)

        hwp = seed_document()
        listed = by_name(inventory(hwp))
        hwp.after_execute = shuffle
        result = ok(self, created(hwp))
        rows = by_name(inventory(hwp))
        self.assertEqual({name: item for name, item in rows.items() if name != NAME}, listed)
        self.assertEqual((result['style']['index'], rows[NAME]['next_style']), (1, '본문'))
        self.assertEqual(hwp.style_names()[-1], '캡션')

    def test_a_consistent_renumbering_that_moves_the_new_style_too_passes(self) -> None:
        """A full serialisation in another order moves every style, the new one included, with every reference: no change of identity."""
        hwp = seed_document()
        listed = by_name(inventory(hwp))
        hwp.after_execute = lambda owner: renumber(owner, list(reversed(range(len(owner.styles)))))
        result = ok(self, created(hwp))
        rows = by_name(inventory(hwp))
        self.assertEqual({name: item for name, item in rows.items() if name != NAME}, listed)
        self.assertEqual((result['style']['index'], rows[NAME]['next_style']), (22, '본문'), 'slot 1 of 24, reversed')


class Bindings(unittest.TestCase):
    """The proof needs each style to be one name and kind, and each next style to be in the list, before anything is written."""

    def test_two_styles_of_one_name_and_kind_are_refused_before_any_native_call(self) -> None:
        styles = seed_styles()
        styles[[item['name'] for item in styles].index('개요 9')]['name'] = '개요 8'
        for op in ('create', 'apply', 'update'):
            with self.subTest(op=op):
                hwp = seed_document(copy.deepcopy(styles))
                if op == 'create':
                    outcome = created(hwp)
                elif op == 'apply':
                    bound = next(item for item in inventory(hwp)['styles'] if item['name'] == '개요 1')['hash']
                    outcome = run(hwp, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=0, expected_text='문서 제목')
                else:
                    found = next(item for item in inventory(hwp, name='개요 1')['styles'])
                    outcome = run(hwp, **UPDATE, style_name='개요 1', expected_style_hash=found['hash'], expected_uses=found['uses']['total'],
                                  expected_uses_hash=found['uses']['hash'], all_uses=True, size_pt=11)
                step = refused(self, outcome, 'style_name_ambiguous', hwp)
                self.assertIn('개요 8', step['refusal_message'])
                self.assertEqual(hwp.style_calls(), [])

    def test_a_next_style_outside_the_list_is_refused_before_any_native_call(self) -> None:
        styles = seed_styles()
        styles[5]['next'] = 99
        hwp = seed_document(styles)
        step = refused(self, created(hwp), 'style_model_unusable', hwp)
        self.assertIn('next style', step['refusal_message'])
        self.assertEqual(hwp.style_calls(), [])


class AdversariesAfterARenumbering(unittest.TestCase):
    """Each wrong outcome comes after a legitimate insertion right after the base, and is held by the predicate it breaks."""

    def hold(self, change=None, **knobs) -> dict:
        hwp = seed_document()
        for key, value in knobs.items():
            setattr(hwp, key, value)
        if change is not None:
            hwp.after_execute = change
        step = held(self, created(hwp))
        self.assertEqual(len(executes(hwp)), 1, 'the action ran, so the proof itself caught it')
        proof = step.get('style_proof')
        self.assertIsNotNone(proof, step)
        self.assertEqual((proof['schema'], proof['command'], proof['old_styles']), ('style-proof/v1', 'style_create_exact', 23))
        self.assertLessEqual(len(step['outcome_detail']), 800)
        self.assertTrue(step['outcome_detail'].endswith('nothing was rolled back'), step['outcome_detail'])
        dumped = json.dumps(proof, ensure_ascii=False)
        for text in BODY_TEXTS:
            self.assertNotIn(text, dumped, 'no document text in the facts')
        return step

    def entry(self, step: dict, predicate: str, **fields) -> dict:
        found = [item for item in problems(step) if item['predicate'] == predicate and all(item.get(key) == value for key, value in fields.items())]
        self.assertEqual(len(found), 1, problems(step))
        return found[0]

    def test_an_old_style_whose_definition_drifted(self) -> None:
        step = self.hold(lambda owner: owner.styles[index_of(owner, '본문')].update(char=char(Height=1100)))
        self.assertEqual(self.entry(step, 'old_style_changed', name='본문')['fields'], ['character'])
        self.assertEqual(predicates(step), {'old_style_changed'})
        self.assertIn('본문', step['outcome_detail'])

    def test_a_font_whose_typeinfo_drifted(self) -> None:
        step = self.hold(lambda owner: owner.font_info.update({DOTUM: {**TYPEINFO, 'Weight': '9'}}))
        self.assertEqual(self.entry(step, 'old_style_changed', name='개요 1')['fields'], ['character'])
        self.assertEqual(predicates(step), {'old_style_changed', 'paragraph_changed'})
        self.assertEqual(len([item for item in problems(step) if item['predicate'] == 'paragraph_changed']), 2, 'the 개요 1 use and the direct-format run')

    def test_an_old_style_renamed(self) -> None:
        step = self.hold(lambda owner: owner.styles[index_of(owner, '개요 5')].update(name='개요 오'))
        self.entry(step, 'old_style_missing', name='개요 5')
        self.entry(step, 'style_added', name='개요 오')
        self.assertEqual(predicates(step), {'old_style_missing', 'style_added'})

    def test_an_old_style_deleted_and_another_added_keeps_the_count_and_still_holds(self) -> None:
        def swap(owner: StyleHwp) -> None:
            delete_style(owner, '개요 9')
            owner.styles.append(style('가짜', eng='Fake', next_index=0))

        step = self.hold(swap)
        self.assertEqual(step['style_proof']['new_styles'], 24, 'one more style, as requested in count only')
        self.entry(step, 'old_style_missing', name='개요 9')
        self.entry(step, 'style_added', name='가짜')

    def test_the_requested_style_under_another_name(self) -> None:
        step = self.hold(lambda owner: owner.styles[1].update(name='다른 새 스타일'))
        self.entry(step, 'new_style_missing', name=NAME)
        self.entry(step, 'style_added', name='다른 새 스타일')

    def test_an_old_style_duplicated(self) -> None:
        step = self.hold(lambda owner: owner.styles.append(copy.deepcopy(owner.styles[index_of(owner, '본문')])))
        self.entry(step, 'old_style_duplicated', name='본문')

    def test_the_new_style_with_other_definitions_type_or_lock(self) -> None:
        for name, change, fields in (('character', lambda owner: owner.styles[1].update(char=char(Height=1300)), ['character']),
                                     ('lock', lambda owner: owner.styles[1].update(lock=1), ['lock']),
                                     ('english_name', lambda owner: owner.styles[1].update(eng='Review'), ['english_name'])):
            with self.subTest(wrong=name):
                step = self.hold(change)
                self.assertEqual(self.entry(step, 'new_style_wrong', name=NAME)['fields'], fields)
        step = self.hold(lambda owner: owner.styles[1].update(type='Char', para=None))
        self.assertEqual(self.entry(step, 'new_style_wrong', name=NAME)['fields'], ['kind'])

    def test_the_new_style_naming_itself_as_c04_found_it(self) -> None:
        """C04's own facts: the new style at index 1, its next style itself instead of 본문."""
        step = self.hold(lambda owner: owner.styles[index_of(owner, NAME)].update(next=index_of(owner, NAME)))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': NAME, 'new_index': 1, 'fields': ['next'],
                                                            'expected': '본문', 'found': NAME}])
        self.assertEqual((step['style_proof']['matched'], step['style_proof']['added'], step['style_proof']['problem_count']), (23, {'name': NAME, 'index': 1}, 1))
        self.assertIn(f'next style is {NAME}, not 본문', step['outcome_detail'])

    def test_the_new_style_used_by_a_paragraph(self) -> None:
        step = self.hold(add_restyles_caret=True)
        found = self.entry(step, 'paragraph_style', expected='바탕글')
        self.assertEqual(found['found'], NAME)

    def test_a_paragraph_or_next_style_swapped_to_a_style_that_looks_the_same(self) -> None:
        def paragraph_to_twin(owner: StyleHwp) -> None:
            item = next(para for para in owner.every_paragraph() if para.get('style') == index_of(owner, '각주'))
            item['style'] = index_of(owner, '미주')

        step = self.hold(paragraph_to_twin)
        self.entry(step, 'paragraph_style', expected='각주', found='미주')
        self.assertEqual(predicates(step), {'paragraph_style'})
        step = self.hold(lambda owner: owner.styles[index_of(owner, '각주')].update(next=index_of(owner, '미주')))
        found = self.entry(step, 'old_style_changed', name='각주')
        self.assertEqual((found['fields'], found['expected'], found['found']), (['next'], '각주', '미주'))

    def test_a_next_style_left_outside_the_list(self) -> None:
        step = self.hold(lambda owner: owner.styles[index_of(owner, '개요 10')].update(next=99))
        self.entry(step, 'next_style_dangling', name='개요 10')

    def test_a_paragraph_naming_no_style_of_the_list_holds_with_no_facts(self) -> None:
        hwp = seed_document()
        hwp.after_execute = lambda owner: owner.lists[0][9].update(style=99)
        step = held(self, created(hwp))
        self.assertNotIn('style_proof', step)
        self.assertIn('after the native call was issued', step['outcome_detail'])

    def test_text_field_control_media_and_header_changes(self) -> None:
        def bookmark_renamed(owner: StyleHwp) -> None:
            atom = next(atom for atom in owner.lists[0][4]['atoms'] if 'BOOKMARK' in atom.get('xml', ''))
            atom['xml'] = atom['xml'].replace('표시', '바뀜')

        def field_text(owner: StyleHwp) -> None:
            atoms = owner.lists[0][4]['atoms']
            begin = next(number for number, atom in enumerate(atoms) if atom.get('ctrl') == '%clk')
            atoms[begin + 1]['c'] = '타'

        def media(owner: StyleHwp) -> None:
            owner.text_edits = [(r'(<BINDATA [^>]*>)(.)', lambda match: match.group(1) + ('B' if match.group(2) != 'B' else 'C'))]

        def header_run(owner: StyleHwp) -> None:
            owner.text_edits = [(r'(<HEADER [^>]*><PARALIST [^>]*><P [^>]*>)', r'\1<TEXT CharShape="0"/>')]

        def header_style(owner: StyleHwp) -> None:
            atom = next(atom for atom in owner.lists[0][0]['atoms'] if atom.get('ctrl') == 'head')
            atom['paras'][0]['style'] = 0

        cases = (('text', lambda owner: owner.lists[0][9]['atoms'].append({'c': '!', 'cs': char()}), 'paragraph_changed', {}),
                 ('field', field_text, 'paragraph_changed', {}),
                 ('bookmark', bookmark_renamed, 'paragraph_changed', {}),
                 ('media', media, 'document_changed', {'field': 'media_sha256'}),
                 ('header_run', header_run, 'document_changed', {'field': 'header_footer'}),
                 ('header_style', header_style, 'paragraph_style', {'expected': '머리말', 'found': '바탕글'}))
        for name, change, predicate, fields in cases:
            with self.subTest(change=name):
                self.entry(self.hold(change), predicate, **fields)

    def test_every_problem_is_counted_and_the_facts_stay_bounded(self) -> None:
        def everything(owner: StyleHwp) -> None:
            for item in owner.styles:
                if item['name'] != NAME:
                    item['char'] = dict(item['char'], Height=item['char']['Height'] + 7)

        step = self.hold(everything)
        proof = step['style_proof']
        self.assertEqual((proof['problem_count'], len(proof['problems']), proof['problems_cut']), (23, 20, True), 'all 23 old styles changed; 20 kept')
        self.assertEqual({item['predicate'] for item in proof['problems']}, {'old_style_changed'})
        self.assertIn('more', step['outcome_detail'])


class AnotherBoundary(unittest.TestCase):
    """The set's Next is planned for the slot after the base. If StyleAddNew puts the new style anywhere else, a Next planned for a
    style at or after that slot, or for the new style itself, names another style, and the create holds by that predicate."""

    def test_at_the_end_c04s_next_names_another_style(self) -> None:
        hwp = seed_document()
        step = held(self, created(hwp, at=23))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': NAME, 'new_index': 23, 'fields': ['next'],
                                                            'expected': '본문', 'found': '개요 1'}])
        self.assertEqual(step['style_proof']['matched'], 23, 'every old style is still itself')

    def test_without_next_style_the_planned_slot_is_another_style(self) -> None:
        hwp = seed_document()
        step = held(self, created(hwp, at=23, next_style=None))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': NAME, 'new_index': 23, 'fields': ['next'],
                                                            'expected': NAME, 'found': '본문'}])

    def test_before_the_base_a_next_style_planned_before_the_slot_names_the_new_style(self) -> None:
        """Inserted at 0, before 바탕글: Next 0, planned for 바탕글 before the slot, now names the new style, and the create holds."""
        hwp = seed_document()
        step = held(self, created(hwp, at=0, next_style='바탕글'))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': NAME, 'new_index': 0, 'fields': ['next'],
                                                            'expected': '바탕글', 'found': NAME}])


class ApplyAndUpdateUnderARenumbering(unittest.TestCase):
    def test_apply_proves_the_target_by_the_style_s_identity(self) -> None:
        hwp = seed_document()
        bound = next(item for item in inventory(hwp)['styles'] if item['name'] == '개요 1')['hash']
        last = next(use['paragraph_index'] for use in inventory(hwp, name='바탕글')['styles'][0]['uses']['rows'] if use['text'] == '마지막 문단')
        hwp.after_execute = lambda owner: renumber(owner, list(reversed(range(len(owner.styles)))))
        result = ok(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=last, expected_text='마지막 문단'))
        self.assertEqual(hwp.style_names()[hwp.lists[0][9]['style']], '개요 1')
        self.assertEqual(result['result']['paragraphs'][0]['style_after'], '개요 1')

    def test_update_proves_the_style_and_its_uses_by_identity(self) -> None:
        hwp = seed_document()
        hwp.cursor = (0, 9, 1)
        hwp.after_execute = lambda owner: renumber(owner, list(reversed(range(len(owner.styles)))))
        found = next(item for item in inventory(hwp, name='개요 1')['styles'])
        result = ok(self, run(hwp, **UPDATE, style_name='개요 1', expected_style_hash=found['hash'], expected_uses=found['uses']['total'],
                              expected_uses_hash=found['uses']['hash'], all_uses=True, size_pt=11))
        listed = next(item for item in inventory(hwp, name='개요 1')['styles'])
        self.assertEqual((result['style_after']['index'], result['style_after']['character']['size_pt']), (listed['index'], 11.0))
        self.assertEqual(result['next_binding']['expected_style_hash'], listed['hash'])
        self.assertEqual(listed['index'], 20, 'old index 2 of 23, reversed')


class Reporting(unittest.TestCase):
    """The facts reach the caller through the bundle step, the CLI normaliser and the runtime journal; a fault while projecting them
    never changes the hold."""

    def held_step(self) -> tuple[dict, dict]:
        hwp = seed_document()
        hwp.after_execute = lambda owner: owner.styles[index_of(owner, NAME)].update(next=index_of(owner, NAME))
        outcome = created(hwp)
        return outcome, held(self, outcome)

    def test_the_cli_keeps_the_facts(self) -> None:
        outcome, step = self.held_step()
        normalized = normalize_command_bundle(outcome)
        self.assertEqual(normalized['steps'][0]['extras']['style_proof'], step['style_proof'])

    def test_a_fault_while_projecting_the_facts_keeps_the_hold(self) -> None:
        with patch.object(style_rules, 'style_proof_facts', side_effect=RuntimeError('PRIVATE'), create=True):
            outcome, step = self.held_step()
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(len(step['outcome_detail']) > 0, True)
        self.assertNotIn('style_proof', step)
        self.assertNotIn('PRIVATE', json.dumps(outcome, ensure_ascii=False))

    def test_the_facts_are_checked_against_their_fixed_shape(self) -> None:
        _outcome, step = self.held_step()
        facts = step['style_proof']
        self.assertEqual(style_rules.style_proof_facts(facts), facts)
        for bad in ({**facts, 'schema': 'other'}, {**facts, 'problems': [{'predicate': 'PRIVATE'}]}, {**facts, 'extra': 1},
                    {**facts, 'problems': [{**facts['problems'][0], 'name': 'x' * 65}]}, {**facts, 'old_styles': True}, [], None):
            with self.subTest(bad=str(bad)[:60]):
                self.assertIsNone(style_rules.style_proof_facts(bad))


class Journal(unittest.TestCase):
    owned = recovery.RecoveryTests.owned

    def test_the_runtime_journal_keeps_the_facts_of_a_held_create(self) -> None:
        manager, session, _wrapper, _calls, wc = self.owned()
        hwp = seed_document()
        hwp.IsModified = False
        hwp.after_execute = lambda owner: owner.styles[index_of(owner, NAME)].update(next=index_of(owner, NAME))
        session._active_handle.hwp = hwp  # Synthetic SDK adapter, not a native rebind.
        binding = {'session_id': 'recovery', 'source_filename': 'fixture.hwpx', 'command_generation': 0, 'native_command_sequence': 0}
        service = object.__new__(svc.LocalCliService)
        service.runtime_manager = manager
        service.command_packages = get_command_package_registry()
        service._require_ready_runtime = lambda *a: None
        service._load_active_binding = lambda **k: dict(binding)
        service._read_binding = lambda **k: dict(binding)
        service._save_binding = lambda b: b
        service._update_live_binding = lambda b, **k: b
        service._record_local_cli_command = lambda *a, **k: None
        service._working_copy_path = lambda b: wc
        service._verify_artifact_readback = lambda *a, **k: wc
        service._artifact_name = lambda **k: 'fixture.hwpx'
        service._artifact_download_path = lambda **k: 'fixture-download'
        service._bundle_page_evidence = lambda h, **kwargs: {'page': 1}
        service._bundle_compact_snapshot = lambda h: {}
        self.enterContext(patch.object(svc, 'snapshot_live_location', lambda **k: {}))
        listing = service.command_bundle(steps=[{'op': 'style_inventory'}])['steps'][0]['result']
        rows = {item['name']: item for item in listing['styles']}
        outcome = service.command_bundle(steps=[{**CREATE, 'name': NAME, 'kind': 'paragraph', 'based_on': '바탕글', 'expected_based_on_hash': rows['바탕글']['hash'],
                                                 'expected_styles_hash': listing['styles_hash'], 'next_style': '본문'}])
        step = outcome['steps'][0]
        self.assertTrue(step.get('native_custody_held'), step)
        command = session._commands_by_id[session._last_command_id]
        self.assertTrue(command.completed_event.wait(1))
        journal = json.loads(rt.command_journal_path(session.session_root).read_text())['commands']
        kept = journal[-1]['result']['steps'][0]
        self.assertEqual(kept['style_proof'], step['style_proof'])
        self.assertEqual(kept['style_proof']['problems'][0]['fields'], ['next'])
        self.assertNotIn('마지막 문단', rt.command_journal_path(session.session_root).read_text())


if __name__ == '__main__':
    unittest.main()
