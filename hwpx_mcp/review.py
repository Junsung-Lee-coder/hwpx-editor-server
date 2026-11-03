"""Readable review of a live document: what the numbering, pages and preservation evidence actually say.

Pure functions over the backend's readback and the text of the native PDF, so the judgement is portable and testable.
Rendered labels come from the native PDF, not from a calculation; the calculation is only what they are compared with.
"""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import re
from typing import Any
from xml.parsers import expat
import xml.etree.ElementTree as ET
import zipfile
import zlib

MAX_PACKAGE_MEMBERS = 4096
MAX_PACKAGE_TOTAL_BYTES = 512 * 1024 * 1024
# Section and header text kept in memory, for one package as a whole. It is decoded once more to count controls.
MAX_PACKAGE_TEXT_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
_HANGUL = '가나다라마바사아자차카타파하'
_POSITIONS = {1: '위 왼쪽', 2: '위 가운데', 3: '위 오른쪽', 4: '아래 왼쪽', 5: '아래 가운데', 6: '아래 오른쪽'}
_EVIDENCE_LABELS = {
    'header_footer': '머리말·꼬리말', 'styles_sha256': '스타일', 'media_sha256': '그림·바이너리 데이터',
    'numberings_sha256': '번호 정의', 'bindata_sha256': '바이너리 데이터 목록',
}
_CONTROL_LABELS = {'tbl': '표', 'gso': '그림·개체', 'head': '머리말', 'foot': '꼬리말', 'pgnp': '쪽 번호', 'nwno': '새 쪽 번호'}


