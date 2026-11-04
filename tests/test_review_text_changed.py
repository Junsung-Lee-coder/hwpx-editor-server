"""A paragraph whose text was replaced in place is one review change with its old and new text. Portable; not Hancom evidence."""
from __future__ import annotations

import unittest

from hwpx_mcp import review
from tests.test_repair_review_rules import built, para


class TextChanged(unittest.TestCase):
    def test_a_replaced_title_names_its_old_and_new_text(self):
        first = built([para('Annual Plan 2025'), para('Body for 2025.'), para('Tail')], ['Annual Plan 2025\nBody for 2025.\nTail\n'])
        result = built([para('Annual Plan 2026'), para('Body for 2025.'), para('Tail')], ['Annual Plan 2026\nBody for 2025.\nTail\n'], baseline=first)
        changes = result['comparison']['changes']
        self.assertEqual([(change['kind'], change['text']) for change in changes], [('removed', 'Annual Plan 2025'), ('added', 'Annual Plan 2026')])
        self.assertEqual((changes[0]['replaced_by'], changes[1]['replaces']), ('Annual Plan 2026', 'Annual Plan 2025'))
        self.assertIn('글 변경 1건', result['summary_text'])
        self.assertNotIn('추가 1건', result['summary_text'])
        markdown = review.to_markdown(result)
        self.assertIn('- 글 변경: Annual Plan 2025 → Annual Plan 2026', markdown)
        self.assertNotIn('- 삭제: Annual Plan 2025', markdown)

    def test_an_inserted_or_removed_paragraph_names_no_replacement(self):
        first = built([para('One'), para('Two')], ['One\nTwo\n'])
        result = built([para('One'), para('New'), para('Two')], ['One\nNew\nTwo\n'], baseline=first)
        self.assertEqual([(change['kind'], 'replaces' in change) for change in result['comparison']['changes']], [('added', False)])
        self.assertIn('추가 1건', result['summary_text'])
        result = built([para('One')], ['One\n'], baseline=first)
        self.assertEqual([(change['kind'], 'replaced_by' in change) for change in result['comparison']['changes']], [('removed', False)])

    def test_a_paragraph_and_a_cell_paragraph_are_never_paired(self):
        first = built([para('Body'), para('Cell', in_body=False)], ['Body\nCell\n'])
        result = built([para('Cell', in_body=False), para('Body')], ['Cell\nBody\n'], baseline=first)
        self.assertFalse(any('replaces' in change or 'replaced_by' in change for change in result['comparison']['changes']))


if __name__ == '__main__':
    unittest.main()
