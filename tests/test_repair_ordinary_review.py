"""N2, N3, N8, N9: review and destructive intent on ordinary documents, through the real readback and review assembly.

Snapshots under tests/fixtures/native_ordinary are HWPML2X captured from real Hancom on the unchanged baseline for
disposable synthetic documents. Replaying them runs real parsing and comparison; it is not a new native run.
"""
from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import native_numbering_census as census, native_paragraph_edit as pe
from hwpx_mcp import review
from tests import test_native_numbering_preservation as preservation
from tests.test_repair_paragraph_targets import Replay
from tests.test_repair_review_rules import EVIDENCE, OUTLINE, built, para
from tests.test_repair_runtime_safety import owned_doc, readback

ROOT = Path(__file__).parent / 'fixtures' / 'native_ordinary'
REPO = Path(__file__).parent.parent


def capture(name):
    return (ROOT / f'{name}.hwpml2x').read_text(encoding='utf-8')


def captured_readback(case, name):
    owned = owned_doc(case)
    owned.hwp.hwpml_text = capture(name)
    return readback(owned), owned


def assembled(rb, texts, baseline=None):
    """The real assembly over a real readback; the printed text is an input."""
    pages = [{'page': n + 1, 'path': f'{n}.png', 'sha256': 'y', 'width': 1, 'height': 1} for n in range(len(texts))]
    return review.assemble(session_id='s', review_id='s/review-2', readback={**rb, 'page_count': len(texts), 'source_filename': 'd.hwpx'}, pdf={'path': 'p.pdf', 'sha256': 'x'},
                           pages=pages, texts=texts, baseline=baseline, saved_output=None, render_error=None)


def printed(rb):
    return ['\n'.join(item['text'] for item in rb['paragraphs'])]


class PictureFreeDocument(unittest.TestCase):
    """N2: a complete empty inventory is evidence; a missing or unreadable one is not."""

    def test_captured_picture_free_document_reviews_completely_twice(self):
        rb, owned = captured_readback(self, 'nopicture')
        self.assertIs(rb['available'], True)
        first = assembled(rb, printed(rb))
        self.assertEqual(first['review_status'], 'complete', first['summary_text'])
        second = assembled(rb, printed(rb), baseline=first)
        self.assertEqual(second['comparison']['evidence']['bindata_sha256'], 'unchanged')
        self.assertEqual(second['comparison']['state'], 'complete')
        self.assertEqual(second['review_status'], 'complete', second['summary_text'])
        self.assertNotIn('확인 못함', second['summary_text'])
        self.assertFalse(owned.session._native_custody_held)

    def test_earlier_native_capture_without_any_binary_list(self):
        raw = (Path(__file__).parent / 'fixtures' / 'native_numbering' / 'native13' / 'raw-1.hwpml2x').read_text(encoding='utf-8')
        self.assertNotIn('BINDATALIST', raw)
        owned = owned_doc(self)
        owned.hwp.hwpml_text = raw
        rb = readback(owned)
        again = assembled(rb, printed(rb), baseline=assembled(rb, printed(rb)))
        self.assertEqual(again['comparison']['evidence']['bindata_sha256'], 'unchanged')

    def test_a_picture_appearing_is_a_change_not_equal_absence(self):
        rb, _owned = captured_readback(self, 'nopicture')
        with_picture, _other = captured_readback(self, 'tabbed')
        before = assembled(rb, printed(rb))
        after = assembled(with_picture, printed(with_picture), baseline=before)
        self.assertEqual(after['comparison']['evidence']['bindata_sha256'], 'changed')

    def test_unresolved_paragraphs_never_claim_an_empty_inventory(self):
        owned = owned_doc(self)
        owned.hwp.hwpml_text = capture('nopicture').replace('ParaShape="0"', 'ParaShape="9999"', 1)
        rb = readback(owned)
        self.assertEqual(rb['reason'], 'paragraphs_unresolved')
        self.assertIsNone(rb['evidence']['bindata_sha256'])

    def test_storage_without_a_list_is_not_empty(self):
        owned = owned_doc(self)
        owned.hwp.hwpml_text = capture('nopicture').replace('</TAIL>', '<BINDATASTORAGE><BINDATA Id="1">AAAA</BINDATA></BINDATASTORAGE></TAIL>', 1)
        self.assertIn('BINDATASTORAGE', owned.hwp.hwpml_text)
        self.assertIsNone(readback(owned)['evidence']['bindata_sha256'])

    def test_missing_evidence_is_still_unproven(self):
        missing = {**EVIDENCE, 'bindata_sha256': None}
        first = built(OUTLINE, ['1. Alpha\n2. Beta\n'], evidence=missing)
        second = built(OUTLINE, ['1. Alpha\n2. Beta\n'], evidence=missing, baseline=first)
        self.assertEqual(second['comparison']['evidence']['bindata_sha256'], 'unproven')
        self.assertEqual(second['review_status'], 'partial')
        self.assertIn('확인 못함', second['summary_text'])

    def test_both_reviews_unavailable_are_not_preservation(self):
        first = built(None, ['text\n'], available=False, reason='census_unavailable', evidence={})
        first['evidence'] = None
        second = built(None, ['text\n'], available=False, reason='census_unavailable', evidence={}, baseline=first)
        self.assertEqual(second['comparison']['state'], 'unverified')
        self.assertTrue(all(state == 'unproven' for state in second['comparison']['evidence'].values()))


