"""Named styles: a sub-set is a copy under Hangul Automation, so each command sets its CharShape sub-set back before Execute.

Hancom staff, forum topic 2354 post 5 (2025-03-27): in Hangul Automation, sub-sets that Item or CreateItemSet make are copies,
not references, so the made or returned sub-set is set on the parent set directly; in HwpCtrl they are references and filling
them is enough. Topic 1552 post 4 (2024-06-11), the StyleDirectEdit example, omits that step; topic 2505 post 2 (2025-05-20),
the StyleAddNew example, takes it. The change command now sets its CharShape sub-set back once, through SetItem("CharShape",
sub-set), whose declared answer is VT_VOID (None), before Execute.

tests.native_styles_fakes copies by default (legacy_copies) and keeps HwpCtrl's references as a knob: both pass. Through the
real owned service, with every attribute, call and write on the SDK object recorded. Not Hancom evidence.
"""
from __future__ import annotations

import copy
import json
import unittest

from tests import test_native_styles_entry as entry
from tests.native_format_fakes import bookmark, char, control
from tests.native_styles_fakes import StyleHwp, _char_items, _LegacySet, area, char_shape_items, paragraph, styled, styles_document
from tests.test_native_styles import BODY, executes
from tests.test_native_styles_legacy import SECRET, Foreign, _Legacy, create_request, update_request
from tests.test_native_styles_repairs import DOTUM, TYPEINFO, Fault, after_fault, raising, returning

L = entry.LEGACY
SET_BACK = f'call:{L}CreateSet().SetItem'


# The click-here field of the native-authored seed (tests/fixtures/native_fields/before.hwpml2x), as Hancom wrote it.
FIELD_BEGIN = ('<FIELDBEGIN Command="Clickhere:set:67:Direction:wstring:5:성명 입력 HelpState:wstring:19:새 필드 fixture 성명 도움말  " '
               'Dirty="true" Editable="true" FieldId="627272811" InstId="1239966591" Name="성명" Property="9" Type="Clickhere"/>')
FIELD_END = '<FIELDEND Editable="true" FieldId="627272811" Property="9" Type="Clickhere"/>'


def copy_document() -> StyleHwp:
    """본문 used twice, one use holding a bookmark control and the other a click-here field; a font with TYPEINFO that only
    direct-format text uses; a header."""
    body = [paragraph('문서 제목'), styled(('본문 ', bookmark('표시'), ' 첫 문단'), 1, para_shape=BODY['para']),
            styled(('본문 ', control(FIELD_BEGIN, '%clk'), '기존성명', control(FIELD_END, ''), ' 둘째 문단'), 1, para_shape=BODY['para']),
            paragraph('앞 ', ('돋움 글자', char(Font=DOTUM)), ' 뒤'), paragraph('마지막 문단')]
    hwp = StyleHwp(body, areas=[area('header', paragraph('머리 문구'))])
    hwp.font_info = {DOTUM: dict(TYPEINFO), '함초롬바탕': dict(TYPEINFO)}
    return hwp


def text_and_controls(hwp: StyleHwp) -> list[list]:
    return [[(atom.get('c'), atom.get('ctrl'), atom.get('xml')) for atom in para['atoms']] for para in hwp.every_paragraph()]


class Semantics(unittest.TestCase):
    def test_item_and_create_item_set_give_detached_copies_and_set_item_sets_them_back(self) -> None:
        parent = _LegacySet('StyleItem')
        made = parent.CreateItemSet('CharShape', 'CharShape')
        made.SetItem('Height', 1100)
        self.assertEqual(parent.items['CharShape'].items, {}, 'the made sub-set is a copy: the parent keeps its own')
        got = parent.Item('CharShape')
        got.SetItem('Bold', 1)
        self.assertEqual(parent.items['CharShape'].items, {}, 'so is the one Item gives')
        parent.SetItem('CharShape', made)
        self.assertEqual(parent.items['CharShape'].items, {'Height': 1100}, 'setting it back is what reaches the parent')
        made.SetItem('Italic', 1)
        self.assertNotIn('Italic', parent.items['CharShape'].items, 'and what is set back is stored as a copy too')
        reference = _LegacySet('StyleItem', copies=False)
        same = reference.CreateItemSet('CharShape', 'CharShape')
        same.SetItem('Height', 1100)
        self.assertEqual(reference.items['CharShape'].items, {'Height': 1100}, "HwpCtrl's references, the knob")


