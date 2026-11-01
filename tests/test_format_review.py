"""The readable review of formatting changes and the independent saved-package check. Pure inputs; not native evidence."""
from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
import unittest.mock
import zipfile

from hwpx_mcp import review as rules


def run(text, **changes):
    return {'text': text, 'length': len(text), 'font_family': '함초롬바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000', **changes}


PARAGRAPH = {'align': 'justify', 'left_indent_pt': 0.0, 'right_indent_pt': 0.0, 'first_line_indent_pt': 0.0, 'line_spacing_type': 'percent', 'line_spacing_value': 160}


def para(text, runs=None, sha=None, index=0, truncated=False, **shape):
    return {'index': index, 'text': text, 'heading': 'NONE', 'level': 0, 'in_body': True, 'context': 'body', 'content_sha256': sha or ('sha' + text),
            'format': {'runs': runs if runs is not None else [run(text)], 'runs_truncated': truncated, 'paragraph': {**PARAGRAPH, **shape}}}


def side(paragraphs, review_id='s/review-1'):
    return {'review_id': review_id, 'paragraphs': paragraphs, 'paragraphs_truncated': False, 'evidence': {'header_footer': [], 'styles_sha256': 'a', 'media_sha256': 'b', 'numberings_sha256': 'absent', 'bindata_sha256': 'absent'}, 'controls': {}, 'numbering': [], 'coverage': {'paragraphs': {'state': 'complete'}}}


def assembled(baseline_paragraphs, current_paragraphs, **kwargs):
    readback = {'available': True, 'paragraphs': current_paragraphs, 'paragraph_total': len(current_paragraphs), 'evidence': side([])['evidence'], 'controls': {}, 'page_count': 1, 'page_numbers': [], 'document_is_modified': True, 'source_filename': 'doc.hwpx'}
    return rules.assemble(session_id='s', review_id='s/review-2', readback=readback, pdf=None, pages=[{'page': 1, 'path': 'p.png', 'sha256': 'x', 'width': 1, 'height': 1}], texts=['문서'],
                          baseline=side(baseline_paragraphs), saved_output=None, render_error=None, **kwargs)


