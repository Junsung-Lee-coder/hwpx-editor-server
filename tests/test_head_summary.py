"""The structural HEAD summary in layout and object results: list names, Count/Id values, entry signature hashes
(Id excluded), SECDEF attributes, NUMBERING/MEMOSHAPE entry attributes and what references them; never document text.
Layout results carry it as head_before/head_after next to document_before/document_after, because MCP clients redact
fields named after documents."""
from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from app.hwpml_invariants import head_summary
from tests.test_layout_exact import _FakeLayoutHwp, _page_step, _run
from tests.test_object_insert_exact import _FakeHwp, _run as _run_object

DOC = ('<HWPML><HEAD SecCnt="1"><MAPPINGTABLE><NUMBERINGLIST Count="2"><NUMBERING Id="1" Start="1"><PARAHEAD Level="1">^1.</PARAHEAD>'
       '</NUMBERING><NUMBERING Id="2" Start="1"><PARAHEAD Level="1">^1.</PARAHEAD></NUMBERING></NUMBERINGLIST>'
       '<PARASHAPELIST Count="2"><PARASHAPE Id="0" HeadingType="None" Heading="0"/><PARASHAPE Id="1" HeadingType="Number" Heading="2"/>'
       '</PARASHAPELIST><MEMOSHAPELIST Count="1"><MEMOSHAPE Id="1" Width="15591"/></MEMOSHAPELIST></MAPPINGTABLE></HEAD>'
       '<BODY><SECTION><P><TEXT><SECDEF OutlineShape="1" MemoShapeId="0"/><CHAR>비밀 본문</CHAR></TEXT></P></SECTION>'
       '<SECTION><P><TEXT><SECDEF OutlineShape="2" MemoShapeId="1"/></TEXT></P></SECTION></BODY></HWPML>')


class HeadSummary(unittest.TestCase):
    def test_lists_ids_signatures_and_secdefs(self):
        summary = head_summary(ET.fromstring(DOC))
        numbering = summary['mapping_lists']['NUMBERINGLIST']
        self.assertEqual((numbering['count'], numbering['entries'], numbering['entry_tags'], numbering['ids']), ('2', 2, ['NUMBERING'], ['1', '2']))
        self.assertEqual(numbering['signatures'][0], numbering['signatures'][1])  # equal apart from Id
        self.assertEqual(summary['secdefs'], [{'MemoShapeId': '0', 'OutlineShape': '1'}, {'MemoShapeId': '1', 'OutlineShape': '2'}])
        self.assertEqual(numbering['entry_attrs'], [{'Id': '1', 'Start': '1'}, {'Id': '2', 'Start': '1'}])
        self.assertEqual(summary['mapping_lists']['MEMOSHAPELIST']['entry_attrs'], [{'Id': '1', 'Width': '15591'}])
        self.assertEqual(summary['references'], {
            'NUMBERINGLIST': {'1': ['SECDEF[0].OutlineShape'], '2': ['PARASHAPE[Id=1].Heading', 'SECDEF[1].OutlineShape']},
            'MEMOSHAPELIST': {'0': ['SECDEF[0].MemoShapeId'], '1': ['SECDEF[1].MemoShapeId']}})
        self.assertNotIn('비밀', json.dumps(summary, ensure_ascii=False))
        self.assertNotIn('^1.', json.dumps(summary, ensure_ascii=False))

    def test_results_carry_it(self):
        hwp = _FakeLayoutHwp()
        result = _run(hwp, _page_step())
        self.assertIn('mapping_lists', result['head_before'])
        self.assertIn('references', result['head_after'])
        self.assertNotIn('head', result['document_before'])
        obj = _run_object(_FakeHwp(), kind='footnote')
        self.assertIn('head_before', obj)
        self.assertIn('secdefs', obj['head_after'])


if __name__ == '__main__':
    unittest.main()
