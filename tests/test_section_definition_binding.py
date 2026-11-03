"""A section break changes only the HEAD definitions its own section definition (SECDEF) references.

As read back live (HWPML2X) on Hancom 2024:

- section_insert inside a paragraph of a section with OutlineShape="1" and no MemoShapeId appended NUMBERING Id 2, a
  copy of NUMBERING 1 apart from its Id, and added a MEMOSHAPELIST (Count 1, one MEMO Id 1 holding Hancom's default
  memo shape) as the last MAPPINGTABLE child; the new SECDEF reads OutlineShape="2" MemoShapeId="1".
- A break inside that second section appended copies of that section's own definitions (NUMBERING 3 = NUMBERING 2,
  MEMO 2 = MEMO 1); the new SECDEF reads OutlineShape="3" MemoShapeId="2".
- section_delete of the second section of the saved two-section document removed NUMBERING 2 and the whole
  MEMOSHAPELIST: exactly what only the removed SECDEF referenced.

Everything else is refused: a new SECDEF naming a different pre-existing definition, a copy that differs, a created
definition nothing references, a new list elsewhere or with a wrong Count, ordinary edits growing these lists, and a
section delete losing a definition that surviving content or HEAD still references, or any other kind of definition.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from app.hwpml_invariants import head_tail_reasons
from app.layout_ops import parse_layout_xml, reason_code, verify_section_delete, verify_section_insert
from app.local_cli_service_support import LocalCliMutationError
from app.step_refusal import step_diagnostic_allowed
from tests.test_layout_exact import _FakeLayoutHwp, _run

DEFAULT_MEMO = 'ActiveColor="13103567" FillColor="15335408" LineColor="11458486" LineType="1" LineWidth="1" MemoType="0" Width="15591"'


def numbering(ident, fmt='^1.'):
    return (f'<NUMBERING Id="{ident}" Start="0"><PARAHEAD Level="1" NumFormat="Digit" Start="1">{fmt}</PARAHEAD>'
            f'<PARAHEAD Level="2" NumFormat="HangulSyllable" Start="1">^2.</PARAHEAD></NUMBERING>')


def memo(ident, attrs=DEFAULT_MEMO):
    return f'<MEMO Id="{ident}" {attrs}/>'


def numbering_list(*items, count=None):
    return f'<NUMBERINGLIST Count="{len(items) if count is None else count}">{"".join(items)}</NUMBERINGLIST>'


def memo_list(*items, count=None):
    return f'<MEMOSHAPELIST Count="{len(items) if count is None else count}">{"".join(items)}</MEMOSHAPELIST>'


CHARS = '<CHARSHAPELIST Count="2"><CHARSHAPE Id="0" Height="1000"/><CHARSHAPE Id="1" Height="1400"/></CHARSHAPELIST>'
BORDERS = '<BORDERFILLLIST Count="2"><BORDERFILL Id="1"/><BORDERFILL Id="2"/></BORDERFILLLIST>'
PARAS = '<PARASHAPELIST Count="2"><PARASHAPE Id="0" HeadingType="None" Heading="0"/><PARASHAPE Id="1" HeadingType="Outline" Level="0"/></PARASHAPELIST>'
STYLES = '<STYLELIST Count="1"><STYLE Id="0" ParaShape="0" CharShape="0"/></STYLELIST>'


def lists(numbers, tail='', *, chars=CHARS, borders=BORDERS, paras=PARAS):
    return f'{borders}{chars}<TABDEFLIST Count="1"><TABDEF Id="0"/></TABDEFLIST>{numbers}{paras}{STYLES}{tail}'


def section(attrs, texts):
    out = []
    for index, text in enumerate(texts):
        defs = f'<SECDEF {attrs}><PAGEDEF Width="59528" Height="84188" Landscape="0"/></SECDEF><COLDEF Count="1"/>' if index == 0 else ''
        shape = '1' if text.startswith('#') else '0'
        out.append(f'<P ParaShape="{shape}" Style="0"><TEXT CharShape="{shape}">{defs}<CHAR>{text.lstrip("#")}</CHAR></TEXT></P>')
    return f'<SECTION>{"".join(out)}</SECTION>'


def doc(mapping, sections, docsetting=''):
    body = ''.join(section(attrs, texts) for attrs, texts in sections)
    return parse_layout_xml(f'<HWPML><HEAD SecCnt="{len(sections)}"><DOCSETTING><CARETPOS List="0" Para="0" Pos="0"/>{docsetting}</DOCSETTING>'
                            f'<MAPPINGTABLE>{mapping}</MAPPINGTABLE></HEAD><BODY>{body}</BODY></HWPML>')


ONE = [('OutlineShape="1"', ['앞 본문', '#제목', '끝'])]
TWO = [('OutlineShape="1"', ['앞']), ('MemoShapeId="1" OutlineShape="2"', [' 본문', '#제목', '끝'])]
THREE = [('OutlineShape="1"', ['앞']), ('MemoShapeId="1" OutlineShape="2"', [' ']), ('MemoShapeId="2" OutlineShape="3"', ['본문', '#제목', '끝'])]
BEFORE_ONE = lists(numbering_list(numbering(1)))
AFTER_TWO = lists(numbering_list(numbering(1), numbering(2)), memo_list(memo(1)))


def codes(verification):
    return [reason_code(reason) for reason in verification['reasons']]


class InsertBinding(unittest.TestCase):
    def insert(self, before_map, after_map, before=ONE, after=TWO, caret=(0, 0, 1), docsetting=''):
        return verify_section_insert(doc(before_map, before), doc(after_map, after, docsetting), caret=list(caret))

    def refused(self, verification, code=None):
        self.assertFalse(verification['ok'], verification)
        if code is not None:
            self.assertIn(code, codes(verification), verification)
            self.assertTrue(step_diagnostic_allowed('verify', code), code)

    def test_the_observed_first_break_passes(self):
        self.assertEqual(self.insert(BEFORE_ONE, AFTER_TWO), {'ok': True, 'reasons': []})

    def test_the_observed_break_inside_a_split_section_passes(self):
        after = lists(numbering_list(numbering(1), numbering(2), numbering(3)), memo_list(memo(1), memo(2)))
        self.assertEqual(self.insert(AFTER_TWO, after, before=TWO, after=THREE, caret=(0, 1, 1)), {'ok': True, 'reasons': []})

    def test_a_different_pre_existing_numbering_is_refused(self):
        before = lists(numbering_list(numbering(1), numbering(2, '^1:')))
        self.refused(self.insert(before, lists(numbering_list(numbering(1), numbering(2, '^1:')), memo_list(memo(1)))),
                     'section_ref_outlineshape_substituted')

    def test_an_appended_numbering_that_is_not_a_copy_is_refused(self):
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2, '^1)')), memo_list(memo(1)))),
                     'section_ref_outlineshape_not_equivalent')

    def test_a_created_memo_shape_other_than_the_default_is_refused(self):
        odd = DEFAULT_MEMO.replace('15335408', '255')
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2)), memo_list(memo(1, odd)))),
                     'section_ref_memoshapeid_not_default')

    def test_a_copied_memo_shape_that_differs_is_refused(self):
        odd = DEFAULT_MEMO.replace('15591', '9000')
        after = lists(numbering_list(numbering(1), numbering(2), numbering(3)), memo_list(memo(1), memo(2, odd)))
        self.refused(self.insert(AFTER_TWO, after, before=TWO, after=THREE, caret=(0, 1, 1)), 'section_ref_memoshapeid_not_equivalent')

    def test_a_reference_without_a_definition_is_refused(self):
        # The new SECDEF names memo shape 1, but no memo shape exists.
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2)))), 'section_ref_memoshapeid_dangling')

    def test_a_dropped_reference_is_refused(self):
        after = lists(numbering_list(numbering(1), numbering(2), numbering(3)), memo_list(memo(1)))
        three = THREE[:2] + [('OutlineShape="3"', THREE[2][1])]
        self.refused(self.insert(AFTER_TWO, after, before=TWO, after=three, caret=(0, 1, 1)), 'section_ref_memoshapeid_dropped')

    def test_a_new_list_with_a_wrong_count_is_refused(self):
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2)), memo_list(memo(1), count=2))))

    def test_a_new_list_nothing_references_is_refused(self):
        two = [TWO[0], ('OutlineShape="2"', TWO[1][1])]
        self.refused(self.insert(BEFORE_ONE, AFTER_TWO, after=two))

    def test_an_extra_appended_definition_is_refused(self):
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2), numbering(3)), memo_list(memo(1)))))

    def test_a_new_list_outside_its_mapping_table_place_is_refused(self):
        # Under DOCSETTING instead of MAPPINGTABLE.
        self.refused(self.insert(BEFORE_ONE, lists(numbering_list(numbering(1), numbering(2))), docsetting=memo_list(memo(1))))
        # Before STYLELIST instead of after it.
        early = (f'{BORDERS}{CHARS}<TABDEFLIST Count="1"><TABDEF Id="0"/></TABDEFLIST>{numbering_list(numbering(1), numbering(2))}{PARAS}'
                 f'{memo_list(memo(1))}{STYLES}')
        self.refused(self.insert(BEFORE_ONE, early))


class OrdinaryEditsKeepSectionDefinitions(unittest.TestCase):
    """Edits that add or remove no section may not add memo shape or numbering definitions (memo insertion adds none)."""

    def root(self, mapping):
        return doc(mapping, ONE)['_root']

    def test_new_or_grown_section_definition_lists_are_refused(self):
        for after in (lists(numbering_list(numbering(1)), memo_list(memo(1))), lists(numbering_list(numbering(1), numbering(2)))):
            with self.subTest(after=after[-60:]):
                self.assertTrue(head_tail_reasons(self.root(BEFORE_ONE), self.root(after)))
        self.assertTrue(head_tail_reasons(self.root(AFTER_TWO), self.root(lists(numbering_list(numbering(1), numbering(2)), memo_list(memo(1), memo(2))))))

    def test_appending_a_char_shape_is_still_growth(self):
        grown = CHARS.replace('Count="2"', 'Count="3"').replace('</CHARSHAPELIST>', '<CHARSHAPE Id="2" Height="900"/></CHARSHAPELIST>')
        self.assertEqual(head_tail_reasons(self.root(BEFORE_ONE), self.root(lists(numbering_list(numbering(1)), chars=grown))), [])


class DeleteBinding(unittest.TestCase):
    PLAN = {'kind': 'section_delete', 'section_index': 2, 'section_count_before': 2}

    def delete(self, before_map, after_map, before=TWO, after=ONE):
        return verify_section_delete(self.PLAN, doc(before_map, before), doc(after_map, after))

    def refused(self, verification, code=None):
        self.assertFalse(verification['ok'], verification)
        if code is not None:
            self.assertIn(code, codes(verification), verification)
            self.assertTrue(step_diagnostic_allowed('verify', code), code)

    def test_the_observed_delete_passes(self):
        self.assertEqual(self.delete(AFTER_TWO, BEFORE_ONE), {'ok': True, 'reasons': []})

    def test_keeping_the_removed_sections_definitions_passes(self):
        self.assertEqual(self.delete(AFTER_TWO, AFTER_TWO), {'ok': True, 'reasons': []})

    def test_other_definition_kinds_may_not_be_lost(self):
        cases = {
            'head_charshapelist_lost': lists(numbering_list(numbering(1)), chars='<CHARSHAPELIST Count="1"><CHARSHAPE Id="0" Height="1000"/></CHARSHAPELIST>'),
            'head_parashapelist_lost': lists(numbering_list(numbering(1)), paras='<PARASHAPELIST Count="1"><PARASHAPE Id="0" HeadingType="None" Heading="0"/></PARASHAPELIST>'),
            'head_mappingtable_lost': lists(numbering_list(numbering(1)), borders=''),
        }
        for code, after in cases.items():
            with self.subTest(code):
                self.refused(self.delete(AFTER_TWO, after), code)

    def test_a_definition_surviving_content_references_may_not_be_lost(self):
        # A numbered heading's paragraph shape names NUMBERING 2 (HeadingType Number), as the removed SECDEF does.
        numbered = '<PARASHAPELIST Count="2"><PARASHAPE Id="0" HeadingType="None" Heading="0"/><PARASHAPE Id="1" HeadingType="Number" Heading="2" Level="0"/></PARASHAPELIST>'
        self.refused(self.delete(lists(numbering_list(numbering(1), numbering(2)), memo_list(memo(1)), paras=numbered),
                                 lists(numbering_list(numbering(1)), paras=numbered)), 'head_numberinglist_lost')

    def test_a_definition_the_surviving_section_shares_may_not_be_lost(self):
        shared = [('MemoShapeId="1" OutlineShape="1"', ['앞']), ('MemoShapeId="1" OutlineShape="1"', [' 본문', '#제목', '끝'])]
        kept = [('MemoShapeId="1" OutlineShape="1"', ['앞 본문', '#제목', '끝'])]
        before = lists(numbering_list(numbering(1)), memo_list(memo(1)))
        self.refused(self.delete(before, lists(numbering_list(numbering(1))), before=shared, after=kept), 'head_mappingtable_lost')
        self.refused(self.delete(before, lists('', memo_list(memo(1))), before=shared, after=kept), 'head_mappingtable_lost')

    def test_a_wrong_count_after_removal_is_refused(self):
        self.refused(self.delete(AFTER_TWO, lists(numbering_list(numbering(1), count=2))))

    def test_duplicate_or_missing_ids_are_refused(self):
        duplicate = lists(numbering_list(numbering(1), numbering(2), numbering(2)), memo_list(memo(1)))
        self.refused(self.delete(duplicate, BEFORE_ONE))
        missing = lists(numbering_list(numbering(1), numbering(2).replace(' Id="2"', '')), memo_list(memo(1)))
        self.refused(self.delete(missing, BEFORE_ONE))


class HancomLikeHead(_FakeLayoutHwp):
    """The synthetic layout SDK with Hancom's observed HEAD behaviour on BreakSection/DeleteBack, or a deviation."""

    def __init__(self, sections, behavior='native', **faults):
        super().__init__(sections, **faults)
        self.behavior = behavior
        self.numbering = {1: '^1.'}
        self.memos: dict[int, str] | None = None
        for item in self.sections:
            item['refs'] = {'OutlineShape': 1}

    def GetTextFile(self, fmt, option):  # noqa: N802
        text = super().GetTextFile(fmt, option)
        numbers = numbering_list(*(numbering(i, f) for i, f in self.numbering.items()))
        memos = memo_list(*(memo(i, a) for i, a in self.memos.items())) if self.memos is not None else ''
        text = text.replace('<HEAD/>', f'<HEAD SecCnt="{len(self.sections)}"><MAPPINGTABLE>{lists(numbers, memos)}</MAPPINGTABLE></HEAD>')
        parts = text.split('<SECDEF>')
        out = parts[0]
        for item, rest in zip(self.sections, parts[1:]):
            refs = ' '.join(f'{k}="{v}"' for k, v in sorted(item['refs'].items()))
            out += f'<SECDEF {refs}>' + rest
        return out

    def run(self, name):
        count = len(self.sections)
        here = self._section_of(self.caret[1])
        removed = self.sections[here]['refs'] if name == 'DeleteBack' else None
        result = super().run(name)
        if name == 'BreakSection' and len(self.sections) == count + 1:
            split, new = self.sections[here], self.sections[here + 1]
            outline = max(self.numbering) + 1
            self.numbering[outline] = self.numbering[split['refs']['OutlineShape']]
            if self.behavior == 'substitute':
                del self.numbering[outline]
                outline = 1 if split['refs']['OutlineShape'] != 1 else 2
                self.numbering.setdefault(outline, '^1:')
            if self.behavior == 'altered':
                self.numbering[outline] = '^1)'
            self.memos = self.memos or {}
            shape = self.memos.get(split['refs'].get('MemoShapeId'), DEFAULT_MEMO)
            memo_id = max(self.memos, default=0) + 1
            self.memos[memo_id] = shape
            new['refs'] = {'OutlineShape': outline, 'MemoShapeId': memo_id}
        if name == 'DeleteBack' and len(self.sections) == count - 1:
            referenced = {(k, v) for item in self.sections for k, v in item['refs'].items()}
            for key, value in removed.items():
                if (key, value) in referenced and self.behavior != 'drop_shared':
                    continue
                table = self.numbering if key == 'OutlineShape' else self.memos
                table.pop(value, None)
            if self.memos == {}:
                self.memos = None
        return result