class ListKinds(unittest.TestCase):
    """N3: automatic numbers, bullets and nested outline numbers stay visible in coverage and comparison."""

    def test_captured_list_kinds_are_reported_not_complete(self):
        rb, _owned = captured_readback(self, 'listkinds')
        kinds = {item['text']: item['heading'] for item in rb['paragraphs']}
        self.assertEqual(kinds['번호종류-자동번호 첫째 문단'], 'NUMBER')
        self.assertEqual(kinds['번호종류-글머리표 문단'], 'BULLET')
        self.assertEqual(kinds['표셀개요-셀안 개요 문단'], 'OUTLINE')
        result = assembled(rb, printed(rb))
        self.assertEqual(result['review_status'], 'partial', result['summary_text'])
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'unverified', 'number': 2, 'bullet': 1, 'nested_outline': 1})
        self.assertIsNone(result['numbering_all_match'])
        first_line = result['summary_text'].splitlines()[0]
        self.assertIn('자동 번호 2개', first_line)
        self.assertIn('글머리표 1개', first_line)
        self.assertIn('표 셀 안 개요 번호 1개', first_line)
        self.assertNotIn('전체 확인 완료', result['summary_text'])
        markdown = review.to_markdown(result)
        self.assertIn('## 인쇄된 번호를 확인하지 않은 문단', markdown)
        self.assertIn('번호종류-글머리표 문단', markdown)
        self.assertIn('- 자동 번호·글머리표: 자동 번호 2개, 글머리표 1개, 표 셀 안 개요 번호 1개는 확인하지 않음', markdown)

    def test_captured_same_text_without_lists_is_complete(self):
        rb, _owned = captured_readback(self, 'listplain')
        result = assembled(rb, printed(rb))
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'none', 'number': 0, 'bullet': 0, 'nested_outline': 0})
        self.assertEqual(result['review_status'], 'complete', result['summary_text'])
        self.assertNotIn('자동 번호', result['summary_text'])
        self.assertNotIn('인쇄된 번호를 확인하지 않은', review.to_markdown(result))

    def test_captured_comparison_sees_numbering_appear(self):
        plain, _a = captured_readback(self, 'listplain')
        lists, _b = captured_readback(self, 'listkinds')
        result = assembled(lists, printed(lists), baseline=assembled(plain, printed(plain)))
        changed = {change['text']: change for change in result['comparison']['changes'] if change['kind'] == 'number_changed'}
        self.assertEqual(changed['번호종류-자동번호 첫째 문단']['before'], None)
        self.assertEqual(changed['번호종류-자동번호 첫째 문단']['after'], '자동 번호 1수준')
        self.assertEqual(changed['번호종류-자동번호 둘째 문단']['after'], '자동 번호 2수준')
        self.assertEqual(changed['번호종류-글머리표 문단']['after'], '글머리표')
        self.assertIn('번호 변경', result['summary_text'])
        self.assertIn('번호 없음 → 글머리표', review.to_markdown(result))

    def rows(self, *kinds):
        return [para(f'P{index}', heading, level) for index, (heading, level) in enumerate(kinds)]

    def printed_rows(self, count):
        return ['\n'.join(f'P{index}' for index in range(count)) + '\n']

    def test_bullet_only_document_is_not_complete(self):
        result = built(self.rows(('BULLET', 0), ('NONE', 0)), self.printed_rows(2))
        self.assertEqual(result['review_status'], 'partial')
        self.assertEqual(result['coverage']['list_numbering']['bullet'], 1)
        self.assertIn('글머리표 1개', result['summary_text'])

    def test_nested_outline_is_not_complete(self):
        paragraphs = [para('In cell', 'OUTLINE', 0, in_body=False), para('Body')]
        result = built(paragraphs, ['In cell\nBody\n'])
        self.assertEqual(result['coverage']['list_numbering']['nested_outline'], 1)
        self.assertEqual(result['review_status'], 'partial')

    def test_outline_rows_match_but_lists_still_block_the_all_match_claim(self):
        paragraphs = [para('Alpha', 'OUTLINE', 0), para('Bullet', 'BULLET', 0)]
        result = built(paragraphs, ['1. Alpha\nBullet\n'])
        self.assertEqual(result['review_status'], 'partial')
        self.assertIsNone(result['numbering_all_match'])
        self.assertNotIn('모두 기대와 일치', result['summary_text'])
        self.assertIn('확인한 1개는 일치', result['summary_text'])

    def test_a_printed_mismatch_still_wins(self):
        paragraphs = [para('Alpha', 'OUTLINE', 0), para('Bullet', 'BULLET', 0)]
        result = built(paragraphs, ['9. Alpha\nBullet\n'])
        self.assertEqual(result['review_status'], 'mismatch')
        self.assertIs(result['numbering_all_match'], False)

    def compare(self, before, after):
        baseline = built(before, self.printed_rows(len(before)))
        return built(after, self.printed_rows(len(after)), baseline=baseline)

    def test_plain_paragraph_becoming_a_list_is_a_change(self):
        for heading, word in (('NUMBER', '자동 번호 1수준'), ('BULLET', '글머리표')):
            with self.subTest(heading=heading):
                result = self.compare(self.rows(('NONE', 0)), self.rows((heading, 0)))
                changes = [c for c in result['comparison']['changes'] if c['kind'] == 'number_changed']
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]['after'], word)
                self.assertIn('번호 변경 1건', result['summary_text'])

    def test_a_list_level_change_is_a_change(self):
        result = self.compare(self.rows(('NUMBER', 0)), self.rows(('NUMBER', 1)))
        self.assertEqual([c['kind'] for c in result['comparison']['changes']], ['number_changed'])
        self.assertEqual(result['comparison']['changes'][0]['before'], '자동 번호 1수준')
        self.assertEqual(result['comparison']['changes'][0]['after'], '자동 번호 2수준')

    def test_a_changed_numbering_definition_on_the_paragraph_is_a_change(self):
        before = [{**para('P0', 'NUMBER', 0), 'numbering_ref_sha256': 'a'}]
        after = [{**para('P0', 'NUMBER', 0), 'numbering_ref_sha256': 'b'}]
        result = self.compare(before, after)
        self.assertEqual([c['kind'] for c in result['comparison']['changes']], ['number_changed'])

    def test_a_nested_outline_level_change_is_a_change(self):
        before = [para('Cell', 'OUTLINE', 0, in_body=False)]
        after = [para('Cell', 'OUTLINE', 1, in_body=False)]
        result = self.compare(before, after)
        self.assertEqual([c['kind'] for c in result['comparison']['changes']], ['number_changed'])

    def test_unchanged_lists_are_counted_as_not_compared_not_as_identical_numbers(self):
        rows = self.rows(('NUMBER', 0), ('BULLET', 0), ('NONE', 0))
        result = self.compare(rows, rows)
        self.assertEqual(result['comparison']['changes'], [])
        self.assertEqual(result['comparison']['numbers_not_compared'], 2)
        self.assertEqual(result['comparison']['state'], 'partial')
        self.assertIn('번호 2개는 인쇄된 번호를 읽지 않았거나 읽지 못해 종류와 수준만 비교했습니다.', result['summary_text'])

    def test_a_removed_list_paragraph_says_what_it_was(self):
        result = self.compare(self.rows(('BULLET', 0), ('NONE', 0)), [para('P1')])
        removed = [c for c in result['comparison']['changes'] if c['kind'] == 'removed']
        self.assertEqual(removed[0]['before'], '글머리표')

    def test_plain_documents_compare_completely(self):
        rows = self.rows(('NONE', 0), ('NONE', 0))
        result = self.compare(rows, rows)
        self.assertEqual(result['comparison']['state'], 'complete')
        self.assertEqual(result['review_status'], 'complete')
        self.assertEqual(result['coverage']['list_numbering']['state'], 'none')

    def test_the_readback_carries_the_paragraph_numbering_definition(self):
        rb, _owned = captured_readback(self, 'listkinds')
        by_text = {item['text']: item for item in rb['paragraphs']}
        self.assertTrue(by_text['번호종류-자동번호 첫째 문단']['numbering_ref_sha256'])
        self.assertTrue(by_text['번호종류-글머리표 문단']['numbering_ref_sha256'])
        self.assertIsNone(by_text['본문센티널-제목 보존 문단']['numbering_ref_sha256'])