class FormatChanges(unittest.TestCase):
    def test_a_substring_change_names_the_range_and_each_property(self) -> None:
        before = para('앞 표적 뒤', index=3)
        after = para('앞 표적 뒤', [run('앞 '), run('표적', bold=True, size_pt=14.0, text_color='#C00000'), run(' 뒤')], sha='changed', index=3)
        review = assembled([before], [after])
        change = review['comparison']['changes'][0]
        self.assertEqual(change['kind'], 'format_changed')
        self.assertEqual(change['paragraph_index'], 3)
        self.assertEqual(change['ranges'], [{'text': '표적', 'changes': {
            'size_pt': {'before': 10.0, 'after': 14.0}, 'bold': {'before': False, 'after': True}, 'text_color': {'before': '#000000', 'after': '#C00000'}}}])
        self.assertEqual(change['paragraph'], {})
        self.assertIn('표적', review['summary_text'])
        self.assertIn('굵게', review['summary_text'])
        self.assertEqual(review['coverage']['format']['state'], 'complete')
        markdown = rules.to_markdown(review)
        self.assertIn('## 서식 변경 상세', markdown)
        self.assertIn('| [3] 앞 표적 뒤 | 표적 | 크기 | 10pt | 14pt |', markdown)
        self.assertIn('| [3] 앞 표적 뒤 | 표적 | 굵게 | 아니요 | 예 |', markdown)

    def test_a_paragraph_change_names_its_values_with_units(self) -> None:
        before = para('문단', index=1)
        after = para('문단', sha='changed', index=1, align='center', left_indent_pt=20.0, line_spacing_type='fixed', line_spacing_value=18.0)
        review = assembled([before], [after])
        change = review['comparison']['changes'][0]
        self.assertEqual(change['ranges'], [])
        self.assertEqual(change['paragraph'], {'align': {'before': 'justify', 'after': 'center'}, 'left_indent_pt': {'before': 0.0, 'after': 20.0},
                                               'line_spacing_type': {'before': 'percent', 'after': 'fixed'}, 'line_spacing_value': {'before': 160, 'after': 18.0}})
        markdown = rules.to_markdown(review)
        self.assertIn('| [1] 문단 | 문단 전체 | 정렬 | 양쪽 | 가운데 |', markdown)
        self.assertIn('| [1] 문단 | 문단 전체 | 왼쪽 여백 | 0pt | 20pt |', markdown)
        self.assertIn('| [1] 문단 | 문단 전체 | 줄 간격 값 | 160 | 18 |', markdown)

    def test_a_change_only_in_unnamed_properties_says_so_and_is_partial(self) -> None:
        review = assembled([para('문단')], [para('문단', sha='changed')])
        change = review['comparison']['changes'][0]
        self.assertTrue(change['other_properties'])
        self.assertEqual(review['coverage']['format']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('다른 서식', review['summary_text'])

    def test_a_truncated_run_list_is_partial_not_a_clean_comparison(self) -> None:
        review = assembled([para('문단', truncated=True)], [para('문단', sha='changed', truncated=True)])
        self.assertEqual(review['coverage']['format']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')

    def test_a_format_change_with_no_format_facts_is_unverified_detail(self) -> None:
        plain = lambda sha: {'index': 0, 'text': '문단', 'heading': 'NONE', 'level': 0, 'in_body': True, 'content_sha256': sha}  # noqa: E731
        review = assembled([plain('a')], [plain('b')])
        self.assertEqual(review['coverage']['format']['state'], 'unverified')
        self.assertEqual(review['review_status'], 'partial')

    def test_no_format_facts_and_no_change_does_not_lower_the_review(self) -> None:
        plain = {'index': 0, 'text': '문단', 'heading': 'NONE', 'level': 0, 'in_body': True, 'content_sha256': 'a'}
        review = assembled([plain], [plain])
        self.assertEqual(review['review_status'], 'complete')


HEADER = '''<hh:head xmlns:hh="h" xmlns:hc="c" xmlns:hp="p"><hh:refList>
<hh:fontfaces><hh:fontface lang="HANGUL"><hh:font id="0" face="굴림"/><hh:font id="1" face="함초롬바탕"/></hh:fontface><hh:fontface lang="LATIN"><hh:font id="0" face="굴림"/><hh:font id="1" face="함초롬바탕"/></hh:fontface></hh:fontfaces>
<hh:charProperties><hh:charPr id="0" height="1000" textColor="#000000"><hh:fontRef hangul="1" latin="1"/><hh:underline type="NONE" shape="SOLID" color="#000000"/></hh:charPr>
<hh:charPr id="1" height="1400" textColor="#C00000"><hh:fontRef hangul="0" latin="0"/><hh:italic/><hh:bold/><hh:underline type="BOTTOM" shape="SOLID" color="#000000"/></hh:charPr></hh:charProperties>
<hh:paraProperties><hh:paraPr id="0"><hh:align horizontal="JUSTIFY"/><hp:switch><hp:case hp:required-namespace="x"><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left value="0" unit="HWPUNIT"/><hc:right value="0" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/></hp:case><hp:default><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left value="0" unit="HWPUNIT"/><hc:right value="0" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/></hp:default></hp:switch></hh:paraPr>
<hh:paraPr id="1"><hh:align horizontal="CENTER"/><hp:switch><hp:case hp:required-namespace="x"><hh:margin><hc:intent value="-500" unit="HWPUNIT"/><hc:left value="2000" unit="HWPUNIT"/><hc:right value="1000" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="FIXED" value="2000" unit="HWPUNIT"/></hp:case><hp:default><hh:margin><hc:intent value="-1000" unit="HWPUNIT"/><hc:left value="4000" unit="HWPUNIT"/><hc:right value="2000" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="FIXED" value="4000" unit="HWPUNIT"/></hp:default></hp:switch></hh:paraPr></hh:paraProperties>
</hh:refList></hh:head>'''
SECTION = '''<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:ctrl><hp:header><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>머리말</hp:t></hp:run></hp:p></hp:subList></hp:header></hp:ctrl><hp:t>앞 </hp:t></hp:run><hp:run charPrIDRef="1"><hp:t>표적<hp:tab width="1"/>끝</hp:t></hp:run></hp:p>
<hp:p paraPrIDRef="1"><hp:run charPrIDRef="0"><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>셀</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr></hp:tbl><hp:t>뒤</hp:t></hp:run></hp:p></hs:sec>'''


def package(section=SECTION, header=HEADER):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('Contents/header.xml', header)
        archive.writestr('Contents/section0.xml', section)
    return out.getvalue()


class PersistedFormat(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'saved.hwpx'
        self.path.write_bytes(package())

    def live(self):
        return [
            para('앞 표적\t끝', [run('앞 '), run('표적\t끝', font_family='굴림', size_pt=14.0, bold=True, italic=True, underline=True, text_color='#C00000')]),
            para('뒤', sha='x', align='center', left_indent_pt=20.0, right_indent_pt=10.0, first_line_indent_pt=-5.0, line_spacing_type='fixed', line_spacing_value=20.0),
            para('셀'),
        ]

    def test_reads_runs_and_paragraph_values_from_the_package_itself(self) -> None:
        rows = rules.package_format(self.path)
        self.assertEqual([row['text'] for row in rows], ['앞 표적\t끝', '뒤', '셀'])
        self.assertEqual(rows[0]['format']['runs'][1], {'text': '표적\t끝', 'length': 4, 'font_family': '굴림', 'size_pt': 14.0, 'bold': True, 'italic': True, 'underline': True, 'text_color': '#C00000'})
        self.assertEqual(rows[1]['format']['paragraph'], {'align': 'center', 'left_indent_pt': 20.0, 'right_indent_pt': 10.0, 'first_line_indent_pt': -5.0, 'line_spacing_type': 'fixed', 'line_spacing_value': 20.0})

    def test_same_values_match(self) -> None:
        self.assertEqual(rules.persisted_format(self.path, self.live()), {'state': 'match', 'paragraphs_checked': 3, 'mismatches': []})

    def test_a_different_value_is_a_mismatch_naming_the_field(self) -> None:
        live = self.live()
        live[0]['format']['runs'][1]['bold'] = False
        value = rules.persisted_format(self.path, live)
        self.assertEqual(value['state'], 'mismatch')
        self.assertEqual(value['mismatches'][0]['text'], '표적\t끝')
        self.assertEqual(value['mismatches'][0]['changes'], {'bold': {'before': False, 'after': True}})

    def test_runs_split_differently_with_the_same_values_still_match(self) -> None:
        live = self.live()
        first = live[0]['format']['runs'][1]
        live[0]['format']['runs'][1:] = [{**first, 'text': '표적', 'length': 2}, {**first, 'text': '\t끝', 'length': 2}]
        self.assertEqual(rules.persisted_format(self.path, live)['state'], 'match')

    def test_different_text_or_an_unreadable_file_is_unverified(self) -> None:
        live = self.live()
        live[1]['text'] = '다른 글'
        self.assertEqual(rules.persisted_format(self.path, live)['state'], 'unverified')
        self.path.write_bytes(b'not a package')
        self.assertEqual(rules.persisted_format(self.path, self.live())['state'], 'unverified')

    def test_a_package_declaring_entities_is_not_parsed(self) -> None:
        bomb = '<!DOCTYPE lolz [<!ENTITY a "aaaa">]>'
        self.path.write_bytes(package(section=bomb + SECTION))
        value = rules.persisted_format(self.path, self.live())
        self.assertEqual((value['state'], value['reason']), ('unverified', 'package_format_unreadable'))

    def test_a_truncated_list_is_never_called_a_full_match(self) -> None:
        live = self.live()[:2]
        value = rules.persisted_format(self.path, live, truncated=True)
        self.assertEqual(value['state'], 'partial')
        self.assertEqual(value['paragraphs_checked'], 2)

    def test_a_persisted_mismatch_makes_the_review_a_mismatch(self) -> None:
        review = assembled([para('문단')], [para('문단')], persisted_format={'state': 'mismatch', 'paragraphs_checked': 1, 'mismatches': [{'paragraph_index': 0, 'text': '문단', 'changes': {'bold': {'before': False, 'after': True}}}]})
        self.assertEqual(review['review_status'], 'mismatch')
        self.assertIn('저장본', review['summary_text'])
        self.assertIn('## 저장된 서식 확인', rules.to_markdown(review))


class ReadableParagraphRows(unittest.TestCase):
    def changed(self, *pairs):
        before = [para(text, index=index, **{}) for index, text, _shape, *_rest in pairs]
        after = []
        for index, text, values, *rest in pairs:
            row = para(text, sha='changed' + str(index), index=index, **values)
            row['format']['objects'] = rest[0] if rest else []
            after.append(row)
        return assembled(before, after)

    def test_each_paragraph_row_names_its_paragraph(self) -> None:
        review = self.changed((3, '오른쪽 정렬할 문단', {'align': 'right'}), (7, '또 다른 오른쪽 문단', {'align': 'right'}))
        markdown = rules.to_markdown(review)
        self.assertIn('| [3] 오른쪽 정렬할 문단 | 문단 전체 | 정렬 | 양쪽 | 오른쪽 |', markdown)
        self.assertIn('| [7] 또 다른 오른쪽 문단 | 문단 전체 | 정렬 | 양쪽 | 오른쪽 |', markdown)
        self.assertNotIn('(문단 전체)', markdown)
        self.assertIn('문단 서식 변경: [3] 오른쪽 정렬할 문단', review['summary_text'])

    def test_blank_and_object_only_paragraphs_are_labelled(self) -> None:
        review = self.changed((11, '', {'align': 'center'}, ['table']), (32, '', {'align': 'center'}), (15, '', {'align': 'right'}, ['picture']), (16, '', {'align': 'left'}, ['table', 'picture']))
        markdown = rules.to_markdown(review)
        for label in ('[11] (표만 있는 문단)', '[32] (빈 문단)', '[15] (그림만 있는 문단)', '[16] (개체만 있는 문단)'):
            self.assertIn(f'| {label} | 문단 전체 | 정렬 |', markdown)
            self.assertIn(f'- 서식 변경: {label}', markdown)
        self.assertNotRegex(markdown, r'- 서식 변경: *\n')
        self.assertNotRegex(review['summary_text'], r'"" —')

    def test_character_rows_name_their_paragraph_and_long_text_is_cut_visibly(self) -> None:
        text = '가' * 70
        before = para(text, index=4)
        after = para(text, [run('가' * 10, bold=True), run('가' * 60)], sha='x', index=4)
        markdown = rules.to_markdown(assembled([before], [after]))
        self.assertIn('| [4] ' + '가' * 30 + '… | ' + '가' * 10 + ' | 굵게 | 아니요 | 예 |', markdown)

    def rows_review(self, count):
        pairs = [(n, f'문단 {n}', {'align': 'center'}) for n in range(count)]
        return self.changed(*pairs)

    def test_a_table_cut_at_the_limit_says_how_many_rows_and_where_the_rest_are(self) -> None:
        review = self.rows_review(205)
        markdown = rules.to_markdown(review)
        self.assertIn('앞 200행만 보여 줍니다. 전체 205행', markdown)
        self.assertIn('review.json', markdown)
        self.assertEqual(len(review['comparison']['changes']), 205, 'the structured detail stays complete')
        self.assertEqual(markdown.count('| 문단 전체 | 정렬 |'), 200)
        self.assertNotIn('summary.md에 있습니다', review['summary_text'], 'the Markdown does not hold every omitted row')
        self.assertIn('review.json', review['summary_text'])

    def test_a_table_within_the_limit_has_no_cut_notice(self) -> None:
        self.assertNotIn('행만 보여 줍니다', rules.to_markdown(self.rows_review(3)))


class OutsideBodyWording(unittest.TestCase):
    def test_outside_body_paragraphs_are_said_to_keep_their_text_only(self) -> None:
        before = {**para('셀 글', index=1), 'in_body': False, 'context': 'table_cell'}
        after = {**para('셀 글', [run('셀 글', bold=True)], sha='x', index=1), 'in_body': False, 'context': 'table_cell'}
        same = {**para('다른 셀', index=2), 'in_body': False, 'context': 'table_cell'}
        review = assembled([before, same], [after, same])
        markdown = rules.to_markdown(review)
        self.assertIn('표 셀 등 본문 밖 문단 2개: 글 동일, 서식 변경 1개', markdown)
        self.assertNotIn('내용 동일', markdown)
        self.assertEqual(review['comparison']['nested_format_changes'], 1)


class UnnamedRemainder(unittest.TestCase):
    def with_rest(self, row, *, rest='r1', paragraph='p1', objects='o1'):
        row['format'].update(paragraph_rest=paragraph, objects_sha256=objects, objects=[])
        for item in row['format']['runs']:
            item['rest'] = rest
        return row

    def test_a_named_change_does_not_hide_an_unnamed_one_in_the_same_run(self) -> None:
        before = self.with_rest(para('문단'))
        after = self.with_rest(para('문단', [run('문단', bold=True)], sha='x'), rest='r2')
        review = assembled([before], [after])
        change = review['comparison']['changes'][0]
        self.assertTrue(change['ranges'])
        self.assertTrue(change['other_properties'])
        self.assertEqual(review['coverage']['format']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')

    def test_nor_in_the_paragraph_shape_or_an_object(self) -> None:
        for fields in ({'paragraph': 'p2'}, {'objects': 'o2'}):
            with self.subTest(fields=fields):
                review = assembled([self.with_rest(para('문단'))], [self.with_rest(para('문단', sha='x', align='center'), **fields)])
                self.assertTrue(review['comparison']['changes'][0]['other_properties'])
                self.assertEqual(review['coverage']['format']['state'], 'partial')

    def test_only_named_changes_stay_complete(self) -> None:
        before = self.with_rest(para('문단'))
        after = self.with_rest(para('문단', [run('문단', bold=True)], sha='x', align='center'))
        review = assembled([before], [after])
        self.assertNotIn('other_properties', review['comparison']['changes'][0])
        self.assertEqual(review['coverage']['format']['state'], 'complete')

    def test_an_earlier_review_without_the_remainder_cannot_call_it_explained(self) -> None:
        review = assembled([para('문단')], [self.with_rest(para('문단', [run('문단', bold=True)], sha='x'))])
        self.assertTrue(review['comparison']['changes'][0]['other_properties'])


def numbered(text, *, mark, index=1, sha=None):
    row = para(text, index=index, sha=sha)
    row['heading'] = 'NUMBER'
    row['format']['mark'] = {'kind': 'number', 'source': 'paragraph_end', 'font_family': '함초롬바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000', **mark}
    return row


class NumberMarkReview(unittest.TestCase):
    def test_a_changed_mark_is_reported_by_paragraph_and_value(self) -> None:
        review = assembled([numbered('둘째 수준', mark={})], [numbered('둘째 수준', mark={'bold': True, 'text_color': '#C00000'})])
        change = next(item for item in review['comparison']['changes'] if item['kind'] == 'number_mark_changed')
        self.assertEqual(change['paragraph_index'], 1)
        self.assertEqual(change['changes'], {'bold': {'before': False, 'after': True}, 'text_color': {'before': '#000000', 'after': '#C00000'}})
        self.assertEqual(review['coverage']['number_mark'], {'state': 'changed', 'numbered': 1, 'compared': 1, 'changed': 1, 'not_compared': 0, 'new': 0})
        self.assertIn('번호 모양 변경: [1] 둘째 수준 — 굵게 아니요→예', review['summary_text'])
        self.assertIn('| [1] 둘째 수준 | 번호 표시 | 굵게 | 아니요 | 예 |', rules.to_markdown(review))

    def test_a_kept_mark_is_compared_and_does_not_lower_the_review(self) -> None:
        review = assembled([numbered('둘째 수준', mark={})], [numbered('둘째 수준', mark={}, sha='x')])
        self.assertEqual(review['coverage']['number_mark']['state'], 'compared')
        self.assertFalse([item for item in review['comparison']['changes'] if item['kind'] == 'number_mark_changed'])
        self.assertIn('번호 모양: 번호 문단 1개를 비교해 바뀌지 않았습니다', review['summary_text'])

    def test_without_a_baseline_it_is_not_compared_and_not_complete(self) -> None:
        readback = {'available': True, 'paragraphs': [numbered('첫째', mark={})], 'paragraph_total': 1, 'evidence': side([])['evidence'], 'controls': {}, 'page_count': 1, 'page_numbers': [], 'document_is_modified': False, 'source_filename': 'd.hwpx'}
        review = rules.assemble(session_id='s', review_id='s/review-1', readback=readback, pdf=None, pages=[{'page': 1, 'path': 'p.png', 'sha256': 'x', 'width': 1, 'height': 1}], texts=['첫째'], baseline=None, saved_output=None, render_error=None)
        self.assertEqual(review['coverage']['number_mark']['state'], 'not_compared')
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('번호 모양은 비교하지 않았습니다', review['summary_text'].splitlines()[0])

    def test_marks_with_no_facts_or_an_unknown_source_are_not_compared(self) -> None:
        plain = {'index': 0, 'text': '문단', 'heading': 'OUTLINE', 'level': 0, 'in_body': True, 'content_sha256': 'a'}
        self.assertEqual(assembled([plain], [plain])['coverage']['number_mark']['state'], 'not_compared')
        unknown = numbered('문단', mark={'source': 'unknown'})
        review = assembled([unknown], [unknown])
        self.assertEqual(review['coverage']['number_mark']['not_compared'], 1)
        self.assertEqual(review['review_status'], 'partial')

    def test_a_paragraph_that_just_got_its_number_has_no_old_mark_to_compare(self) -> None:
        review = assembled([para('문단')], [numbered('문단', mark={}, index=0)])
        self.assertEqual(review['coverage']['number_mark'], {'state': 'new_only', 'numbered': 1, 'compared': 0, 'changed': 0, 'not_compared': 0, 'new': 1})
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('번호 모양: 새 번호 문단 1개는 이전 모양이 없어 비교하지 못했습니다', review['summary_text'])
        self.assertIn('새 번호 문단 1개의 번호 모양은 이전 값이 없어 비교하지 못했습니다', review['summary_text'].splitlines()[0])
        self.assertNotIn('비교해', review['summary_text'])
        markdown = rules.to_markdown(review)
        self.assertIn('- 판정: 새 번호라 이전 모양이 없어 비교하지 못함', markdown)
        self.assertIn('- 번호 문단 1개 중 비교 0개, 바뀜 0개, 새 번호 1개, 비교 못함 0개', markdown)
        self.assertNotIn('비교했고', markdown)
        self.assertNotIn('서식 값으로 비교한 결과', markdown)
        self.assertIn('번호 글자·종류·수준 확인과는 별개', markdown)

    def test_only_new_marks_cannot_leave_an_otherwise_whole_review_complete(self) -> None:
        current = {**numbered('문단', mark={}, index=0), 'heading': 'OUTLINE', 'text': '문단'}
        readback = {'available': True, 'paragraphs': [current], 'paragraph_total': 1, 'evidence': side([])['evidence'], 'controls': {}, 'page_count': 1, 'page_numbers': [], 'document_is_modified': True, 'source_filename': 'doc.hwpx'}
        review = rules.assemble(session_id='s', review_id='s/review-2', readback=readback, pdf=None, pages=[{'page': 1, 'path': 'p.png', 'sha256': 'x', 'width': 1, 'height': 1}], texts=['1. 문단'],
                                baseline=side([para('문단')]), saved_output=None, render_error=None)
        self.assertEqual(review['numbering'][0]['status'], 'match', 'the label is verified on its own')
        self.assertEqual(review['coverage']['number_mark']['state'], 'new_only')
        self.assertEqual(review['review_status'], 'partial')
        self.assertNotIn('전체 확인 완료', review['summary_text'])
        self.assertIn('렌더링된 번호가 모두 기대와 일치합니다', review['summary_text'], 'a verified label stays reported apart')
        keeps = next(line for line in review['summary_text'].splitlines() if line.startswith('보존 확인'))
        self.assertIn('번호 모양', keeps, 'the preservation line says the marks are not part of it')

    def test_new_and_existing_marks_together_are_mixed_and_partial(self) -> None:
        review = assembled([numbered('첫째', mark={}, index=0), para('둘째', index=1)], [numbered('첫째', mark={}, index=0), numbered('둘째', mark={}, index=1)])
        self.assertEqual(review['coverage']['number_mark'], {'state': 'mixed', 'numbered': 2, 'compared': 1, 'changed': 0, 'not_compared': 0, 'new': 1})
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('번호 모양: 번호 문단 1개를 비교해 바뀌지 않았습니다. 새 번호 문단 1개는 이전 모양이 없어 비교하지 못했습니다', review['summary_text'])
        markdown = rules.to_markdown(review)
        self.assertIn('- 판정: 일부만 비교함, 새 번호는 이전 모양이 없음', markdown)
        self.assertIn('비교 1개, 바뀜 0개, 새 번호 1개, 비교 못함 0개', markdown)

    def test_new_marks_next_to_a_changed_one_keep_both_counts(self) -> None:
        review = assembled([numbered('첫째', mark={}, index=0), para('둘째', index=1)], [numbered('첫째', mark={'bold': True}, index=0), numbered('둘째', mark={}, index=1)])
        self.assertEqual(review['coverage']['number_mark'], {'state': 'mixed', 'numbered': 2, 'compared': 1, 'changed': 1, 'not_compared': 0, 'new': 1})
        self.assertIn('번호 문단 1개를 비교해 1개가 바뀌었습니다. 새 번호 문단 1개는', review['summary_text'])
        self.assertIn('번호 모양 변경: [0] 첫째', review['summary_text'])

    def test_an_added_numbered_paragraph_has_none_either(self) -> None:
        review = assembled([numbered('첫째', mark={}, index=0)], [numbered('첫째', mark={}, index=0), numbered('새 문단', mark={}, index=1)])
        self.assertEqual(review['coverage']['number_mark']['state'], 'mixed')
        self.assertEqual(review['coverage']['number_mark']['new'], 1)
        self.assertEqual(review['coverage']['number_mark']['not_compared'], 0)

    def test_a_definition_that_cannot_be_read_is_missing_not_new(self) -> None:
        unknown = numbered('둘째', mark={'source': 'unknown'}, index=1)
        review = assembled([numbered('첫째', mark={}, index=0), unknown], [numbered('첫째', mark={}, index=0), unknown])
        self.assertEqual(review['coverage']['number_mark'], {'state': 'not_compared', 'numbered': 2, 'compared': 1, 'changed': 0, 'not_compared': 1, 'new': 0})
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('번호 모양: 번호 문단 2개 중 1개를 비교하지 못했습니다', review['summary_text'])
        self.assertIn('번호 문단 1개의 번호 모양을 비교하지 못했습니다', review['summary_text'].splitlines()[0])
        self.assertIn('- 판정: 일부 또는 전체를 비교하지 못함', rules.to_markdown(review))

    def test_genuinely_compared_marks_can_still_be_complete(self) -> None:
        row = {**numbered('문단', mark={}, index=0), 'heading': 'OUTLINE'}
        readback = {'available': True, 'paragraphs': [row], 'paragraph_total': 1, 'evidence': side([])['evidence'], 'controls': {}, 'page_count': 1, 'page_numbers': [], 'document_is_modified': True, 'source_filename': 'doc.hwpx'}
        baseline = side([row])
        baseline['numbering'] = [{'text': '문단', 'rendered_label': '1.', 'status': 'match'}]
        review = rules.assemble(session_id='s', review_id='s/review-2', readback=readback, pdf=None, pages=[{'page': 1, 'path': 'p.png', 'sha256': 'x', 'width': 1, 'height': 1}], texts=['1. 문단'],
                                baseline=baseline, saved_output=None, render_error=None)
        self.assertEqual(review['coverage']['number_mark']['state'], 'compared')
        self.assertEqual(review['review_status'], 'complete')
        self.assertIn('번호 모양: 번호 문단 1개를 비교해 바뀌지 않았습니다', review['summary_text'])
        self.assertIn('- 판정: 비교했고 바뀌지 않음', rules.to_markdown(review))

    def test_no_numbered_paragraph_has_nothing_to_compare(self) -> None:
        review = assembled([para('문단')], [para('문단')])
        self.assertEqual(review['coverage']['number_mark']['state'], 'none')
        self.assertEqual(review['review_status'], 'complete')


class TruthfulNames(unittest.TestCase):
    def test_top_and_bottom_underlines_are_both_known_values(self) -> None:
        top, bottom = {'underline': True, 'underline_type': 'top'}, {'underline': True}
        for old, new, word_old, word_new in ((top, bottom, '위쪽', '아래쪽'), (bottom, top, '아래쪽', '위쪽')):
            with self.subTest(old=word_old):
                review = assembled([para('문단', [run('문단', **old)], index=2)], [para('문단', [run('문단', **new)], sha='x', index=2)])
                change = review['comparison']['changes'][0]
                self.assertEqual(change['ranges'][0]['changes'], {'underline_type': {'before': {'위쪽': 'top', '아래쪽': 'bottom'}[word_old], 'after': {'위쪽': 'top', '아래쪽': 'bottom'}[word_new]}})
                self.assertIn(f'| [2] 문단 | 문단 | 밑줄 위치 | {word_old} | {word_new} |', rules.to_markdown(review))
                self.assertNotIn('확인 못함', '\n'.join(line for line in rules.to_markdown(review).splitlines() if '밑줄 위치' in line))
                self.assertEqual(review['coverage']['format']['state'], 'complete')

    def test_turning_a_bottom_underline_on_is_one_row(self) -> None:
        review = assembled([para('문단', index=0)], [para('문단', [run('문단', underline=True)], sha='x', index=0)])
        self.assertEqual(review['comparison']['changes'][0]['ranges'][0]['changes'], {'underline': {'before': False, 'after': True}})

    def test_turning_a_top_underline_on_names_the_position(self) -> None:
        review = assembled([para('문단', index=0)], [para('문단', [run('문단', underline=True, underline_type='top')], sha='x', index=0)])
        changes = review['comparison']['changes'][0]['ranges'][0]['changes']
        self.assertEqual(changes['underline_type'], {'before': 'none', 'after': 'top'})
        self.assertIn('| 밑줄 위치 | 없음 | 위쪽 |', rules.to_markdown(review))

    def test_a_position_change_with_an_unmeasured_shape_change_stays_partial(self) -> None:
        old, new = run('문단', underline=True, underline_type='top', underline_style='a'), run('문단', underline=True, underline_style='b')
        review = assembled([para('문단', [old], index=0)], [para('문단', [new], sha='x', index=0)])
        self.assertTrue(review['comparison']['changes'][0]['other_properties'])
        self.assertEqual(review['coverage']['format']['state'], 'partial')

    def facts(self, text, *, heading='NONE', rest='same', index=0, sha=None, ref=None, **shape):
        row = para(text, sha=sha, index=index, **shape)
        row['heading'] = heading
        row['numbering_ref_sha256'] = ref
        row['format']['paragraph_rest'] = rest
        row['format']['paragraph_heading'] = heading
        row['format']['objects_sha256'] = 'objects'
        return row

    def test_a_number_change_alone_leaves_no_unexplained_format_row(self) -> None:
        review = assembled([self.facts('문단')], [self.facts('문단', heading='NUMBER', sha='x', ref='r')])
        kinds = [item['kind'] for item in review['comparison']['changes']]
        self.assertEqual(kinds, ['number_changed'])
        self.assertEqual(review['coverage']['format']['state'], 'complete')
        self.assertNotIn('다른 서식', review['summary_text'])
        self.assertNotIn('이름 없는', rules.to_markdown(review))

    def test_a_number_change_with_a_named_change_is_explained(self) -> None:
        review = assembled([self.facts('문단')], [self.facts('문단', heading='NUMBER', sha='x', ref='r', align='center')])
        change = next(item for item in review['comparison']['changes'] if item['kind'] == 'format_changed')
        self.assertEqual(list(change['paragraph']), ['align'])
        self.assertNotIn('other_properties', change)
        self.assertEqual(review['coverage']['format']['state'], 'complete')

    def test_a_number_and_named_and_unexplained_change_together_stay_partial(self) -> None:
        review = assembled([self.facts('문단')], [self.facts('문단', heading='NUMBER', sha='x', ref='r', rest='other', align='center')])
        change = next(item for item in review['comparison']['changes'] if item['kind'] == 'format_changed')
        self.assertEqual(list(change['paragraph']), ['align'])
        self.assertTrue(change['other_properties'])
        self.assertEqual(review['coverage']['format']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')

    def test_a_heading_difference_no_row_reports_stays_unexplained(self) -> None:
        old, new = self.facts('문단', heading='NUMBER', ref='r'), self.facts('문단', heading='NUMBER', sha='x', ref='r')
        new['format']['paragraph_heading'] = 'other'
        review = assembled([old], [new])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])
        self.assertTrue(review['comparison']['changes'][0]['other_properties'])

    def test_a_changed_definition_is_not_explained_by_a_number_row(self) -> None:
        old, new = self.facts('문단', heading='NUMBER', ref='r'), self.facts('문단', heading='NUMBER', sha='x', ref='s')
        new['format']['paragraph_heading'] = 'other'
        review = assembled([old], [new])
        self.assertEqual({item['kind'] for item in review['comparison']['changes']}, {'number_changed', 'format_changed'})
        change = next(item for item in review['comparison']['changes'] if item['kind'] == 'format_changed')
        self.assertTrue(change['other_properties'])
        self.assertEqual(review['coverage']['format']['state'], 'partial')

    def test_a_paragraph_without_object_facts_is_not_called_blank(self) -> None:
        old, new = para('', index=4), para('', sha='x', index=4)
        del old['format'], new['format']
        review = assembled([old], [new])
        self.assertEqual(review['coverage']['format']['state'], 'unverified')
        markdown = rules.to_markdown(review)
        self.assertIn('[4] (글 없음, 내용 확인 못함)', markdown)
        self.assertNotIn('(빈 문단)', markdown)
        self.assertNotIn('(표만', markdown)

    def test_facts_without_an_object_list_are_not_called_blank(self) -> None:
        old, new = para('', [], index=4, align='left'), para('', [], sha='x', index=4, align='right')
        review = assembled([old], [new])
        self.assertIn('[4] (글 없음, 내용 확인 못함)', rules.to_markdown(review))

    def test_a_known_blank_and_a_known_table_paragraph_keep_their_names(self) -> None:
        for objects, word in (([], '(빈 문단)'), (['table'], '(표만 있는 문단)'), (['picture'], '(그림만 있는 문단)')):
            with self.subTest(word=word):
                old, new = para('', [], index=4, align='left'), para('', [], sha='x', index=4, align='right')
                old['format']['objects'] = new['format']['objects'] = objects
                review = assembled([old], [new])
                self.assertIn(f'[4] {word}', rules.to_markdown(review))

    def test_a_range_beyond_the_shown_text_is_named_by_its_length(self) -> None:
        old = run('가' * 200, length=260)
        changed = [run('가' * 200, length=240), {**run('', length=20), 'bold': True}]
        review = assembled([para('가' * 200, [old], index=1)], [para('가' * 200, changed, sha='x', index=1)])
        rows = review['comparison']['changes'][0]['ranges']
        self.assertEqual(rows[0]['text'], '')
        self.assertEqual(rows[0]['length'], 20)
        markdown = rules.to_markdown(review)
        self.assertIn('(앞부분 밖 글자 20개)', markdown)
        self.assertNotIn('|  | 굵게', markdown)
        self.assertIn('(앞부분 밖 글자 20개)', review['summary_text'])

    def test_a_range_of_spaces_and_a_cut_range_say_so(self) -> None:
        review = assembled([para('가 나', [run('가 나')], index=1)], [para('가 나', [run('가'), run(' ', bold=True), run('나')], sha='x', index=1)])
        self.assertIn('(공백 1개)', rules.to_markdown(review))
        long_text = '가' * 90
        review = assembled([para(long_text, [run(long_text)], index=1)], [para(long_text, [run(long_text, bold=True)], sha='y', index=1)])
        self.assertIn('가' * 60 + '…', rules.to_markdown(review))
        self.assertIn('가' * 30 + '…"', review['summary_text'])

    def test_a_cut_paragraph_name_in_a_number_row_says_so(self) -> None:
        text = '가' * 90
        review = assembled([para(text, index=1)], [numbered(text, mark={}, index=1, sha='x')])
        self.assertIn('가' * 60 + '…', rules.to_markdown(review))

    def test_more_saved_differences_than_the_table_shows_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'x.hwpx'
            path.write_bytes(b'x')
            rows = [{'text': f'문단{n}', 'format': {'runs': [run(f'문단{n}', bold=True)], 'paragraph': PARAGRAPH, 'mark': None}} for n in range(205)]
            live = [para(f'문단{n}', index=n) for n in range(205)]
            with unittest.mock.patch.object(rules, 'package_format', return_value=rows):
                result = rules.persisted_format(path, live)
        self.assertEqual(result['mismatch_total'], 205)
        self.assertEqual(len(result['mismatches']), 200)
        review = assembled([para('문단')], [para('문단')], persisted_format=result)
        self.assertIn('다른 곳이 205건', review['summary_text'])
        self.assertIn('앞 200건만 보여 줍니다', rules.to_markdown(review))


NUMBERED_HEADER = HEADER.replace('</hh:paraProperties>', '<hh:paraPr id="2"><hh:align horizontal="JUSTIFY"/><hh:heading type="NUMBER" idRef="1" level="0"/><hp:switch><hp:case hp:required-namespace="x"><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left value="0" unit="HWPUNIT"/><hc:right value="0" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/></hp:case></hp:switch></hh:paraPr></hh:paraProperties><hh:numberings><hh:numbering id="1"><hh:paraHead level="1" charPrIDRef="4294967295"/></hh:numbering></hh:numberings>')
NUMBERED_SECTION = '<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p paraPrIDRef="2"><hp:run charPrIDRef="0"><hp:t>앞 </hp:t></hp:run><hp:run charPrIDRef="1"><hp:t>끝</hp:t></hp:run><hp:run charPrIDRef="0"/></hp:p><hp:p paraPrIDRef="2"><hp:run charPrIDRef="0"><hp:t>뒤 </hp:t></hp:run><hp:run charPrIDRef="1"><hp:t>끝글</hp:t></hp:run></hp:p></hs:sec>'


class PersistedMark(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'saved.hwpx'
        self.path.write_bytes(package(section=NUMBERED_SECTION, header=NUMBERED_HEADER))

    def live(self, first=None, second=None):
        strong = dict(font_family='굴림', size_pt=14.0, bold=True, italic=True, underline=True, text_color='#C00000')
        return [
            {**numbered('앞 끝', mark=first or {}), 'format': {**numbered('앞 끝', mark=first or {})['format'], 'runs': [run('앞 '), run('끝', **strong)]}},
            {**numbered('뒤 끝글', mark=second or strong), 'format': {**numbered('뒤 끝글', mark=second or strong)['format'], 'runs': [run('뒤 '), run('끝글', **strong)]}},
        ]

    def test_the_trailing_run_decides_the_mark_in_the_file(self) -> None:
        rows = rules.package_format(self.path)
        self.assertEqual((rows[0]['format']['mark']['kind'], rows[0]['format']['mark']['source'], rows[0]['format']['mark']['bold']), ('number', 'paragraph_end', False))
        self.assertIs(rows[1]['format']['mark']['bold'], True)
        self.assertEqual(rules.persisted_format(self.path, self.live())['state'], 'match')

    def test_a_different_mark_in_the_file_is_a_mismatch(self) -> None:
        value = rules.persisted_format(self.path, self.live(first={'bold': True}))
        self.assertEqual(value['state'], 'mismatch')
        self.assertEqual(value['mismatches'][0]['text'], '(번호 표시)')


class SavedPackageDeclarations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'saved.hwpx'

    def verdict(self, section: bytes) -> dict:
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('Contents/header.xml', HEADER)
            archive.writestr('Contents/section0.xml', section)
        self.path.write_bytes(out.getvalue())
        return rules.persisted_format(self.path, [para('앞 표적\t끝', [run('앞 '), run('표적\t끝', font_family='굴림', size_pt=14.0, bold=True, italic=True, underline=True, text_color='#C00000')]),
                                                  para('뒤', sha='x', align='center', left_indent_pt=20.0, right_indent_pt=10.0, first_line_indent_pt=-5.0, line_spacing_type='fixed', line_spacing_value=20.0), para('셀')])

    def test_utf16_declarations_are_refused_by_the_parser(self) -> None:
        for declaration in ('<!DOCTYPE lolz [<!ENTITY a "aaaa">]>', '<!DOCTYPE x SYSTEM "file:///c:/windows/win.ini">'):
            with self.subTest(declaration=declaration):
                value = self.verdict((declaration + SECTION).encode('utf-16'))
                self.assertEqual((value['state'], value['reason']), ('unverified', 'package_format_unreadable'))

    def test_utf8_declarations_are_refused_too(self) -> None:
        value = self.verdict(('<!DOCTYPE lolz [<!ENTITY a "aaaa">]>' + SECTION).encode('utf-8'))
        self.assertEqual((value['state'], value['reason']), ('unverified', 'package_format_unreadable'))

    def test_a_declared_entity_is_never_expanded(self) -> None:
        bomb = '<!DOCTYPE x [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>' + SECTION.replace('앞 ', '&b;')
        self.assertEqual(self.verdict(bomb.encode('utf-8'))['state'], 'unverified')

    def test_ordinary_utf8_and_utf16_parts_still_read(self) -> None:
        for encoding in ('utf-8', 'utf-16'):
            with self.subTest(encoding=encoding):
                self.assertEqual(self.verdict(SECTION.encode(encoding))['state'], 'match')

    def test_malformed_xml_is_unverified_not_a_failure(self) -> None:
        self.assertEqual(self.verdict(b'<hs:sec xmlns:hs="s"><unclosed>')['state'], 'unverified')


if __name__ == '__main__':
    unittest.main()
