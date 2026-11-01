"""The real parser and comparisons over HWPML2X captured from Hancom for the disposable synthetic fixture.

Replaying retained snapshots exercises real code; it is not a new native run. page_after is the state where PageNumPos
added a font and a character shape, renumbering every FontId/CharShape reference without changing any content.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from app import native_numbering as nn, native_numbering_census as census

ROOT = Path(__file__).parent / 'fixtures' / 'native_paragraph_edit'


def load(name: str) -> dict:
    return nn._validated_census(census.parse_hwpml2x_census((ROOT / f'{name}.hwpml2x').read_text(encoding='utf-8')))


class ObservedPageNumberRenumbering(unittest.TestCase):
    def test_renumbered_font_and_shape_references_are_not_a_document_change(self) -> None:
        before, after = load('page_before'), load('page_after')
        self.assertEqual(nn._page_number_delta_problems(before, after, request_start=3, require_restart=True), [])

    def test_the_observed_reference_shift_is_really_in_the_snapshots(self) -> None:
        before = (ROOT / 'page_before.hwpml2x').read_text(encoding='utf-8')
        after = (ROOT / 'page_after.hwpml2x').read_text(encoding='utf-8')
        self.assertIn('<CHARSHAPELIST Count="2">', before)
        self.assertIn('<CHARSHAPELIST Count="3">', after)
        self.assertNotEqual(load('page_before')['definitions']['facenamelist'], load('page_after')['definitions']['facenamelist'])

    def test_a_changed_existing_font_is_still_a_document_change(self) -> None:
        text = (ROOT / 'page_after.hwpml2x').read_text(encoding='utf-8').replace('함초롬돋움', '다른글꼴')
        after = nn._validated_census(census.parse_hwpml2x_census(text))
        problems = nn._page_number_delta_problems(load('page_before'), after, request_start=3, require_restart=True)
        self.assertTrue(any('facenamelist' in item or 'charshapelist' in item for item in problems), problems)

    def test_a_changed_character_height_is_still_a_document_change(self) -> None:
        text = (ROOT / 'page_after.hwpml2x').read_text(encoding='utf-8').replace('Height="1400"', 'Height="1500"')
        after = nn._validated_census(census.parse_hwpml2x_census(text))
        self.assertNotEqual(nn._page_number_delta_problems(load('page_before'), after, request_start=3, require_restart=True), [])

    def test_a_removed_character_shape_is_still_a_document_change(self) -> None:
        before, after = load('page_before'), load('page_after')
        reduced = {**after, 'definitions': {**after['definitions'], 'charshapelist': after['definitions']['charshapelist'][:1]}}
        self.assertTrue(any('charshapelist' in item for item in nn._page_number_definition_problems(before['definitions'], reduced['definitions'])))


class ObservedRemoveAndUndo(unittest.TestCase):
    def paragraphs(self, name):
        return load(name)['paragraphs']

    def test_the_paragraph_after_a_removal_keeps_its_content_and_level(self) -> None:
        before = self.paragraphs('remove_before')
        after = self.paragraphs('remove_after')
        self.assertEqual(len(after), len(before) - 1)
        index = next(i for i, item in enumerate(before) if item['text'].startswith('항목병'))
        for left, right in zip(before[:index] + before[index + 1:], after):
            self.assertEqual((left['text'], left['heading'], left['level'], left['content_sha256']),
                             (right['text'], right['heading'], right['level'], right['content_sha256']))

    def test_one_undo_restores_content_although_native_identifiers_and_an_empty_run_differ(self) -> None:
        before, undone = self.paragraphs('remove_before'), self.paragraphs('remove_undone')
        self.assertNotEqual([item['sha256'] for item in before], [item['sha256'] for item in undone])
        self.assertEqual([item['content_sha256'] for item in before], [item['content_sha256'] for item in undone])
        self.assertEqual([(item['heading'], item['level']) for item in before], [(item['heading'], item['level']) for item in undone])

    def test_structure_facts_come_from_the_native_elements(self) -> None:
        paragraphs = self.paragraphs('remove_before')
        by_text = {item['text']: item for item in paragraphs}
        self.assertEqual(by_text['그림설명-보존 문구']['inline_controls'], ['picture'])
        self.assertTrue(by_text['둘째쪽센티널-쪽 넘김 문단']['page_break'])
        self.assertIsNone(by_text['표셀A1-보존']['body_index'])
        self.assertEqual(by_text['본문센티널-제목 보존 문단']['body_index'], 0)
        self.assertEqual(by_text['항목갑 첫째 수준']['body_index'], 4)


if __name__ == '__main__':
    unittest.main()