class EngineOverHancomLikeHead(unittest.TestCase):
    """Through the layout engine (real planning, native calls and verification) over the synthetic SDK."""

    def insert(self, behavior):
        hwp = HancomLikeHead([['앞 본문', '제목', '끝']], behavior)
        hwp.caret = (0, 0, 1)
        return hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 1], 'confirm_layout': True}

    def test_native_behaviour_inserts_and_deletes(self):
        hwp, step = self.insert('native')
        result = _run(hwp, step)
        self.assertTrue(result['verification']['ok'], result['verification'])
        self.assertEqual(hwp.sections[1]['refs'], {'OutlineShape': 2, 'MemoShapeId': 1})
        self.assertEqual(result['head_after']['references']['MEMOSHAPELIST'], {'1': ['SECDEF[1].MemoShapeId']})
        hwp.caret = (0, 1, 0)
        result = _run(hwp, {'kind': 'section_delete', 'expected_pos': [0, 1, 0], 'confirm_layout': True, 'section_index': 2})
        self.assertTrue(result['verification']['ok'], result['verification'])
        self.assertEqual((hwp.numbering, hwp.memos), ({1: '^1.'}, None))

    def test_deviating_hancom_behaviour_is_not_verified(self):
        for behavior in ('substitute', 'altered'):
            with self.subTest(behavior):
                hwp, step = self.insert(behavior)
                with self.assertRaisesRegex(LocalCliMutationError, 'section definition reference OutlineShape'):
                    _run(hwp, step)
                self.assertEqual(hwp.mutations(), ['BreakSection'])

    def test_a_delete_that_drops_a_shared_definition_is_not_verified(self):
        hwp = HancomLikeHead([['앞'], [' 본문', '제목', '끝']], 'drop_shared')
        hwp.memos = {1: DEFAULT_MEMO}
        hwp.sections[1]['refs'] = {'OutlineShape': 1, 'MemoShapeId': 1}
        hwp.caret = (0, 1, 0)
        with self.assertRaisesRegex(LocalCliMutationError, 'HEAD'):
            _run(hwp, {'kind': 'section_delete', 'expected_pos': [0, 1, 0], 'confirm_layout': True, 'section_index': 2})
        self.assertEqual(hwp.mutations(), ['DeleteBack'])


if __name__ == '__main__':
    unittest.main()
