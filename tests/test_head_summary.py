"""The structural HEAD summary in layout and object results: list names, Count/Id values, entry signature hashes
(Id excluded) and SECDEF attributes, and never document text."""
from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from app.hwpml_invariants import head_summary
from tests.test_layout_exact import _FakeLayoutHwp, _page_step, _run
from tests.test_object_insert_exact import _FakeHwp, _run as _run_object

DOC = ('<HWPML><HEAD SecCnt="1"><MAPPINGTABLE><NUMBERINGLIST Count="2"><NUMBERING Id="1" Start="1"><PARAHEAD Level="1">^1.</PARAHEAD>'
       '</NUMBERING><NUMBERING Id="2" Start="1"><PARAHEAD Level="1">^1.</PARAHEAD></NUMBERING></NUMBERINGLIST></MAPPINGTABLE></HEAD>'
       '<BODY><SECTION><P><TEXT><SECDEF OutlineShape="1" MemoShapeId="0"/><CHAR>비밀 본문</CHAR></TEXT></P></SECTION></BODY></HWPML>')


class HeadSummary(unittest.TestCase):
    def test_lists_ids_signatures_and_secdefs(self):
        summary = head_summary(ET.fromstring(DOC))
        numbering = summary['mapping_lists']['NUMBERINGLIST']
        self.assertEqual((numbering['count'], numbering['entries'], numbering['entry_tags'], numbering['ids']), ('2', 2, ['NUMBERING'], ['1', '2']))
        self.assertEqual(numbering['signatures'][0], numbering['signatures'][1])  # equal apart from Id
        self.assertEqual(summary['secdefs'], [{'MemoShapeId': '0', 'OutlineShape': '1'}])
        self.assertNotIn('비밀', json.dumps(summary, ensure_ascii=False))
        self.assertNotIn('^1.', json.dumps(summary, ensure_ascii=False))

    def test_results_carry_it(self):
        hwp = _FakeLayoutHwp()
        result = _run(hwp, _page_step())
        self.assertIn('head', result['document_before'])
        self.assertIn('mapping_lists', result['document_after']['head'])
        obj = _run_object(_FakeHwp(), kind='footnote')
        self.assertIn('head_before', obj)
        self.assertIn('secdefs', obj['head_after'])


if __name__ == '__main__':
    unittest.main()
