from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from local_cli_v1 import hwpx_reader, reading_cli
from local_cli_v1.hwpx_reader import (
    HwpxReadError,
    chunk_document,
    export_html,
    export_text,
    load_document,
    outline,
    position_index,
    search_chunks,
    word_count,
)

HH = 'http://www.hancom.co.kr/hwpml/2011/head'
HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HS = 'http://www.hancom.co.kr/hwpml/2011/section'

HEADER_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="{HH}">
  <hh:refList>
    <hh:paraProperties>
      <hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>
      <hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>
      <hh:paraPr id="2"><hh:heading type="NUMBER" idRef="0" level="1"/></hh:paraPr>
    </hh:paraProperties>
    <hh:styles>
      <hh:style id="0" type="PARA" name="바탕글" engName="Normal"/>
      <hh:style id="3" type="PARA" name="개요 2" engName="Outline 2"/>
    </hh:styles>
  </hh:refList>
</hh:head>
'''


def _p(text: str, para_pr: str = '0', style: str = '0', extra: str = '') -> str:
    return (f'<hp:p paraPrIDRef="{para_pr}" styleIDRef="{style}">'
            f'<hp:run charPrIDRef="0">{extra}<hp:t>{text}</hp:t></hp:run></hp:p>')


def _cell(row: int, col: int, text: str) -> str:
    return (f'<hp:tc><hp:subList>{_p(text)}</hp:subList>'
            f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/></hp:tc>')


SECTION0_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">
  {_p('제1장 개요 Introduction', para_pr='1')}
  {_p('본문 첫 문단입니다. The quick brown fox.',
      extra='<hp:ctrl><hp:header><hp:subList>' + _p('머리말 HEADER TEXT') + '</hp:subList></hp:header></hp:ctrl>')}
  <hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>앞<hp:tab/>탭 뒤<hp:lineBreak/>둘째 줄</hp:t></hp:run></hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>각주 문단</hp:t><hp:ctrl><hp:footNote><hp:subList>{_p('FOOTNOTE SECRET')}</hp:subList></hp:footNote></hp:ctrl></hp:run></hp:p>
  <hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="2" colCnt="2">
    <hp:tr>{_cell(0, 0, '이름')}{_cell(0, 1, 'Value')}</hp:tr>
    <hp:tr>{_cell(1, 0, '사과 apple')}{_cell(1, 1, '3 &lt;개&gt;')}</hp:tr>
  </hp:tbl></hp:run></hp:p>
</hs:sec>
'''

SECTION1_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">
  {_p('1.1 세부 Details', style='3')}
  {_p('둘째 구역 문단 second section paragraph with fox.')}
  {_p('Escapes &amp; &lt;script&gt;alert(1)&lt;/script&gt;')}
</hs:sec>
'''

CONTENT_HPF = '''<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>
  </opf:manifest>
  <opf:spine><opf:itemref idref="header"/><opf:itemref idref="section0"/><opf:itemref idref="section1"/></opf:spine>