_CHARACTER_FIELDS = ('font_family', 'size_pt', 'bold', 'italic', 'underline', 'underline_type', 'text_color')
_PARAGRAPH_FIELDS = ('align', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt', 'line_spacing_type', 'line_spacing_value')
_FORMAT_LABELS = {
    'font_family': '글꼴', 'size_pt': '크기', 'bold': '굵게', 'italic': '기울임', 'underline': '밑줄', 'underline_type': '밑줄 위치', 'text_color': '글자색',
    'align': '정렬', 'left_indent_pt': '왼쪽 여백', 'right_indent_pt': '오른쪽 여백', 'first_line_indent_pt': '첫 줄', 'line_spacing_type': '줄 간격 종류', 'line_spacing_value': '줄 간격 값',
}
_ALIGN_WORDS = {'justify': '양쪽', 'left': '왼쪽', 'right': '오른쪽', 'center': '가운데', 'distribute': '배분', 'distribute_space': '나눔'}
_SPACING_WORDS = {'percent': '글자에 따라(%)', 'fixed': '고정 값(pt)', 'between_lines': '여백만 지정(pt)', 'at_least': '최소(pt)'}
_MAX_FORMAT_ROWS = 200
_OBJECT_WORDS = {'table': '표', 'picture': '그림'}
_UNDERLINE_WORDS = {'top': '위쪽', 'bottom': '아래쪽', 'none': '없음'}


_STATUS_LABELS = {'complete': '전체 확인', 'partial': '일부만 확인', 'unverified': '확인하지 못함', 'mismatch': '불일치'}


class PackageUnreadable(ValueError):
    """The file could not be inspected as an HWPX package; ``code`` says whether it is corrupt or over a limit."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def status_label(status: str) -> str:
    return _STATUS_LABELS[status]


def official_label(level: int, count: int) -> str | None:
    """Official-document label for a 0-based level and 1-based count; None where no rule is claimed."""
    if count < 1 or level not in {0, 1, 2, 3}:
        return None
    if level in {0, 2}:
        return f'{count}{"." if level == 0 else ")"}'
    if count > len(_HANGUL):
        return None
    return f'{_HANGUL[count - 1]}{"." if level == 1 else ")"}'


def expected_labels(paragraphs: list[dict[str, Any]]) -> list[str | None]:
    """One label per OUTLINE body paragraph: a level counts up and every deeper level starts over."""
    counters = [0] * 10
    labels: list[str | None] = []
    for item in paragraphs:
        if item['heading'] != 'OUTLINE' or not item.get('in_body'):
            continue
        level = item['level']
        counters[level] += 1
        counters[level + 1:] = [0] * (len(counters) - level - 1)
        labels.append(official_label(level, counters[level]))
    return labels


def _compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def _has_text(page_texts: list[str] | None) -> bool:
    return bool(page_texts) and any(text.strip() for text in page_texts)


def rendered_numbering(paragraphs: list[dict[str, Any]], page_texts: list[str] | None, *, pages_complete: bool = True) -> list[dict[str, Any]]:
    """Find each numbered paragraph in the native PDF's text, in order, and read the label printed before it.

    A row says what is known about it: match, mismatch (printed differently or without a label), outside_rendered_pages,
    or unverified (no readable text, no label rule, or the paragraph could not be found in text that was read).
    """
    readable = _has_text(page_texts)
    lines = [(number, _compact(line)) for number, text in enumerate(page_texts or [], start=1) for line in text.splitlines() if line.strip()]
    expected = iter(expected_labels(paragraphs))
    cursor = 0
    rows: list[dict[str, Any]] = []
    for item in paragraphs:
        if item['heading'] != 'OUTLINE' or not item.get('in_body'):
            continue
        whole = _compact(item['text'])
        # A line break can end the first printed line early; then that line must end with exactly this text.
        first_line = _compact(item['text'].split('\n', 1)[0]) if '\n' in item['text'] else ''
        row = {'level': item['level'] + 1, 'text': item['text'], 'expected_label': next(expected), 'rendered_label': None, 'page': None}
        # The first printed line of a long paragraph can hold only the start of its text.
        for wanted in (whole[:24], whole[:10]) if whole else ():
            found = next((index for index in range(cursor, len(lines)) if wanted in lines[index][1]), None)
            if found is None and first_line:
                wanted = first_line
                found = next((index for index in range(cursor, len(lines)) if lines[index][1].endswith(wanted)), None)
            if found is not None:
                row['rendered_label'] = lines[found][1][:lines[found][1].find(wanted)] or None
                row['page'] = lines[found][0]
                cursor = found + 1
                break
        if not readable or row['expected_label'] is None and row['page'] is not None:
            row['status'] = 'unverified'
        elif row['page'] is None:
            row['status'] = 'outside_rendered_pages' if not pages_complete else 'unverified'
        else:
            row['status'] = 'match' if row['rendered_label'] == row['expected_label'] else 'mismatch'
        row['matches'] = row['status'] == 'match'
        rows.append(row)
    return rows


def rendered_page_numbers(readback: dict[str, Any], page_texts: list[str] | None, *, total_pages: int | None = None) -> dict[str, Any]:
    """all_found is true only when every page of the document was read; null when that is not known."""
    controls = readback.get('page_numbers')
    if controls is None:
        return {'present': None, 'state': 'unverified', 'all_found': None, 'pages': []}
    if not controls:
        return {'present': False, 'pages': []}
    control = controls[0]
    if not isinstance(control, dict) or control.get('draw_pos') is None or not _has_text(page_texts):
        return {'present': True, 'state': 'unverified', 'all_found': None, 'pages': [], 'position': _POSITIONS.get(control.get('draw_pos')) if isinstance(control, dict) else None}
    restarts = readback.get('page_restarts') or []
    start = int(restarts[0]) if restarts and str(restarts[0]).isdigit() else 1
    dash = control.get('side_char') == 45
    pages = []
    for index, text in enumerate(page_texts):
        number = start + index
        shown = f'- {number} -' if dash else str(number)
        # A footer can share the printed line, so the number is its own token on the page's outermost line at that edge.
        pattern = rf'(?:^|\s)-\s*{number}\s*-(?:\s|$)' if dash else rf'(?:^|\s){number}(?:\s|$)'
        lines = [line for line in text.splitlines() if line.strip()]
        edge = lines[-1 if control.get('draw_pos') in {4, 5, 6} else 0] if lines else ''
        pages.append({'page': index + 1, 'expected': shown, 'found': re.search(pattern, edge) is not None})
    if not all(page['found'] for page in pages):
        state, all_found = 'missing', False
    elif isinstance(total_pages, int) and total_pages > len(pages):
        state, all_found = 'partial', None
    else:
        state, all_found = 'complete', True
    return {'present': True, 'state': state, 'position': _POSITIONS.get(control.get('draw_pos')), 'start_number': start, 'side_char': 'dash' if dash else 'none',
            'pages': pages, 'all_found': all_found}


def _field_value(fact: dict[str, Any], name: str) -> Any:
    """underline_type is named only for a position other than bottom, so a known underline is always one of three values."""
    if name == 'underline_type' and isinstance(fact.get('underline'), bool):
        return fact.get('underline_type') or ('bottom' if fact['underline'] else 'none')
    return fact.get(name)


def _changed_fields(left: dict[str, Any], right: dict[str, Any], names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name in names:
        old, new = _field_value(left, name), _field_value(right, name)
        # Turning a bottom underline on or off is the underline row alone.
        if old != new and not (name == 'underline_type' and {old, new} == {'none', 'bottom'}):
            rows[name] = {'before': old, 'after': new}
    return rows


def _characters(fact: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """One entry per character of a run list; a run longer than its shown text still counts its length."""
    rows: list[tuple[str, dict[str, Any]]] = []
    for run in fact.get('runs') or []:
        shown = run.get('text') or ''
        rows.extend((shown[offset] if offset < len(shown) else '', run) for offset in range(int(run.get('length', len(shown)))))
    return rows


def _unnamed_change(before: dict[str, Any], after: dict[str, Any], *, heading_explained: bool = False) -> bool:
    """Whether anything the named values do not cover differs: the remainder of a shape, by content, or an object in the paragraph."""
    if any(before.get(name) != after.get(name) for name in ('paragraph_rest', 'objects_sha256')):
        return True
    if not heading_explained and before.get('paragraph_heading') != after.get('paragraph_heading'):
        return True
    for (_old_char, old_run), (_char, new_run) in zip(_characters(before), _characters(after)):
        if old_run.get('rest') != new_run.get('rest'):
            return True
        if old_run.get('underline_style') and new_run.get('underline_style') and old_run['underline_style'] != new_run['underline_style']:
            return True
    return False


def _range_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """The ranges whose named character values differ, joined while the difference stays the same."""
    ranges: list[dict[str, Any]] = []
    for (_old_char, old_run), (char, new_run) in zip(_characters(before), _characters(after)):
        changes = _changed_fields(old_run, new_run, _CHARACTER_FIELDS)
        if changes and ranges and ranges[-1]['changes'] == changes and ranges[-1].pop('_open', False):
            ranges[-1]['text'] += char
            ranges[-1]['_count'] += 1
            ranges[-1]['_open'] = True
        elif changes:
            ranges.append({'text': char, 'changes': changes, '_open': True, '_count': 1})
        elif ranges:
            ranges[-1].pop('_open', None)
    for item in ranges:
        item.pop('_open', None)
        count = item.pop('_count')
        # A run shows a bounded start of its text; the length says how much of the range that left out.
        if len(item['text']) < count:
            item['length'] = count
    return ranges


def format_changes(before: dict[str, Any], after: dict[str, Any], *, heading_explained: bool = False) -> dict[str, Any]:
    """What the paragraph's character and paragraph values say changed, in the units a caller uses.

    heading_explained: the review reports this paragraph's number change itself, so that difference needs no other explanation.
    """
    old, new = before.get('format'), after.get('format')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {}
    ranges = _range_changes(old, new)
    paragraph = _changed_fields(old.get('paragraph') or {}, new.get('paragraph') or {}, _PARAGRAPH_FIELDS)
    detail: dict[str, Any] = {'ranges': ranges, 'paragraph': paragraph, 'objects': new.get('objects')}
    if old.get('runs_truncated') or new.get('runs_truncated'):
        detail['truncated'] = True
    elif _unnamed_change(old, new, heading_explained=heading_explained) or not (ranges or paragraph or (heading_explained and old.get('paragraph_heading') != new.get('paragraph_heading'))):
        detail['other_properties'] = True
    return detail


def _nothing_left(detail: dict[str, Any]) -> bool:
    """A format difference the reported number change explains entirely."""
    return 'ranges' in detail and not (detail['ranges'] or detail['paragraph'] or detail.get('truncated') or detail.get('other_properties'))


def _mark_values(fact: dict[str, Any]) -> dict[str, Any] | None:
    mark = fact.get('format', {}).get('mark') if isinstance(fact.get('format'), dict) else None
    return mark if isinstance(mark, dict) and mark.get('source') in {'paragraph_end', 'definition'} else None


def number_mark_coverage(paragraphs: list[dict[str, Any]] | None, comparison: dict[str, Any] | None) -> dict[str, Any]:
    """How much of the look of the printed numbers was compared. A label's text, kind and level say nothing about it."""
    numbered = [item for item in paragraphs or [] if item.get('heading') in {'NUMBER', 'BULLET', 'OUTLINE'}]
    if not numbered:
        return {'state': 'none', 'numbered': 0, 'compared': 0, 'changed': 0, 'not_compared': 0, 'new': 0}
    tally = (comparison or {}).get('number_marks') or {}
    compared, changed, new = tally.get('compared', 0), tally.get('changed', 0), tally.get('new', 0)
    missing = max(len(numbered) - compared - new, 0) if comparison and comparison.get('state') != 'unverified' else len(numbered)
    state = 'not_compared' if missing else 'mixed' if new and compared else 'new_only' if new else 'changed' if changed else 'compared'
    return {'state': state, 'numbered': len(numbered), 'compared': compared, 'changed': changed, 'not_compared': missing, 'new': new}


# A printed mark no earlier look exists for, or one that could not be read, leaves the review partial. A label's text and kind are checked apart.
_MARKS_NOT_ALL_COMPARED = frozenset({'not_compared', 'new_only', 'mixed'})


def _new_marks_clause(new: int) -> str:
    return f'새 번호 문단 {new}개는 이전 모양이 없어 비교하지 못했습니다'


def _mark_line(coverage: dict[str, Any]) -> str | None:
    """The look of the marks in one line, with every count that applies; 'compared' is said only when marks were."""
    state, compared, changed, new = coverage.get('state'), coverage.get('compared', 0), coverage.get('changed', 0), coverage.get('new', 0)
    result = f'번호 문단 {compared}개를 비교해 ' + (f'{changed}개가 바뀌었습니다' if changed else '바뀌지 않았습니다')
    if state == 'compared':
        return f'번호 모양: {result}(인쇄가 아니라 서식 값 비교).'
    if state == 'changed':
        return f'번호 모양: {result}.'
    if state == 'new_only':
        return f'번호 모양: {_new_marks_clause(new)}. 번호 글자 확인과는 별개입니다.'
    if state == 'mixed':
        return f'번호 모양: {result}. {_new_marks_clause(new)}.'
    if state == 'not_compared':
        counted = [f'비교 {compared}개'] * bool(compared) + [f'바뀜 {changed}개'] * bool(changed) + [f'새 번호 {new}개'] * bool(new)
        return f"번호 모양: 번호 문단 {coverage.get('numbered')}개 중 {coverage.get('not_compared')}개를 비교하지 못했습니다" + (f"({', '.join(counted)})." if counted else '.')
    return None


def format_coverage(comparison: dict[str, Any] | None) -> dict[str, Any]:
    """How well the reported format changes are explained. Only a change that cannot be explained lowers a review."""
    changes = [item for item in (comparison or {}).get('changes', []) if item['kind'] == 'format_changed']
    unexplained = sum('ranges' not in item for item in changes)
    limited = sum(bool(item.get('truncated') or item.get('other_properties')) for item in changes)
    state = 'unverified' if unexplained else 'partial' if limited else 'complete'
    return {'state': state, 'format_changes': len(changes), 'unexplained': unexplained, 'not_fully_named': limited}


def _paragraph_state(side: dict[str, Any]) -> str:
    if side.get('paragraphs') is None or side.get('coverage', {}).get('paragraphs', {}).get('state') == 'unverified':
        return 'unverified'
    return 'partial' if side.get('paragraphs_truncated') else 'complete'


def _evidence_state(before: dict[str, Any] | None, after: dict[str, Any] | None, name: str) -> str:
    left, right = (before or {}).get(name), (after or {}).get(name)
    if left is None or right is None:
        return 'unproven'
    return 'unchanged' if left == right else 'changed'


def _named_table_change(item: dict[str, Any]) -> bool:
    return bool(item.get('grid') or item.get('width') or any(item.get(key) for key in _NAMED_TABLE_CHANGES))


def _explained_tables(tables: dict[str, Any] | None, current: list[dict[str, Any]] | None) -> set[str]:
    """Tables whose change the comparison names, with the tables holding them: a paragraph holding either changes with them."""
    if not tables:
        return set()
    parents = {row['table_id']: row.get('nested_in') for row in current or []}
    explained: set[str] = set()
    for item in tables['changed']:
        if _named_table_change(item):
            at = item['table_id']
            while at and at not in explained:
                explained.add(at)
                at = parents.get(at)
    # Only a stored height or size moved in a table of the same hash: that is what its holder's object digest changed with.
    for item in (*tables.get('stored_heights', []), *tables.get('stored_sizes', [])):
        at = item['table_id']
        while at and at not in explained:
            explained.add(at)
            at = parents.get(at)
    return explained


def _only_table_object_differs(before: dict[str, Any], after: dict[str, Any], explained: set[str]) -> bool:
    """A paragraph holding a table changes its object digest with the table. Only the change of that same table, by name, explains it."""
    if not explained or not set(after.get('holds_tables') or ()) & explained or before.get('holds_tables') != after.get('holds_tables'):
        return False
    old, new = before.get('format'), after.get('format')
    if not isinstance(old, dict) or not isinstance(new, dict) or 'table' not in (new.get('objects') or []) or old.get('objects') != new.get('objects'):
        return False
    ignored = {'objects_sha256'}
    return {key: value for key, value in old.items() if key not in ignored} == {key: value for key, value in new.items() if key not in ignored}


def _table_mark(paragraph: dict[str, Any], tables: dict[str, Any] | None) -> dict[str, Any]:
    """A paragraph of a table cell that came or went is the table's change, which the table comparison reports."""
    return {'in_table': True} if tables and (tables['changed'] or tables['added'] or tables['removed']) and (paragraph.get('context') == 'table_cell' or paragraph.get('table_id')) else {}


def _object_mark(paragraph: dict[str, Any]) -> dict[str, Any]:
    """A removed paragraph's objects, so a picture gone from a saved file can be traced to it."""
    objects = (paragraph.get('format') or {}).get('objects') if isinstance(paragraph.get('format'), dict) else None
    return {'objects': objects} if objects else {}


def _scoped(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """Whether every cell paragraph on both sides says which table and cell it belongs to."""
    return all(item.get('table_id') for side in (before, after) for item in side if item.get('context') == 'table_cell')


def _paragraph_groups(baseline: dict[str, Any], current: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[tuple[list[int], list[int]]]:
    """What may pair with what: the document's own paragraphs as one sequence, and each table's only within the cell that became which. A blank cell paragraph never pairs with a blank holder."""
    if baseline.get('paragraphs_truncated') or current.get('paragraphs_truncated') or not _scoped(before, after):
        return [(list(range(len(before))), list(range(len(after))))]
    groups = [([index for index, item in enumerate(before) if not item.get('table_id')], [index for index, item in enumerate(after) if not item.get('table_id')])]
    old_tables, new_tables = {row['table_id']: row for row in baseline.get('tables') or []}, {row['table_id']: row for row in current.get('tables') or []}
    left_ids = {item['table_id'] for item in before if item.get('table_id')}
    right_ids = {item['table_id'] for item in after if item.get('table_id')}
    for table_id in sorted(left_ids | right_ids):
        left = {}
        for index, item in enumerate(before):
            if item.get('table_id') == table_id:
                left.setdefault(item.get('cell'), []).append(index)
        right = {}
        for index, item in enumerate(after):
            if item.get('table_id') == table_id:
                right.setdefault(item.get('cell'), []).append(index)
        if table_id in old_tables and table_id in new_tables:
            pairs, removed, added = _cell_pairs(old_tables[table_id]['cells'], new_tables[table_id]['cells'])
            for old, new in pairs:
                groups.append((left.pop(old['address'], []), right.pop(new['address'], [])))
            for old in removed:
                groups.append((left.pop(old['address'], []), []))
            for new in added:
                groups.append(([], right.pop(new['address'], [])))
        groups.append(([index for rows in left.values() for index in rows], [index for rows in right.values() for index in rows]))
    return groups


def compare(baseline: dict[str, Any], current: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Paragraph changes and preservation against an earlier review of the same document or its saved copy."""
    evidence = {name: _evidence_state(baseline.get('evidence'), current.get('evidence'), name) for name in _EVIDENCE_LABELS}
    before_controls, after_controls = baseline.get('controls'), current.get('controls')
    controls = None if before_controls is None or after_controls is None else {
        name: {'before': before_controls.get(name, 0), 'after': after_controls.get(name, 0)} for name in sorted(set(before_controls) | set(after_controls))}
    result = {'baseline': baseline.get('review_id'), 'changes': [], 'evidence': evidence, 'controls': controls, 'numbers_not_compared': 0,
              'nested_paragraphs_identical': None, 'nested_paragraph_count': None, 'nested_format_changes': None, 'number_marks': {'compared': 0, 'changed': 0, 'new': 0}}
    states = {_paragraph_state(baseline), _paragraph_state(current)}
    if 'unverified' in states:
        return {**result, 'state': 'unverified'}
    before, after = baseline['paragraphs'], current['paragraphs']
    tables_compared = compare_tables(baseline.get('tables'), current.get('tables'))
    result['tables'] = tables_compared
    explained_tables = _explained_tables(tables_compared, current.get('tables'))
    old_labels, new_labels = _labels_by_paragraph(before, baseline.get('numbering') or []), _labels_by_paragraph(after, rows)
    changes: list[dict[str, Any]] = []
    uncompared = 0
    nested_format = 0
    marks = {'compared': 0, 'changed': 0, 'new': 0}
    for left_indexes, right_indexes in _paragraph_groups(baseline, current, before, after):
        # A paragraph holding a table is that table's place: it pairs with no other blank paragraph.
        matcher = difflib.SequenceMatcher(None, [(before[index]['text'], tuple(before[index].get('holds_tables') or ())) for index in left_indexes], [(after[index]['text'], tuple(after[index].get('holds_tables') or ())) for index in right_indexes], autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == 'equal':
                for left, right in zip(left_indexes[i1:i2], right_indexes[j1:j2]):
                    (old_label, old_known), (new_label, new_known) = old_labels[left], new_labels[right]
                    changes_before = len(changes)
                    if old_known and new_known:
                        if old_label != new_label:
                            changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_label, 'after': new_label})
                    elif ((before[left]['heading'], before[left]['level'], before[left].get('numbering_ref_sha256'))
                          != (after[right]['heading'], after[right]['level'], after[right].get('numbering_ref_sha256'))):
                        changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_label or _numbering_kind(before[left]), 'after': new_label or _numbering_kind(after[right])})
                    else:
                        uncompared += 1
                    if before[left]['content_sha256'] != after[right]['content_sha256']:
                        # A number row explains the paragraph's numbering values, but not a definition that changed under a number it already had.
                        explained = changes_before != len(changes) and (before[left]['heading'] == 'NONE' or after[right]['heading'] == 'NONE' or before[left].get('numbering_ref_sha256') == after[right].get('numbering_ref_sha256'))
                        detail = format_changes(before[left], after[right], heading_explained=explained)
                        if not _only_table_object_differs(before[left], after[right], explained_tables) and not (explained and _nothing_left(detail)):
                            changes.append({'kind': 'format_changed', 'text': after[right]['text'], 'paragraph_index': after[right].get('index', right), **detail})
                            nested_format += not after[right].get('in_body')
                    old_mark, new_mark = _mark_values(before[left]), _mark_values(after[right])
                    if before[left]['heading'] == 'NONE' and after[right]['heading'] != 'NONE':
                        marks['new'] += 1  # It had no mark, so there is no earlier look to compare.
                    elif old_mark is not None and new_mark is not None:
                        marks['compared'] += 1
                        mark_changes = _changed_fields(old_mark, new_mark, ('kind', 'source', *_CHARACTER_FIELDS))
                        if mark_changes:
                            marks['changed'] += 1
                            changes.append({'kind': 'number_mark_changed', 'text': after[right]['text'], 'paragraph_index': after[right].get('index', right), 'changes': mark_changes, 'objects': (after[right].get('format') or {}).get('objects')})
                continue
            # What runs off the end of a truncated list says nothing about the document.
            if not (baseline.get('paragraphs_truncated') and i2 == len(left_indexes)):
                changes.extend({'kind': 'removed', 'text': before[index]['text'], 'before': old_labels[index][0] or _numbering_kind(before[index]), **_table_mark(before[index], tables_compared), **_object_mark(before[index])} for index in left_indexes[i1:i2])
            if not (current.get('paragraphs_truncated') and j2 == len(right_indexes)):
                changes.extend({'kind': 'added', 'text': after[index]['text'], 'after': new_labels[index][0] or _numbering_kind(after[index]), **_table_mark(after[index], tables_compared)} for index in right_indexes[j1:j2])
                marks['new'] += sum(after[index]['heading'] != 'NONE' for index in right_indexes[j1:j2])
    partial = 'partial' in states
    cells_before = [item['text'] for item in before if not item.get('in_body')]
    cells_after = [item['text'] for item in after if not item.get('in_body')]
    return {
        **result,
        'state': 'partial' if partial or uncompared or 'unproven' in evidence.values() or controls is None else 'complete',
        'changes': changes,
        'numbers_not_compared': uncompared,
        'nested_paragraphs_identical': None if partial else cells_before == cells_after,
        'nested_paragraph_count': None if partial else len(cells_after),
        'nested_format_changes': None if partial else nested_format,
        'number_marks': marks,
    }


_SIDE_WORDS = {'left': '왼쪽 선', 'right': '오른쪽 선', 'top': '위쪽 선', 'bottom': '아래쪽 선'}
_LINE_WORDS = {'none': '없음', 'solid': '실선', 'dash': '긴 점선', 'dot': '점선', 'dash_dot': '일점쇄선', 'dash_dot_dot': '이점쇄선', 'long_dash': '긴 파선', 'circle': '원형 점선',
               'double': '이중선', 'slim_thick': '가는+굵은 이중선', 'thick_slim': '굵은+가는 이중선', 'slim_thick_slim': '삼중선'}
_MAX_TABLE_ROWS = 200
_NAMED_TABLE_CHANGES = ('edges', 'fills', 'moved_cells', 'span_changes', 'content_changes', 'added_cells', 'removed_cells', 'frame_changes', 'cell_properties', 'width_changes', 'styled_content_cells')
_FRAME_WORDS = {'zorder': '표 겹침 순서', 'page_break': '쪽 나눔', 'repeat_header': '제목 줄 반복', 'text_wrap': '본문과의 배치', 'cell_spacing': '셀 간격', 'numbering_type': '번호 종류', 'lock': '잠금', 'treat_as_char': '글자처럼 취급',
                'horz_rel_to': '가로 기준', 'horz_align': '가로 정렬', 'horz_offset': '가로 위치', 'vert_rel_to': '세로 기준', 'vert_align': '세로 정렬', 'vert_offset': '세로 위치', 'flow_with_text': '쪽 영역 안으로 제한', 'allow_overlap': '겹침 허용',
                'outside_margin': '바깥 여백', 'inside_margin': '안쪽 여백'}
_CELL_WORDS = {'margin': '셀 여백', 'vertical_align': '세로 정렬', 'line_wrap': '줄 나눔', 'text_direction': '글 방향', 'has_margin': '셀 여백 지정', 'header': '제목 셀', 'protect': '보호', 'editable': '양식 편집'}
_SCOPE_WORDS = {'header': '머리말', 'footer': '꼬리말'}
_FILL_FAMILIES = {'pattern': '무늬', 'gradation': '그러데이션', 'image': '그림', 'mixed': '여러 채우기'}


def _table_cell_key(cell: dict[str, Any]) -> tuple[Any, ...]:
    return (cell.get('content'), tuple(cell.get('objects') or ()))


def _cell_pairs(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Which cell became which. Cells with content are followed by what they hold; the rest stay at their address, else count as added or removed."""
    matcher = difflib.SequenceMatcher(None, [_table_cell_key(cell) for cell in before], [_table_cell_key(cell) for cell in after], autojunk=False)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    loose_before: list[dict[str, Any]] = []
    loose_after: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            pairs.extend(zip(before[i1:i2], after[j1:j2]))
        else:
            loose_before.extend(before[i1:i2])
            loose_after.extend(after[j1:j2])
    by_address = {cell['address']: cell for cell in loose_after}
    removed: list[dict[str, Any]] = []
    for cell in loose_before:
        partner = by_address.pop(cell['address'], None)
        if partner is None:
            removed.append(cell)
        else:
            pairs.append((cell, partner))
    return pairs, removed, list(by_address.values())


def _compare_table(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    pairs, removed, added = _cell_pairs(before['cells'], after['cells'])
    row: dict[str, Any] = {'table_id': after['table_id'], 'page': after.get('page'), 'index': after.get('index'), 'scope': after.get('scope', 'body'), 'before_text': after.get('before_text'), 'edges': [], 'fills': [], 'moved_cells': [], 'span_changes': [], 'content_changes': [],
                           'frame_changes': [], 'cell_properties': [], 'width_changes': [], 'added_cells': [{'cell': cell['address'], 'text': cell['text']} for cell in added],
                           'removed_cells': [{'cell': cell['address'], 'text': cell['text'], **({'objects': cell.get('all_objects') or cell['objects']} if cell.get('all_objects') or cell.get('objects') else {})} for cell in removed]}
    if (before['rows'], before['cols']) != (after['rows'], after['cols']):
        row['grid'] = {'before': {'rows': before['rows'], 'cols': before['cols']}, 'after': {'rows': after['rows'], 'cols': after['cols']}}
    other = False
    old_frame, new_frame = before.get('frame_values') or {}, after.get('frame_values') or {}
    for name in sorted(set(old_frame) | set(new_frame)):
        if old_frame.get(name) != new_frame.get(name):
            row['frame_changes'].append({'field': name, 'before': old_frame.get(name), 'after': new_frame.get(name)})
    if before.get('frame') != after.get('frame') and not row['frame_changes']:
        other = True
    old_width, new_width = _table_width(before), _table_width(after)
    if old_width is not None and new_width is not None and old_width != new_width:
        row['width'] = {'before_hu': old_width, 'after_hu': new_width}
    if before.get('height_hu') is not None and after.get('height_hu') is not None and before['height_hu'] != after['height_hu']:
        row['stored_height'] = {'before_hu': before['height_hu'], 'after_hu': after['height_hu']}
    for old, new in pairs:
        if old['address'] != new['address']:
            row['moved_cells'].append({'from': old['address'], 'to': new['address'], 'text': new['text']})
        if (old['row_span'], old['col_span']) != (new['row_span'], new['col_span']):
            row['span_changes'].append({'cell': new['address'], 'before': {'row_span': old['row_span'], 'col_span': old['col_span']}, 'after': {'row_span': new['row_span'], 'col_span': new['col_span']}})
        if old.get('content') != new.get('content'):
            if old['text'] != new['text']:
                row['content_changes'].append({'cell': new['address'], 'before': old['text'], 'after': new['text']})
            else:
                row.setdefault('styled_content_cells', []).append(new['address'])
        for side in ('left', 'right', 'top', 'bottom'):
            if old['border'][side] != new['border'][side]:
                row['edges'].append({'cell': new['address'], 'side': side, 'before': old['border'][side], 'after': new['border'][side]})
        if old['fill'] != new['fill']:
            row['fills'].append({'cell': new['address'], 'before': old['fill'], 'after': new['fill']})
        if old['width'] != new['width'] and (old['row_span'], old['col_span']) == (new['row_span'], new['col_span']):
            row['width_changes'].append({'cell': new['address'], 'before_hu': old['width'], 'after_hu': new['width']})
        old_values, new_values = old.get('attr_values') or {}, new.get('attr_values') or {}
        named = [name for name in sorted(set(old_values) | set(new_values)) if old_values.get(name) != new_values.get(name)]
        row['cell_properties'].extend({'cell': new['address'], 'field': name, 'before': old_values.get(name), 'after': new_values.get(name)} for name in named)
        if old.get('rest') != new.get('rest') or (old.get('attrs') != new.get('attrs') and not named):
            other = True
    # A hash that moved with nothing named is never called unchanged: something this comparison has no name for changed.
    if other or not _named_table_change(row):
        row['other_properties'] = True
    return row


def _table_width(table: dict[str, Any]) -> int | None:
    value = (table.get('layout') or {}).get('width_hu')
    return value if value is not None else table.get('width_hu')


def _size_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The table's own stored size. Layout moves its height after any edit, of this table or another; a move is told, never equal by fiat."""
    moved: dict[str, Any] = {}
    for name, key in (('width', 'width_hu'), ('height', 'height_hu')):
        old, new = before.get(key), after.get(key)
        if old is not None and new is not None and old != new:
            moved[name] = {'before_hu': old, 'after_hu': new}
    return moved


def _height_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """A table's hash leaves heights out, as layout moves them. A height moving is still a difference, told by name."""
    old = {cell['address']: cell.get('height') for cell in before['cells']}
    return [{'cell': cell['address'], 'before': old[cell['address']], 'after': cell.get('height')} for cell in after['cells'] if cell['address'] in old and cell.get('height') is not None and old[cell['address']] is not None and old[cell['address']] != cell.get('height')]


def compare_tables(baseline: list[dict[str, Any]] | None, current: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Each table by identity: what changed, with values, and which tables did not. A table is never called unchanged unless its hash agrees."""
    if baseline is None or current is None:
        return None
    before, after = {item['table_id']: item for item in baseline}, {item['table_id']: item for item in current}
    result: dict[str, Any] = {'changed': [], 'unchanged': [], 'added': [], 'removed': []}
    for table_id, table in after.items():
        old = before.get(table_id)
        if old is None:
            result['added'].append({'table_id': table_id, 'page': table.get('page'), 'rows': table['rows'], 'cols': table['cols'], 'before_text': table.get('before_text'), 'scope': table.get('scope', 'body')})
        elif old.get('state_sha256') == table.get('state_sha256'):
            result['unchanged'].append(table_id)
            heights = _height_changes(old, table)
            if heights:
                result.setdefault('stored_heights', []).append({'table_id': table_id, 'page': table.get('page'), 'scope': table.get('scope', 'body'), 'cells': heights})
            size = _size_changes(old, table)
            if size:
                result.setdefault('stored_sizes', []).append({'table_id': table_id, 'page': table.get('page'), 'scope': table.get('scope', 'body'), **size})
        else:
            result['changed'].append(_compare_table(old, table))
    result['removed'] = [{'table_id': table_id, 'page': table.get('page'), 'rows': table['rows'], 'cols': table['cols'], 'scope': table.get('scope', 'body'),
                          'pictures': sum((cell.get('all_objects') or cell.get('objects') or []).count('picture') for cell in table['cells'])} for table_id, table in before.items() if table_id not in after]
    return result


def table_coverage(tables: list[dict[str, Any]] | None, truncated: bool, comparison: dict[str, Any] | None, *, has_baseline: bool) -> dict[str, Any]:
    if tables is None:
        return {'state': 'unverified', 'tables': None, 'compared': 0}
    if truncated:
        return {'state': 'partial', 'tables': len(tables), 'compared': 0}
    limited = sum(bool(item.get('other_properties')) for item in (comparison or {}).get('changed', []))
    if not tables and not (comparison or {}).get('removed'):
        return {'state': 'none', 'tables': 0, 'compared': 0}
    if has_baseline and comparison is None:
        return {'state': 'unverified', 'tables': len(tables), 'compared': 0}
    compared = len((comparison or {}).get('changed', [])) + len((comparison or {}).get('unchanged', []))
    pages_unknown = sum(item.get('scope', 'body') == 'body' and item.get('page') is None for item in tables)
    return {'state': 'partial' if limited or pages_unknown else 'complete', 'tables': len(tables), 'compared': compared, 'not_fully_named': limited, 'pages_unknown': pages_unknown}


def layout_coverage(tables: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Every table's width against the text and the page, as the live document states them: what overflows and what could not be proved, by table and page."""
    rows = [item for item in tables or [] if item.get('layout')]
    if not rows:
        return {'state': 'not_reported', 'overflow': [], 'unverified': []}
    overflow = [{'table_id': item['table_id'], 'page': item.get('page'), 'status': item['layout']['status'], **{key: item['layout'][key] for key in ('width_mm', 'overflow_mm', 'off_page_mm', 'width_hu', 'overflow_hu') if key in item['layout']}}
                for item in rows if item['layout'].get('status') in {'off_page', 'past_margin', 'wider_than_parent'}]
    unverified = [{'table_id': item['table_id'], 'page': item.get('page'), 'reason': item['layout'].get('reason')} for item in rows if item['layout'].get('status') == 'unverified']
    return {'state': 'problem' if overflow else 'unverified' if unverified else 'complete', 'tables': len(rows), 'overflow': overflow, 'unverified': unverified}


def _layout_note(row: dict[str, Any]) -> str:
    where = f"{row['table_id']}({row['page']}쪽)" if row.get('page') else row['table_id']
    if row['status'] == 'off_page':
        return f"표 {where}가 본문 오른쪽 여백을 {row.get('overflow_mm'):g}mm 넘고 쪽 밖으로 {row.get('off_page_mm'):g}mm 나가 인쇄에서 잘립니다"
    if row['status'] == 'past_margin':
        return f"표 {where}가 본문 오른쪽 여백을 {row.get('overflow_mm'):g}mm 넘습니다"
    return f"표 {where}가 담긴 셀의 안쪽 폭보다 {row.get('overflow_mm'):g}mm 넓습니다"


def _lost_by_identity(lost: list[str], before: list[str | None] | None, after: list[str | None] | None) -> tuple[list[str], list[str], list[str]]:
    """Which lost image data a removal explains, by the data itself: (explained, unexplained, untraceable).

    A picture removed between the earlier review and now explains the loss of its data only when no picture left still shows it. Data a
    picture left still shows, lost, is unexplained. Data no picture showed at the earlier review cannot be traced to anything here."""
    remaining: dict[str, int] = {}
    for payload in before or ():
        remaining[payload] = remaining.get(payload, 0) + 1
    for payload in after or ():
        remaining[payload] = remaining.get(payload, 0) - 1
    removed = {payload for payload, count in remaining.items() if count > 0}
    explained, unexplained, untraceable = [], [], []
    for payload in lost:
        if payload in set(after or ()):
            unexplained.append(payload)
        elif payload in removed:
            explained.append(payload)
        else:
            untraceable.append(payload)
    return explained, unexplained, untraceable


def package_media_coverage(saved_output: dict[str, Any] | None, baseline: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """The saved file's image data against the file that was opened: pictures the document shows apart from data nothing refers to, and
    data a requested removal took apart from any other loss, matched by the data's identity and never by a count."""
    if not isinstance(saved_output, dict) or saved_output.get('retrieved') is not True:
        return {'state': 'not_saved'}
    media = (saved_output.get('compared_with_source') or {}).get('media')
    if not isinstance(media, dict):
        return {'state': 'not_compared'}
    result = {key: media.get(key) for key in ('referenced_source', 'referenced_saved', 'referenced_lost', 'unreferenced_lost', 'dangling_saved', 'all_bindata_identical')}
    lost_count = media.get('referenced_lost') or 0
    lost = media.get('referenced_lost_payloads')
    before = (baseline or {}).get('picture_payloads') if baseline is not None else None
    after = current.get('picture_payloads')
    explained: list[str] = []
    unexplained: list[str] = []
    untraceable: list[str] = []
    if lost_count:
        known = (type(lost) is list and len(lost) == lost_count and type(before) is list and type(after) is list
                 and None not in before and None not in after)
        if known:
            explained, unexplained, untraceable = _lost_by_identity(lost, before, after)
        else:
            untraceable = list(lost) if type(lost) is list else []
    if media.get('dangling_saved') or unexplained:
        state = 'mismatch'
    elif lost_count and (untraceable or not explained or len(explained) != lost_count):
        state = 'unverified'
    elif media.get('unreferenced_lost'):
        state = 'partial'
    elif lost_count:
        state = 'removed_by_edit'
    else:
        state = 'complete'
    return {'state': state, **result, 'removed_by_edit': len(explained), 'unexplained_lost': unexplained, 'untraceable_lost': len(untraceable)}


def _media_line(media: dict[str, Any]) -> str | None:
    state = media.get('state')
    if state in {None, 'not_saved', 'not_compared'}:
        return None
    parts = [f"저장 파일의 그림: 저장본에 참조 그림 {media.get('referenced_saved')}개가 있습니다"]
    if state == 'complete':
        parts = [f"저장 파일의 그림: 참조 그림 {media.get('referenced_source')}개의 데이터가 보존됐습니다"]
        if media.get('all_bindata_identical'):
            parts.append('패키지의 그림 데이터 전체도 원본과 같습니다')
    if media.get('removed_by_edit'):
        parts.append(f"삭제한 셀의 그림 {media['removed_by_edit']}개는 요청대로 저장본에서 빠졌습니다")
    if media.get('unexplained_lost'):
        parts.append(f"아직 문서에 있는 그림 {len(media['unexplained_lost'])}개의 데이터가 저장본에 없습니다")
    elif (media.get('referenced_lost') or 0) > (media.get('removed_by_edit') or 0):
        parts.append(f"참조되던 그림 {media['referenced_lost'] - (media.get('removed_by_edit') or 0)}개의 데이터가 저장본에 없고, 어느 삭제로 빠졌는지 대조하지 못했습니다")
    if media.get('dangling_saved'):
        parts.append(f"저장본에서 데이터 없는 참조가 {media['dangling_saved']}개 있습니다")
    if media.get('unreferenced_lost'):
        parts.append(f"원본의 참조 없는 이미지 {media['unreferenced_lost']}개는 저장본에 없습니다. 어떤 그림도 쓰지 않던 데이터로, 한글이 문서를 열 때 읽어 들이지 않습니다")
    return '. '.join(parts) + '.'


def _line_word(line: dict[str, Any]) -> str:
    kind = _LINE_WORDS.get(line.get('line'), str(line.get('line')))
    if line.get('line') == 'none':
        return kind
    return f"{kind} {line.get('width_mm'):g}mm {line.get('color')}"


def _fill_word(fill: dict[str, Any]) -> str:
    if fill.get('kind') == 'other':
        return _FILL_FAMILIES.get(fill.get('family'), '무늬·그러데이션·그림')
    return {'none': '없음', 'solid': str(fill.get('color'))}.get(fill.get('kind'), str(fill.get('kind')))


def _table_name(item: dict[str, Any]) -> str:
    page = _SCOPE_WORDS.get(item.get('scope')) or (f"{item['page']}쪽" if item.get('page') else '쪽 확인 못함')
    return f"{item['table_id']} ({page})"


def _property_word(value: Any) -> str:
    if isinstance(value, dict):
        return ' '.join(f'{key} {inner}' for key, inner in value.items())
    return _cell('없음' if value is None else value)


def _table_rows(tables: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for item in tables['added']:
        rows.append(f"| {_table_name(item)} | 표 전체 | 새 표 | 없음 | {item['rows']}행 {item['cols']}열 |")
    for item in tables['removed']:
        rows.append(f"| {_table_name(item)} | 표 전체 | 표 제거 | {item['rows']}행 {item['cols']}열 | 없음 |")
    for item in tables['changed']:
        name = _table_name(item)
        grid = item.get('grid')
        if grid:
            rows.append(f"| {name} | 표 전체 | 크기 | {grid['before']['rows']}행 {grid['before']['cols']}열 | {grid['after']['rows']}행 {grid['after']['cols']}열 |")
        rows += [f"| {name} | {cell['cell']} | 새 셀 | 없음 | {_cell(_clip(cell['text'], 40)) or '(빈 셀)'} |" for cell in item['added_cells']]
        rows += [f"| {name} | {cell['cell']} | 셀 제거 | {_cell(_clip(cell['text'], 40)) or '(빈 셀)'} | 없음 |" for cell in item['removed_cells']]
        rows += [f"| {name} | {cell['to']} | 위치 이동 | {cell['from']} | {cell['to']} |" for cell in item['moved_cells']]
        rows += [f"| {name} | {cell['cell']} | 병합 범위 | {cell['before']['row_span']}행 {cell['before']['col_span']}열 | {cell['after']['row_span']}행 {cell['after']['col_span']}열 |" for cell in item['span_changes']]
        rows += [f"| {name} | {cell['cell']} | 내용 | {_cell(_clip(cell['before'], 40)) or '(빈 셀)'} | {_cell(_clip(cell['after'], 40)) or '(빈 셀)'} |" for cell in item['content_changes']]
        rows += [f"| {name} | {edge['cell']} | {_SIDE_WORDS[edge['side']]} | {_line_word(edge['before'])} | {_line_word(edge['after'])} |" for edge in item['edges']]
        rows += [f"| {name} | {fill['cell']} | 채우기 | {_fill_word(fill['before'])} | {_fill_word(fill['after'])} |" for fill in item['fills']]
        rows += [f"| {name} | {cell} | 글 서식 | 같은 글 | 서식이나 개체가 다름 |" for cell in item.get('styled_content_cells', [])]
        if item.get('width'):
            rows.append(f"| {name} | 표 전체 | 너비 | {_mm(item['width']['before_hu'])} | {_mm(item['width']['after_hu'])} |")
        rows += [f"| {name} | {cell['cell']} | 셀 너비 | {_mm(cell['before_hu'])} | {_mm(cell['after_hu'])} |" for cell in item.get('width_changes', [])]
        rows += [f"| {name} | 표 전체 | 표 속성: {_FRAME_WORDS.get(change['field'], change['field'])} | {_property_word(change['before'])} | {_property_word(change['after'])} |" for change in item.get('frame_changes', [])]
        rows += [f"| {name} | {change['cell']} | {_CELL_WORDS.get(change['field'], change['field'])} | {_property_word(change['before'])} | {_property_word(change['after'])} |" for change in item.get('cell_properties', [])]
        if item.get('other_properties'):
            rows.append(f'| {name} | 표 안 | 이름 없는 다른 값 | 확인 못함 | 바뀜 |')
    for item in tables.get('stored_heights', []):
        rows += [f"| {_table_name(item)} | {cell['cell']} | 저장된 높이 | {cell['before']} | {cell['after']} |" for cell in item['cells']]
    for item in tables.get('stored_sizes', []):
        rows += [f"| {_table_name(item)} | 표 전체 | {word} | {item[key]['before_hu']} | {item[key]['after_hu']} |" for key, word in (('height', '저장된 높이'), ('width', '저장된 너비')) if key in item]
    for item in tables['changed']:
        if item.get('stored_height'):
            rows.append(f"| {_table_name(item)} | 표 전체 | 저장된 높이 | {item['stored_height']['before_hu']} | {item['stored_height']['after_hu']} |")
    return rows


def _mm(value: int) -> str:
    return f'{round(value * 25.4 / 7200, 2):g}mm'


def _tables_changed(comparison: dict[str, Any]) -> bool:
    tables = comparison.get('tables') or {}
    return bool(tables.get('changed') or tables.get('added') or tables.get('removed'))


def _table_lines(tables: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in tables['added']:
        lines.append(f"표 변경: {_table_name(item)} 새 표 {item['rows']}행 {item['cols']}열")
    for item in tables['removed']:
        lines.append(f"표 변경: {_table_name(item)} 표 제거")
    for item in tables['changed']:
        parts: list[str] = []
        grid = item.get('grid')
        if grid:
            parts.append(f"{grid['before']['rows']}행 {grid['before']['cols']}열 → {grid['after']['rows']}행 {grid['after']['cols']}열")
        if item.get('width'):
            parts.append(f"표 너비 {_mm(item['width']['before_hu'])} → {_mm(item['width']['after_hu'])}")
        for key, word in (('added_cells', '새 셀'), ('removed_cells', '제거된 셀'), ('moved_cells', '이동한 셀'), ('span_changes', '병합 범위가 바뀐 셀'), ('content_changes', '내용이 바뀐 셀'), ('styled_content_cells', '글 서식이 바뀐 셀'), ('edges', '바뀐 선'), ('fills', '채우기가 바뀐 셀'),
                          ('width_changes', '너비가 바뀐 셀'), ('frame_changes', '바뀐 표 속성'), ('cell_properties', '바뀐 셀 속성')):
            if item.get(key):
                parts.append(f'{word} {len(item[key])}개')
        if item.get('other_properties'):
            parts.append('이름 없는 다른 값이 바뀜')
        lines.append(f"표 변경: {_table_name(item)} " + ', '.join(parts))
    if tables['unchanged']:
        lines.append(f"변경 없는 표 {len(tables['unchanged'])}개: {', '.join(tables['unchanged'][:6])}" + (' 외' if len(tables['unchanged']) > 6 else ''))
    for item in tables.get('stored_heights', []):
        lines.append(f"표 저장 높이: {_table_name(item)} " + ', '.join(f"{cell['cell']} {cell['before']} → {cell['after']}" for cell in item['cells'][:4]) + ' (다른 값은 같습니다)')
    sizes = tables.get('stored_sizes', [])
    if sizes:
        named = ', '.join(f"{item['table_id']} " + ' '.join(f"{word} {item[key]['before_hu']} → {item[key]['after_hu']}" for key, word in (('height', '저장 높이'), ('width', '저장 너비')) if key in item) for item in sizes[:4])
        lines.append(f"저장 크기만 바뀐 표 {len(sizes)}개: {named}" + (' 외' if len(sizes) > 4 else '') + ' (내용·선·채우기는 같습니다)')
    return lines


# Measured on Hancom 13.0.0.3457: native line 2 prints as dashes and native line 3 as dots, and the file calls them DOT and DASH.
_HWPX_LINES = {'NONE': 'none', 'SOLID': 'solid', 'DOT': 'dash', 'DASH': 'dot', 'DASH_DOT': 'dash_dot', 'DASH_DOT_DOT': 'dash_dot_dot', 'LONG_DASH': 'long_dash', 'CIRCLE': 'circle', 'DOUBLE_SLIM': 'double', 'SLIM_THICK': 'slim_thick', 'THICK_SLIM': 'thick_slim', 'SLIM_THICK_SLIM': 'slim_thick_slim'}


def _hwpx_int(element: ET.Element, name: str) -> int:
    try:
        return int(_attribute(element, name) or 0)
    except ValueError:
        return 0


def _hwpx_other_fill(brushes: list[ET.Element]) -> dict[str, Any]:
    """A fill this code does not edit, by the meaning the live document can state too. A picture's data is not compared here."""
    names = sorted(_local(item) for item in brushes)
    if names == ['winBrush']:
        brush = brushes[0]
        face = _attribute(brush, 'faceColor') or 'none'
        return {'kind': 'other', 'family': 'pattern', 'meaning': {'style': (_attribute(brush, 'hatchStyle') or '').replace('_', '').lower(), 'face': 'none' if face.lower() == 'none' else face.upper(), 'hatch': (_attribute(brush, 'hatchColor') or '').upper(), 'alpha': _hwpx_int(brush, 'alpha')}}
    if names == ['gradation']:
        brush = brushes[0]
        return {'kind': 'other', 'family': 'gradation', 'meaning': {'type': (_attribute(brush, 'type') or '').replace('_', '').lower(), 'angle': _hwpx_int(brush, 'angle'), 'center_x': _hwpx_int(brush, 'centerX'), 'center_y': _hwpx_int(brush, 'centerY'), 'step': _hwpx_int(brush, 'step'),
                                                                    'step_center': _hwpx_int(brush, 'stepCenter'), 'colors': [(_attribute(item, 'value') or '').upper() for item in brush if _local(item) == 'color'], 'alpha': _hwpx_int(brush, 'alpha')}}
    return {'kind': 'other', 'family': 'image' if names == ['imgBrush'] else 'mixed'}


def _hwpx_border_fill(element: ET.Element) -> dict[str, Any]:
    border: dict[str, Any] = {}
    fill: dict[str, Any] = {'kind': 'none'}
    for child in element:
        name = _local(child)
        if name.endswith('Border') and name[:-6] in {'left', 'right', 'top', 'bottom'}:
            kind = (_attribute(child, 'type') or '').upper()
            width = (_attribute(child, 'width') or '').replace('mm', '').strip()
            border[name[:-6]] = {'line': _HWPX_LINES.get(kind, kind.lower()), 'width_mm': float(width), 'color': (_attribute(child, 'color') or '').upper()}
        elif name == 'fillBrush':
            brushes = list(child)
            if len(brushes) == 1 and _local(brushes[0]) == 'winBrush' and _attribute(brushes[0], 'hatchStyle') in {None, 'NONE'}:
                face = (_attribute(brushes[0], 'faceColor') or 'none')
                fill = {'kind': 'none'} if face.lower() == 'none' else {'kind': 'solid', 'color': face.upper()}
            else:
                fill = _hwpx_other_fill(brushes)
    return {'border': border, 'fill': fill}


def _hwpx_cell_text(cell: ET.Element) -> str:
    rows: list[str] = []
    holder = next((item for item in cell if _local(item) == 'subList'), None)
    for paragraph in holder if holder is not None else ():
        if _local(paragraph) != 'p':
            continue
        text = ''
        for run in paragraph:
            if _local(run) != 'run':
                continue
            for item in run:
                if _local(item) != 't':
                    continue
                text += item.text or ''
                for inner in item:
                    text += _HWPX_INLINE.get(_local(inner), '') + (inner.tail or '')
        rows.append(text)
    return '\n'.join(rows)


def package_tables(path: Path) -> list[dict[str, Any]]:
    """Every table the HWPX file holds, read with no help from Hancom: identity, grid, cell text, spans, lines and fill."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos if re.fullmatch(r'Contents/(section\d+|header)\.xml', info.filename)) > MAX_PACKAGE_TEXT_BYTES:
            raise PackageUnreadable('package_limit', 'The text of this HWPX package is larger than this inspection reads.')
        names = sorted((info.filename for info in infos if re.fullmatch(r'Contents/section\d+\.xml', info.filename)), key=lambda name: int(re.search(r'\d+', name).group()))
        header, *sections = [_parsed_part(archive.read(name)) for name in ('Contents/header.xml', *names)]
    fills = {_attribute(item, 'id') or '': _hwpx_border_fill(item) for item in header.iter() if _local(item) == 'borderFill'}
    tables: list[dict[str, Any]] = []

    def visit(element: ET.Element, scope: str) -> None:
        for child in element:
            name = _local(child)
            inner = name if name in {'header', 'footer'} else scope
            if name == 'tbl':
                tables.append(read_table(child, inner))
            visit(child, inner)

    def read_table(table: ET.Element, scope: str) -> dict[str, Any]:
        cells: list[dict[str, Any]] = []
        for row in table:
            if _local(row) != 'tr':
                continue
            for cell in row:
                if _local(cell) != 'tc':
                    continue
                addr = next(item for item in cell if _local(item) == 'cellAddr')
                span = next(item for item in cell if _local(item) == 'cellSpan')
                size = next(item for item in cell if _local(item) == 'cellSz')
                number_row, number_col = int(_attribute(addr, 'rowAddr')) + 1, int(_attribute(addr, 'colAddr')) + 1
                letters = ''
                remaining = number_col
                while remaining:
                    remaining, rem = divmod(remaining - 1, 26)
                    letters = chr(65 + rem) + letters
                cells.append({'address': f'{letters}{number_row}', 'row': number_row, 'col': number_col, 'row_span': int(_attribute(span, 'rowSpan')), 'col_span': int(_attribute(span, 'colSpan')), 'width': int(_attribute(size, 'width')),
                              'text': _hwpx_cell_text(cell), **fills[_attribute(cell, 'borderFillIDRef') or '']})
        size = next((item for item in table if _local(item) == 'sz'), None)
        return {'table_id': f"tbl:{_attribute(table, 'id')}", 'scope': scope, 'rows': int(_attribute(table, 'rowCnt')), 'cols': int(_attribute(table, 'colCnt')), 'width_hu': int(_attribute(size, 'width')) if size is not None else None,
                'height_hu': int(_attribute(size, 'height')) if size is not None and _attribute(size, 'height') is not None else None,
                'cells': sorted(cells, key=lambda item: (item['row'], item['col']))}

    for section in sections:
        visit(section, 'body')
    return tables


def _same_fill(live: dict[str, Any], saved: dict[str, Any]) -> bool | None:
    """Whether two fills mean the same, or None when the two sides cannot both say. A digest only the live side has is not a difference."""
    if live.get('kind') != saved.get('kind'):
        return False
    if live.get('kind') != 'other':
        return live == saved
    if live.get('family') is None or saved.get('family') is None:
        return None
    if live['family'] != saved['family']:
        return False
    if live.get('meaning') is None or saved.get('meaning') is None:
        return None
    return live['meaning'] == saved['meaning']


def persisted_tables(path: Path, live: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    """The live tables against what the saved file holds: grid, spans, text, every line and every fill."""
    def unverified(reason: str) -> dict[str, Any]:
        return {'state': 'unverified', 'reason': reason, 'tables_checked': 0, 'mismatches': []}

    try:
        saved = {item['table_id']: item for item in package_tables(path)}
    except PackageUnreadable as exc:
        return unverified(exc.code)
    except (zipfile.BadZipFile, ET.ParseError, expat.ExpatError, zlib.error, KeyError, ValueError, TypeError, AttributeError, OSError, RuntimeError, NotImplementedError, EOFError, StopIteration):
        return unverified('package_tables_unreadable')
    mismatches: list[dict[str, Any]] = []
    known = {item['table_id'] for item in live}
    # A table in a header or footer the live side did not report is a scope it did not cover, by name. Anything else on one side only is a difference.
    unverified_tables = sorted(table_id for table_id, item in saved.items() if table_id not in known and item.get('scope', 'body') != 'body')
    if not truncated and known != set(saved) - set(unverified_tables):
        mismatches.append({'table_id': None, 'cell': None, 'field': 'tables', 'live': sorted(known), 'saved': sorted(saved)})
    limited = truncated or bool(unverified_tables)
    fills_not_compared = 0
    stored_size_differences: list[dict[str, Any]] = []
    stored_size_not_compared = 0
    for table in live:
        theirs = saved.get(table['table_id'])
        if theirs is None:
            continue
        if table.get('scope', 'body') != theirs['scope']:
            mismatches.append({'table_id': table['table_id'], 'cell': None, 'field': 'scope', 'live': table.get('scope', 'body'), 'saved': theirs['scope']})
        if table.get('width_hu') is not None and theirs.get('width_hu') is not None and table['width_hu'] != theirs['width_hu']:
            mismatches.append({'table_id': table['table_id'], 'cell': None, 'field': 'width_hu', 'live': table['width_hu'], 'saved': theirs['width_hu']})
        # The stored height follows layout. Measured equal at save; a difference is its own observation, apart from the table's content.
        if table.get('height_hu') is None or theirs.get('height_hu') is None:
            stored_size_not_compared += 1
        elif table['height_hu'] != theirs['height_hu']:
            stored_size_differences.append({'table_id': table['table_id'], 'field': 'height_hu', 'live': table['height_hu'], 'saved': theirs['height_hu']})
        if (table['rows'], table['cols']) != (theirs['rows'], theirs['cols']):
            mismatches.append({'table_id': table['table_id'], 'cell': None, 'field': 'grid', 'live': [table['rows'], table['cols']], 'saved': [theirs['rows'], theirs['cols']]})
            continue
        cells = {cell['address']: cell for cell in theirs['cells']}
        if {cell['address'] for cell in table['cells']} != set(cells):
            mismatches.append({'table_id': table['table_id'], 'cell': None, 'field': 'cells', 'live': [cell['address'] for cell in table['cells']], 'saved': sorted(cells)})
            continue
        for cell in table['cells']:
            other = cells[cell['address']]
            for field in ('row_span', 'col_span', 'width'):
                if cell[field] != other[field]:
                    mismatches.append({'table_id': table['table_id'], 'cell': cell['address'], 'field': field, 'live': cell[field], 'saved': other[field]})
            same_fill = _same_fill(cell['fill'], other['fill'])
            if same_fill is None:
                fills_not_compared += 1
            elif not same_fill:
                mismatches.append({'table_id': table['table_id'], 'cell': cell['address'], 'field': 'fill', 'live': cell['fill'], 'saved': other['fill']})
            for side in ('left', 'right', 'top', 'bottom'):
                if cell['border'][side] != other['border'][side]:
                    mismatches.append({'table_id': table['table_id'], 'cell': cell['address'], 'field': f'border.{side}', 'live': cell['border'][side], 'saved': other['border'][side]})
            shown = cell['text']
            # Only a row that says it was cut is compared by its start.
            if not (other['text'].startswith(shown) if cell.get('text_truncated') is True else other['text'] == shown):
                mismatches.append({'table_id': table['table_id'], 'cell': cell['address'], 'field': 'text', 'live': shown, 'saved': other['text'][:300]})
    state = 'mismatch' if mismatches else 'partial' if limited or fills_not_compared or stored_size_differences or stored_size_not_compared else 'match'
    return {'state': state, 'tables_checked': len(live), 'fills_not_compared': fills_not_compared, 'stored_size_differences': stored_size_differences[:_MAX_TABLE_ROWS], 'stored_size_not_compared': stored_size_not_compared,
            'mismatches': mismatches[:_MAX_TABLE_ROWS], **({'unverified_tables': unverified_tables} if unverified_tables else {}), **({'mismatch_total': len(mismatches)} if len(mismatches) > _MAX_TABLE_ROWS else {})}


def _labels_by_paragraph(paragraphs: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[tuple[str | None, bool]]:
    """(label, whether it was actually read from the print) per paragraph; a paragraph without numbering has no label to read."""
    pending = iter(rows)
    labels: list[tuple[str | None, bool]] = []
    for item in paragraphs:
        if item['heading'] == 'OUTLINE' and item.get('in_body'):
            row = next(pending, {})
            labels.append((row.get('rendered_label'), row.get('status') in {'match', 'mismatch'}))
        else:
            labels.append((None, item['heading'] == 'NONE'))
    return labels


def _numbering_kind(item: dict[str, Any]) -> str | None:
    """What a paragraph's numbering is when no printed label was read for it; None for a paragraph without numbering."""
    heading, level = item['heading'], item['level']
    return {'NUMBER': f'자동 번호 {level + 1}수준', 'BULLET': '글머리표', 'OUTLINE': f'개요 번호 {level + 1}수준'}.get(heading)


def list_numbering(paragraphs: list[dict[str, Any]] | None, *, truncated: bool = False) -> dict[str, Any]:
    """Numbered paragraphs whose printed label is not read: this review counts them and never calls them verified.

    'none' describes a whole document that was read. Unread structure has no counts; a truncated list counts only what it read.
    """
    if paragraphs is None:
        return {'state': 'unverified', 'number': None, 'bullet': None, 'nested_outline': None}
    counts = {'number': sum(item['heading'] == 'NUMBER' for item in paragraphs), 'bullet': sum(item['heading'] == 'BULLET' for item in paragraphs),
              'nested_outline': sum(item['heading'] == 'OUTLINE' and not item.get('in_body') for item in paragraphs)}
    return {'state': 'partial' if truncated else 'unverified' if any(counts.values()) else 'none', **counts}


def _unread_list_paragraphs(paragraphs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in paragraphs or [] if item['heading'] in {'NUMBER', 'BULLET'} or (item['heading'] == 'OUTLINE' and not item.get('in_body'))]


def _list_numbering_text(coverage: dict[str, Any]) -> str | None:
    """One clause naming each kind found, or None when none was found."""
    if coverage.get('state') not in {'unverified', 'partial'}:
        return None
    parts = [f'{label} {coverage[key]}개' for key, label in (('number', '자동 번호'), ('bullet', '글머리표'), ('nested_outline', '표 셀 안 개요 번호')) if coverage[key]]
    if not parts:
        return None
    return ('읽은 문단 중 ' if coverage['state'] == 'partial' else '') + ', '.join(parts)


def package_inventory(path: Path) -> dict[str, Any]:
    """Independent facts from the HWPX file itself, within fixed limits and with every member's checksum read."""
    bindata: dict[str, str] = {}
    texts: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PACKAGE_MEMBERS or sum(info.file_size for info in infos) > MAX_PACKAGE_TOTAL_BYTES:
                raise PackageUnreadable('package_limit', 'The HWPX package is larger than this inspection reads.')
            is_text = [re.fullmatch(r'Contents/((section\d+|header)\.xml|content\.hpf)', info.filename) is not None for info in infos]
            if sum(info.file_size for info, text in zip(infos, is_text) if text) > MAX_PACKAGE_TEXT_BYTES:
                raise PackageUnreadable('package_limit', 'The text of this HWPX package is larger than this inspection reads.')
            for info, text in zip(infos, is_text):
                kept = bytearray() if text else None
                digest = hashlib.sha256()
                with archive.open(info) as stream:
                    while chunk := stream.read(_READ_CHUNK):
                        digest.update(chunk)
                        if kept is not None:
                            kept.extend(chunk)
                if info.filename.startswith('BinData/'):
                    bindata[info.filename] = digest.hexdigest()
                if kept is not None:
                    texts[info.filename] = bytes(kept)
    except PackageUnreadable:
        raise
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError, OSError, ValueError) as exc:
        raise PackageUnreadable('package_corrupt', 'The file could not be read as an HWPX package.') from exc
    section_text = b''.join(data for name, data in sorted(texts.items()) if 'section' in name).decode('utf-8', 'replace')
    header = texts.get('Contents/header.xml')
    references = _bindata_references(section_text + (header or b'').decode('utf-8', 'replace'), (texts.get('Contents/content.hpf') or b'').decode('utf-8', 'replace'), bindata)
    return {
        'member_count': len(infos),
        'bindata': bindata,
        'references': references,
        'unreferenced_bindata': sorted(set(bindata.values()) - {value for value in references.values() if value}),
        'sections': sum('section' in name for name in texts),
        'tables': len(re.findall(r'<hp:tbl\b', section_text)),
        'pictures': len(re.findall(r'<hp:pic\b', section_text)),
        'headers': len(re.findall(r'<hp:header\b', section_text)),
        'footers': len(re.findall(r'<hp:footer\b', section_text)),
        'page_number_controls': len(re.findall(r'<hp:pageNum\b', section_text)),
        'outline_paragraph_shapes': None if header is None else len(re.findall(r'<hh:heading type="OUTLINE"', header.decode('utf-8', 'replace'))),
    }


def _bindata_references(text: str, manifest: str, bindata: dict[str, str]) -> dict[str, str | None]:
    """Each image id the document's parts refer to and the data it reaches: by the package manifest, or else by member name. None when it reaches nothing."""
    members = {name.casefold(): digest for name, digest in bindata.items()}
    by_stem = {Path(name).stem.casefold(): digest for name, digest in bindata.items()}
    hrefs: dict[str, str] = {}
    for item in re.findall(r'<(?:\w+:)?item\b[^>]*>', manifest):
        ident, href = re.search(r'\bid="([^"]+)"', item), re.search(r'\bhref="([^"]+)"', item)
        if ident and href:
            hrefs[ident.group(1)] = href.group(1)
    references: dict[str, str | None] = {}
    for ident in sorted(set(re.findall(r'binaryItemIDRef="([^"]+)"', text))):
        href = hrefs.get(ident)
        references[ident] = members.get(href.casefold()) if href and href.casefold() in members else by_stem.get(ident.casefold())
    return references


def _media_comparison(source: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    source_refs, saved_refs = source.get('references', {}), saved.get('references', {})
    kept = {value for value in saved_refs.values() if value}
    saved_payloads = set(saved['bindata'].values())
    return {
        'referenced_source': len(source_refs), 'referenced_saved': len(saved_refs),
        'referenced_lost': sum(1 for value in source_refs.values() if value and value not in kept),
        # Keep one identity per lost package reference, just like referenced_lost.
        # Distinct IDs can reach identical bytes; deduplicating only this list
        # hides an unrelated missing survivor behind a count mismatch.
        'referenced_lost_payloads': sorted(value for value in source_refs.values() if value and value not in kept),
        'unreferenced_lost': sum(1 for value in source.get('unreferenced_bindata', []) if value not in saved_payloads),
        'dangling_saved': sum(1 for value in saved_refs.values() if value is None),
        'all_bindata_identical': sorted(source['bindata'].values()) == sorted(saved['bindata'].values()),
    }


def compare_packages(source: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    return {
        # Hancom may rename a member on save (image1.png to image1.PNG); the payload is what must survive.
        'bindata_identical': sorted(source['bindata'].values()) == sorted(saved['bindata'].values()),
        'bindata_files': len(saved['bindata']),
        'media': _media_comparison(source, saved),
        **{key: {'source': source[key], 'saved': saved[key]} for key in ('tables', 'pictures', 'headers', 'footers', 'page_number_controls')},
    }


def _local(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1] if isinstance(element.tag, str) else ''


def _attribute(element: ET.Element, name: str) -> str | None:
    return next((value for key, value in element.attrib.items() if key.rsplit('}', 1)[-1] == name), None)


_HWPX_ALIGN = {'JUSTIFY': 'justify', 'LEFT': 'left', 'RIGHT': 'right', 'CENTER': 'center', 'DISTRIBUTE': 'distribute', 'DISTRIBUTE_SPACE': 'distribute_space'}
_HWPX_SPACING = {'PERCENT': 'percent', 'FIXED': 'fixed', 'BETWEEN_LINES': 'between_lines', 'BETWEENLINES': 'between_lines', 'AT_LEAST': 'at_least', 'ATLEAST': 'at_least'}
_HWPX_INLINE = {'tab': '\t', 'lineBreak': '\n', 'nbSpace': '\u00a0', 'fwSpace': '\u2007'}


def _hwpx_character(element: ET.Element, fonts: dict[tuple[str, str], str]) -> dict[str, Any]:
    faces: dict[str, str] = {}
    values: dict[str, Any] = {'bold': False, 'italic': False, 'underline': False}
    for child in element:
        name = _local(child)
        if name == 'fontRef':
            faces = {language: fonts[(language, ref)] for language, ref in child.attrib.items()}
        elif name in {'bold', 'italic'}:
            values[name] = True
        elif name == 'underline':
            kind = (_attribute(child, 'type') or 'NONE').upper()
            values['underline'] = kind != 'NONE'
            if kind not in {'NONE', 'BOTTOM'}:
                values['underline_type'] = kind.lower()
    distinct = set(faces.values())
    return {'font_family': next(iter(distinct)) if len(distinct) == 1 else faces, 'size_pt': int(_attribute(element, 'height') or 0) / 100, **values,
            'text_color': (_attribute(element, 'textColor') or '').upper() or None}


def _hwpx_paragraph(element: ET.Element) -> dict[str, Any]:
    """The branch Hancom itself marks in real HWPUNITs (1 pt = 100); the plain default branch holds double values for older readers."""
    holder = next((item for item in element.iter() if _local(item) == 'case'), None)
    if holder is None:
        raise ValueError('paragraph margins are not in the expected branch')
    numbers: dict[str, Any] = {'align': None, 'left_indent_pt': None, 'right_indent_pt': None, 'first_line_indent_pt': None, 'line_spacing_type': None, 'line_spacing_value': None}
    align = next((item for item in element if _local(item) == 'align'), None)
    if align is not None:
        numbers['align'] = _HWPX_ALIGN.get((_attribute(align, 'horizontal') or '').upper())
    for item in holder.iter():
        name = _local(item)
        if name in {'left', 'right', 'intent'}:
            key = {'left': 'left_indent_pt', 'right': 'right_indent_pt', 'intent': 'first_line_indent_pt'}[name]
            numbers[key] = int(_attribute(item, 'value') or 0) / 100
        elif name == 'lineSpacing':
            kind = _HWPX_SPACING.get((_attribute(item, 'type') or '').upper())
            value = int(_attribute(item, 'value') or 0)
            numbers.update(line_spacing_type=kind, line_spacing_value=value if kind == 'percent' else value / 100)
    return numbers


def _hwpx_rows(paragraph: ET.Element, characters: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    text = ''
    runs: list[dict[str, Any]] = []
    end: dict[str, Any] | None = None
    for run in paragraph:
        if _local(run) != 'run':
            continue
        values = characters[_attribute(run, 'charPrIDRef') or '']
        end = values
        shown = ''
        for item in run:
            if _local(item) != 't':
                continue
            shown += item.text or ''
            for inner in item:
                shown += _HWPX_INLINE.get(_local(inner), '') + (inner.tail or '')
        if shown:
            text += shown
            runs.append({'text': shown, 'length': len(shown), **values})
    return text, runs, end


def _parsed_part(raw: bytes) -> ET.Element:
    """One XML part in any encoding the parser supports; expat refuses a declaration when it meets one, before any entity exists."""
    def refuse(*_args: Any) -> None:
        raise ValueError('an XML part declares a DOCTYPE or ENTITY')

    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator='}')
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    parser.Parse(raw, True)
    return builder.close()


def package_format(path: Path) -> list[dict[str, Any]]:
    """Each paragraph's text and values, read from the HWPX file with no help from Hancom, in the live document's order."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos if re.fullmatch(r'Contents/(section\d+|header)\.xml', info.filename)) > MAX_PACKAGE_TEXT_BYTES:
            raise PackageUnreadable('package_limit', 'The text of this HWPX package is larger than this inspection reads.')
        names = sorted((info.filename for info in infos if re.fullmatch(r'Contents/section\d+\.xml', info.filename)), key=lambda name: int(re.search(r'\d+', name).group()))
        header, *sections = [_parsed_part(archive.read(name)) for name in ('Contents/header.xml', *names)]
    fonts: dict[tuple[str, str], str] = {}
    for face in header.iter():
        if _local(face) == 'fontface':
            language = (_attribute(face, 'lang') or '').lower()
            for font in face:
                if _local(font) == 'font':
                    fonts[(language, _attribute(font, 'id') or '')] = _attribute(font, 'face') or ''
    characters = {_attribute(item, 'id') or '': _hwpx_character(item, fonts) for item in header.iter() if _local(item) == 'charPr'}
    shapes = {_attribute(item, 'id') or '': _hwpx_paragraph(item) for item in header.iter() if _local(item) == 'paraPr'}
    headings = {_attribute(item, 'id') or '': next(((_attribute(head, 'type') or '').lower(), _attribute(head, 'idRef') or '', _attribute(head, 'level') or '0') for head in item if _local(head) == 'heading') for item in header.iter()
                if _local(item) == 'paraPr' and any(_local(head) == 'heading' for head in item)}
    definitions: dict[tuple[str, str], dict[str, str | None]] = {}
    for item in header.iter():
        if _local(item) in {'numbering', 'bullet'}:
            definitions[(_local(item), _attribute(item, 'id') or '')] = {_attribute(head, 'level') or '': _attribute(head, 'charPrIDRef') for head in item if _local(head) == 'paraHead'}
    rows: list[dict[str, Any]] = []

    def mark_of(shape_id: str, end: dict[str, Any] | None, outline: str | None) -> dict[str, Any] | None:
        kind, reference, level = headings.get(shape_id, ('none', '', '0'))
        if kind not in {'number', 'bullet', 'outline'}:
            return None
        heads = definitions.get(('bullet', reference)) if kind == 'bullet' else definitions.get(('numbering', outline if kind == 'outline' else reference))
        if heads is None:
            return {'kind': kind, 'source': 'unknown'}
        named = next(iter(heads.values()), None) if kind == 'bullet' else heads.get(str(int(level) + 1))
        if named is None and kind != 'bullet' and str(int(level) + 1) not in heads:
            return {'kind': kind, 'source': 'unknown'}
        if named is None or named == '4294967295':
            return {'kind': kind, 'source': 'paragraph_end', **(end or {})}
        return {'kind': kind, 'source': 'definition', **characters[named]}

    def visit(element: ET.Element, outline: str | None) -> None:
        for child in element:
            name = _local(child)
            if name in {'header', 'footer'}:
                continue
            if name == 'p':
                text, runs, end = _hwpx_rows(child, characters)
                shape_id = _attribute(child, 'paraPrIDRef') or ''
                rows.append({'text': text, 'format': {'runs': runs, 'paragraph': shapes[shape_id], 'mark': mark_of(shape_id, end, outline)}})
            visit(child, outline)

    for section in sections:
        visit(section, next((_attribute(item, 'outlineShapeIDRef') for item in section.iter() if _local(item) == 'secPr'), None))
    return rows


def persisted_format(path: Path, live: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    """The live document's values against what the saved file holds. Only text that lines up can be compared; the rest stays unverified."""
    def unverified(reason: str) -> dict[str, Any]:
        return {'state': 'unverified', 'reason': reason, 'paragraphs_checked': 0, 'mismatches': []}

    try:
        saved = package_format(path)
    except PackageUnreadable as exc:
        return unverified(exc.code)
    except (zipfile.BadZipFile, ET.ParseError, expat.ExpatError, zlib.error, KeyError, ValueError, TypeError, AttributeError, OSError, RuntimeError, NotImplementedError, EOFError):
        return unverified('package_format_unreadable')
    if len(live) > len(saved) or (not truncated and len(live) != len(saved)):
        return unverified('paragraph_count_differs')
    mismatches: list[dict[str, Any]] = []
    limited = truncated
    for index, (mine, theirs) in enumerate(zip(live, saved)):
        # The live list shows a bounded start of each paragraph's text.
        if not theirs['text'].startswith(mine['text']):
            return unverified('paragraph_text_differs')
        fact = mine.get('format')
        if not isinstance(fact, dict):
            return unverified('live_format_unavailable')
        limited = limited or fact.get('runs_truncated') is True
        for item in _range_changes(fact, theirs['format']):
            mismatches.append({'paragraph_index': index, **item})
        paragraph = _changed_fields(fact.get('paragraph') or {}, theirs['format']['paragraph'], _PARAGRAPH_FIELDS)
        if paragraph:
            mismatches.append({'paragraph_index': index, 'text': _clip(mine['text'], 60), 'changes': paragraph})
        mine_mark, their_mark = _mark_values(mine), theirs['format'].get('mark')
        if mine_mark is not None and isinstance(their_mark, dict) and their_mark.get('source') in {'paragraph_end', 'definition'}:
            mark = _changed_fields(mine_mark, their_mark, ('kind', 'source', *_CHARACTER_FIELDS))
            if mark:
                mismatches.append({'paragraph_index': index, 'text': '(번호 표시)', 'changes': mark})
        elif mine_mark is not None:
            limited = True
    state = 'mismatch' if mismatches else 'partial' if limited else 'match'
    return {'state': state, 'paragraphs_checked': len(live), 'mismatches': mismatches[:_MAX_FORMAT_ROWS], **({'mismatch_total': len(mismatches)} if len(mismatches) > _MAX_FORMAT_ROWS else {})}


def _format_word(name: str, value: Any) -> str:
    if value is None:
        return '확인 못함'
    if name in {'bold', 'italic', 'underline'}:
        return '예' if value else '아니요'
    if name == 'align':
        return _ALIGN_WORDS.get(value, str(value))
    if name == 'underline_type':
        return _UNDERLINE_WORDS.get(value, str(value))
    if name == 'line_spacing_type':
        return _SPACING_WORDS.get(value, str(value))
    if isinstance(value, float):
        text = f'{value:g}'
        return f'{text}pt' if name in {'size_pt', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt'} else text
    return str(value)


def _clip(text: str, width: int) -> str:
    """At most width characters, saying when some were left out."""
    return text[:width] + '…' if len(text) > width else text


def _range_name(item: dict[str, Any], width: int) -> str:
    """A changed range by its words; a range with none to show says what it is rather than being blank."""
    text = str(item.get('text') or '').replace('\n', ' ').replace('\t', ' ')
    length = item.get('length', len(text))
    if not text:
        return f'(앞부분 밖 글자 {length}개)'
    if not text.strip():
        return f'(공백 {len(text)}개)'
    return text[:width] + '…' if length > width or len(text) < length else text


def _paragraph_name(change: dict[str, Any], *, width: int = 30) -> str:
    """A paragraph a reader can find: its review index, and its words or what it holds instead."""
    index = change.get('paragraph_index')
    prefix = f'[{index}] ' if index is not None else ''
    text = ' '.join(str(change.get('text') or '').split())
    if text:
        return prefix + (text[:width] + '…' if len(text) > width else text)
    objects = change.get('objects')
    if objects is None:
        return prefix + '(글 없음, 내용 확인 못함)'
    if not objects:
        return prefix + '(빈 문단)'
    kinds = set(objects)
    word = _OBJECT_WORDS.get(next(iter(kinds))) if len(kinds) == 1 else None
    return prefix + f'({word or "개체"}만 있는 문단)'


def _change_clause(changes: dict[str, dict[str, Any]]) -> str:
    return ', '.join(f"{_FORMAT_LABELS[name]} {_format_word(name, item['before'])}→{_format_word(name, item['after'])}" for name, item in changes.items())


def _format_lines(comparison: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for change in comparison['changes']:
        if change['kind'] == 'number_mark_changed':
            lines.append(f"번호 모양 변경: {_paragraph_name(change)} — {_change_clause(change['changes'])}")
        if change['kind'] != 'format_changed':
            continue
        for item in change.get('ranges') or []:
            lines.append(f"서식 변경: {_paragraph_name(change)}의 \"{_range_name(item, 30)}\" — {_change_clause(item['changes'])}")
        if change.get('paragraph'):
            lines.append(f"문단 서식 변경: {_paragraph_name(change)} — {_change_clause(change['paragraph'])}")
        if change.get('other_properties'):
            lines.append(f"서식 변경: {_paragraph_name(change)} — 글꼴·크기·굵기·기울임·밑줄·색·정렬·여백·줄 간격이 아닌 다른 서식도 바뀌었습니다.")
    return lines


def _short_id(review_id: str | None, review: dict[str, Any]) -> str:
    """A review of this session by its short name; another session's keeps its full identity."""
    owner, _, name = str(review_id).partition('/')
    return name if owner == review.get('session_id') else f'이전 문서 {name}'


def _coverage_notes(review: dict[str, Any]) -> list[str]:
    coverage = review.get('coverage') or {}
    notes: list[str] = []
    paragraphs = coverage.get('paragraphs') or {}
    if paragraphs.get('state') == 'unverified':
        notes.append('문서 구조를 읽지 못했습니다')
    elif paragraphs.get('state') == 'partial':
        notes.append(f"문단 {paragraphs.get('total')}개 중 앞 {paragraphs.get('reviewed')}개만 읽었습니다")
    pages = coverage.get('pages') or {}
    if pages.get('state') == 'unverified':
        notes.append('쪽을 렌더링하지 못했습니다')
    elif pages.get('state') == 'partial':
        notes.append(f"{pages.get('total')}쪽 중 앞 {pages.get('rendered')}쪽만 렌더링했습니다")
    if (coverage.get('text') or {}).get('state') == 'unverified' and pages.get('state') != 'unverified':
        notes.append('PDF 글자를 읽지 못했습니다')
    if coverage.get('controls') == 'unverified':
        notes.append('컨트롤 목록을 끝까지 읽지 못했습니다')
    unread = _list_numbering_text(coverage.get('list_numbering') or {})
    if unread:
        notes.append(f'{unread}는 인쇄된 번호를 확인하지 않았습니다')
    marks = coverage.get('number_mark') or {}
    if marks.get('state') == 'not_compared':
        notes.append(f"번호 문단 {marks.get('not_compared')}개의 번호 모양을 비교하지 못했습니다" if marks.get('compared') or marks.get('new') else '번호 모양은 비교하지 않았습니다')
    elif marks.get('state') in _MARKS_NOT_ALL_COMPARED:
        notes.append(f"새 번호 문단 {marks.get('new')}개의 번호 모양은 이전 값이 없어 비교하지 못했습니다")
    layout = coverage.get('layout') or {}
    notes.extend(_layout_note(row) for row in (layout.get('overflow') or [])[:5])
    if len(layout.get('overflow') or []) > 5:
        notes.append(f"그 밖에 넘치는 표 {len(layout['overflow']) - 5}개")
    if layout.get('unverified'):
        notes.append('쪽 안에 드는지 확인하지 못한 표 ' + ', '.join(row['table_id'] for row in layout['unverified'][:6]) + (' 외' if len(layout['unverified']) > 6 else ''))
    tables = coverage.get('tables') or {}
    if tables.get('pages_unknown'):
        notes.append(f"쪽을 확인하지 못한 표 {tables['pages_unknown']}개")
    if tables.get('not_fully_named'):
        notes.append(f"이름 없는 값이 바뀐 표 {tables['not_fully_named']}개")
    media = coverage.get('package_media') or {}
    if media.get('state') == 'partial':
        notes.append(f"원본의 참조 없는 이미지 {media.get('unreferenced_lost')}개가 저장본에 없습니다")
    elif media.get('state') == 'unverified':
        notes.append(f"참조되던 그림 {media.get('referenced_lost')}개의 데이터가 저장본에 없는데, 어느 삭제로 빠졌는지 그림 데이터로 대조하지 못했습니다")
    elif media.get('state') == 'mismatch':
        notes.append('저장본의 그림 데이터가 원본의 참조와 맞지 않습니다')
    return notes


def _headline(review: dict[str, Any]) -> str | None:
    status = review.get('review_status')
    if status is None:
        return None
    notes = ', '.join(_coverage_notes(review))
    if status == 'complete':
        return '확인 결과: 전체 확인 완료.'
    if status == 'mismatch':
        return '확인 결과: 불일치가 있습니다.' + (f' 확인 범위도 제한됩니다: {notes}.' if notes else '')
    if status == 'partial':
        return f"확인 결과: 일부만 확인했습니다. {notes + '.' if notes else '일부 항목을 확인하지 못했습니다.'}"
    return f"확인 결과: 확인하지 못함. {notes + '.' if notes else ''}".rstrip()


def _row_status(row: dict[str, Any]) -> str:
    return row.get('status') or ('match' if row.get('matches') else 'mismatch')


def _persisted_gaps(fact: dict[str, Any]) -> str:
    gaps = []
    if fact.get('fills_not_compared'):
        gaps.append(f"채우기 {fact['fills_not_compared']}칸")
    if fact.get('unverified_tables'):
        gaps.append('머리말·꼬리말 표 ' + ', '.join(fact['unverified_tables'][:6]))
    if fact.get('stored_size_differences'):
        gaps.append('저장 높이가 다른 표 ' + ', '.join(item['table_id'] for item in fact['stored_size_differences'][:6]))
    if fact.get('stored_size_not_compared'):
        gaps.append(f"저장 크기를 비교하지 못한 표 {fact['stored_size_not_compared']}개")
    return f"({', '.join(gaps)})" if gaps else ''


def summarize(review: dict[str, Any]) -> str:
    """Coverage first: how much was actually seen, then whether what was seen is right, whether it changed and is saved."""
    rows = review['numbering']
    readable = review.get('rendered_text_available') is not False
    statuses = [_row_status(row) for row in rows]
    all_seen_match = rows and readable and all(status == 'match' for status in statuses)
    if 'numbering_all_match' in review:
        all_seen_match = review['numbering_all_match'] is True
    structure_read = (review.get('coverage') or {}).get('paragraphs', {}).get('state') != 'unverified'
    lists = (review.get('coverage') or {}).get('list_numbering') or {}
    first = (f"{review['page_count']}쪽, {'개요 ' if _list_numbering_text(lists) else ''}번호 문단 {len(rows)}개" if structure_read
             else f"{review['page_count']}쪽, 번호 문단은 확인하지 못함")
    if all_seen_match:
        first += ' — 렌더링된 번호가 모두 기대와 일치합니다.'
    elif rows and readable and statuses.count('match') and 'mismatch' not in statuses:
        unseen = len(rows) - statuses.count('match')
        first += f" — 확인한 {statuses.count('match')}개는 일치" + (f", {unseen}개는 확인하지 못했습니다." if unseen else '합니다.')
    lines = [line for line in (_headline(review), first) if line]
    if not readable:
        lines.append('PDF 글자를 읽을 수 없어 번호와 쪽 번호를 확인하지 못했습니다. 미리보기 이미지로 직접 확인하세요.')
    if review.get('pages_truncated'):
        lines.append(f"미리보기는 앞 {review['pages_rendered']}쪽까지만 만들었습니다.")
    wrong = [row for row, status in zip(rows, statuses) if status == 'mismatch'] if readable else []
    if wrong:
        lines.append(f'번호가 기대와 다른 문단 {len(wrong)}개: ' + ', '.join(f"{_clip(row['text'], 20)}({row['rendered_label']} / 기대 {row['expected_label']})" for row in wrong[:5]))
    pages = review['page_numbers']
    if pages.get('present') and readable:
        state = {True: '모든 쪽에서 확인됨', False: '일부 쪽에서 확인되지 않음', None: '렌더링한 쪽에서만 확인됨' if pages.get('state') == 'partial' else '확인하지 못함'}[pages.get('all_found')]
        position = f" ({pages.get('position')}, {pages.get('start_number')}부터)" if pages.get('start_number') else ''
        lines.append('쪽 번호: ' + state + position)
    comparison = review.get('comparison')
    mark_coverage = (review.get('coverage') or {}).get('number_mark') or {}
    mark_state = mark_coverage.get('state')
    if comparison:
        counts: dict[str, int] = {}
        for change in comparison['changes']:
            if change.get('in_table'):
                continue
            counts[change['kind']] = counts.get(change['kind'], 0) + 1
        names = {'added': '추가', 'removed': '삭제', 'number_changed': '번호 변경', 'format_changed': '서식 변경', 'number_mark_changed': '번호 모양 변경'}
        found = ', '.join(f'{names[key]} {value}건' for key, value in counts.items())
        label = _short_id(comparison['baseline'], review)
        state = comparison.get('state', 'complete')
        if state == 'unverified':
            lines.append(f'{label} 대비: 문단을 비교하지 못했습니다')
        elif state == 'partial' and (comparison.get('nested_paragraphs_identical') is None or comparison.get('numbers_not_compared')):
            lines.append(f"{label} 대비: 확인한 범위에서 {found or '바뀐 문단 없음'}")
        else:
            lines.append(f"{label} 대비: {found or '바뀐 문단 없음'}")
        if comparison.get('tables'):
            lines.extend(_table_lines(comparison['tables'])[:12])
        rows = _format_lines(comparison)
        lines.extend(rows[:8])
        if len(rows) > 8:
            lines.append(f"서식 변경 {len(rows) - 8}건 더: 전체는 review.json의 comparison.changes에 있고, summary.md에는 표로 {_MAX_FORMAT_ROWS}행까지 있습니다.")
        coverage_format = (review.get('coverage') or {}).get('format') or {}
        if coverage_format.get('state') in {'partial', 'unverified'}:
            lines.append('서식 변경 중 일부는 어떤 값이 바뀌었는지 확인하지 못했습니다.')
        mark_line = _mark_line(mark_coverage)
        if mark_line:
            lines.append(mark_line)
        if comparison.get('numbers_not_compared'):
            lines.append(f"번호 {comparison['numbers_not_compared']}개는 인쇄된 번호를 읽지 않았거나 읽지 못해 종류와 수준만 비교했습니다.")
        changed = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'changed']
        unproven = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'unproven']
        if changed:
            lines.append('보존 확인: 변경 감지 — ' + ', '.join(changed))
        if unproven:
            lines.append('보존 확인: 확인 못함 — ' + ', '.join(unproven))
        if not changed and not unproven:
            media_state = ((review.get('coverage') or {}).get('package_media') or {}).get('state')
            scope_word = '보존 확인(열린 문서 기준)' if media_state in {'partial', 'mismatch', 'unverified', 'removed_by_edit'} else '보존 확인'
            lines.append(f'{scope_word}: 머리말·꼬리말, 스타일, 그림 데이터, 번호 정의 동일' + ('(번호 모양은 이 확인에 들지 않습니다)' if mark_state in _MARKS_NOT_ALL_COMPARED else ''))
    table_state = (review.get('coverage') or {}).get('tables') or {}
    if table_state.get('state') == 'unverified':
        lines.append('표: 값을 읽지 못해 확인하지 못했습니다.')
    elif table_state.get('state') == 'partial':
        lines.append('표: 일부 값만 확인했습니다. 전체는 review.json에 있는 범위까지입니다.')
    media_line = _media_line((review.get('coverage') or {}).get('package_media') or {})
    if media_line:
        lines.append(media_line)
    persisted_tables_fact = review.get('persisted_tables')
    if isinstance(persisted_tables_fact, dict):
        lines.append({'match': f"저장본 표: 표 {persisted_tables_fact.get('tables_checked')}개의 격자·병합·글·선·채우기가 저장 파일과 일치합니다.",
                      'partial': f"저장본 표: 읽은 표 {persisted_tables_fact.get('tables_checked')}개는 일치합니다. 일부는 비교하지 못했습니다" + _persisted_gaps(persisted_tables_fact) + '.',
                      'mismatch': f"저장본 표: 저장 파일과 다른 곳이 {persisted_tables_fact.get('mismatch_total', len(persisted_tables_fact.get('mismatches') or []))}건 있습니다.",
                      'unverified': '저장본 표: 저장 파일과 비교하지 못했습니다.'}[persisted_tables_fact['state']])
    persisted = review.get('persisted_format')
    if isinstance(persisted, dict):
        lines.append({'match': f"저장본 서식: 문단 {persisted.get('paragraphs_checked')}개가 저장 파일의 값과 일치합니다.",
                      'partial': f"저장본 서식: 읽은 문단 {persisted.get('paragraphs_checked')}개는 일치합니다. 문서 일부는 비교하지 못했습니다.",
                      'mismatch': f"저장본 서식: 저장 파일의 값과 다른 곳이 {persisted.get('mismatch_total', len(persisted.get('mismatches') or []))}건 있습니다.",
                      'unverified': '저장본 서식: 저장 파일과 비교하지 못했습니다.'}[persisted['state']])
    saved = review.get('saved_output')
    if isinstance(saved, dict) and saved.get('retrieved') is False:
        lines.append('최신 저장본을 산출물 폴더로 복사하지 못했습니다. hwpx_save를 다시 호출하세요. 복사 전에는 닫기를 거부합니다.')
    lines.append('저장 안 된 변경 있음' if review['document_is_modified'] else '저장 안 된 변경 없음')
    return '\n'.join(lines)


def assemble(*, session_id: str, review_id: str, readback: dict[str, Any], pdf: dict[str, Any] | None, pages: list[dict[str, Any]],
             texts: list[str] | None, baseline: dict[str, Any] | None, saved_output: dict[str, Any] | None, render_error: str | None,
             persisted_format: dict[str, Any] | None = None, persisted_tables: dict[str, Any] | None = None) -> dict[str, Any]:
    """One review from the backend readback and the native render: what was covered, what it shows, what changed."""
    available = readback.get('available') is not False
    paragraphs = readback.get('paragraphs') if available else None
    truncated = readback.get('paragraphs_truncated') is True
    native_pages = readback.get('page_count') if type(readback.get('page_count')) is int else None
    pages_complete = bool(pages) and (native_pages is None or native_pages <= len(pages))
    readable = _has_text(texts)
    controls = readback.get('controls')
    current: dict[str, Any] = {
        'review_id': review_id, 'session_id': session_id, 'document': readback.get('source_filename'),
        'page_count': native_pages, 'pages_rendered': len(pages), 'pages_truncated': bool(pages) and not pages_complete,
        'document_is_modified': readback.get('document_is_modified') is True,
        'paragraphs': paragraphs, 'paragraphs_truncated': truncated,
        'tables': readback.get('tables'), 'tables_truncated': readback.get('tables_truncated') is True, 'picture_payloads': readback.get('picture_payloads'),
        'controls': controls, 'evidence': readback.get('evidence'),
        'caret_unchanged': readback.get('caret_unchanged'),
        'readback_reason': readback.get('reason'), 'render_error': render_error,
        'pdf': pdf, 'pages': pages, 'rendered_text_available': readable, 'saved_output': saved_output,
    }
    current['numbering'] = rendered_numbering(paragraphs or [], texts, pages_complete=pages_complete)
    current['page_numbers'] = rendered_page_numbers(readback, texts, total_pages=native_pages)
    paragraph_total = readback.get('paragraph_total')
    if paragraphs is None:
        paragraph_coverage = {'state': 'unverified', 'reviewed': 0, 'total': None}
    else:
        paragraph_coverage = {'state': 'partial' if truncated else 'complete', 'reviewed': len(paragraphs), 'total': paragraph_total if paragraph_total is not None else len(paragraphs)}
    page_coverage = {'state': 'unverified' if not pages else ('complete' if pages_complete else 'partial'), 'rendered': len(pages), 'total': native_pages}
    text_coverage = {'state': 'complete' if readable else 'unverified'}
    unread_lists = list_numbering(paragraphs, truncated=truncated)
    current['coverage'] = {'paragraphs': paragraph_coverage, 'pages': page_coverage, 'text': text_coverage, 'controls': 'unverified' if controls is None else 'complete', 'list_numbering': unread_lists}
    current['comparison'] = compare(baseline, current, current['numbering']) if baseline else None
    current['coverage']['format'] = format_coverage(current['comparison'])
    current['coverage']['number_mark'] = number_mark_coverage(paragraphs, current['comparison'])
    current['persisted_format'] = persisted_format
    current['persisted_tables'] = persisted_tables
    # A backend that does not report tables at all says nothing about them; one that reports them as unreadable lowers the review.
    current['coverage']['tables'] = (table_coverage(current['tables'], current['tables_truncated'], (current['comparison'] or {}).get('tables'), has_baseline=baseline is not None)
                                     if 'tables' in readback else {'state': 'not_reported', 'tables': None, 'compared': 0})
    current['coverage']['layout'] = layout_coverage(current['tables'])
    current['coverage']['package_media'] = package_media_coverage(saved_output, baseline, current)
    statuses = [row['status'] for row in current['numbering']]
    if 'unverified' in {paragraph_coverage['state'], page_coverage['state'], text_coverage['state']}:
        coverage_status = 'unverified'
    elif ('partial' in {paragraph_coverage['state'], page_coverage['state']} or controls is None or any(status != 'match' and status != 'mismatch' for status in statuses)
          or current['page_numbers'].get('state') in {'partial', 'unverified'} or (current['comparison'] or {}).get('state', 'complete') != 'complete'
          or unread_lists['state'] == 'unverified' or current['coverage']['format']['state'] != 'complete'
          or current['coverage']['number_mark']['state'] in _MARKS_NOT_ALL_COMPARED
          or current['coverage']['tables']['state'] in {'unverified', 'partial'}
          or current['coverage']['layout']['state'] in {'problem', 'unverified'}
          or current['coverage']['package_media']['state'] in {'partial', 'unverified'}
          or (persisted_tables or {}).get('state') in {'partial', 'unverified'}):
        coverage_status = 'partial'
    else:
        coverage_status = 'complete'
    current['coverage']['status'] = coverage_status
    mismatch = 'mismatch' in statuses or current['page_numbers'].get('state') == 'missing' or (persisted_format or {}).get('state') == 'mismatch' or (persisted_tables or {}).get('state') == 'mismatch' or current['coverage']['package_media']['state'] == 'mismatch'
    current['review_status'] = 'mismatch' if mismatch else coverage_status
    current['numbering_all_match'] = False if 'mismatch' in statuses else (True if statuses and coverage_status != 'unverified' and paragraph_coverage['state'] == 'complete' and page_coverage['state'] == 'complete' and unread_lists['state'] == 'none' and all(status == 'match' for status in statuses) else None)
    current['summary_text'] = summarize(current)
    return current


_ROW_WORDS = {'match': '예', 'mismatch': '아니요', 'outside_rendered_pages': '렌더링 범위 밖', 'unverified': '확인 못함'}


def _cell(text: Any) -> str:
    return str('' if text is None else text).replace('|', '\\|').replace('\n', ' ')


def _format_table_rows(comparison: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for change in comparison['changes']:
        name = _cell(_paragraph_name(change))
        word = lambda field, value: f"{_FORMAT_LABELS[field]} | {_format_word(field, value['before'])} | {_format_word(field, value['after'])} |"  # noqa: E731
        if change['kind'] == 'number_mark_changed':
            rows += [f'| {name} | 번호 표시 | {word(field, value)}' for field, value in change['changes'].items() if field in _FORMAT_LABELS]
        if change['kind'] != 'format_changed':
            continue
        rows += [f"| {name} | {_cell(_range_name(item, 60))} | {word(field, value)}" for item in change.get('ranges') or [] for field, value in item['changes'].items()]
        rows += [f'| {name} | 문단 전체 | {word(field, value)}' for field, value in (change.get('paragraph') or {}).items()]
        if change.get('other_properties'):
            rows.append(f'| {name} | 문단 안 | 이름 없는 다른 서식 | 확인 못함 | 바뀜 |')
    return rows


def to_markdown(review: dict[str, Any]) -> str:
    out = [f"# 문서 확인 요약: {review['document']}", '', review['summary_text'].replace('\n', '  \n'), '']
    coverage = review.get('coverage')
    if coverage:
        out += ['## 확인 범위', '', f"- 판정: {status_label(review['review_status'])}", f"- 문단: {coverage['paragraphs']['reviewed']}개 읽음 (전체 {coverage['paragraphs']['total'] if coverage['paragraphs']['total'] is not None else '알 수 없음'})",
                f"- 쪽: {coverage['pages']['rendered']}쪽 렌더링 (전체 {coverage['pages']['total'] if coverage['pages']['total'] is not None else '알 수 없음'})",
                f"- PDF 글자: {'읽음' if coverage['text']['state'] == 'complete' else '읽지 못함'}"]
        unread = _list_numbering_text(coverage.get('list_numbering') or {})
        if unread:
            out.append(f'- 자동 번호·글머리표: {unread}는 확인하지 않음')
        out.append('')
    if review['numbering']:
        out += ['## 번호 문단 (한글이 만든 PDF 기준)', '', '| 쪽 | 수준 | 화면 번호 | 기대 번호 | 일치 | 내용 |', '|---|---|---|---|---|---|']
        out += [f"| {_cell(row['page'])} | {row['level']} | {_cell(row['rendered_label'])} | {_cell(row['expected_label'])} | {_ROW_WORDS[_row_status(row)]} | {_cell(_clip(row['text'], 60))} |"
                for row in review['numbering']]
        out.append('')
    unread_rows = _unread_list_paragraphs(review.get('paragraphs'))
    if unread_rows:
        out += ['## 인쇄된 번호를 확인하지 않은 문단', '', '| 종류 | 내용 |', '|---|---|']
        out += [f"| {_numbering_kind(item)} | {_cell(_clip(item['text'], 60))} |" for item in unread_rows]
        out.append('')
    if review['page_numbers'].get('present'):
        out += ['## 쪽 번호', '']
        out += [f"- {page['page']}쪽: {page['expected']} — {'확인' if page['found'] else '찾지 못함'}" for page in review['page_numbers']['pages']]
        if not review['page_numbers']['pages']:
            out.append('- 확인하지 못함')
        out.append('')
    comparison = review.get('comparison')
    if comparison:
        out += [f"## 바뀐 내용 ({_short_id(comparison['baseline'], review)} 대비)", '']
        names = {'added': '추가', 'removed': '삭제', 'number_changed': '번호 변경', 'format_changed': '서식 변경', 'number_mark_changed': '번호 모양 변경'}
        for change in comparison['changes']:
            arrow = f" ({change.get('before') or '번호 없음'} → {change.get('after') or '번호 없음'})" if change['kind'] == 'number_changed' else (f" ({change['after']})" if change.get('after') else (f" ({change['before']})" if change.get('before') else ''))
            label = _paragraph_name(change, width=60) if change['kind'] in {'format_changed', 'number_mark_changed'} else _clip(change['text'], 60)
            if change.get('in_table'):
                label = f"표 안 문단 {label or '(빈 문단)'}"
            out.append(f"- {names[change['kind']]}: {label}{arrow}")
        if not comparison['changes']:
            out.append('- 바뀐 문단 없음')
        out += ['', '## 보존 확인', '']
        words = {'unchanged': '동일', 'changed': '변경 감지', 'unproven': '확인 못함'}
        out += [f"- {_EVIDENCE_LABELS[name]}: {words[state]}" for name, state in comparison['evidence'].items()]
        out += [f"- {_CONTROL_LABELS.get(name, name)}: {item['before']}개 → {item['after']}개" for name, item in (comparison['controls'] or {}).items() if name in _CONTROL_LABELS]
        if comparison['nested_paragraphs_identical'] is None:
            out.append('- 표 셀 등 본문 밖 문단: 전체를 비교하지 못함')
        else:
            nested = comparison.get('nested_format_changes')
            out.append(f"- 표 셀 등 본문 밖 문단 {comparison['nested_paragraph_count']}개: {'글 동일' if comparison['nested_paragraphs_identical'] else ('글 변경 감지(위 표 변경 참고)' if _tables_changed(comparison) else '글 변경 감지')}" + (f', 서식 변경 {nested}개' if nested else ''))
        out.append('')
    tables = (comparison or {}).get('tables')
    if tables:
        table_rows = _table_rows(tables)
        out += ['## 표 변경', '']
        if table_rows:
            if len(table_rows) > _MAX_TABLE_ROWS:
                out += [f'앞 {_MAX_TABLE_ROWS}행만 보여 줍니다. 전체 {len(table_rows)}행의 값은 `review.json`의 `comparison.tables`에 있습니다.', '']
            out += ['| 표 | 셀 | 항목 | 이전 | 이후 |', '|---|---|---|---|---|'] + table_rows[:_MAX_TABLE_ROWS] + ['']
        if tables['unchanged']:
            out += [f"- 변경 없는 표 {len(tables['unchanged'])}개: {', '.join(tables['unchanged'])}", '']
    table_state = (coverage or {}).get('tables') or {}
    if table_state.get('state') in {'unverified', 'partial'}:
        out += ['## 표 확인 범위', '', '- 표 값을 ' + ('읽지 못했습니다.' if table_state['state'] == 'unverified' else '일부만 읽었습니다.'), '']
    rows = _format_table_rows(comparison) if comparison else []
    if rows:
        out += ['## 서식 변경 상세', '']
        if len(rows) > _MAX_FORMAT_ROWS:
            out += [f'앞 {_MAX_FORMAT_ROWS}행만 보여 줍니다. 전체 {len(rows)}행은 `review.json`의 `comparison.changes`에 있습니다.', '']
        out += ['문단의 대괄호 안 숫자는 `review.json`의 문단 `index`입니다.', '', '| 문단 | 범위 | 항목 | 이전 | 이후 |', '|---|---|---|---|---|'] + rows[:_MAX_FORMAT_ROWS] + ['']
    mark_coverage = (coverage or {}).get('number_mark') or {}
    if mark_coverage.get('numbered'):
        words = {'compared': '비교했고 바뀌지 않음', 'changed': '비교했고 일부 바뀜', 'new_only': '새 번호라 이전 모양이 없어 비교하지 못함', 'mixed': '일부만 비교함, 새 번호는 이전 모양이 없음', 'not_compared': '일부 또는 전체를 비교하지 못함'}
        out += ['## 번호 모양', '', f"- 판정: {words[mark_coverage['state']]}",
                f"- 번호 문단 {mark_coverage['numbered']}개 중 비교 {mark_coverage['compared']}개, 바뀜 {mark_coverage['changed']}개, 새 번호 {mark_coverage['new']}개, 비교 못함 {mark_coverage['not_compared']}개",
                '- 서식 값으로 비교한 결과이며, 인쇄된 모양은 미리보기로 확인합니다.' if mark_coverage['compared'] else '- 인쇄된 모양은 미리보기로 확인합니다.',
                '- 번호 글자·종류·수준 확인과는 별개입니다.', '']
    layout = (coverage or {}).get('layout') or {}
    if layout.get('overflow') or layout.get('unverified'):
        out += ['## 표 너비', '']
        out += [f'- {_layout_note(row)}' for row in layout.get('overflow') or []]
        out += [f"- 표 {row['table_id']}: 쪽 안에 드는지 확인하지 못함 ({row.get('reason')})" for row in layout.get('unverified') or []]
        out.append('')
    media_line = _media_line((coverage or {}).get('package_media') or {})
    if media_line:
        out += ['## 저장 파일의 그림', '', f'- {media_line}', '']
    saved_tables = review.get('persisted_tables')
    if saved_tables:
        words = {'match': '일치', 'partial': '읽은 범위에서 일치', 'mismatch': '불일치', 'unverified': '비교하지 못함'}
        out += ['## 저장된 표 확인', '', f"- 판정: {words[saved_tables['state']]}", f"- 비교한 표: {saved_tables.get('tables_checked')}개 (격자, 병합, 글, 선, 채우기, 너비)"]
        if saved_tables.get('fills_not_compared'):
            out.append(f"- 비교하지 못한 채우기: {saved_tables['fills_not_compared']}칸")
        if saved_tables.get('unverified_tables'):
            out.append(f"- 비교하지 못한 머리말·꼬리말 표: {', '.join(saved_tables['unverified_tables'])}")
        for item in saved_tables.get('stored_size_differences') or []:
            out.append(f"- {item['table_id']}의 저장 높이: 실행 중 문서 {item['live']}, 저장 파일 {item['saved']} (내용 비교와 별도)")
        if saved_tables.get('stored_size_not_compared'):
            out.append(f"- 저장 크기를 비교하지 못한 표: {saved_tables['stored_size_not_compared']}개")
        if saved_tables.get('reason'):
            out.append(f"- 이유: {saved_tables['reason']}")
        if saved_tables.get('mismatches'):
            out += ['', '| 표 | 셀 | 항목 | 실행 중 문서 | 저장 파일 |', '|---|---|---|---|---|']
            out += [f"| {_cell(item.get('table_id'))} | {_cell(item.get('cell'))} | {_cell(item['field'])} | {_cell(item['live'])} | {_cell(item['saved'])} |" for item in saved_tables['mismatches'][:_MAX_TABLE_ROWS]]
        out.append('')
    persisted = review.get('persisted_format')
    if persisted:
        words = {'match': '일치', 'partial': '읽은 범위에서 일치', 'mismatch': '불일치', 'unverified': '비교하지 못함'}
        out += ['## 저장된 서식 확인', '', f"- 판정: {words[persisted['state']]}", f"- 비교한 문단: {persisted.get('paragraphs_checked')}개"]
        if persisted.get('path'):
            out.append(f"- 비교한 파일: {persisted['path']}")
        if persisted.get('reason'):
            out.append(f"- 이유: {persisted['reason']}")
        if persisted.get('mismatches'):
            if persisted.get('mismatch_total', 0) > len(persisted['mismatches']):
                out += [f"- 전체 {persisted['mismatch_total']}건 중 앞 {len(persisted['mismatches'])}건만 보여 줍니다."]
            out += ['', '| 문단 | 범위 | 항목 | 실행 중 문서 | 저장 파일 |', '|---|---|---|---|---|']
            out += [f"| [{item.get('paragraph_index')}] | {_cell(_range_name(item, 60))} | {_FORMAT_LABELS.get(name, name)} | {_format_word(name, value['before'])} | {_format_word(name, value['after'])} |"
                    for item in persisted['mismatches'] for name, value in item['changes'].items()]
        out.append('')
    saved = review.get('saved_output')
    out += ['## 파일', '']
    if review.get('pdf'):
        out.append(f"- PDF: {review['pdf']['path']}")
    out += [f"- {page['page']}쪽 미리보기: {page['path']}" for page in review['pages']]
    if saved and saved.get('path'):
        out.append(f"- 저장된 문서: {saved['path']}")
    elif saved:
        out.append(f"- 저장된 문서: 복사 실패 ({saved.get('code')}). hwpx_save를 다시 호출하세요.")
    out.append('')
    return '\n'.join(out)
