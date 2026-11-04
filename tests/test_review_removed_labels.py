"""A removed or added paragraph without text is named in the readable review instead of an empty '- 삭제: '.

The label comes from the existing naming helper: the objects the paragraph row lists ('(그림만 있는 문단)', '(표만 있는 문단)',
'(개체만 있는 문단)'), or '(글 없음, 내용 확인 못함)' when the row lists none, as for a paragraph holding only a bookmark,
which the object list does not record. Text rows, pairing of a paragraph replaced in place and the separate removal of a
paragraph that lost its objects are unchanged. Portable; not Hancom evidence.
"""
from __future__ import annotations

import unittest

from hwpx_mcp import review
from tests.test_review_removed_objects import compare, with_objects


def rows(markdown):
    return [line for line in markdown.splitlines() if line.startswith(('- 삭제:', '- 추가:', '- 글 변경:'))]


class RemovedRowLabels(unittest.TestCase):
    def test_an_object_only_paragraph_replaced_by_text_names_what_was_removed(self):
        for objects, label in ((['equation'], '(개체만 있는 문단)'), (['picture'], '(그림만 있는 문단)')):
            with self.subTest(objects=objects):
                _result, changes, markdown = compare(with_objects('', objects, 'object-1'), with_objects('Replacement text'))
                self.assertEqual([change['kind'] for change in changes], ['removed', 'added'])
                self.assertEqual(rows(markdown), [f'- 삭제: {label}', '- 추가: Replacement text'])

    def test_a_removed_paragraph_whose_objects_are_not_listed_is_not_given_a_name_it_does_not_have(self):
        # A bookmark is part of the object digest but not of the object list.
        _result, _changes, markdown = compare(with_objects('', [], 'bookmark-1'), with_objects('Replacement text'))
        self.assertEqual(rows(markdown), ['- 삭제: (글 없음, 내용 확인 못함)', '- 추가: Replacement text'])

    def test_a_paragraph_removed_outright_is_named_too(self):
        first = review_pair([with_objects('Head'), with_objects('', ['picture'], 'pic-1'), with_objects('Tail')])
        second = review_pair([with_objects('Head'), with_objects('Tail')], baseline=first)
        self.assertEqual(rows(review.to_markdown(second)), ['- 삭제: (그림만 있는 문단)'])

    def test_text_rows_and_pairing_are_unchanged(self):
        _result, _changes, markdown = compare(with_objects('Annual Plan 2025'), with_objects('Annual Plan 2026'))
        self.assertEqual(rows(markdown), ['- 글 변경: Annual Plan 2025 → Annual Plan 2026'])
        _result, _changes, markdown = compare(with_objects('Before the picture and after', ['picture'], 'pic-1'), with_objects('Before and after'))
        self.assertEqual(rows(markdown), ['- 삭제: Before the picture and after', '- 추가: Before and after'])


def review_pair(paragraphs, baseline=None):
    from tests.test_repair_review_rules import built
    return built(paragraphs, ['\n'.join(item['text'] for item in paragraphs) + '\n'], baseline=baseline)


if __name__ == '__main__':
    unittest.main()
