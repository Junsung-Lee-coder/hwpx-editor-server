"""The readable review of formatting changes and the independent saved-package check. Pure inputs; not native evidence."""
from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
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
        self.assertIn('| 표적 | 크기 | 10pt | 14pt |', markdown)
        self.assertIn('| 표적 | 굵게 | 아니요 | 예 |', markdown)

    def test_a_paragraph_change_names_its_values_with_units(self) -> None:
        before = para('문단', index=1)
        after = para('문단', sha='changed', index=1, align='center', left_indent_pt=20.0, line_spacing_type='fixed', line_spacing_value=18.0)
        review = assembled([before], [after])
        change = review['comparison']['changes'][0]
        self.assertEqual(change['ranges'], [])
        self.assertEqual(change['paragraph'], {'align': {'before': 'justify', 'after': 'center'}, 'left_indent_pt': {'before': 0.0, 'after': 20.0},
                                               'line_spacing_type': {'before': 'percent', 'after': 'fixed'}, 'line_spacing_value': {'before': 160, 'after': 18.0}})
        markdown = rules.to_markdown(review)
        self.assertIn('| (문단 전체) | 정렬 | 양쪽 | 가운데 |', markdown)
        self.assertIn('| (문단 전체) | 왼쪽 여백 | 0pt | 20pt |', markdown)
        self.assertIn('| (문단 전체) | 줄 간격 값 | 160 | 18 |', markdown)

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


if __name__ == '__main__':
    unittest.main()