class WholeParagraphText(unittest.TestCase):
    """N9: text after a tab or a line break is part of the paragraph, exactly as Hancom holds it."""

    FULL = '탭문단-앞글자\t탭뒤글자\n줄바꿈뒤글자 묶음빈칸뒤글자 고정폭뒤글자하이픈뒤글자'

    def paragraph(self):
        parsed = census.parse_hwpml2x_census(capture('tabbed'))
        return next(item for item in parsed['paragraphs'] if item['text'].startswith('탭문단-앞글자'))

    def test_captured_text_keeps_everything_after_the_inline_elements(self):
        self.assertEqual(self.paragraph()['text'], self.FULL)

    def test_a_known_inline_element_is_not_a_control(self):
        self.assertEqual(self.paragraph()['inline_controls'], [])

    def test_an_unknown_inline_element_is_a_control_so_removal_refuses(self):
        text = capture('tabbed').replace('<TAB/>', '<TITLEMARK Ignore="false"/>', 1)
        parsed = census.parse_hwpml2x_census(text)
        item = next(p for p in parsed['paragraphs'] if p['text'].startswith('탭문단-앞글자'))
        self.assertEqual(item['inline_controls'], ['titlemark'])

    def test_the_census_finds_a_match_after_the_tab(self):
        parsed = census.parse_hwpml2x_census(capture('tabbed'))
        self.assertEqual(len(pe._census_hits(parsed['paragraphs'], '탭뒤글자', True)), 1)

    def test_removal_with_only_the_text_before_the_tab_is_refused(self):
        raw = capture('tabbed')
        hwp = Replay(raw, raw)
        with self.assertRaises(pe.NativeNumberingRefusal) as caught:
            pe.remove_paragraph(hwp, match='탭문단-앞글자', expected_text='탭문단-앞글자', protection=lambda: census.parse_hwpml2x_census(hwp.hwpml_text))
        self.assertEqual(caught.exception.code, 'target_text_mismatch')
        self.assertNotIn(('Run', 'Delete'), hwp.calls)

    def test_removal_with_lossy_text_is_refused(self):
        raw = capture('tabbed')
        for lossy in (self.FULL.replace('\t', ' '), self.FULL.replace('\n', ''), self.FULL.replace(' ', ' '), self.FULL + ' '):
            with self.subTest(text=lossy):
                hwp = Replay(raw, raw)
                with self.assertRaises(pe.NativeNumberingRefusal) as caught:
                    pe.remove_paragraph(hwp, match='탭문단-앞글자', expected_text=lossy, protection=lambda: census.parse_hwpml2x_census(hwp.hwpml_text))
                self.assertEqual(caught.exception.code, 'target_text_mismatch')

    def test_removal_with_the_exact_full_text_reaches_the_native_call(self):
        raw = capture('tabbed')
        hwp = Replay(raw, raw)
        with self.assertRaises(pe.NativeNumberingMutationError):  # The replay keeps the same after-text, so the outcome is reported as unknown.
            pe.remove_paragraph(hwp, match='탭문단-앞글자', expected_text=self.FULL, protection=lambda: census.parse_hwpml2x_census(hwp.hwpml_text))
        self.assertIn(('Run', 'Delete'), hwp.calls)

    def test_a_change_after_the_tab_is_a_text_change_in_the_review(self):
        before, _a = captured_readback(self, 'tabbed')
        owned = owned_doc(self)
        owned.hwp.hwpml_text = capture('tabbed').replace('탭뒤글자', '바뀐글자')
        after = readback(owned)
        result = assembled(after, printed(after), baseline=assembled(before, printed(before)))
        kinds = {change['kind'] for change in result['comparison']['changes']}
        self.assertEqual(kinds, {'added', 'removed'})

    def test_printed_label_is_read_when_a_line_break_comes_early(self):
        row = review.rendered_numbering([para('짧음\n둘째 줄이 있는 개요 문단입니다', 'OUTLINE', 0)], ['1. 짧음\n둘째 줄이 있는 개요 문단입니다\n'])[0]
        self.assertEqual(row['status'], 'match')
        self.assertEqual(row['rendered_label'], '1.')