</opf:package>
'''


def _write_hwpx(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('mimetype', 'application/hwp+zip')
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _default_entries() -> dict[str, str | bytes]:
    return {
        'Contents/content.hpf': CONTENT_HPF,
        'Contents/header.xml': HEADER_XML,
        'Contents/section0.xml': SECTION0_XML,
        'Contents/section1.xml': SECTION1_XML,
    }


class HwpxReaderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = _write_hwpx(self.tmp / 'sample.hwpx', _default_entries())
        self.doc = load_document(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def texts(self) -> list[str]:
        return [para['text'] for para in self.doc['paragraphs']]


class LoadDocumentTests(HwpxReaderTestBase):
    def test_sections_and_paragraph_order(self) -> None:
        self.assertEqual(len(self.doc['sections']), 2)
        self.assertEqual(self.doc['evidence'], 'local-static-hwpx')
        texts = self.texts()
        self.assertEqual(texts[0], '제1장 개요 Introduction')
        self.assertIn('1.1 세부 Details', texts)
        self.assertLess(texts.index('제1장 개요 Introduction'), texts.index('1.1 세부 Details'))

    def test_tab_and_linebreak(self) -> None:
        self.assertIn('앞\t탭 뒤\n둘째 줄', self.texts())

    def test_header_and_footnote_excluded_but_counted(self) -> None:
        joined = self.doc['text']
        self.assertNotIn('HEADER TEXT', joined)
        self.assertNotIn('FOOTNOTE SECRET', joined)
        self.assertIn('각주 문단', joined)
        self.assertEqual(self.doc['control_counts']['headers'], 1)
        self.assertEqual(self.doc['control_counts']['footnotes'], 1)

    def test_table_cells_attributed(self) -> None:
        self.assertEqual(len(self.doc['tables']), 1)
        table = self.doc['tables'][0]
        self.assertEqual(table['rows'], [['이름', 'Value'], ['사과 apple', '3 <개>']])
        apple = next(p for p in self.doc['paragraphs'] if p['text'] == '사과 apple')
        self.assertTrue(apple['in_table'])
        self.assertEqual((apple['table_index'], apple['row'], apple['col']), (0, 1, 0))

    def test_section_order_falls_back_to_numeric_without_spine(self) -> None:
        entries = _default_entries()
        del entries['Contents/content.hpf']
        entries['Contents/section10.xml'] = SECTION1_XML.replace('1.1 세부 Details', 'TENTH')
        entries['Contents/section2.xml'] = SECTION1_XML.replace('1.1 세부 Details', 'SECOND')
        doc = load_document(_write_hwpx(self.tmp / 'numeric.hwpx', entries))
        self.assertEqual([s['entry'] for s in doc['sections']], [
            'Contents/section0.xml', 'Contents/section1.xml', 'Contents/section2.xml', 'Contents/section10.xml'])
        texts = [p['text'] for p in doc['paragraphs']]
        self.assertLess(texts.index('SECOND'), texts.index('TENTH'))


class OutlineAndIndexTests(HwpxReaderTestBase):
    def test_outline_from_parapr_and_style(self) -> None:
        items = outline(self.doc)
        self.assertEqual([(i['level'], i['text']) for i in items],
                         [(1, '제1장 개요 Introduction'), (2, '1.1 세부 Details')])
        self.assertEqual(items[1]['section_index'], 1)

    def test_number_heading_is_not_outline(self) -> None:
        entries = _default_entries()
        entries['Contents/section1.xml'] = SECTION1_XML.replace('styleIDRef="3"', 'styleIDRef="0"').replace(
            'paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>둘째', 'paraPrIDRef="2" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>둘째')
        doc = load_document(_write_hwpx(self.tmp / 'n.hwpx', entries))
        self.assertEqual([i['text'] for i in outline(doc)], ['제1장 개요 Introduction'])

    def test_position_index_offsets(self) -> None:
        rows = position_index(self.doc)
        self.assertEqual(len(rows), len(self.doc['paragraphs']))
        for row, para in zip(rows, self.doc['paragraphs']):
            self.assertEqual(self.doc['text'][row['char_start']:row['char_end']], para['text'])
        first_s1 = next(r for r in rows if r['section_index'] == 1)
        self.assertEqual(first_s1['section_paragraph_index'], 0)
        self.assertEqual(first_s1['outline_level'], 2)
        cell = next(r for r in rows if r['in_table'] and r['row'] == 0 and r['col'] == 1)
        self.assertEqual(cell['table_index'], 0)
        self.assertEqual([r['global_index'] for r in rows], list(range(len(rows))))


class ChunkAndSearchTests(HwpxReaderTestBase):
    def test_chunks_cover_all_text_without_cutting_paragraphs(self) -> None:
        chunks = chunk_document(self.doc, max_chars=60, overlap_paragraphs=0)
        self.assertEqual(chunks[0]['id'], 'c0001')
        for chunk in chunks:
            self.assertLessEqual(len(chunk['text']), 60)
            self.assertEqual(self.doc['text'][chunk['char_start']:chunk['char_end']], chunk['text'])
        for para in self.doc['paragraphs']:
            if para['text'].strip():
                self.assertTrue(any(c['char_start'] <= para['char_start'] and para['char_end'] <= c['char_end']
                                    for c in chunks), para['text'])
        self.assertEqual([c['paragraph_start'] for c in chunks], sorted(c['paragraph_start'] for c in chunks))

    def test_overlap_repeats_last_paragraph(self) -> None:
        chunks = chunk_document(self.doc, max_chars=60, overlap_paragraphs=1)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(chunks[1]['paragraph_start'], chunks[0]['paragraph_end'])
        self.assertEqual(chunks[1]['overlap_units'], 1)

    def test_single_chunk_with_heading_path(self) -> None:
        chunks = chunk_document(self.doc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['heading_path'], ['제1장 개요 Introduction'])
        later = chunk_document(self.doc, max_chars=40, overlap_paragraphs=0)
        last = later[-1]
        self.assertEqual(last['heading_path'], ['제1장 개요 Introduction', '1.1 세부 Details'])

    def test_oversized_paragraph_is_split_on_boundaries(self) -> None:
        long_text = ' '.join(['가나다라마바사 문장입니다.'] * 30) + ' ' + 'x' * 50
        entries = _default_entries()
        entries['Contents/section1.xml'] = SECTION1_XML.replace('Escapes &amp;', long_text + ' Escapes &amp;')
        doc = load_document(_write_hwpx(self.tmp / 'long.hwpx', entries))
        chunks = chunk_document(doc, max_chars=100, overlap_paragraphs=1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk['text']), 100)
            self.assertFalse(chunk['text'].startswith(' '))
        covered = ''.join(c['text'] for c in chunks)
        self.assertIn('x' * 50, covered)
        self.assertTrue(any(c['text'].endswith('문장입니다.') for c in chunks))

    def test_invalid_chunk_arguments(self) -> None:
        with self.assertRaises(ValueError):
            chunk_document(self.doc, max_chars=0)
        with self.assertRaises(ValueError):
            chunk_document(self.doc, overlap_paragraphs=-1)

    def test_search_terms_phrase_and_ranking(self) -> None:
        chunks = chunk_document(self.doc, max_chars=60, overlap_paragraphs=0)
        hits = search_chunks(chunks, 'FOX')
        self.assertEqual(len(hits), 2)
        self.assertTrue(all('fox' in h['snippet'].lower() for h in hits))
        self.assertEqual(search_chunks(chunks, 'fox 둘째')[0]['score'], 2)
        self.assertEqual(search_chunks(chunks, '"quick brown"')[0]['score'], 1)
        self.assertEqual(search_chunks(chunks, '"brown quick"'), [])
        self.assertEqual(search_chunks(chunks, 'fox nonexistentterm'), [])
        self.assertEqual(search_chunks(chunks, '   '), [])
        self.assertEqual(len(search_chunks(chunks, 'fox', limit=1)), 1)
        ranked = search_chunks([
            {'id': 'a', 'text': 'fox', 'paragraph_start': 0, 'paragraph_end': 0, 'char_start': 0, 'heading_path': []},
            {'id': 'b', 'text': 'fox fox', 'paragraph_start': 1, 'paragraph_end': 1, 'char_start': 4, 'heading_path': []},
        ], 'fox')
        self.assertEqual([h['id'] for h in ranked], ['b', 'a'])


class ExportAndStatsTests(HwpxReaderTestBase):
    def test_export_text(self) -> None:
        text = export_text(self.doc)
        self.assertIn('제1장 개요 Introduction\n', text)
        self.assertIn('이름\tValue\n사과 apple\t3 <개>\n', text)
        self.assertNotIn('FOOTNOTE', text)

    def test_export_html_escaped_and_structured(self) -> None:
        page = export_html(self.doc)
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn('<h1>제1장 개요 Introduction</h1>', page)
        self.assertIn('<h2>1.1 세부 Details</h2>', page)
        self.assertIn('<td>3 &lt;개&gt;</td>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script', page)
        self.assertIn('앞\t탭 뒤<br>둘째 줄', page)
        self.assertIn('local-static-hwpx', page)

    def test_word_count(self) -> None:
        stats = word_count(self.doc)
        self.assertEqual(stats['sections'], 2)
        self.assertEqual(stats['tables'], 1)
        self.assertEqual(stats['headings'], 2)
        self.assertEqual(stats['table_paragraphs'], 4)
        self.assertEqual(stats['paragraphs'], len(self.doc['paragraphs']))
        joined = ''.join(self.texts())
        self.assertEqual(stats['characters_with_spaces'], len(joined))
        self.assertEqual(stats['characters_without_spaces'], len(''.join(joined.split())))
        self.assertEqual(stats['words'], sum(len(t.split()) for t in self.texts()))
        self.assertEqual(stats['excluded_controls']['footnotes'], 1)


class InputSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_and_non_zip(self) -> None:
        with self.assertRaises(HwpxReadError):
            load_document(self.tmp / 'missing.hwpx')
        bad = self.tmp / 'bad.hwpx'
        bad.write_bytes(b'not a zip')
        with self.assertRaises(HwpxReadError):
            load_document(bad)

    def test_no_sections(self) -> None:
        with self.assertRaises(HwpxReadError):
            load_document(_write_hwpx(self.tmp / 'empty.hwpx', {'Contents/header.xml': HEADER_XML}))

    def test_path_traversal_and_absolute_entries(self) -> None:
        for name in ('../evil.xml', '/etc/evil.xml', 'Contents/../../evil.xml', 'C:/evil.xml'):
            entries = _default_entries()
            entries[name] = 'x'
            path = _write_hwpx(self.tmp / 'trav.hwpx', entries)
            with self.subTest(name=name), self.assertRaises(HwpxReadError):
                load_document(path)

    def test_file_size_limit(self) -> None:
        path = _write_hwpx(self.tmp / 'ok.hwpx', _default_entries())
        with mock.patch.object(hwpx_reader, 'MAX_FILE_BYTES', 10):
            with self.assertRaisesRegex(HwpxReadError, 'file too large'):
                load_document(path)

    def test_entry_size_limit(self) -> None:
        path = _write_hwpx(self.tmp / 'ok.hwpx', _default_entries())
        with mock.patch.object(hwpx_reader, 'MAX_ENTRY_BYTES', 100):
            with self.assertRaisesRegex(HwpxReadError, 'too large'):
                load_document(path)

    def test_malformed_xml_and_doctype(self) -> None:
        entries = _default_entries()
        entries['Contents/section0.xml'] = '<hs:sec'
        with self.assertRaises(HwpxReadError):
            load_document(_write_hwpx(self.tmp / 'malformed.hwpx', entries))
        entries['Contents/section0.xml'] = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><sec>&a;</sec>'
        with self.assertRaisesRegex(HwpxReadError, 'DOCTYPE'):
            load_document(_write_hwpx(self.tmp / 'doctype.hwpx', entries))

    def test_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(HwpxReadError, ValueError))


class ReadingCliTests(HwpxReaderTestBase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest='command', required=True)
        reading_cli.register(subparsers)
        args = parser.parse_args(argv)
        self.assertIn(args.command, reading_cli.READING_COMMANDS)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reading_cli.dispatch(args)
        return code, out.getvalue(), err.getvalue()

    def test_commands_registered(self) -> None:
        self.assertEqual(reading_cli.READING_COMMANDS, {
            'doc-chunks', 'doc-search', 'doc-index', 'doc-outline', 'doc-export', 'doc-stats'})

    def test_json_outputs(self) -> None:
        cases = {
            'doc-chunks': ['--max-chars', '60', '--overlap', '0'],
            'doc-search': ['fox', '--limit', '5'],
            'doc-index': [],
            'doc-outline': [],
            'doc-stats': [],
        }
        for command, extra in cases.items():
            with self.subTest(command=command):
                argv = [command, str(self.path)] + (extra[:1] if command == 'doc-search' else [])
                argv += extra[1:] if command == 'doc-search' else extra
                code, out, err = self.run_cli(argv + ['--json'])
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertEqual(payload['evidence'], 'local-static-hwpx')
                self.assertEqual(payload['command'], command)
        code, out, _ = self.run_cli(['doc-search', str(self.path), '"quick brown"', '--json'])
        self.assertEqual(json.loads(out)['hit_count'], 1)
        code, out, _ = self.run_cli(['doc-outline', str(self.path), '--json'])
        self.assertEqual(json.loads(out)['headings'][0]['text'], '제1장 개요 Introduction')

    def test_human_outputs(self) -> None:
        for argv in (['doc-chunks', str(self.path)], ['doc-search', str(self.path), 'fox'],
                     ['doc-index', str(self.path)], ['doc-outline', str(self.path)],
                     ['doc-stats', str(self.path)]):
            with self.subTest(argv=argv[0]):
                code, out, err = self.run_cli(argv)
                self.assertEqual(code, 0, err)
                self.assertIn('evidence: local-static-hwpx', out)
                self.assertIn('not Hancom-rendered proof', out)
        _, out, _ = self.run_cli(['doc-outline', str(self.path)])
        self.assertIn('  1.1 세부 Details  (H2', out)
        _, out, _ = self.run_cli(['doc-index', str(self.path)])
        self.assertIn('tbl0[r1,c0]', out)

    def test_export_stdout_and_file(self) -> None:
        code, out, _ = self.run_cli(['doc-export', str(self.path), '--format', 'text'])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('# evidence: local-static-hwpx'))
        self.assertIn('이름\tValue', out)
        target = self.tmp / 'out.html'
        code, out, _ = self.run_cli(['doc-export', str(self.path), '--format', 'html', '--out', str(target)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['evidence'], 'local-static-hwpx')
        self.assertIn('<h1>제1장 개요 Introduction</h1>', target.read_text(encoding='utf-8'))
        code, _, err = self.run_cli(['doc-export', str(self.path), '--format', 'text', '--out', str(self.path)])
        self.assertEqual(code, 2)
        self.assertIn('source document', err)
        self.assertTrue(zipfile.is_zipfile(self.path))

    def test_errors_return_2(self) -> None:
        bad = self.tmp / 'bad.hwpx'
        bad.write_bytes(b'nope')
        for command in ('doc-chunks', 'doc-index', 'doc-outline', 'doc-stats'):
            with self.subTest(command=command):
                code, out, err = self.run_cli([command, str(bad)])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err.startswith('error: '))
                self.assertEqual(err.count('\n'), 1)
        code, _, err = self.run_cli(['doc-search', str(self.tmp / 'missing.hwpx'), 'x'])
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli(['doc-export', str(bad), '--format', 'html'])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
