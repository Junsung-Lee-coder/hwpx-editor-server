"""What a review may and may not say about tables: unexplained hashes, header and footer tables, fills it cannot compare, the page, paragraph scopes and media.

On synthetic readbacks and parts Hancom wrote. Not a native run.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from hwpx_mcp import review as rules
from tests.test_table_review import FIXTURES, assemble, base_grid, cell, readback, table


def frame(**values):
    return {'zorder': '1', 'page_break': 'Cell', 'repeat_header': 'true', 'text_wrap': 'TopAndBottom', **values}


def named_table(inst, cells, *, frame_digest='f', values=None, rows=2, cols=2, **fields):
    row = table(inst, rows, cols, cells, **fields)
    row.update(frame=frame_digest, frame_values=values or frame(), scope=fields.get('scope', 'body'))
    return row


def properties(item, attrs='a', **values):
    item.update(attrs=attrs, attr_values={'margin': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}, 'vertical_align': 'Center', **values})
    return item


def grid_with_properties(prefix=''):
    return [properties(item) for item in base_grid(prefix)]


def changed_hash(row):
    row['state_sha256'] = row['state_sha256'] + 'x'
    return row


def body(text, **fields):
    return {'text': text, 'heading': 'NONE', 'level': 0, 'in_body': True, 'content_sha256': f'c:{text}', 'numbering_ref_sha256': None, 'index': 0, 'context': 'body',
            'format': {'runs': [], 'runs_truncated': False, 'paragraph': {}, 'paragraph_rest': 'p', 'paragraph_heading': 'h', 'objects': [], 'objects_sha256': 'o'}, **fields}


def in_cell(text, table_id, address, **fields):
    return {**body(text), 'in_body': False, 'context': 'table_cell', 'table_id': table_id, 'cell': address, **fields}


def holder(table_id, digest='before', **fields):
    row = body('', holds_tables=[table_id], **fields)
    row['content_sha256'] = f'holder:{digest}'
    row['format'] = {**row['format'], 'objects': ['table'], 'objects_sha256': digest}
    return row


def reviewed(first_tables, second_tables, first_paragraphs=None, second_paragraphs=None, **fields):
    first = rules.assemble(session_id='s', review_id='s/review-1', readback=readback(first_tables, paragraphs=first_paragraphs or [], paragraph_total=len(first_paragraphs or [])), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=None, render_error=None)
    return rules.assemble(session_id='s', review_id='s/review-2', readback=readback(second_tables, paragraphs=second_paragraphs or [], paragraph_total=len(second_paragraphs or [])), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=first, saved_output=fields.pop('saved_output', None), render_error=None, **fields)


class UnexplainedHash(unittest.TestCase):
    def test_a_frame_change_is_named_with_its_values(self) -> None:
        before = [named_table('1', grid_with_properties())]
        after = [changed_hash(named_table('1', grid_with_properties(), frame_digest='g', values=frame(page_break='None')))]
        review = reviewed(before, after)
        row = review['comparison']['tables']['changed'][0]
        self.assertEqual(row['frame_changes'], [{'field': 'page_break', 'before': 'Cell', 'after': 'None'}])
        self.assertNotIn('other_properties', row)
        self.assertEqual(review['coverage']['tables']['state'], 'complete')
        self.assertIn('표 속성', '\n'.join(rules._table_rows(review['comparison']['tables'])))  # noqa: SLF001
        self.assertNotIn('값 차이 없음', review['summary_text'])
        self.assertNotIn('바뀐 표 없음', rules.to_markdown(review))

    def test_a_cell_margin_or_alignment_change_is_named_with_its_values(self) -> None:
        cells = grid_with_properties()
        cells[1] = properties(cells[1], attrs='b', vertical_align='Top')
        cells[2] = properties(cells[2], attrs='c', margin={'left': 100, 'right': 510, 'top': 141, 'bottom': 141})
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', cells))])
        row = review['comparison']['tables']['changed'][0]
        self.assertIn({'cell': 'B1', 'field': 'vertical_align', 'before': 'Center', 'after': 'Top'}, row['cell_properties'])
        self.assertIn({'cell': 'A2', 'field': 'margin', 'before': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}, 'after': {'left': 100, 'right': 510, 'top': 141, 'bottom': 141}}, row['cell_properties'])
        self.assertEqual(review['review_status'], 'complete')
        markdown = rules.to_markdown(review)
        self.assertIn('| tbl:1 (1쪽) | B1 | 세로 정렬 |', markdown)
        self.assertIn('| tbl:1 (1쪽) | A2 | 셀 여백 |', markdown)

    def test_a_changed_hash_with_nothing_named_is_never_called_unchanged_or_complete(self) -> None:
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', grid_with_properties()))])
        row = review['comparison']['tables']['changed'][0]
        self.assertTrue(row['other_properties'])
        self.assertEqual(review['coverage']['tables']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('이름 없는 다른 값', review['summary_text'])
        self.assertNotIn('값 차이 없음', review['summary_text'])
        self.assertNotIn('바뀐 표 없음', rules.to_markdown(review))

    def test_a_frame_digest_moving_without_a_named_value_is_partial(self) -> None:
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', grid_with_properties(), frame_digest='g'))])
        self.assertTrue(review['comparison']['tables']['changed'][0]['other_properties'])
        self.assertEqual(review['review_status'], 'partial')

    def test_an_attr_digest_moving_without_a_named_value_is_partial(self) -> None:
        cells = grid_with_properties()
        cells[0] = properties(cells[0], attrs='moved')
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', cells))])
        self.assertTrue(review['comparison']['tables']['changed'][0]['other_properties'])
        self.assertEqual(review['review_status'], 'partial')

    def test_named_and_unnamed_together_show_both(self) -> None:
        cells = grid_with_properties()
        cells[0]['fill'] = {'kind': 'solid', 'color': '#FFF2CC'}
        cells[1] = properties(cells[1], attrs='moved')
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', cells))])
        row = review['comparison']['tables']['changed'][0]
        self.assertEqual(len(row['fills']), 1)
        self.assertTrue(row['other_properties'])
        self.assertEqual(review['review_status'], 'partial')

    def test_cells_with_new_style_and_the_same_text_are_rendered(self) -> None:
        cells = grid_with_properties()
        cells[0]['content'] = 'other-style'
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', cells))])
        self.assertEqual(review['comparison']['tables']['changed'][0]['styled_content_cells'], ['A1'])
        self.assertIn('글 서식이 바뀐 셀 1개', review['summary_text'])
        self.assertIn('| tbl:1 (1쪽) | A1 | 글 서식 |', rules.to_markdown(review))


class Holder(unittest.TestCase):
    def changed(self, inst='1'):
        cells = grid_with_properties()
        cells[0]['fill'] = {'kind': 'solid', 'color': '#FFF2CC'}
        return changed_hash(named_table(inst, cells))

    def test_the_holder_of_the_table_that_changed_is_explained(self) -> None:
        review = reviewed([named_table('1', grid_with_properties())], [self.changed()], [holder('tbl:1')], [holder('tbl:1', 'after')])
        self.assertEqual(review['comparison']['changes'], [])
        self.assertEqual(review['review_status'], 'complete')

    def test_the_holder_of_another_table_is_not(self) -> None:
        tables = [named_table('1', grid_with_properties()), named_table('2', grid_with_properties('x'), index=1)]
        review = reviewed(tables, [self.changed(), named_table('2', grid_with_properties('x'), index=1)], [holder('tbl:2')], [holder('tbl:2', 'after')])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])
        self.assertEqual(review['review_status'], 'partial')

    def test_a_holder_with_its_own_style_change_is_not_hidden_by_the_table(self) -> None:
        after = holder('tbl:1', 'after')
        after['format'] = {**after['format'], 'paragraph_rest': 'moved'}
        review = reviewed([named_table('1', grid_with_properties())], [self.changed()], [holder('tbl:1')], [after])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])
        self.assertEqual(review['review_status'], 'partial')

    def test_a_table_changed_only_in_unnamed_values_explains_no_holder(self) -> None:
        review = reviewed([named_table('1', grid_with_properties())], [changed_hash(named_table('1', grid_with_properties()))], [holder('tbl:1')], [holder('tbl:1', 'after')])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])

    def test_the_holder_of_a_table_whose_nested_table_changed_is_explained(self) -> None:
        inner = [properties(item) for item in base_grid('속')]
        changed_inner = copy.deepcopy(inner)
        changed_inner[0]['fill'] = {'kind': 'solid', 'color': '#FFF2CC'}
        before = [named_table('1', grid_with_properties()), named_table('2', inner, index=1)]
        before[1]['nested_in'] = 'tbl:1'
        after = [named_table('1', grid_with_properties()), changed_hash(named_table('2', changed_inner, index=1))]
        after[1]['nested_in'] = 'tbl:1'
        review = reviewed(before, after, [holder('tbl:1')], [holder('tbl:1', 'after')])
        self.assertEqual(review['comparison']['changes'], [])

    def test_a_height_moving_alone_is_named_and_explains_only_its_own_holder(self) -> None:
        cells = grid_with_properties()
        for item in cells:
            item['height'] = 1800
        moved = copy.deepcopy(cells)
        moved[2]['height'] = 3600
        same = named_table('1', cells)
        after = named_table('1', moved)
        after['state_sha256'] = same['state_sha256']  # A table's hash leaves heights out.
        review = reviewed([same], [after], [holder('tbl:1')], [holder('tbl:1', 'after')])
        self.assertEqual(review['comparison']['changes'], [])
        self.assertEqual(review['comparison']['tables']['stored_heights'][0]['cells'], [{'cell': 'A2', 'before': 1800, 'after': 3600}])
        self.assertIn('A2 1800 → 3600', review['summary_text'])
        self.assertIn('저장된 높이', rules.to_markdown(review))
        other = [same, named_table('2', grid_with_properties('x'), index=1)]
        review = reviewed(other, [after, other[1]], [holder('tbl:2')], [holder('tbl:2', 'after')])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])

    def test_a_holder_row_that_does_not_say_what_it_holds_is_not_suppressed(self) -> None:
        old, new = holder('tbl:1'), holder('tbl:1', 'after')
        old.pop('holds_tables'), new.pop('holds_tables')
        review = reviewed([named_table('1', grid_with_properties())], [self.changed()], [old], [new])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])


class ParagraphScopes(unittest.TestCase):
    """A blank table holder must not pair with a blank cell paragraph."""

    def inserted(self):
        before_cells, after_cells = grid_with_properties(), grid_with_properties()
        added = [properties(cell('A3', 3, 1, '', content='blank')), properties(cell('B3', 3, 2, '', content='blank'))]
        return named_table('1', before_cells), changed_hash(named_table('1', after_cells + added, rows=3))

    def cell_rows(self):
        return [in_cell(name, 'tbl:1', name) for name in ('A1', 'B1', 'A2', 'B2')]

    def test_new_blank_cells_invent_no_body_paragraph_and_no_holder_format(self) -> None:
        before, after = self.inserted()
        first = [body('앞'), holder('tbl:1'), *self.cell_rows(), body('끝')]
        second = [body('앞'), holder('tbl:1', 'after'), *self.cell_rows(), in_cell('', 'tbl:1', 'A3'), in_cell('', 'tbl:1', 'B3'), body('끝')]
        review = reviewed([before], [after], first, second)
        changes = review['comparison']['changes']
        self.assertEqual([(item['kind'], item.get('in_table')) for item in changes], [('added', True), ('added', True)])
        self.assertNotIn('추가', review['summary_text'].split('표 변경')[0].split('대비:')[1])
        self.assertEqual(review['coverage']['format']['state'], 'complete')
        self.assertEqual(review['review_status'], 'complete')

    def test_blank_cells_added_just_before_the_next_tables_blank_holder_stay_with_their_table(self) -> None:
        # As measured in a real review: blank rows at a table's end, then a blank holder, then a blank table.
        one, grown = self.inserted()
        blank_cells = [properties(cell('A1', 1, 1, '', content='blank')), properties(cell('B1', 1, 2, '', content='blank'))]
        two = named_table('2', blank_cells, rows=1, index=1)
        first = [body('앞'), holder('tbl:1'), *self.cell_rows(), holder('tbl:2'), in_cell('', 'tbl:2', 'A1'), in_cell('', 'tbl:2', 'B1'), body('')]
        second = [body('앞'), holder('tbl:1', 'after'), *self.cell_rows(), in_cell('', 'tbl:1', 'A3'), in_cell('', 'tbl:1', 'B3'), holder('tbl:2'), in_cell('', 'tbl:2', 'A1'), in_cell('', 'tbl:2', 'B1'), body('')]
        review = reviewed([one, two], [grown, two], first, second)
        changes = review['comparison']['changes']
        self.assertEqual([(item['kind'], item.get('in_table')) for item in changes], [('added', True), ('added', True)], changes)
        self.assertEqual(review['coverage']['format']['state'], 'complete')
        self.assertEqual(review['review_status'], 'complete')

    def test_rows_with_no_scope_on_either_side_keep_one_sequence(self) -> None:
        plain = [body('앞'), body(''), body('끝')]
        self.assertEqual(reviewed([], [], plain, plain)['comparison']['changes'], [])

    def test_a_real_new_body_paragraph_is_still_reported_beside_a_table_edit(self) -> None:
        before, after = self.inserted()
        first = [body('앞'), holder('tbl:1'), *self.cell_rows(), body('끝')]
        second = [body('앞'), body(''), holder('tbl:1', 'after'), *self.cell_rows(), in_cell('', 'tbl:1', 'A3'), in_cell('', 'tbl:1', 'B3'), body('끝')]
        review = reviewed([before], [after], first, second)
        outside = [item for item in review['comparison']['changes'] if not item.get('in_table')]
        self.assertEqual([item['kind'] for item in outside], ['added'])

    def test_a_blank_holder_paragraph_with_a_real_style_change_is_still_reported(self) -> None:
        before, after = self.inserted()
        changed = holder('tbl:1', 'after')
        changed['format'] = {**changed['format'], 'paragraph_rest': 'moved'}
        first = [body('앞'), holder('tbl:1'), *self.cell_rows(), body('끝')]
        second = [body('앞'), changed, *self.cell_rows(), in_cell('', 'tbl:1', 'A3'), in_cell('', 'tbl:1', 'B3'), body('끝')]
        kinds = [item['kind'] for item in reviewed([before], [after], first, second)['comparison']['changes'] if not item.get('in_table')]
        self.assertEqual(kinds, ['format_changed'])

    def test_blank_body_paragraph_style_changes_are_not_waived(self) -> None:
        moved = body('')
        moved['content_sha256'] = 'moved'
        moved['format'] = {**moved['format'], 'paragraph_rest': 'moved'}
        review = reviewed([], [], [body('앞'), body('')], [body('앞'), moved])
        self.assertEqual([item['kind'] for item in review['comparison']['changes']], ['format_changed'])

    def test_a_paragraph_moving_between_cells_is_paired_inside_its_table_only(self) -> None:
        first = [named_table('1', grid_with_properties()), named_table('2', grid_with_properties('x'), index=1)]
        cells = grid_with_properties('x')
        cells[0]['text'], cells[0]['content'] = '', 'blank'
        second = [named_table('1', grid_with_properties()), changed_hash(named_table('2', cells, index=1))]
        rows_one = [in_cell('', 'tbl:1', 'A1'), in_cell('xA1', 'tbl:2', 'A1')]
        rows_two = [in_cell('', 'tbl:1', 'A1'), in_cell('', 'tbl:2', 'A1')]
        changes = reviewed(first, second, rows_one, rows_two)['comparison']['changes']
        self.assertEqual(sorted(item['kind'] for item in changes), ['added', 'removed'])
        self.assertTrue(all(item.get('in_table') for item in changes))


class SavedParts(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def package(self, name='saved_margins', **parts) -> Path:
        target = self.folder / f'{name}.hwpx'
        with zipfile.ZipFile(target, 'w') as out:
            out.writestr('Contents/header.xml', parts.get('header', (FIXTURES / f'{name}.header.xml').read_bytes()))
            out.writestr('Contents/section0.xml', parts.get('section', (FIXTURES / f'{name}.section0.xml').read_bytes()))
        return target

    def live(self, path):
        return copy.deepcopy(rules.package_tables(path))


class HeaderFooterScope(SavedParts):
    def test_the_reader_says_which_part_of_the_section_a_table_is_in(self) -> None:
        tables = {item['table_id']: item['scope'] for item in rules.package_tables(self.package())}
        self.assertEqual(tables, {'tbl:1011': 'header', 'tbl:1012': 'footer', 'tbl:1013': 'body', 'tbl:1014': 'body'})

    def test_both_scopes_are_compared_when_both_are_reported(self) -> None:
        path = self.package()
        live = self.live(path)
        self.assertEqual(rules.persisted_tables(path, live)['state'], 'match')
        changed = copy.deepcopy(live)
        target = next(item for item in changed if item['scope'] == 'header')
        target['cells'][0]['text'] = '다른 글'
        result = rules.persisted_tables(path, changed)
        self.assertEqual(result['state'], 'mismatch')
        self.assertEqual((result['mismatches'][0]['table_id'], result['mismatches'][0]['field']), (target['table_id'], 'text'))

    def test_a_saved_margin_table_the_live_side_does_not_report_is_unverified_scope_not_a_mismatch(self) -> None:
        path = self.package()
        live = [item for item in self.live(path) if item['scope'] == 'body']
        result = rules.persisted_tables(path, live)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(sorted(result['unverified_tables']), ['tbl:1011', 'tbl:1012'])
        self.assertEqual(result['mismatches'], [])

    def test_a_body_table_missing_on_either_side_is_still_a_mismatch(self) -> None:
        path = self.package()
        live = self.live(path)
        fewer = [item for item in live if item['table_id'] != 'tbl:1014']
        self.assertEqual(rules.persisted_tables(path, fewer)['state'], 'mismatch')
        extra = copy.deepcopy(live) + [{**copy.deepcopy(live[-1]), 'table_id': 'tbl:9'}]
        self.assertEqual(rules.persisted_tables(path, extra)['state'], 'mismatch')

    def test_a_live_margin_table_gone_from_the_file_is_a_mismatch(self) -> None:
        path = self.package()
        live = self.live(path) + [{'table_id': 'tbl:7', 'scope': 'footer', 'rows': 1, 'cols': 1, 'cells': []}]
        self.assertEqual(rules.persisted_tables(path, live)['state'], 'mismatch')

    def test_the_reader_also_gives_the_tables_stored_width(self) -> None:
        tables = {item['table_id']: item for item in rules.package_tables(self.package())}
        self.assertEqual(tables['tbl:1011']['width_hu'], 18000)
        path = self.package()
        live = self.live(path)
        live[0]['width_hu'] = 1
        result = rules.persisted_tables(path, live)
        self.assertIn('width_hu', [item['field'] for item in result['mismatches']])


class OtherFills(SavedParts):
    def live_with_fills(self, path):
        return {item['table_id']: item for item in self.live(path)}

    def test_pattern_and_gradation_read_with_their_meaning_from_the_file(self) -> None:
        cells = {item['address']: item for item in self.live_with_fills(self.package())['tbl:1013']['cells']}
        self.assertEqual(cells['B1']['fill'], {'kind': 'other', 'family': 'pattern', 'meaning': {'style': 'slash', 'face': '#FFFFFF', 'hatch': '#0000FF', 'alpha': 0}})
        self.assertEqual(cells['C1']['fill'], {'kind': 'other', 'family': 'gradation', 'meaning': {'type': 'linear', 'angle': 90, 'center_x': 0, 'center_y': 0, 'step': 255, 'step_center': 50, 'colors': ['#FF0000', '#0000FF'], 'alpha': 0}})

    def test_a_live_digest_is_no_difference_and_the_same_meaning_is_a_match(self) -> None:
        path = self.package()
        live = self.live(path)
        for item in live:
            for row in item['cells']:
                if row['fill']['kind'] == 'other':
                    row['fill'] = {**row['fill'], 'digest': '0123456789abcdef'}
        result = rules.persisted_tables(path, live)
        self.assertEqual(result['state'], 'match')
        self.assertEqual(result['fills_not_compared'], 0)

    def test_another_pattern_or_family_is_a_mismatch(self) -> None:
        path = self.package()
        for changed in ({'kind': 'other', 'family': 'pattern', 'meaning': {'style': 'cross', 'face': '#FFFFFF', 'hatch': '#0000FF', 'alpha': 0}},
                        {'kind': 'other', 'family': 'gradation', 'meaning': {}},
                        {'kind': 'solid', 'color': '#FFFFFF'}):
            with self.subTest(changed=changed):
                live = self.live(path)
                row = next(item for item in live if item['table_id'] == 'tbl:1013')['cells'][1]
                row['fill'] = changed
                result = rules.persisted_tables(path, live)
                self.assertEqual(result['state'], 'mismatch')
                self.assertEqual(result['mismatches'][0]['field'], 'fill')

    def test_what_cannot_be_read_on_both_sides_is_not_compared_and_is_partial(self) -> None:
        path = self.package()
        live = self.live(path)
        row = next(item for item in live if item['table_id'] == 'tbl:1013')['cells'][1]
        row['fill'] = {'kind': 'other', 'family': 'pattern'}
        result = rules.persisted_tables(path, live)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['fills_not_compared'], 1)
        self.assertEqual(result['mismatches'], [])

    def test_an_image_fill_is_not_compared_beyond_its_family(self) -> None:
        header = (FIXTURES / 'saved_margins.header.xml').read_bytes().replace(b'<hc:winBrush faceColor="#FFFFFF" hatchColor="#0000FF" hatchStyle="SLASH" alpha="0"/>', b'<hc:imgBrush mode="TOTAL"><hc:img binaryItemIDRef="image1"/></hc:imgBrush>')
        path = self.package(header=header)
        live = self.live(path)
        cell_ = next(item for item in live if item['table_id'] == 'tbl:1013')['cells'][1]
        self.assertEqual(cell_['fill'], {'kind': 'other', 'family': 'image'})
        result = rules.persisted_tables(path, live)
        self.assertEqual((result['state'], result['fills_not_compared']), ('partial', 1))

    def test_a_fill_row_names_its_family_to_a_reader(self) -> None:
        cells = grid_with_properties()
        changed = grid_with_properties()
        changed[0]['fill'] = {'kind': 'other', 'family': 'gradation', 'digest': 'd'}
        review = reviewed([named_table('1', cells)], [changed_hash(named_table('1', changed))])
        self.assertIn('그러데이션', rules.to_markdown(review))


class FullText(SavedParts):
    def package_with(self):
        return self.package('saved_edges')

    def test_a_shorter_or_blank_live_text_is_a_mismatch(self) -> None:
        path = self.package_with()
        for text in ('', 'A'):
            with self.subTest(text=text):
                live = self.live(path)
                target = next(item for item in live[0]['cells'] if len(item['text']) > 1)
                target['text'] = text
                result = rules.persisted_tables(path, live)
                self.assertEqual(result['state'], 'mismatch')
                self.assertEqual(result['mismatches'][0]['field'], 'text')

    def test_a_start_is_enough_only_when_the_row_says_it_was_cut(self) -> None:
        path = self.package_with()
        live = self.live(path)
        target = next(item for item in live[0]['cells'] if len(item['text']) > 1)
        target['text'], target['text_truncated'] = target['text'][:1], True
        self.assertEqual(rules.persisted_tables(path, live)['state'], 'match')
        target['text'] = '다'
        self.assertEqual(rules.persisted_tables(path, live)['state'], 'mismatch')


class Page(unittest.TestCase):
    def over(self, status='off_page'):
        layout = {'status': status, 'width_hu': 54000, 'width_mm': 190.5, 'text_width_hu': 42520, 'left_hu': 8787, 'right_edge_hu': 62787, 'page_width_hu': 59528, 'overflow_hu': 11763, 'overflow_mm': 41.5, 'off_page_mm': 11.5}
        cells = grid_with_properties()
        row = named_table('1012', cells)
        row['layout'] = layout
        return row

    def test_a_table_off_the_page_is_named_and_the_review_is_not_complete(self) -> None:
        row = self.over()
        review = reviewed([row], [row])
        self.assertEqual(review['review_status'], 'partial')
        self.assertEqual(review['coverage']['layout']['overflow'][0]['table_id'], 'tbl:1012')
        self.assertIn('tbl:1012', review['summary_text'].splitlines()[0])
        self.assertIn('쪽 밖으로 11.5mm', review['summary_text'].splitlines()[0])
        self.assertIn('## 표 너비', rules.to_markdown(review))
        self.assertNotIn('전체 확인 완료', review['summary_text'])

    def test_a_table_past_the_margin_is_named_too(self) -> None:
        row = self.over('past_margin')
        row['layout'].pop('off_page_mm')
        review = reviewed([row], [row])
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('본문 오른쪽 여백을 41.5mm', review['summary_text'].splitlines()[0])

    def test_unproved_geometry_is_named_as_unverified_with_its_reason(self) -> None:
        row = named_table('1', grid_with_properties())
        row['layout'] = {'status': 'unverified', 'reason': 'floating', 'width_hu': 9000}
        review = reviewed([row], [row])
        self.assertEqual(review['coverage']['layout']['unverified'], [{'table_id': 'tbl:1', 'page': 1, 'reason': 'floating'}])
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('tbl:1', review['summary_text'])

    def test_tables_that_fit_keep_a_complete_review_and_a_width_change_is_named(self) -> None:
        before = named_table('1', grid_with_properties())
        before['layout'] = {'status': 'fits', 'width_hu': 18000, 'width_mm': 63.5, 'overflow_hu': 0}
        cells = grid_with_properties()
        for item in cells:
            item['width'] = 8000
        after = changed_hash(named_table('1', cells))
        after['layout'] = {'status': 'fits', 'width_hu': 16000, 'width_mm': 56.44, 'overflow_hu': 0}
        review = reviewed([before], [after])
        row = review['comparison']['tables']['changed'][0]
        self.assertEqual(row['width'], {'before_hu': 18000, 'after_hu': 16000})
        self.assertEqual(len(row['width_changes']), 4)
        self.assertNotIn('other_properties', row)
        self.assertEqual(review['review_status'], 'complete')
        self.assertIn('| tbl:1 (1쪽) | 표 전체 | 너비 | 63.5mm | 56.44mm |', rules.to_markdown(review))

    def test_a_row_that_gives_no_layout_says_nothing_about_it(self) -> None:
        row = named_table('1', grid_with_properties())
        review = reviewed([row], [row])
        self.assertEqual(review['coverage']['layout']['state'], 'not_reported')


class Pages(unittest.TestCase):
    def test_a_table_with_no_known_page_lowers_the_review(self) -> None:
        row = named_table('1', grid_with_properties())
        row['page'] = None
        review = reviewed([row], [row])
        self.assertEqual(review['coverage']['tables']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')

    def test_a_margin_table_has_no_page_and_that_is_not_a_gap(self) -> None:
        body_row = named_table('1', grid_with_properties())
        margin = named_table('2', grid_with_properties('x'))
        margin.update(scope='header', page=None)
        review = reviewed([body_row, margin], [body_row, margin])
        self.assertEqual(review['coverage']['tables']['state'], 'complete')
        self.assertIn('머리말', '\n'.join(rules._table_rows({'changed': [], 'unchanged': [], 'added': [{**margin}], 'removed': []})))  # noqa: SLF001


class Media(SavedParts):
    PNG = b'\x89PNG payload'

    def hwpx(self, name, *, referenced=(), files=None) -> Path:
        target = self.folder / f'{name}.hwpx'
        pictures = ''.join(f'<hp:pic><hc:img binaryItemIDRef="{item}"/></hp:pic>' for item in referenced)
        with zipfile.ZipFile(target, 'w') as out:
            out.writestr('Contents/header.xml', '<head/>')
            out.writestr('Contents/section0.xml', f'<sec xmlns:hp="p" xmlns:hc="c">{pictures}</sec>')
            for member, data in (files or {}).items():
                out.writestr(member, data)
        return target

    def compare(self, source, saved):
        return rules.compare_packages(rules.package_inventory(source), rules.package_inventory(saved))

    def test_a_kept_picture_with_a_renamed_member_is_preserved(self) -> None:
        media = self.compare(self.hwpx('a', referenced=['image1'], files={'BinData/image1.png': self.PNG}), self.hwpx('b', referenced=['image1'], files={'BinData/image1.PNG': self.PNG}))['media']
        self.assertEqual((media['referenced_source'], media['referenced_saved'], media['referenced_lost'], media['unreferenced_lost'], media['dangling_saved']), (1, 1, 0, 0, 0))
        self.assertTrue(media['all_bindata_identical'])

    def test_an_unreferenced_image_lost_is_told_apart_from_a_picture(self) -> None:
        media = self.compare(self.hwpx('a', files={'BinData/image1.png': self.PNG}), self.hwpx('b'))['media']
        self.assertEqual((media['referenced_source'], media['referenced_lost'], media['unreferenced_lost']), (0, 0, 1))
        self.assertFalse(media['all_bindata_identical'])

    def test_a_referenced_picture_lost_or_replaced_counts_as_lost(self) -> None:
        source = self.hwpx('a', referenced=['image1'], files={'BinData/image1.png': self.PNG})
        self.assertEqual(self.compare(source, self.hwpx('b'))['media']['referenced_lost'], 1)
        self.assertEqual(self.compare(source, self.hwpx('c', referenced=['image1'], files={'BinData/image1.png': b'other'}))['media']['referenced_lost'], 1)

    def test_a_reference_with_no_payload_is_dangling(self) -> None:
        self.assertEqual(self.compare(self.hwpx('a'), self.hwpx('b', referenced=['image9']))['media']['dangling_saved'], 1)

    def saved(self, **media):
        return {'retrieved': True, 'path': 'x.hwpx', 'compared_with_source': {'bindata_identical': media.get('all_bindata_identical', True), 'media': {'referenced_source': 0, 'referenced_saved': 0, 'referenced_lost': 0, 'unreferenced_lost': 0, 'dangling_saved': 0, 'all_bindata_identical': True, **media}}}

    def review_with(self, saved, first=None, second=None):
        row = named_table('1', grid_with_properties())
        return reviewed(first or [row], second or [row], saved_output=saved)

    def test_an_identical_package_keeps_a_complete_review(self) -> None:
        review = self.review_with(self.saved(referenced_source=2, referenced_saved=2))
        self.assertEqual(review['coverage']['package_media']['state'], 'complete')
        self.assertEqual(review['review_status'], 'complete')
        self.assertIn('참조 그림 2개', review['summary_text'])

    def test_lost_unreferenced_media_is_disclosed_and_is_not_complete_or_a_mismatch(self) -> None:
        review = self.review_with(self.saved(unreferenced_lost=1, all_bindata_identical=False))
        self.assertEqual(review['coverage']['package_media']['state'], 'partial')
        self.assertEqual(review['review_status'], 'partial')
        self.assertIn('참조 없는 이미지 1개', review['summary_text'])
        self.assertNotIn('전체 확인 완료', review['summary_text'])
        self.assertIn('## 저장 파일의 그림', rules.to_markdown(review))
        self.assertIn('열린 문서 기준', review['summary_text'])

    def test_a_lost_picture_no_edit_explains_is_a_mismatch(self) -> None:
        review = self.review_with(self.saved(referenced_source=1, referenced_lost=1, all_bindata_identical=False))
        self.assertEqual(review['coverage']['package_media']['state'], 'mismatch')
        self.assertEqual(review['review_status'], 'mismatch')

    def test_a_lost_picture_with_no_earlier_review_to_explain_it_is_unverified(self) -> None:
        row = named_table('1', grid_with_properties())
        review = rules.assemble(session_id='s', review_id='s/review-1', readback=readback([row]), pdf=None, pages=[{'page': 1, 'path': 'p'}], texts=['글'], baseline=None, saved_output=self.saved(referenced_source=1, referenced_lost=1, all_bindata_identical=False), render_error=None)
        self.assertEqual(review['coverage']['package_media']['state'], 'unverified')
        self.assertEqual(review['review_status'], 'partial')

    def test_a_lost_picture_in_a_requested_delete_is_named_as_removed(self) -> None:
        first_cells = grid_with_properties()
        first_cells[2]['objects'] = ['table', 'picture']
        first_cells[2]['all_objects'] = ['table', 'picture']
        second_cells = grid_with_properties()[:2]
        review = self.review_with(self.saved(referenced_source=1, referenced_lost=1, all_bindata_identical=False), [named_table('1', first_cells)], [changed_hash(named_table('1', second_cells, rows=1))])
        self.assertEqual(review['coverage']['package_media']['state'], 'removed_by_edit')
        self.assertNotEqual(review['review_status'], 'mismatch')
        self.assertIn('삭제한 셀의 그림', review['summary_text'])

    def test_a_dangling_reference_is_a_mismatch(self) -> None:
        self.assertEqual(self.review_with(self.saved(dangling_saved=1))['review_status'], 'mismatch')

    def test_a_package_that_could_not_be_compared_is_not_called_identical(self) -> None:
        review = self.review_with({'retrieved': True, 'path': 'x.doc', 'package': None})
        self.assertEqual(review['coverage']['package_media']['state'], 'not_compared')
        self.assertEqual(review['review_status'], 'complete')

    def test_no_saved_file_says_nothing_about_a_package(self) -> None:
        self.assertEqual(self.review_with(None)['coverage']['package_media']['state'], 'not_saved')


if __name__ == '__main__':
    unittest.main()
