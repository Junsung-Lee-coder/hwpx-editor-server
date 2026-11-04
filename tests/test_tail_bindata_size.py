"""The document TAIL is compared exactly except for the stored BINDATA Size, which native reads report differently for an
unchanged payload.

Measured on Hancom 2024 13.0.0.3457 (owned characterization on the settled objects fixture, one embedded PNG of 439 bytes):
the opens of four runs' settled copies read Size 669, 664, 669 and 654 over the same base64 payload, and one insertion's
after-read turned 654 into 669 with the payload unchanged, so that insertion was refused success and the session held. The numbering census
elides the same value (app.native_numbering_census). Portable; not Hancom evidence.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from app.hwpml_invariants import head_reason_code, head_tail_reasons

PAYLOAD = 'iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJ\nAAABfklEQVR4nO3ayxHCMAwEUIWh'


def doc(size='654', payload=PAYLOAD, encoding='Base64', ident='1', script=''):
    return ET.fromstring(
        '<HWPML><HEAD SecCnt="1"><MAPPINGTABLE/></HEAD><BODY><SECTION Id="0"/></BODY>'
        f'<TAIL><BINDATASTORAGE><BINDATA Encoding="{encoding}" Id="{ident}" Size="{size}">{payload}</BINDATA></BINDATASTORAGE>'
        f'<SCRIPTCODE Type="JScript" Version="1.0"><SCRIPTHEADER>{script}</SCRIPTHEADER><SCRIPTSOURCE></SCRIPTSOURCE></SCRIPTCODE></TAIL></HWPML>')


class TailComparison(unittest.TestCase):
    def codes(self, before, after):
        return [head_reason_code(reason) for reason in head_tail_reasons(before, after)]

    def test_a_different_stored_size_over_the_same_payload_is_not_a_change(self):
        self.assertEqual(head_tail_reasons(doc('654'), doc('669')), [])
        self.assertEqual(head_tail_reasons(doc('669'), doc('664')), [])

    def test_a_changed_payload_is_a_change_whatever_the_size(self):
        self.assertEqual(self.codes(doc(), doc(payload=PAYLOAD.replace('w0K', 'w1K'))), ['tail_changed'])
        self.assertEqual(self.codes(doc('654'), doc('669', payload=PAYLOAD + 'A')), ['tail_changed'])

    def test_every_other_bindata_attribute_and_part_stays_bound(self):
        self.assertEqual(self.codes(doc(), doc(encoding='Raw')), ['tail_changed'])
        self.assertEqual(self.codes(doc(), doc(ident='2')), ['tail_changed'])
        self.assertEqual(self.codes(doc(), doc(script='x')), ['tail_changed'])


if __name__ == '__main__':
    unittest.main()
