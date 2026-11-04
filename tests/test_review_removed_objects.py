"""A paragraph replaced in place is shown as one text change only when it kept exactly its objects and was not objects alone.

Otherwise the old paragraph stays a removal (with its objects) beside the new one, so an equation, picture or bookmark that
went away is never hidden behind a text change. Portable; not Hancom evidence.
"""
from __future__ import annotations

import hashlib
import unittest

from hwpx_mcp import review
from tests.test_repair_review_rules import built, para

NONE = hashlib.sha256(b'[]').hexdigest()[:16]  # native_format's digest of a paragraph with no object


def with_objects(text, objects=(), digest=NONE):
    row = para(text)
    row['format'] = {'objects': list(objects), 'objects_sha256': digest}
    return row


def compare(old, new):
    first = built([with_objects('Head'), old, with_objects('Tail')], ['Head\n' + old['text'] + '\nTail\n'])
    result = built([with_objects('Head'), new, with_objects('Tail')], ['Head\n' + new['text'] + '\nTail\n'], baseline=first)
    return result, result['comparison']['changes'], review.to_markdown(result)


class RemovedObjectsStayVisible(unittest.TestCase):
    def assert_not_paired(self, changes, markdown, summary):
        self.assertEqual([change['kind'] for change in changes], ['removed', 'added'])
        self.assertFalse(any('replaced_by' in change or 'replaces' in change for change in changes), changes)
        self.assertIn('삭제 1건', summary)
        self.assertIn('추가 1건', summary)
        self.assertNotIn('글 변경', summary)
        self.assertNotIn('- 글 변경:', markdown)
        self.assertIn('- 삭제:', markdown)

    def test_an_equation_only_paragraph_replaced_by_text_is_a_removal_with_its_equation(self):
        result, changes, markdown = compare(with_objects('', ['equation'], 'eq-1'), with_objects('Replacement text'))
        self.assert_not_paired(changes, markdown, result['summary_text'])
        self.assertEqual(changes[0]['objects'], ['equation'])

    def test_text_that_lost_its_picture_is_a_removal_with_the_picture(self):
        result, changes, markdown = compare(with_objects('Before the picture and after', ['picture'], 'pic-1'), with_objects('Before and after'))
        self.assert_not_paired(changes, markdown, result['summary_text'])
        self.assertEqual(changes[0]['objects'], ['picture'])

    def test_text_that_lost_its_bookmark_is_a_removal(self):
        # A bookmark is not listed among a paragraph's objects, but it is part of their digest.
        result, changes, markdown = compare(with_objects('Before mark after', [], 'bookmark-1'), with_objects('Before after'))
        self.assert_not_paired(changes, markdown, result['summary_text'])

    def test_text_beside_an_object_that_stayed_is_one_text_change(self):
        result, changes, markdown = compare(with_objects('Picture caption old', ['picture'], 'pic-1'), with_objects('Picture caption new', ['picture'], 'pic-1'))
        self.assertEqual([(change['kind'], change.get('replaced_by'), change.get('replaces')) for change in changes],
                         [('removed', 'Picture caption new', None), ('added', None, 'Picture caption old')])
        self.assertIn('- 글 변경: Picture caption old → Picture caption new', markdown)

    def test_an_ordinary_text_only_replacement_is_one_text_change(self):
        result, changes, markdown = compare(with_objects('Annual Plan 2025'), with_objects('Annual Plan 2026'))
        self.assertEqual([(change['kind'], change.get('replaced_by'), change.get('replaces')) for change in changes],
                         [('removed', 'Annual Plan 2026', None), ('added', None, 'Annual Plan 2025')])
        self.assertIn('글 변경 1건', result['summary_text'])
        self.assertIn('- 글 변경: Annual Plan 2025 → Annual Plan 2026', markdown)

    def test_paragraphs_whose_objects_are_unknown_are_not_paired(self):
        result, changes, _markdown = compare(para('Old words'), para('New words'))
        self.assertEqual([change['kind'] for change in changes], ['removed', 'added'])
        self.assertFalse(any('replaced_by' in change or 'replaces' in change for change in changes), changes)

    def test_the_empty_object_digest_is_the_one_the_backend_writes(self):
        from app import native_format
        self.assertEqual(native_format._short_digest([]), NONE)  # noqa: SLF001


if __name__ == '__main__':
    unittest.main()
