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

    def test_caret_position_change(self):
        self.assertEqual(self.codes(doc('0'), doc('1')), ('head_caretpos_changed', 'head_caretpos_changed'))

    def test_tail_change(self):
        self.assertEqual(self.codes(doc(), doc(tail='WFla')), ('tail_changed', 'tail_changed'))

    def test_unknown_head_element(self):
        self.assertEqual(self.codes(doc(extra='<ZZUNKNOWN A="1"/>'), doc(extra='<ZZUNKNOWN A="2"/>')), ('head_changed', 'head_changed'))

    def test_column_count_moved(self):
        self.assertEqual(layout_ops.reason_code('COLDEF count moved by 2, expected 0 or 1'), 'coldef_count_moved')

    def test_codes_reach_the_diagnostic(self):
        for code in ('head_caretpos_changed', 'head_beginnumber_changed', 'tail_changed', 'coldef_count_moved'):
            self.assertTrue(step_diagnostic_allowed('verify', code), code)
        self.assertFalse(step_diagnostic_allowed('verify', 'head_whatever_changed'))


if __name__ == '__main__':
    unittest.main()
