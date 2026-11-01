"""F03, F11: production review functions over truncated, missing and hostile inputs. Portable; not Hancom evidence."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from hwpx_mcp import review


def para(text, heading='NONE', level=0, in_body=True):
    return {'text': text, 'heading': heading, 'level': level, 'in_body': in_body, 'content_sha256': 'c:' + text}


EVIDENCE = {'header_footer': ['h'], 'styles_sha256': 's', 'media_sha256': 'm', 'numberings_sha256': 'n', 'bindata_sha256': 'b'}


def built(paragraphs, texts, *, page_count=None, page_numbers=(), restarts=(), truncated=False, total=None, baseline=None, evidence=None, available=True, reason=None, controls=None, pages=None):
    """The same assembly the adapter runs; only the backend response and the rendered text are inputs."""
    readback = {'available': available, 'reason': reason, 'source_filename': 'd.hwpx', 'document_is_modified': False, 'page_count': page_count if page_count is not None else (len(texts) if texts else 1),
                'paragraphs': paragraphs, 'paragraphs_truncated': truncated, 'paragraph_total': total if total is not None else len(paragraphs or []),
                'controls': {} if controls is None else controls, 'page_numbers': list(page_numbers), 'page_restarts': list(restarts), 'evidence': dict(EVIDENCE) if evidence is None else evidence, 'caret_unchanged': True}
    rendered = pages if pages is not None else len(texts or [])
    return review.assemble(session_id='s', review_id='s/review-2', readback=readback, pdf={'path': 'p.pdf', 'sha256': 'x'} if rendered else None,
                           pages=[{'page': n + 1, 'path': f'{n}.png', 'sha256': 'y', 'width': 1, 'height': 1} for n in range(rendered)],
                           texts=texts, baseline=baseline, saved_output=None, render_error=None if rendered else 'RENDER_FAILED')


OUTLINE = [para('Alpha', 'OUTLINE', 0), para('Beta', 'OUTLINE', 0)]


class Coverage(unittest.TestCase):
    def test_complete_only_when_everything_was_seen_and_matches(self):
        mark = {'kind': 'outline', 'source': 'paragraph_end', 'bold': False}
        marked = [{**item, 'format': {'mark': mark}} for item in OUTLINE]
        first = built(marked, ['1. Alpha\n2. Beta\n'])
        self.assertEqual(first['review_status'], 'partial', 'the labels match, but the look of the numbers has nothing to be compared with')
        self.assertIs(first['numbering_all_match'], True)
        result = built(marked, ['1. Alpha\n2. Beta\n'], baseline=first)
        self.assertEqual(result['review_status'], 'complete')
        self.assertIs(result['numbering_all_match'], True)
        self.assertIn('모두 기대와 일치', result['summary_text'])

    def test_a_printed_wrong_number_is_a_mismatch_and_says_so_first(self):
        result = built(OUTLINE, ['1. Alpha\n9. Beta\n'])
        self.assertEqual(result['review_status'], 'mismatch')
        self.assertIs(result['numbering_all_match'], False)
        self.assertIn('불일치', result['summary_text'].splitlines()[0])
        self.assertEqual([row['status'] for row in result['numbering']], ['match', 'mismatch'])

    def test_truncated_paragraphs_are_partial_never_all_match(self):
        result = built(OUTLINE, ['1. Alpha\n2. Beta\n'], truncated=True, total=2500)
        self.assertEqual(result['review_status'], 'partial')
        self.assertIsNone(result['numbering_all_match'])
        self.assertNotIn('모두 기대와 일치', result['summary_text'])
        self.assertIn('2500', result['summary_text'].splitlines()[0])
        self.assertEqual(result['coverage']['paragraphs'], {'state': 'partial', 'reviewed': 2, 'total': 2500})

    def test_labels_beyond_the_rendered_pages_are_outside_coverage_not_failures(self):
        result = built(OUTLINE, ['1. Alpha\n'], page_count=5)
        self.assertEqual(result['review_status'], 'partial')
        self.assertEqual([row['status'] for row in result['numbering']], ['match', 'outside_rendered_pages'])
        self.assertIsNone(result['numbering_all_match'])
        self.assertNotIn('기대와 다른', result['summary_text'])
        self.assertEqual(result['coverage']['pages'], {'state': 'partial', 'rendered': 1, 'total': 5})

    def test_a_missing_page_number_inside_coverage_is_a_mismatch_even_when_partial(self):
        result = built(OUTLINE, ['1. Alpha\nfoot\n'], page_count=5, page_numbers=[{'draw_pos': 5, 'side_char': 45}], restarts=['3'])
        self.assertEqual(result['review_status'], 'mismatch')
        self.assertIs(result['page_numbers']['all_found'], False)

    def test_page_numbers_on_rendered_pages_of_a_longer_document_are_not_all_found(self):
        result = built([], ['foot  - 3 -\n'], page_count=5, page_numbers=[{'draw_pos': 5, 'side_char': 45}], restarts=['3'])
        self.assertIsNone(result['page_numbers']['all_found'])
        self.assertEqual(result['page_numbers']['state'], 'partial')
        self.assertNotIn('모든 쪽에서 확인됨', result['summary_text'])

    def test_unreadable_pdf_text_is_unverified_with_a_null_page_number_result(self):
        result = built(OUTLINE, None, page_count=2, pages=2, page_numbers=[{'draw_pos': 5, 'side_char': 45}], restarts=['3'])
        self.assertEqual(result['review_status'], 'unverified')
        self.assertIsNone(result['page_numbers']['all_found'])
        self.assertIsNone(result['numbering_all_match'])
        self.assertEqual({row['status'] for row in result['numbering']}, {'unverified'})
        self.assertNotIn('기대와 다른', result['summary_text'])

    def test_blank_pdf_text_is_treated_like_unreadable_text(self):
        result = built(OUTLINE, ['', ''], page_count=2)
        self.assertEqual(result['review_status'], 'unverified')
        self.assertEqual({row['status'] for row in result['numbering']}, {'unverified'})

    def test_an_unavailable_readback_is_unverified_and_keeps_its_previews(self):
        result = built(None, ['1. Alpha\n'], available=False, reason='census_unavailable', page_count=1)
        self.assertEqual(result['review_status'], 'unverified')
        self.assertIn('문서 구조', result['summary_text'].splitlines()[0])
        self.assertNotIn('번호 문단 0개', result['summary_text'])
        self.assertEqual(result['coverage']['paragraphs']['state'], 'unverified')
        self.assertEqual(len(result['pages']), 1)

    def test_structured_and_readable_results_say_the_same(self):
        for args, kwargs in (((OUTLINE, ['1. Alpha\n2. Beta\n']), {}), ((OUTLINE, ['1. Alpha\n']), {'page_count': 5}), ((OUTLINE, None), {'pages': 1})):
            result = built(*args, **kwargs)
            self.assertEqual(result['coverage']['status'] if result['review_status'] != 'mismatch' else 'mismatch', result['review_status'])
            self.assertIn(review.status_label(result['review_status']), result['summary_text'].splitlines()[0])
            self.assertIn(review.status_label(result['review_status']), review.to_markdown(result))


class Comparison(unittest.TestCase):
    def base(self, paragraphs=None, **kw):
        return built(paragraphs if paragraphs is not None else OUTLINE, ['1. Alpha\n2. Beta\n'], **kw)

    def test_evidence_missing_on_both_sides_is_unproven_not_unchanged(self):
        missing = {**EVIDENCE, 'header_footer': None}
        result = built(OUTLINE, ['1. Alpha\n2. Beta\n'], baseline=self.base(evidence=missing), evidence=missing)
        self.assertEqual(result['comparison']['evidence']['header_footer'], 'unproven')
        self.assertNotIn('머리말·꼬리말, 스타일', result['summary_text'])
        self.assertIn('확인 못함', result['summary_text'])
        self.assertEqual(result['review_status'], 'partial')

    def test_a_truncated_side_cannot_claim_no_changed_paragraphs(self):
        result = built(OUTLINE, ['1. Alpha\n2. Beta\n'], truncated=True, total=2500, baseline=self.base())
        self.assertEqual(result['comparison']['state'], 'partial')
        self.assertIsNone(result['comparison']['nested_paragraphs_identical'])
        self.assertNotIn('바뀐 문단 없음', result['summary_text'].replace('확인한 범위에서 바뀐 문단 없음', ''))
        self.assertIn('확인한 범위에서', result['summary_text'])

    def test_truncation_does_not_invent_removals_at_the_cut(self):
        before = [para(f'P{n}') for n in range(5)]
        after = [para('New'), *[para(f'P{n}') for n in range(4)]]
        result = built(after, ['x\n'], truncated=True, total=6, baseline=built(before, ['x\n'], truncated=True, total=5))
        self.assertEqual([(c['kind'], c['text']) for c in result['comparison']['changes']], [('added', 'New')])

    def test_a_baseline_without_a_structure_cannot_compare_paragraphs(self):
        baseline = built(None, ['x\n'], available=False, reason='census_unavailable')
        result = built(OUTLINE, ['1. Alpha\n2. Beta\n'], baseline=baseline)
        self.assertEqual(result['comparison']['state'], 'unverified')
        self.assertEqual(result['comparison']['changes'], [])
        self.assertNotIn('바뀐 문단 없음', result['summary_text'])

    def test_an_uncovered_label_is_not_a_number_change_while_the_level_is_the_same(self):
        baseline = self.base()
        result = built(OUTLINE, ['1. Alpha\n'], page_count=5, baseline=baseline)
        self.assertEqual(result['comparison']['changes'], [])
        self.assertEqual(result['comparison']['numbers_not_compared'], 1)
        self.assertEqual(result['comparison']['state'], 'partial')
        self.assertIn('수준만 비교', result['summary_text'])

    def test_a_partial_baseline_keeps_a_later_whole_review_from_reading_as_complete(self):
        baseline = built(OUTLINE, ['1. Alpha\n'], page_count=2)
        result = built(OUTLINE, ['1. Alpha\n', '2. Beta\n'], baseline=baseline)
        self.assertEqual(result['comparison']['numbers_not_compared'], 1)
        self.assertEqual(result['review_status'], 'partial')
        self.assertIs(result['numbering_all_match'], True)

    def test_an_uncovered_label_with_a_changed_level_is_still_a_change(self):
        baseline = self.base()
        result = built([para('Alpha', 'OUTLINE', 0), para('Beta', 'OUTLINE', 2)], ['1. Alpha\n'], page_count=5, baseline=baseline)
        self.assertEqual([c['kind'] for c in result['comparison']['changes']], ['number_changed'])

    def test_real_changes_still_report(self):
        changed = {**EVIDENCE, 'media_sha256': 'other'}
        result = built([*OUTLINE, para('New')], ['1. Alpha\n2. Beta\nNew\n'], baseline=self.base(), evidence=changed)
        self.assertEqual(result['comparison']['evidence']['media_sha256'], 'changed')
        self.assertEqual([c['kind'] for c in result['comparison']['changes']], ['added'])
        self.assertIn('변경 감지', result['summary_text'])


class PageNumberPrimitive(unittest.TestCase):
    def test_no_text_is_not_all_found(self):
        control = {'page_numbers': [{'draw_pos': 5, 'side_char': 45}], 'page_restarts': ['3']}
        self.assertIsNone(review.rendered_page_numbers(control, [])['all_found'])


class EvidencePrimitive(unittest.TestCase):
    def test_none_on_both_sides_is_not_unchanged(self):
        side = {'review_id': 'a/review-1', 'paragraphs': [], 'numbering': [], 'controls': {}, 'evidence': {**EVIDENCE, 'header_footer': None}}
        self.assertEqual(review.compare(side, dict(side), [])['evidence']['header_footer'], 'unproven')


class BoundedPackage(unittest.TestCase):
    """F11: declared and actual sizes are bounded and hostile members never raise past the caller."""

    def zip_with(self, members, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'x.hwpx'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def test_a_highly_compressed_section_is_not_read_into_memory(self):
        path = self.zip_with([('Contents/section0.xml', b'\0' * (review.MAX_PACKAGE_TEXT_BYTES + 1))])
        with self.assertRaises(review.PackageUnreadable) as caught:
            review.package_inventory(path)
        self.assertEqual(caught.exception.code, 'package_limit')

    def test_many_members_or_a_large_total_are_refused(self):
        path = self.zip_with([(f'BinData/m{n}', b'') for n in range(review.MAX_PACKAGE_MEMBERS + 1)])
        with self.assertRaises(review.PackageUnreadable):
            review.package_inventory(path)

    def test_corrupt_data_is_a_package_error_not_a_raw_exception(self):
        path = self.zip_with([('Contents/section0.xml', b'<a/>' * 1000)])
        raw = bytearray(path.read_bytes())
        start = raw.find(b'section0.xml') + len('section0.xml')
        raw[start:start + 20] = b'\xff' * 20
        path.write_bytes(bytes(raw))
        with self.assertRaises(review.PackageUnreadable) as caught:
            review.package_inventory(path)
        self.assertEqual(caught.exception.code, 'package_corrupt')

    def test_not_a_zip_is_a_package_error(self):
        path = self.zip_with([('a', b'b')])
        path.write_bytes(b'not a zip')
        with self.assertRaises(review.PackageUnreadable):
            review.package_inventory(path)


if __name__ == '__main__':
    unittest.main()
