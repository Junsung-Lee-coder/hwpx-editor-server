"""Fixed diagnostic labels for HEAD/TAIL and column-count verification reasons.

A verification failure after a native edit reports one fixed reason code in the journal and the public
failure_diagnostic. HEAD/TAIL reasons name the deepest changed HWPML HEAD element from a closed list of schema element
names; nothing from the document text is used.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from app import layout_ops, object_insert
from app.hwpml_invariants import head_tail_reasons
from app.step_refusal import step_diagnostic_allowed


def doc(caret='0', tail='QUJD', extra=''):
    return ET.fromstring(f'<HWPML><HEAD SecCnt="1"><DOCSETTING><BEGINNUMBER Page="1" Footnote="1"/><CARETPOS List="0" Para="2" Pos="{caret}"/>'
                         f'</DOCSETTING>{extra}</HEAD><BODY><SECTION/></BODY><TAIL><BINDATASTORAGE>{tail}</BINDATASTORAGE></TAIL></HWPML>')


class HeadReasonCodes(unittest.TestCase):
    def codes(self, before, after):
        reasons = head_tail_reasons(before, after)
        self.assertTrue(reasons)
        return layout_ops.reason_code(reasons[0]), object_insert.reason_code(reasons[0])

    def test_a_caret_move_is_not_a_document_change(self):
        # DOCSETTING/CARETPOS is the caret's position (view state); a footnote or section break moves it.
        self.assertEqual(head_tail_reasons(doc('0'), doc('1')), [])

    def test_other_document_settings_still_count(self):
        before = doc()
        after = doc()
        after.find('HEAD/DOCSETTING/BEGINNUMBER').set('Footnote', '2')
        self.assertEqual(self.codes(before, after), ('head_beginnumber_attrs', 'head_beginnumber_attrs'))

    def test_tail_change(self):
        self.assertEqual(self.codes(doc(), doc(tail='WFla')), ('tail_changed', 'tail_changed'))

    def test_a_new_growable_list_is_growth(self):
        # As on Hancom 2024 after a section break: MAPPINGTABLE gains a MEMOSHAPELIST with one memo shape.
        before = doc(extra='<MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="0"/></CHARSHAPELIST><STYLELIST/></MAPPINGTABLE>')
        after = doc(extra='<MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="0"/></CHARSHAPELIST><STYLELIST/><MEMOSHAPELIST Count="1"><MEMOSHAPE Id="0"/></MEMOSHAPELIST></MAPPINGTABLE>')
        self.assertEqual(head_tail_reasons(before, after), [])

    def test_other_new_children_or_changed_lists_are_still_named(self):
        before = doc(extra='<MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="0"/></CHARSHAPELIST></MAPPINGTABLE>')
        cases = (('<BULLETLIST Count="1"><BULLET Id="0"/></BULLETLIST>', 'head_mappingtable_gained_bulletlist'),
                 ('<MEMOSHAPELIST Count="1"><CHARSHAPE Id="0"/></MEMOSHAPELIST>', 'head_mappingtable_gained_memoshapelist'))
        for added, code in cases:
            after = doc(extra=f'<MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="0"/></CHARSHAPELIST>{added}</MAPPINGTABLE>')
            self.assertEqual(self.codes(before, after), (code, code))
        after = doc(extra='<MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="9"/></CHARSHAPELIST><MEMOSHAPELIST Count="1"><MEMOSHAPE Id="0"/></MEMOSHAPELIST></MAPPINGTABLE>')
        self.assertEqual(self.codes(before, after), ('head_charshapelist_entry', 'head_charshapelist_entry'))

    def test_unknown_head_element(self):
        self.assertEqual(self.codes(doc(extra='<ZZUNKNOWN A="1"/>'), doc(extra='<ZZUNKNOWN A="2"/>')), ('head_changed', 'head_changed'))

    def test_column_count_moved(self):
        self.assertEqual(layout_ops.reason_code('COLDEF count moved by 2, expected 0 or 1'), 'coldef_count_moved')

    def test_codes_reach_the_diagnostic(self):
        for code in ('head_beginnumber_attrs', 'head_mappingtable_gained_memoshapelist', 'tail_changed', 'coldef_count_moved'):
            self.assertTrue(step_diagnostic_allowed('verify', code), code)
        self.assertFalse(step_diagnostic_allowed('verify', 'head_whatever_changed'))



class InsertWithCaretInHead(unittest.TestCase):
    """The object engine over an HWPML readback whose HEAD records the caret, as Hancom 2024 writes it."""

    def test_footnote_succeeds_when_only_the_caret_moved(self):
        from tests.test_object_insert_exact import _FakeHwp, _run

        class CaretHead(_FakeHwp):
            def xml(self):
                # As on Hancom 2024, the recorded caret differs after the insert (this fake returns the caret itself).
                moved = int(any(entry.startswith('Insert') for entry in self.log))
                head = f'<HEAD><DOCSETTING><BEGINNUMBER Page="1" Footnote="1"/><CARETPOS List="{self.caret[0]}" Para="{self.caret[1]}" Pos="{self.caret[2] + moved}"/></DOCSETTING></HEAD>'
                return super().xml().replace('<HEAD/>', head, 1)
        hwp = CaretHead()
        result = _run(hwp, kind='footnote')
        self.assertTrue(result['succeeded'])
        self.assertTrue(result['verification']['ok'], result['verification'])


if __name__ == '__main__':
    unittest.main()
