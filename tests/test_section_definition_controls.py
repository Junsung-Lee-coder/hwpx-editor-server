"""Controls that keep each section-definition guard observable on its own (see tests/test_section_definition_binding.py).

A definition list Hancom creates for a new section belongs in MAPPINGTABLE. A second copy of a correctly created memo
shape list under any other HEAD element is refused even when that element holds nothing else, so the MAPPINGTABLE
check does not depend on the order check seeing an unknown neighbour.
"""
from __future__ import annotations

import unittest

from app.layout_ops import parse_layout_xml, reason_code, verify_section_insert
from tests.test_section_definition_binding import AFTER_TWO, BEFORE_ONE, ONE, TWO, memo, memo_list, section


def doc(mapping, sections, extra=''):
    body = ''.join(section(attrs, texts) for attrs, texts in sections)
    return parse_layout_xml(f'<HWPML><HEAD SecCnt="{len(sections)}"><DOCSETTING><CARETPOS List="0" Para="0" Pos="0"/></DOCSETTING>'
                            f'<MAPPINGTABLE>{mapping}</MAPPINGTABLE><FORBIDDENSTRING>{extra}</FORBIDDENSTRING></HEAD><BODY>{body}</BODY></HWPML>')


class NewListOnlyInMappingTable(unittest.TestCase):
    def test_a_copy_under_another_element_is_refused(self):
        self.assertEqual(verify_section_insert(doc(BEFORE_ONE, ONE), doc(AFTER_TWO, TWO), caret=[0, 0, 1]), {'ok': True, 'reasons': []})
        result = verify_section_insert(doc(BEFORE_ONE, ONE), doc(AFTER_TWO, TWO, memo_list(memo(1))), caret=[0, 0, 1])
        self.assertFalse(result['ok'], result)
        self.assertEqual([reason_code(reason) for reason in result['reasons']], ['head_forbiddenstring_newlist_position'])


if __name__ == '__main__':
    unittest.main()
