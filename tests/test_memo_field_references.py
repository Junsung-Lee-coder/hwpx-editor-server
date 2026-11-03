"""A memo field's memo-shape reference keeps its definition through a section delete (M1).

As read back live (HWPML2X) on Hancom 2024 (tests/fixtures/native_memo): a memo field is
``<FIELDBEGIN Type="Memo" Command="MEMO/<memo shape>/<number>/...">``. A memo inserted into either section of a
saved two-section document whose second section has memo shape 1, and a memo in a single-section document without
memo shapes, all read ``MEMO/65535/...``, before and after saving and reopening: 65535 is the characterized value
for a memo field that names no memo shape.

Any other value must name an existing memo shape, which then counts as a surviving reference: a section delete may
not drop it. A memo field whose reference cannot be read (no Command, not MEMO/<digits>/, an uncharacterized value
such as 4294967295, a memo shape that does not exist, a Memo/MEMO type and command that disagree) is refused before
DeleteBack whenever the removed section names a memo shape, and the verifier never lets a memo shape go while such a
field exists.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from app.hwpml_invariants import definition_references
from app.layout_ops import parse_layout_xml, verify_section_delete
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import _Checks, counted, modifies, run
from tests.test_layout_exact import _FakeLayoutHwp
from tests.test_section_definition_binding import (AFTER_TWO, BEFORE_ONE, DEFAULT_MEMO, HancomLikeHead, codes, lists, memo, memo_list,
                                                   numbering, numbering_list, section)

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'native_memo'
INDEX = json.loads((FIXTURES / 'INDEX.json').read_text(encoding='utf-8'))['cases']
MARK = '|'


def native_field(name: str = 'memo_section1_no_memo_shape_reopened.xml') -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def field_with(command: str | None, *, field_type: str = 'Memo') -> str:
    field = native_field()
    field = re.sub(r' Command="[^"]*"', '' if command is None else f' Command="{command}"', field, count=1)
    return field.replace('Type="Memo"', f'Type="{field_type}"')


def referring(ref: str) -> str:
    return field_with(f'MEMO/{ref}/1/3518969632/31288258/owner/\\;;')


def doc_with(mapping: str, sections: list[tuple[str, list[str]]], field: str) -> dict:
    body = ''.join(section(attrs, texts) for attrs, texts in sections).replace(MARK, f'</CHAR>{field}<CHAR>')
    return parse_layout_xml(f'<HWPML><HEAD SecCnt="{len(sections)}"><DOCSETTING><CARETPOS List="0" Para="0" Pos="0"/></DOCSETTING>'
                            f'<MAPPINGTABLE>{mapping}</MAPPINGTABLE></HEAD><BODY>{body}</BODY></HWPML>')


# Section 2 alone names memo shape 1; the memo field sits in section 1, which survives the delete.
TWO = [('OutlineShape="1"', ['앞' + MARK + '글']), ('MemoShapeId="1" OutlineShape="2"', [' 본문', '#제목', '끝'])]
ONE = [('OutlineShape="1"', ['앞' + MARK + '글 본문', '#제목', '끝'])]
KEPT = lists(numbering_list(numbering(1)), memo_list(memo(1)))
UNREADABLE = {
    'no Command': field_with(None),
    'not MEMO/<digits>/': field_with('MEMO/one/1/3518969632/31288258/owner/\\;;'),
    'no MEMO prefix': field_with('65535/1/3518969632/31288258/owner/\\;;'),
    'uncharacterized 4294967295': referring('4294967295'),
    'a memo shape that does not exist': referring('7'),
    'MEMO command on a non-memo field': field_with('MEMO/1/1/3518969632/31288258/owner/\\;;', field_type='Hyperlink'),
}


class HancomAuthoredMemoFields(unittest.TestCase):
    def test_every_pinned_memo_field_names_no_memo_shape(self):
        for name in INDEX:
            with self.subTest(name):
                self.assertTrue(name.endswith('.xml') and 'Command="MEMO/65535/' in native_field(name))
                root = doc_with(AFTER_TWO, TWO, native_field(name))['_root']
                self.assertEqual(definition_references(root)['MEMOSHAPELIST'], {'1': ['SECDEF[1].MemoShapeId']})
                plan = {'kind': 'section_delete', 'section_index': 2, 'section_count_before': 2}
                after = doc_with(BEFORE_ONE, ONE, native_field(name))
                self.assertEqual(verify_section_delete(plan, doc_with(AFTER_TWO, TWO, native_field(name)), after), {'ok': True, 'reasons': []})

    def test_a_memo_field_naming_a_memo_shape_is_a_reference(self):
        root = doc_with(AFTER_TWO, TWO, referring('1'))['_root']
        self.assertEqual(definition_references(root)['MEMOSHAPELIST'], {'1': ['FIELDBEGIN[0].Command', 'SECDEF[1].MemoShapeId']})


class SectionDeleteKeepsReferencedMemoShapes(unittest.TestCase):
    PLAN = {'kind': 'section_delete', 'section_index': 2, 'section_count_before': 2}

    def delete(self, field: str, after_map: str) -> dict:
        return verify_section_delete(self.PLAN, doc_with(AFTER_TWO, TWO, field), doc_with(after_map, ONE, field))

    def test_with_the_native_memo_field_the_removed_sections_memo_shape_may_go_or_stay(self):
        self.assertEqual(self.delete(native_field(), BEFORE_ONE), {'ok': True, 'reasons': []})
        self.assertEqual(self.delete(native_field(), KEPT), {'ok': True, 'reasons': []})

    def test_a_memo_shape_a_surviving_memo_field_names_is_kept(self):
        dropped = self.delete(referring('1'), BEFORE_ONE)
        self.assertFalse(dropped['ok'], dropped)
        self.assertIn('head_mappingtable_lost', codes(dropped))
        self.assertEqual(self.delete(referring('1'), KEPT), {'ok': True, 'reasons': []})

    def test_an_unreadable_memo_field_keeps_every_memo_shape(self):
        for label, field in UNREADABLE.items():
            with self.subTest(label):
                self.assertFalse(self.delete(field, BEFORE_ONE)['ok'])
                self.assertEqual(self.delete(field, KEPT), {'ok': True, 'reasons': []})


class _MemoHead(HancomLikeHead):
    """Hancom's observed section delete (the removed section's own memo shape goes) over a document holding one memo field."""

    def __init__(self, field: str) -> None:
        super().__init__([['앞' + MARK + '글'], [' 본문', '제목', '끝']])
        self.field = field
        self.memos = {1: DEFAULT_MEMO}
        self.sections[1]['refs'] = {'OutlineShape': 1, 'MemoShapeId': 1}

    def GetTextFile(self, fmt, option):  # noqa: N802
        return super().GetTextFile(fmt, option).replace(MARK, f'</CHAR>{self.field}<CHAR>')


def memo_doc(field: str) -> _MemoHead:
    hwp = counted(_MemoHead(field))
    for name in ('execute', 'run'):
        modifies(hwp, name, lambda action, *rest: action in _FakeLayoutHwp.MUTATING and action not in hwp.noop)
    hwp.fault_mark = None
    hwp.caret = (0, 1, 0)
    return hwp


DELETE = {'op': 'layout_exact', 'kind': 'section_delete', 'expected_pos': [0, 1, 0], 'confirm_layout': True, 'section_index': 2}


class SectionDeleteThroughTheBundle(_Checks):
    def test_an_unreadable_memo_field_is_refused_before_delete_back(self):
        for label, field in UNREADABLE.items():
            with self.subTest(label):
                hwp = memo_doc(field)
                owner = owner_for(self, hwp)
                result = run(owner, DELETE)
                self.assert_clean_refusal(owner, result, 0, 'layout_unsupported', dirty=False)
                self.assertEqual(hwp.mutations(), [])
                self.assertEqual(len(hwp.sections), 2)
                hwp.caret = (0, 2, 1)  # between the two characters of 제목
                self.assert_continues_and_saves(owner, {'op': 'layout_exact', 'kind': 'section_insert', 'expected_pos': [0, 2, 1], 'confirm_layout': True})

    def test_the_native_memo_field_lets_the_delete_run_and_verify(self):
        hwp = memo_doc(native_field())
        owner = owner_for(self, hwp)
        result = run(owner, DELETE)
        self.assertTrue(result['ok'], result)
        self.assertEqual(hwp.mutations(), ['DeleteBack'])
        self.assertIsNone(hwp.memos)

    def test_a_delete_that_drops_a_memo_shape_a_surviving_field_names_is_held(self):
        hwp = memo_doc(referring('1'))
        owner = owner_for(self, hwp)
        result = run(owner, DELETE)
        self.assertEqual(hwp.mutations(), ['DeleteBack'])
        self.assert_held(owner, hwp, result, mutated=True, phase='verify')


if __name__ == '__main__':
    unittest.main()