class PackageMemoryLimit(unittest.TestCase):
    """N8: the section text kept in memory has one limit for the whole package, not one per member."""

    def package(self, *members):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'p.hwpx'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def test_members_each_below_but_together_above_the_limit(self):
        path = self.package(('Contents/section0.xml', b'a' * 600), ('Contents/section1.xml', b'b' * 600))
        with patch.object(review, 'MAX_PACKAGE_TEXT_BYTES', 1000):
            with self.assertRaises(review.PackageUnreadable) as caught:
                review.package_inventory(path)
        self.assertEqual(caught.exception.code, 'package_limit')

    def test_the_limit_is_read_before_any_member_is_kept(self):
        path = self.package(('Contents/section0.xml', b'a' * 600), ('Contents/section1.xml', b'b' * 600))
        with patch.object(review, 'MAX_PACKAGE_TEXT_BYTES', 1000), patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('read a member')):
            with self.assertRaises(review.PackageUnreadable):
                review.package_inventory(path)

    def test_a_package_at_the_limit_is_inspected(self):
        path = self.package(('Contents/section0.xml', b'a' * 500), ('Contents/section1.xml', b'b' * 500))
        with patch.object(review, 'MAX_PACKAGE_TEXT_BYTES', 1000):
            self.assertEqual(review.package_inventory(path)['sections'], 2)

    def test_binary_members_do_not_count_as_text(self):
        path = self.package(('Contents/section0.xml', b'a' * 500), ('BinData/image1.png', b'p' * 5000))
        with patch.object(review, 'MAX_PACKAGE_TEXT_BYTES', 1000):
            self.assertEqual(len(review.package_inventory(path)['bindata']), 1)

    def test_the_setup_guide_states_the_limits_the_code_has(self):
        guide = (REPO / 'docs' / 'MCP_SETUP.md').read_text(encoding='utf-8')
        # server.py needs the adapter's own environment, so its constants are read from the source here.
        source = (REPO / 'hwpx_mcp' / 'server.py').read_text(encoding='utf-8')
        limits = [int(re.search(rf'^{name} = (\d+) \* 1024 \* 1024$', source, re.M).group(1)) for name in ('MAX_FILE', 'MAX_SAVED_FILE')]
        self.assertEqual(limits, [25, 256])
        for value in (*limits, review.MAX_PACKAGE_TEXT_BYTES // (1024 * 1024)):
            self.assertIn(f'{value} MiB', guide)
        self.assertIn('SOURCE_SIZE_LIMIT', guide)
        self.assertIn('ARTIFACT_SIZE_LIMIT', guide)
        self.assertIn('package_error', guide)


if __name__ == '__main__':
    unittest.main()
