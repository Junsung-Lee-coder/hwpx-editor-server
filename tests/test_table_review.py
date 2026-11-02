"""The review of table edits and the independent reader of saved tables, on captured Hancom output and synthetic readbacks. Not a native run."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from hwpx_mcp import review as rules

FIXTURES = Path(__file__).parent / 'fixtures' / 'native_tables'


def cell(address, row, col, text='', *, rs=1, cs=1, width=9000, fill=None, border=None, content=None, objects=()):
    line = {'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}
    return {'address': address, 'row': row, 'col': col, 'row_span': rs, 'col_span': cs, 'width': width, 'text': text, 'objects': list(objects), 'content': content or f'c:{text}',
            'border': {side: dict(line) for side in ('left', 'right', 'top', 'bottom')} | (border or {}), 'fill': fill or {'kind': 'none'}, 'rest': 'r'}


def table(inst, rows, cols, cells, *, page=1, before='앞 문단', index=0):
    return {'table_id': f'tbl:{inst}', 'index': index, 'page': page, 'rows': rows, 'cols': cols, 'cells': cells, 'before_text': before, 'state_sha256': json.dumps(cells, sort_keys=True, ensure_ascii=False), 'nested_in': None}


def base_grid(prefix=''):
    return [cell(f'{"AB"[c]}{r + 1}', r + 1, c + 1, f'{prefix}{"AB"[c]}{r + 1}') for r in range(2) for c in range(2)]


def readback(tables, **fields):
    return {'available': True, 'paragraphs': [], 'paragraph_total': 0, 'paragraphs_truncated': False, 'controls': {'tbl': len(tables or [])}, 'page_numbers': [], 'evidence': {'header_footer': [], 'styles_sha256': 's', 'media_sha256': 'm', 'numberings_sha256': 'n', 'bindata_sha256': 'b'},
            'page_count': 1, 'document_is_modified': True, 'source_filename': 'a.hwpx', 'tables': tables, 'tables_truncated': False, **fields}


def assemble(current, baseline=None, **fields):
    first = rules.assemble(session_id='s', review_id='s/review-1', readback=readback(baseline), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=None, render_error=None) if baseline is not None else None
    return rules.assemble(session_id='s', review_id='s/review-2', readback=readback(current), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=None, render_error=None, **fields)


class TableComparison(unittest.TestCase):
    def test_a_fill_and_an_edge_are_named_with_before_and_after(self) -> None:
        before = [table('1', 2, 2, base_grid())]
        cells = base_grid()
        cells[3]['fill'] = {'kind': 'solid', 'color': '#FFF2CC'}
        cells[3]['border']['top'] = {'line': 'dash', 'width_mm': 0.3, 'color': '#FF0000'}
        cells[1]['border']['bottom'] = {'line': 'dash', 'width_mm': 0.3, 'color': '#FF0000'}
        review = assemble([table('1', 2, 2, cells)], before)
        rows = review['comparison']['tables']['changed'][0]
        self.assertEqual(rows['table_id'], 'tbl:1')
        self.assertEqual(rows['page'], 1)
        self.assertEqual(rows['fills'], [{'cell': 'B2', 'before': {'kind': 'none'}, 'after': {'kind': 'solid', 'color': '#FFF2CC'}}])
        self.assertEqual({(row['cell'], row['side']) for row in rows['edges']}, {('B1', 'bottom'), ('B2', 'top')})
        self.assertEqual(review['coverage']['tables']['state'], 'complete')
        self.assertEqual(review['review_status'], 'complete')
        self.assertIn('tbl:1', review['summary_text'])
        markdown = rules.to_markdown(review)
        self.assertIn('## 표 변경', markdown)
        self.assertIn('| tbl:1 (1쪽) | B2 | 위쪽 선 | 실선 0.12mm #000000 | 긴 점선 0.3mm #FF0000 |', markdown)
        self.assertIn('| tbl:1 (1쪽) | B2 | 채우기 | 없음 | #FFF2CC |', markdown)

    def test_an_inserted_row_is_explained_and_never_called_unchanged_or_a_mismatch(self) -> None:
        before = [table('1', 2, 2, base_grid())]
        cells = base_grid()
        cells = cells[:2] + [cell('A2', 2, 1), cell('B2', 2, 2)] + [cell('A3', 3, 1, 'A2'), cell('B3', 3, 2, 'B2')]
        review = assemble([table('1', 3, 2, cells)], before)
        rows = review['comparison']['tables']['changed'][0]
        self.assertEqual(rows['grid'], {'before': {'rows': 2, 'cols': 2}, 'after': {'rows': 3, 'cols': 2}})
        self.assertEqual(rows['added_cells'], [{'cell': 'A2', 'text': ''}, {'cell': 'B2', 'text': ''}])
        self.assertEqual(rows['moved_cells'], [{'from': 'A2', 'to': 'A3', 'text': 'A2'}, {'from': 'B2', 'to': 'B3', 'text': 'B2'}])
        self.assertEqual(rows['removed_cells'], [])
        self.assertEqual(review['review_status'], 'complete')
        self.assertIn('2행 2열 → 3행 2열', review['summary_text'])

    def test_a_merge_is_told_as_the_cells_it_joined(self) -> None:
        before = [table('1', 2, 2, base_grid())]
        cells = [cell('A1', 1, 1, 'A1\nB1', cs=2, width=18000, content='c:joined')] + base_grid()[2:]
        review = assemble([table('1', 2, 2, cells)], before)
        rows = review['comparison']['tables']['changed'][0]
        self.assertEqual(rows['span_changes'], [{'cell': 'A1', 'before': {'row_span': 1, 'col_span': 1}, 'after': {'row_span': 1, 'col_span': 2}}])
        self.assertEqual(rows['removed_cells'], [{'cell': 'B1', 'text': 'B1'}])
        self.assertEqual(rows['content_changes'], [{'cell': 'A1', 'before': 'A1', 'after': 'A1\nB1'}])

    def test_tables_that_did_not_change_are_preserved_by_name(self) -> None:
        before = [table('1', 2, 2, base_grid()), table('2', 2, 2, base_grid('x'), index=1)]
        cells = base_grid()
        cells[0]['fill'] = {'kind': 'solid', 'color': '#000000'}
        review = assemble([table('1', 2, 2, cells), table('2', 2, 2, base_grid('x'), index=1)], before)
        self.assertEqual([item['table_id'] for item in review['comparison']['tables']['changed']], ['tbl:1'])
        self.assertEqual(review['comparison']['tables']['unchanged'], ['tbl:2'])
        self.assertIn('변경 없는 표 1개', review['summary_text'])

    def test_a_new_and_a_removed_table_are_named(self) -> None:
        review = assemble([table('2', 2, 2, base_grid())], [table('1', 2, 2, base_grid())])
        self.assertEqual(review['comparison']['tables']['added'][0]['table_id'], 'tbl:2')
        self.assertEqual(review['comparison']['tables']['removed'][0]['table_id'], 'tbl:1')

    def test_unread_or_truncated_tables_say_so_and_lower_the_review(self) -> None:
        review = assemble(None, [table('1', 2, 2, base_grid())])
        self.assertEqual(review['coverage']['tables']['state'], 'unverified')
        self.assertEqual(review['review_status'], 'partial')
        current = readback([table('1', 2, 2, base_grid())], tables_truncated=True)
        first = assemble([table('1', 2, 2, base_grid())])
        second = rules.assemble(session_id='s', review_id='s/review-2', readback=current, pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=None, render_error=None)
        self.assertEqual(second['coverage']['tables']['state'], 'partial')
        self.assertEqual(second['review_status'], 'partial')

    def test_a_document_without_tables_is_none_and_keeps_a_complete_review(self) -> None:
        review = assemble([], [])
        self.assertEqual(review['coverage']['tables']['state'], 'none')
        self.assertEqual(review['review_status'], 'complete')

    def test_the_paragraph_holding_a_changed_table_is_not_left_unexplained(self) -> None:
        format_fact = {'runs': [], 'runs_truncated': False, 'paragraph': {}, 'paragraph_rest': 'p', 'paragraph_heading': 'h', 'objects': ['table'], 'objects_sha256': 'before'}
        holder = {'text': '', 'heading': 'NONE', 'level': 0, 'in_body': True, 'content_sha256': 'x', 'numbering_ref_sha256': None, 'index': 0, 'context': 'body', 'format': format_fact}
        after = {**holder, 'content_sha256': 'y', 'format': {**format_fact, 'objects_sha256': 'after'}}
        cells = base_grid()
        cells[0]['fill'] = {'kind': 'solid', 'color': '#000000'}
        first = rules.assemble(session_id='s', review_id='s/review-1', readback=readback([table('1', 2, 2, base_grid())], paragraphs=[holder], paragraph_total=1), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=None, render_error=None)
        second = rules.assemble(session_id='s', review_id='s/review-2', readback=readback([table('1', 2, 2, cells)], paragraphs=[after], paragraph_total=1), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=None, render_error=None)
        self.assertEqual(second['coverage']['format']['not_fully_named'], 0)
        self.assertEqual(second['comparison']['changes'], [])
        self.assertEqual(second['review_status'], 'complete')

    def test_paragraphs_inside_a_changed_table_count_with_the_table_not_as_body_changes(self) -> None:
        row = {'heading': 'NONE', 'level': 0, 'in_body': False, 'content_sha256': 'x', 'numbering_ref_sha256': None, 'index': 0, 'context': 'table_cell', 'format': {'runs': [], 'runs_truncated': False, 'paragraph': {}, 'paragraph_rest': 'p', 'paragraph_heading': 'h', 'objects': [], 'objects_sha256': 'o'}}
        first = rules.assemble(session_id='s', review_id='s/review-1', readback=readback([table('1', 2, 2, base_grid())], paragraphs=[{**row, 'text': 'A1'}], paragraph_total=1), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=None, render_error=None)
        cells = base_grid()
        cells[0]['text'] = 'A1'
        added = cell('A3', 3, 1, '')
        second = rules.assemble(session_id='s', review_id='s/review-2', readback=readback([table('1', 3, 2, cells + [added])], paragraphs=[{**row, 'text': 'A1'}, {**row, 'text': '', 'index': 1}], paragraph_total=2), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=None, render_error=None)
        kinds = [item for item in second['comparison']['changes'] if item['kind'] == 'added']
        self.assertEqual(kinds[0].get('in_table'), True)
        self.assertNotIn('추가 1건', second['summary_text'])
        self.assertIn('표 변경', second['summary_text'])


class TextBesideTables(unittest.TestCase):
    def test_the_nested_text_line_points_to_the_table_changes(self) -> None:
        row = {'heading': 'NONE', 'level': 0, 'in_body': False, 'content_sha256': 'x', 'numbering_ref_sha256': None, 'index': 0, 'context': 'table_cell', 'format': {'runs': [], 'runs_truncated': False, 'paragraph': {}, 'paragraph_rest': 'p', 'paragraph_heading': 'h', 'objects': [], 'objects_sha256': 'o'}}
        first = rules.assemble(session_id='s', review_id='s/review-1', readback=readback([table('1', 2, 2, base_grid())], paragraphs=[{**row, 'text': 'A1'}], paragraph_total=1), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=None, render_error=None)
        second = rules.assemble(session_id='s', review_id='s/review-2', readback=readback([table('1', 3, 2, base_grid() + [cell('A3', 3, 1, '')])], paragraphs=[{**row, 'text': 'A1'}, {**row, 'text': '', 'index': 1}], paragraph_total=2), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=None, render_error=None)
        self.assertIn('글 변경 감지(위 표 변경 참고)', rules.to_markdown(second))


class SavedTables(unittest.TestCase):
    """The saved file itself, read with no help from Hancom: captured from a synthetic document saved by Hancom 13.0.0.3457."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.saved = self.package('saved_edges')

    def package(self, name: str) -> Path:
        # The parts as Hancom wrote them; a whole .hwpx is not kept in the repository.
        target = self.folder / f'{name}.hwpx'
        with zipfile.ZipFile(target, 'w') as out:
            out.writestr('Contents/header.xml', (FIXTURES / f'{name}.header.xml').read_bytes())
            out.writestr('Contents/section0.xml', (FIXTURES / f'{name}.section0.xml').read_bytes())
        return target

    def live(self):
        tables = rules.package_tables(self.saved)
        return copy.deepcopy(tables)

    def test_the_reader_resolves_cells_spans_borders_and_fills(self) -> None:
        tables = {item['table_id']: item for item in rules.package_tables(self.saved)}
        first = {cell_['address']: cell_ for cell_ in tables['tbl:1011']['cells']}
        self.assertEqual(first['B4']['border']['top']['line'], 'double')
        self.assertEqual(first['B4']['border']['top']['color'], '#0000FF')
        self.assertEqual(first['A1']['fill'], {'kind': 'solid', 'color': '#CCE5FF'})
        self.assertEqual(first['C4']['fill'], {'kind': 'none'})
        merged = {cell_['address']: cell_ for cell_ in tables['tbl:1015']['cells']}
        self.assertEqual((merged['A1']['col_span'], merged['A2']['row_span']), (2, 2))
        self.assertEqual(merged['B2']['border']['top'], {'line': 'solid', 'width_mm': 2.0, 'color': '#FF0000'})
        self.assertEqual(merged['C2']['fill'], {'kind': 'solid', 'color': '#FFF2CC'})

    def test_the_file_names_dash_and_dot_the_other_way_round_than_hancom_prints_them(self) -> None:
        # Measured: native line 2 prints as dashes and is DOT in the file, native line 3 prints as dots and is DASH there, and both reopen as they were.
        tables = {item['table_id']: item for item in rules.package_tables(self.package('saved_lines'))}
        lines = {cell_['address']: cell_ for cell_ in tables['tbl:1012']['cells']}
        self.assertEqual(lines['B2']['border']['top'], {'line': 'dash', 'width_mm': 0.4, 'color': '#00AA00'})
        self.assertEqual(lines['C3']['border']['top'], {'line': 'dot', 'width_mm': 0.4, 'color': '#0000FF'})
        for address, line in (('A1', 'solid'), ('D4', 'dash_dot'), ('A5', 'dash_dot_dot'), ('B1', 'long_dash'), ('C2', 'circle'), ('D3', 'double'), ('A4', 'slim_thick'), ('B5', 'thick_slim'), ('C1', 'slim_thick_slim')):
            self.assertEqual(lines[address]['border']['top']['line'], line, address)

    def test_white_and_no_fill_stay_apart_in_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'white.hwpx'
            with zipfile.ZipFile(self.saved) as source, zipfile.ZipFile(target, 'w') as out:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == 'Contents/header.xml':
                        data = data.replace(b'faceColor="#FFF2CC"', b'faceColor="#FFFFFF"')
                    out.writestr(info, data)
            merged = {cell_['address']: cell_ for cell_ in {item['table_id']: item for item in rules.package_tables(target)}['tbl:1015']['cells']}
            self.assertEqual(merged['C2']['fill'], {'kind': 'solid', 'color': '#FFFFFF'})
            self.assertEqual(merged['C3']['fill'], {'kind': 'none'})

    def test_values_the_file_holds_match_and_a_difference_is_a_mismatch(self) -> None:
        live = self.live()
        self.assertEqual(rules.persisted_tables(self.saved, live)['state'], 'match')
        changed = copy.deepcopy(live)
        changed[0]['cells'][0]['fill'] = {'kind': 'solid', 'color': '#010203'}
        result = rules.persisted_tables(self.saved, changed)
        self.assertEqual(result['state'], 'mismatch')
        self.assertEqual(result['mismatches'][0]['table_id'], 'tbl:1011')
        self.assertEqual(result['mismatches'][0]['field'], 'fill')
        spans = copy.deepcopy(live)
        spans[0]['rows'] = 99
        self.assertEqual(rules.persisted_tables(self.saved, spans)['state'], 'mismatch')

    def test_an_unreadable_file_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'broken.hwpx'
            target.write_bytes(b'not a zip')
            self.assertEqual(rules.persisted_tables(target, self.live())['state'], 'unverified')

    def test_a_mismatch_lowers_the_whole_review(self) -> None:
        review = assemble([table('1', 2, 2, base_grid())], [table('1', 2, 2, base_grid())], persisted_tables={'state': 'mismatch', 'mismatches': [{'table_id': 'tbl:1', 'cell': 'A1', 'field': 'fill', 'live': 'x', 'saved': 'y'}], 'tables_checked': 1})
        self.assertEqual(review['review_status'], 'mismatch')
        self.assertIn('저장본 표', review['summary_text'])


if __name__ == '__main__':
    unittest.main()
