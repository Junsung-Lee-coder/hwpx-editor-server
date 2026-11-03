"""Which paragraph attribute differs when a section break's new paragraph does not keep the split paragraph's.

On Hancom 2024 a section break inside a paragraph stopped with section_break_attributes, then (with the attribute
named) section_break_attr_columnbreak: the paragraph opening the new section has its own ColumnBreak flag, and then (section_break_attr_pagebreak) its own
PageBreak flag. Those break flags may differ there; every other attribute must be kept, and the first differing one is
named.
"""
from __future__ import annotations

import unittest

from app.layout_ops import body_token_reasons, parse_layout_xml, reason_code
from app.step_refusal import step_diagnostic_allowed

SECDEF = '<SECDEF><PAGEDEF Width="1"/></SECDEF><COLDEF Count="1"/>'


def one():
    return parse_layout_xml(f'<HWPML><BODY><SECTION><P ParaShape="1" Style="0"><TEXT>{SECDEF}<CHAR>ab</CHAR></TEXT></P></SECTION></BODY></HWPML>')


def two(second):
    return parse_layout_xml(f'<HWPML><BODY><SECTION><P ParaShape="1" Style="0"><TEXT>{SECDEF}<CHAR>a</CHAR></TEXT></P></SECTION>'
                            f'<SECTION><P {second}><TEXT>{SECDEF}<CHAR>b</CHAR></TEXT></P></SECTION></BODY></HWPML>')


class SectionBreakAttributes(unittest.TestCase):
    def test_the_differing_attribute_is_named(self):
        for second, code in (('ParaShape="2" Style="0"', 'section_break_attr_parashape'), ('ParaShape="1" Style="3"', 'section_break_attr_style'),
                             ('ParaShape="1" Style="0" Odd="1"', 'section_break_attr_other')):
            with self.subTest(second):
                reasons = body_token_reasons('section_insert', one(), two(second), caret=[0, 0, 1])
                self.assertTrue(reasons)
                self.assertEqual(reason_code(reasons[0]), code)
                self.assertTrue(step_diagnostic_allowed('verify', code))

    def test_the_break_flag_of_the_new_section_is_allowed(self):
        # As read back on Hancom 2024: the paragraph opening the new section has its own ColumnBreak and PageBreak flags.
        for flag in ('ColumnBreak="true"', 'ColumnBreak="false"', 'PageBreak="true"', 'ColumnBreak="true" PageBreak="true"'):
            self.assertEqual(body_token_reasons('section_insert', one(), two(f'ParaShape="1" Style="0" {flag}'), caret=[0, 0, 1]), [])

    def test_the_same_attributes_pass(self):
        self.assertEqual(body_token_reasons('section_insert', one(), two('ParaShape="1" Style="0"'), caret=[0, 0, 1]), [])


class SectionCopyDifference(unittest.TestCase):
    def after(self, new_secdef):
        return parse_layout_xml(f'<HWPML><BODY><SECTION><P ParaShape="1" Style="0"><TEXT>{SECDEF}<CHAR>a</CHAR></TEXT></P></SECTION>'
                                f'<SECTION><P ParaShape="1" Style="0"><TEXT>{new_secdef}<CHAR>b</CHAR></TEXT></P></SECTION></BODY></HWPML>')

    def test_the_memo_shape_reference_of_the_new_section_may_differ(self):
        # As read back on Hancom 2024: the new section's SECDEF refers to the memo shape created with it.
        self.assertEqual(body_token_reasons('section_insert', one(), self.after('<SECDEF MemoShapeId="1"><PAGEDEF Width="1"/></SECDEF><COLDEF Count="1"/>'),
                                            caret=[0, 0, 1]), [])

    def test_the_first_differing_definition_attribute_is_named(self):
        old = f'<HWPML><BODY><SECTION><P ParaShape="1" Style="0"><TEXT>{SECDEF}<CHAR>ab</CHAR></TEXT></P></SECTION></BODY></HWPML>'
        new_secdef = '<SECDEF TabStop="9"><PAGEDEF Width="1"/></SECDEF><COLDEF Count="1"/>'
        after = parse_layout_xml(f'<HWPML><BODY><SECTION><P ParaShape="1" Style="0"><TEXT>{SECDEF}<CHAR>a</CHAR></TEXT></P></SECTION>'
                                 f'<SECTION><P ParaShape="1" Style="0"><TEXT>{new_secdef}<CHAR>b</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        reasons = body_token_reasons('section_insert', parse_layout_xml(old), after, caret=[0, 0, 1])
        self.assertTrue(reasons)
        self.assertEqual(reason_code(reasons[0]), 'section_copy_secdef_tabstop')
        self.assertTrue(step_diagnostic_allowed('verify', 'section_copy_secdef_tabstop'))


if __name__ == '__main__':
    unittest.main()