class ChangeSetsBack(_Legacy):
    def test_a_change_takes_effect_under_copies_and_under_references(self) -> None:
        for copies in (True, False):
            with self.subTest(copies=copies):
                hwp = copy_document()
                hwp.legacy_copies = copies
                before = copy.deepcopy(hwp)
                owned, inner, trace, start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11), document=lambda: hwp)
                self.assertTrue(outcome['ok'], outcome)
                result = outcome['steps'][-1]['result']
                self.assertEqual((result['result']['native_actions'], result['undo']['native_editing_actions']), (['Execute:StyleDirectEdit'], 1))
                seen = trace[start:]
                self.assertEqual(seen.count(SET_BACK), 2, 'NameLocal, then the CharShape sub-set')
                self.assertLess(seen.index('call:CharShape.SetItem'), len(seen) - seen[::-1].index(SET_BACK) - 1)
                self.assertLess(len(seen) - seen[::-1].index(SET_BACK) - 1, seen.index(f'call:{L}Execute'), 'set back before Execute')
                whole = {key: (('set', 'BorderFill') if key == 'BorderFill' else value) for key, value in char_shape_items(BODY['char']).items()}
                self.assertEqual(executes(inner)[0][4], {**whole, 'Height': 1100},
                                 "the executed CharShape: the whole shape read at the style's use, only the requested item changed")
                self.assertEqual(inner.styles[1]['char'], {**BODY['char'], 'Height': 1100})
                self.assertEqual((inner.styles[1]['para'], inner.styles[1]['name'], inner.styles[1]['eng']), (BODY['para'], '본문', 'Body'))
                self.assertEqual(inner.styles[0], before.styles[0])
                uses = [item for item in inner.every_paragraph() if item.get('style', 0) == 1]
                self.assertEqual(len(uses), 2)
                self.assertTrue(all(atom['cs'] == inner.styles[1]['char'] for item in uses for atom in item['atoms']), 'the control too')
                self.assertEqual(text_and_controls(inner), text_and_controls(before), 'every text and control, the bookmark and the field included')
                written = inner.get_text_file('HWPML2X')
                self.assertEqual((written.count(FIELD_BEGIN), written.count(FIELD_END)), (1, 1), 'the field, as it was')
                self.assertIn('<CHAR>기존성명</CHAR>', written)
                self.assertEqual(inner.font_info, before.font_info)
                self.assertEqual([item for item in inner.every_paragraph() if item.get('style', 0) != 1],
                                 [item for item in before.every_paragraph() if item.get('style', 0) != 1], 'the direct-format TYPEINFO font text')

    def test_a_set_back_that_does_not_answer_none_holds_before_execute(self) -> None:
        cases = ((True, 'true'), (False, 'false'), (0, 'int'), ('', 'str'), (Foreign(), 'object'))
        for value, shape in cases:
            with self.subTest(value=shape):
                owned, inner, trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11),
                                                                     Fault(SET_BACK, returning(value), skip=1))
                self.assert_held(owned, inner, trace, outcome)
                self.assertEqual((self.diagnostic(outcome)['native_site'], self.diagnostic(outcome)['native_return']), ('set_item.CharShape', shape))
                self.assertNotIn('Foreign', json.dumps(outcome, ensure_ascii=False))
        owned, inner, trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11),
                                                             Fault(SET_BACK, raising(RuntimeError(SECRET)), skip=1))
        self.assert_held(owned, inner, trace, outcome)
        self.assertEqual(self.diagnostic(outcome)['native_return'], 'raised')
        self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))
        self.assertNotIn(f'call:{L}Execute', after_fault(trace))

    def test_after_an_earlier_dirty_edit_a_set_back_fault_holds_with_the_debt_kept(self) -> None:
        """The earlier edit applies 개요 1 to the last paragraph, which gives 개요 1 its one plain body use to change."""
        owned, inner, trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, '개요 1', size_pt=13),
                                                             Fault(SET_BACK, returning(True), skip=1), document=styles_document, dirty_first=True)
        step = outcome['steps'][-1]
        self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
        self.assertNotIn('refusal_code', step)
        self.assertEqual(after_fault(trace), [])
        self.assertEqual(len(executes(inner)), 1, 'only the earlier edit was written')
        self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')


class CreateSetsBack(_Legacy):
    def test_a_create_with_character_properties_takes_effect_under_copies_and_under_references(self) -> None:
        for copies in (True, False):
            with self.subTest(copies=copies):
                hwp = copy_document()
                hwp.legacy_copies = copies
                _owned, inner, trace, start, outcome = self.run_with('create', lambda owned: create_request(owned, size_pt=14, bold=True), document=lambda: hwp)
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual((inner.styles[2]['name'], inner.styles[2]['char']), ('새 본문', {**BODY['char'], 'Height': 1400, 'Bold': True}))
                self.assertEqual(executes(inner)[0][4]['Height'], 1400, 'the CharShape the action took holds the requested items over the base')
                seen = trace[start:]
                self.assertLess(len(seen) - seen[::-1].index(SET_BACK) - 1, seen.index(f'call:{L}Execute'))


if __name__ == '__main__':
    unittest.main()
