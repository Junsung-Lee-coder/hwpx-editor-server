"""Readable review judgement over readback facts and native PDF text. Portable; not Hancom evidence."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from hwpx_mcp import review


def para(text, heading='NONE', level=0, in_body=True, content='c'):
    return {'text': text, 'heading': heading, 'level': level, 'in_body': in_body, 'content_sha256': f'{content}:{text}'}


OFFICIAL = [para('Alpha', 'OUTLINE', 0), para('Beta', 'OUTLINE', 1), para('Gamma', 'OUTLINE', 2), para('Delta', 'OUTLINE', 3),
            para('Epsilon', 'OUTLINE', 0), para('Plain')]
PRINTED = ['head\n1. Alpha\n가. Beta\n1) Gamma\n가) Delta\n2. Epsilon\nPlain\nfoot          - 3 -\n']


class Labels(unittest.TestCase):
    def test_official_labels_and_restart_under_a_new_parent(self):
        labels = review.expected_labels([para('a', 'OUTLINE', 0), para('b', 'OUTLINE', 1), para('c', 'OUTLINE', 1), para('d', 'OUTLINE', 0), para('e', 'OUTLINE', 1)])
        self.assertEqual(labels, ['1.', '가.', '나.', '2.', '가.'])

    def test_no_label_is_claimed_past_the_known_rule(self):
        self.assertIsNone(review.official_label(1, 15))
        self.assertIsNone(review.official_label(4, 1))
        self.assertEqual(review.official_label(2, 15), '15)')

    def test_table_cell_paragraphs_do_not_count(self):
        self.assertEqual(review.expected_labels([para('x', 'OUTLINE', 0, in_body=False), para('y', 'OUTLINE', 0)]), ['1.'])


class Rendered(unittest.TestCase):
    def test_labels_come_from_the_printed_text(self):
        rows = review.rendered_numbering(OFFICIAL, PRINTED)
        self.assertEqual([row['rendered_label'] for row in rows], ['1.', '가.', '1)', '가)', '2.'])
        self.assertTrue(all(row['matches'] and row['page'] == 1 for row in rows))

    def test_wrong_or_missing_printed_labels_are_not_a_match(self):
        rows = review.rendered_numbering(OFFICIAL, ['9. Alpha\n가. Beta\nGamma\n가) Delta\n'])
        self.assertEqual([row['matches'] for row in rows], [False, True, False, True, False])
        self.assertIsNone(rows[2]['rendered_label'])
        self.assertIsNone(rows[4]['page'])

    def test_page_number_sharing_the_footer_line_is_found(self):
        readback = {'page_numbers': [{'draw_pos': 5, 'side_char': 45}], 'page_restarts': ['3']}
        result = review.rendered_page_numbers(readback, ['footer text     - 3 -\n', 'footer    - 4 -\n'])
        self.assertTrue(result['all_found'])
        self.assertEqual(result['position'], '아래 가운데')
        self.assertEqual([page['expected'] for page in result['pages']], ['- 3 -', '- 4 -'])

    def test_a_missing_or_wrong_page_number_is_reported(self):
        readback = {'page_numbers': [{'draw_pos': 5, 'side_char': 45}], 'page_restarts': ['3']}
        self.assertFalse(review.rendered_page_numbers(readback, ['- 3 -\n', '- 5 -\n'])['all_found'])
        self.assertFalse(review.rendered_page_numbers(readback, ['body with - 3 - in it\nfooter\n'])['all_found'])
        plain = {'page_numbers': [{'draw_pos': 5, 'side_char': 0}], 'page_restarts': []}
        self.assertFalse(review.rendered_page_numbers(plain, ['page 21 of 3a\n'])['all_found'])
        self.assertTrue(review.rendered_page_numbers(plain, ['footer  1\n'])['all_found'])

    def test_no_page_number_control_claims_nothing(self):
        self.assertEqual(review.rendered_page_numbers({'page_numbers': []}, ['1\n']), {'present': False, 'pages': []})


class Compare(unittest.TestCase):
    EVIDENCE = {'header_footer': ['h'], 'styles_sha256': 's', 'media_sha256': 'm', 'numberings_sha256': 'n', 'bindata_sha256': 'b'}

    def snapshot(self, paragraphs, **extra):
        rows = review.rendered_numbering(paragraphs, extra.pop('printed', ['']))
        return {'review_id': 'a/review-1', 'paragraphs': paragraphs, 'numbering': rows, 'evidence': dict(self.EVIDENCE), 'controls': {'tbl': 1, 'gso': 1}, **extra}, rows

    def test_insert_renumbers_the_following_items_and_nothing_else(self):
        before, _ = self.snapshot([para('A', 'OUTLINE', 0), para('B', 'OUTLINE', 0), para('cell', in_body=False)], printed=['1. A\n2. B\n'])
        after, rows = self.snapshot([para('A', 'OUTLINE', 0), para('New', 'OUTLINE', 0), para('B', 'OUTLINE', 0), para('cell', in_body=False)], printed=['1. A\n2. New\n3. B\n'])
        result = review.compare(before, after, rows)
        self.assertEqual([(c['kind'], c['text']) for c in result['changes']], [('added', 'New'), ('number_changed', 'B')])
        self.assertEqual((result['changes'][1]['before'], result['changes'][1]['after']), ('2.', '3.'))
        self.assertTrue(result['nested_paragraphs_identical'])
        self.assertEqual(set(result['evidence'].values()), {'unchanged'})

    def test_changed_evidence_format_and_nested_text_are_each_reported(self):
        before, _ = self.snapshot([para('A'), para('cell', in_body=False)])
        after, rows = self.snapshot([para('A', content='other'), para('cell2', in_body=False)])
        after['evidence']['media_sha256'] = 'changed'
        after['controls'] = {'tbl': 0, 'gso': 1}
        result = review.compare(before, after, rows)
        self.assertIn('format_changed', [c['kind'] for c in result['changes']])
        self.assertEqual(result['evidence']['media_sha256'], 'changed')
        self.assertFalse(result['nested_paragraphs_identical'])
        self.assertEqual(result['controls']['tbl'], {'before': 1, 'after': 0})

    def test_summary_never_reads_as_success_when_the_evidence_disagrees(self):
        base = {'session_id': 'a', 'document': 'd.hwpx', 'page_count': 1, 'document_is_modified': True,
                'page_numbers': {'present': True, 'all_found': False, 'position': '아래 가운데', 'start_number': 1, 'pages': []},
                'numbering': [{'matches': False, 'text': 'Alpha', 'rendered_label': '9.', 'expected_label': '1.'}],
                'comparison': {'baseline': 'a/review-1', 'changes': [], 'evidence': {'media_sha256': 'changed'}}}
        text = review.summarize(base)
        self.assertNotIn('모두 기대와 일치', text)
        self.assertIn('번호가 기대와 다른 문단 1개', text)
        self.assertIn('일부 쪽에서 확인되지 않음', text)
        self.assertIn('변경 감지', text)
        self.assertIn('저장 안 된 변경 있음', text)


class Unreadable(unittest.TestCase):
    def test_missing_pdf_text_is_said_plainly_instead_of_reading_as_a_wrong_number(self):
        text = review.summarize({'session_id': 'a', 'page_count': 3, 'pages_rendered': 2, 'pages_truncated': True, 'document_is_modified': False,
                                 'rendered_text_available': False, 'page_numbers': {'present': True, 'all_found': False, 'position': None, 'start_number': 1},
                                 'numbering': [{'matches': False, 'text': 'Alpha', 'rendered_label': None, 'expected_label': '1.'}], 'comparison': None})
        self.assertIn('PDF 글자를 읽을 수 없어', text)
        self.assertIn('앞 2쪽까지만', text)
        self.assertNotIn('기대와 다른', text)
        self.assertNotIn('모두 기대와 일치', text)


class Package(unittest.TestCase):
    def test_inventory_and_payload_comparison_ignore_a_member_rename(self):
        with tempfile.TemporaryDirectory() as raw:
            left, right = Path(raw) / 'a.hwpx', Path(raw) / 'b.hwpx'
            section = '<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p><hp:tbl/><hp:pic/><hp:header/><hp:footer/></hp:p></hs:sec>'
            for path, member, payload in ((left, 'BinData/image1.png', b'x'), (right, 'BinData/image1.PNG', b'x')):
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('Contents/section0.xml', section)
                    archive.writestr(member, payload)
            source, saved = review.package_inventory(left), review.package_inventory(right)
            result = review.compare_packages(source, saved)
            self.assertTrue(result['bindata_identical'])
            self.assertEqual(result['tables'], {'source': 1, 'saved': 1})
            with zipfile.ZipFile(right, 'w') as archive:
                archive.writestr('Contents/section0.xml', section)
                archive.writestr('BinData/image1.PNG', b'different')
            self.assertFalse(review.compare_packages(source, review.package_inventory(right))['bindata_identical'])


if __name__ == '__main__':
    unittest.main()
