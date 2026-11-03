"""Which paragraph attribute differs when a section break's new paragraph does not keep the split paragraph's.

On Hancom 2024 a section break inside a paragraph stopped with section_break_attributes, then (with the attribute
named) section_break_attr_columnbreak: the paragraph opening the new section has its own ColumnBreak flag. That flag
may differ there; every other attribute of the split paragraph must be kept, and the first differing one is named.
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
                             ('ParaShape="1" Style="0" PageBreak="true"', 'section_break_attr_pagebreak'),
                             ('ParaShape="1" Style="0" Odd="1"', 'section_break_attr_other')):
            with self.subTest(second):
                reasons = body_token_reasons('section_insert', one(), two(second), caret=[0, 0, 1])
                self.assertTrue(reasons)
                self.assertEqual(reason_code(reasons[0]), code)
                self.assertTrue(step_diagnostic_allowed('verify', code))

    def test_the_break_flag_of_the_new_section_is_allowed(self):
        # As read back on Hancom 2024: the paragraph opening the new section has its own ColumnBreak flag.
        for flag in ('ColumnBreak="true"', 'ColumnBreak="false"'):
            self.assertEqual(body_token_reasons('section_insert', one(), two(f'ParaShape="1" Style="0" {flag}'), caret=[0, 0, 1]), [])

    def test_the_same_attributes_pass(self):
        self.assertEqual(body_token_reasons('section_insert', one(), two('ParaShape="1" Style="0"'), caret=[0, 0, 1]), [])


if __name__ == '__main__':
    unittest.main()
