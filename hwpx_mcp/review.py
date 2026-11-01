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


_CHARACTER_FIELDS = ('font_family', 'size_pt', 'bold', 'italic', 'underline', 'text_color')
_PARAGRAPH_FIELDS = ('align', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt', 'line_spacing_type', 'line_spacing_value')
_FORMAT_LABELS = {
    'font_family': '글꼴', 'size_pt': '크기', 'bold': '굵게', 'italic': '기울임', 'underline': '밑줄', 'text_color': '글자색',
    'align': '정렬', 'left_indent_pt': '왼쪽 여백', 'right_indent_pt': '오른쪽 여백', 'first_line_indent_pt': '첫 줄', 'line_spacing_type': '줄 간격 종류', 'line_spacing_value': '줄 간격 값',
}
_ALIGN_WORDS = {'justify': '양쪽', 'left': '왼쪽', 'right': '오른쪽', 'center': '가운데', 'distribute': '배분', 'distribute_space': '나눔'}
_SPACING_WORDS = {'percent': '글자에 따라(%)', 'fixed': '고정 값(pt)', 'between_lines': '여백만 지정(pt)', 'at_least': '최소(pt)'}
_MAX_FORMAT_ROWS = 200


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


def _changed_fields(left: dict[str, Any], right: dict[str, Any], names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {name: {'before': left.get(name), 'after': right.get(name)} for name in names if left.get(name) != right.get(name)}


def _characters(fact: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """One entry per character of a run list; a run longer than its shown text still counts its length."""
    rows: list[tuple[str, dict[str, Any]]] = []
    for run in fact.get('runs') or []:
        shown = run.get('text') or ''
        rows.extend((shown[offset] if offset < len(shown) else '', run) for offset in range(int(run.get('length', len(shown)))))
    return rows


def _range_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """The ranges whose named character values differ, joined while the difference stays the same."""
    ranges: list[dict[str, Any]] = []
    for (_old_char, old_run), (char, new_run) in zip(_characters(before), _characters(after)):
        changes = _changed_fields(old_run, new_run, _CHARACTER_FIELDS)
        if changes and ranges and ranges[-1]['changes'] == changes and ranges[-1].pop('_open', False):
            ranges[-1]['text'] += char
            ranges[-1]['_open'] = True
        elif changes:
            ranges.append({'text': char, 'changes': changes, '_open': True})
        elif ranges:
            ranges[-1].pop('_open', None)
    for item in ranges:
        item.pop('_open', None)
    return ranges


def format_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What the paragraph's character and paragraph values say changed, in the units a caller uses."""
    old, new = before.get('format'), after.get('format')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {}
    ranges = _range_changes(old, new)
    paragraph = _changed_fields(old.get('paragraph') or {}, new.get('paragraph') or {}, _PARAGRAPH_FIELDS)
    detail: dict[str, Any] = {'ranges': ranges, 'paragraph': paragraph}
    if old.get('runs_truncated') or new.get('runs_truncated'):
        detail['truncated'] = True
    elif not ranges and not paragraph:
        detail['other_properties'] = True
    return detail


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


def compare(baseline: dict[str, Any], current: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Paragraph changes and preservation against an earlier review of the same document or its saved copy."""
    evidence = {name: _evidence_state(baseline.get('evidence'), current.get('evidence'), name) for name in _EVIDENCE_LABELS}
    before_controls, after_controls = baseline.get('controls'), current.get('controls')
    controls = None if before_controls is None or after_controls is None else {
        name: {'before': before_controls.get(name, 0), 'after': after_controls.get(name, 0)} for name in sorted(set(before_controls) | set(after_controls))}
    result = {'baseline': baseline.get('review_id'), 'changes': [], 'evidence': evidence, 'controls': controls, 'numbers_not_compared': 0,
              'nested_paragraphs_identical': None, 'nested_paragraph_count': None}
    states = {_paragraph_state(baseline), _paragraph_state(current)}
    if 'unverified' in states:
        return {**result, 'state': 'unverified'}
    before, after = baseline['paragraphs'], current['paragraphs']
    old_labels, new_labels = _labels_by_paragraph(before, baseline.get('numbering') or []), _labels_by_paragraph(after, rows)
    changes: list[dict[str, Any]] = []
    uncompared = 0
    matcher = difflib.SequenceMatcher(None, [item['text'] for item in before], [item['text'] for item in after], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            for left, right in zip(range(i1, i2), range(j1, j2)):
                (old_label, old_known), (new_label, new_known) = old_labels[left], new_labels[right]
                if old_known and new_known:
                    if old_label != new_label:
                        changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_label, 'after': new_label})
                elif ((before[left]['heading'], before[left]['level'], before[left].get('numbering_ref_sha256'))
                      != (after[right]['heading'], after[right]['level'], after[right].get('numbering_ref_sha256'))):
                    changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_label or _numbering_kind(before[left]), 'after': new_label or _numbering_kind(after[right])})
                else:
                    uncompared += 1
                if before[left]['content_sha256'] != after[right]['content_sha256']:
                    changes.append({'kind': 'format_changed', 'text': after[right]['text'], 'paragraph_index': after[right].get('index', right), **format_changes(before[left], after[right])})
            continue
        # What runs off the end of a truncated list says nothing about the document.
        if not (baseline.get('paragraphs_truncated') and i2 == len(before)):
            changes.extend({'kind': 'removed', 'text': before[index]['text'], 'before': old_labels[index][0] or _numbering_kind(before[index])} for index in range(i1, i2))
        if not (current.get('paragraphs_truncated') and j2 == len(after)):
            changes.extend({'kind': 'added', 'text': after[index]['text'], 'after': new_labels[index][0] or _numbering_kind(after[index])} for index in range(j1, j2))
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
    }


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
            is_text = [re.fullmatch(r'Contents/(section\d+|header)\.xml', info.filename) is not None for info in infos]
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
    return {
        'member_count': len(infos),
        'bindata': bindata,
        'sections': sum('section' in name for name in texts),
        'tables': len(re.findall(r'<hp:tbl\b', section_text)),
        'pictures': len(re.findall(r'<hp:pic\b', section_text)),
        'headers': len(re.findall(r'<hp:header\b', section_text)),
        'footers': len(re.findall(r'<hp:footer\b', section_text)),
        'page_number_controls': len(re.findall(r'<hp:pageNum\b', section_text)),
        'outline_paragraph_shapes': None if header is None else len(re.findall(r'<hh:heading type="OUTLINE"', header.decode('utf-8', 'replace'))),
    }


def compare_packages(source: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    return {
        # Hancom may rename a member on save (image1.png to image1.PNG); the payload is what must survive.
        'bindata_identical': sorted(source['bindata'].values()) == sorted(saved['bindata'].values()),
        'bindata_files': len(saved['bindata']),
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
            values['underline'] = (_attribute(child, 'type') or 'NONE').upper() != 'NONE'
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


def _hwpx_rows(paragraph: ET.Element, characters: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    text = ''
    runs: list[dict[str, Any]] = []
    for run in paragraph:
        if _local(run) != 'run':
            continue
        values = characters[_attribute(run, 'charPrIDRef') or '']
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
    return text, runs


def _parsed_part(raw: bytes) -> ET.Element:
    if re.search(rb'<!\s*(DOCTYPE|ENTITY)', raw, re.IGNORECASE):
        raise ValueError('an XML part declares a DOCTYPE or ENTITY')
    return ET.fromstring(raw)


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
    rows: list[dict[str, Any]] = []

    def visit(element: ET.Element) -> None:
        for child in element:
            name = _local(child)
            if name in {'header', 'footer'}:
                continue
            if name == 'p':
                text, runs = _hwpx_rows(child, characters)
                rows.append({'text': text, 'format': {'runs': runs, 'paragraph': shapes[_attribute(child, 'paraPrIDRef') or '']}})
            visit(child)

    for section in sections:
        visit(section)
    return rows


def persisted_format(path: Path, live: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    """The live document's values against what the saved file holds. Only text that lines up can be compared; the rest stays unverified."""
    def unverified(reason: str) -> dict[str, Any]:
        return {'state': 'unverified', 'reason': reason, 'paragraphs_checked': 0, 'mismatches': []}

    try:
        saved = package_format(path)
    except PackageUnreadable as exc:
        return unverified(exc.code)
    except (zipfile.BadZipFile, ET.ParseError, zlib.error, KeyError, ValueError, TypeError, AttributeError, OSError, RuntimeError, NotImplementedError, EOFError):
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
            mismatches.append({'paragraph_index': index, 'text': mine['text'][:60], 'changes': paragraph})
    state = 'mismatch' if mismatches else 'partial' if limited else 'match'
    return {'state': state, 'paragraphs_checked': len(live), 'mismatches': mismatches[:_MAX_FORMAT_ROWS]}


def _format_word(name: str, value: Any) -> str:
    if value is None:
        return '확인 못함'
    if name in {'bold', 'italic', 'underline'}:
        return '예' if value else '아니요'
    if name == 'align':
        return _ALIGN_WORDS.get(value, str(value))
    if name == 'line_spacing_type':
        return _SPACING_WORDS.get(value, str(value))
    if isinstance(value, float):
        text = f'{value:g}'
        return f'{text}pt' if name in {'size_pt', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt'} else text
    return str(value)


def _change_clause(changes: dict[str, dict[str, Any]]) -> str:
    return ', '.join(f"{_FORMAT_LABELS[name]} {_format_word(name, item['before'])}→{_format_word(name, item['after'])}" for name, item in changes.items())


def _format_lines(comparison: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for change in comparison['changes']:
        if change['kind'] != 'format_changed':
            continue
        for item in change.get('ranges') or []:
            lines.append(f"서식 변경: \"{item['text'][:30]}\" — {_change_clause(item['changes'])}")
        if change.get('paragraph'):
            lines.append(f"문단 서식 변경: \"{change['text'][:30]}\" — {_change_clause(change['paragraph'])}")
        if change.get('other_properties'):
            lines.append(f"서식 변경: \"{change['text'][:30]}\" — 글꼴·크기·굵기·기울임·밑줄·색·정렬·여백·줄 간격이 아닌 다른 서식이 바뀌었습니다.")
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
        lines.append(f'번호가 기대와 다른 문단 {len(wrong)}개: ' + ', '.join(f"{row['text'][:20]}({row['rendered_label']} / 기대 {row['expected_label']})" for row in wrong[:5]))
    pages = review['page_numbers']
    if pages.get('present') and readable:
        state = {True: '모든 쪽에서 확인됨', False: '일부 쪽에서 확인되지 않음', None: '렌더링한 쪽에서만 확인됨' if pages.get('state') == 'partial' else '확인하지 못함'}[pages.get('all_found')]
        position = f" ({pages.get('position')}, {pages.get('start_number')}부터)" if pages.get('start_number') else ''
        lines.append('쪽 번호: ' + state + position)
    comparison = review.get('comparison')
    if comparison:
        counts: dict[str, int] = {}
        for change in comparison['changes']:
            counts[change['kind']] = counts.get(change['kind'], 0) + 1
        names = {'added': '추가', 'removed': '삭제', 'number_changed': '번호 변경', 'format_changed': '서식 변경'}
        found = ', '.join(f'{names[key]} {value}건' for key, value in counts.items())
        label = _short_id(comparison['baseline'], review)
        state = comparison.get('state', 'complete')
        if state == 'unverified':
            lines.append(f'{label} 대비: 문단을 비교하지 못했습니다')
        elif state == 'partial' and (comparison.get('nested_paragraphs_identical') is None or comparison.get('numbers_not_compared')):
            lines.append(f"{label} 대비: 확인한 범위에서 {found or '바뀐 문단 없음'}")
        else:
            lines.append(f"{label} 대비: {found or '바뀐 문단 없음'}")
        rows = _format_lines(comparison)
        lines.extend(rows[:8])
        if len(rows) > 8:
            lines.append(f'서식 변경 {len(rows) - 8}건 더: review.json과 summary.md에 있습니다.')
        coverage_format = (review.get('coverage') or {}).get('format') or {}
        if coverage_format.get('state') in {'partial', 'unverified'}:
            lines.append('서식 변경 중 일부는 어떤 값이 바뀌었는지 확인하지 못했습니다.')
        if comparison.get('numbers_not_compared'):
            lines.append(f"번호 {comparison['numbers_not_compared']}개는 인쇄된 번호를 읽지 않았거나 읽지 못해 종류와 수준만 비교했습니다.")
        changed = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'changed']
        unproven = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'unproven']
        if changed:
            lines.append('보존 확인: 변경 감지 — ' + ', '.join(changed))
        if unproven:
            lines.append('보존 확인: 확인 못함 — ' + ', '.join(unproven))
        if not changed and not unproven:
            lines.append('보존 확인: 머리말·꼬리말, 스타일, 그림 데이터, 번호 정의 동일')
    persisted = review.get('persisted_format')
    if isinstance(persisted, dict):
        lines.append({'match': f"저장본 서식: 문단 {persisted.get('paragraphs_checked')}개가 저장 파일의 값과 일치합니다.",
                      'partial': f"저장본 서식: 읽은 문단 {persisted.get('paragraphs_checked')}개는 일치합니다. 문서 일부는 비교하지 못했습니다.",
                      'mismatch': f"저장본 서식: 저장 파일의 값과 다른 곳이 {len(persisted.get('mismatches') or [])}건 있습니다.",
                      'unverified': '저장본 서식: 저장 파일과 비교하지 못했습니다.'}[persisted['state']])
    saved = review.get('saved_output')
    if isinstance(saved, dict) and saved.get('retrieved') is False:
        lines.append('최신 저장본을 산출물 폴더로 복사하지 못했습니다. hwpx_save를 다시 호출하세요. 복사 전에는 닫기를 거부합니다.')
    lines.append('저장 안 된 변경 있음' if review['document_is_modified'] else '저장 안 된 변경 없음')
    return '\n'.join(lines)


def assemble(*, session_id: str, review_id: str, readback: dict[str, Any], pdf: dict[str, Any] | None, pages: list[dict[str, Any]],
             texts: list[str] | None, baseline: dict[str, Any] | None, saved_output: dict[str, Any] | None, render_error: str | None,
             persisted_format: dict[str, Any] | None = None) -> dict[str, Any]:
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
    current['persisted_format'] = persisted_format
    statuses = [row['status'] for row in current['numbering']]
    if 'unverified' in {paragraph_coverage['state'], page_coverage['state'], text_coverage['state']}:
        coverage_status = 'unverified'
    elif ('partial' in {paragraph_coverage['state'], page_coverage['state']} or controls is None or any(status != 'match' and status != 'mismatch' for status in statuses)
          or current['page_numbers'].get('state') in {'partial', 'unverified'} or (current['comparison'] or {}).get('state', 'complete') != 'complete'
          or unread_lists['state'] == 'unverified' or current['coverage']['format']['state'] != 'complete'):
        coverage_status = 'partial'
    else:
        coverage_status = 'complete'
    current['coverage']['status'] = coverage_status
    mismatch = 'mismatch' in statuses or current['page_numbers'].get('state') == 'missing' or (persisted_format or {}).get('state') == 'mismatch'
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
        if change['kind'] != 'format_changed':
            continue
        rows += [f"| {_cell(item['text'][:60])} | {_FORMAT_LABELS[name]} | {_format_word(name, value['before'])} | {_format_word(name, value['after'])} |"
                 for item in change.get('ranges') or [] for name, value in item['changes'].items()]
        rows += [f"| (문단 전체) | {_FORMAT_LABELS[name]} | {_format_word(name, value['before'])} | {_format_word(name, value['after'])} |" for name, value in (change.get('paragraph') or {}).items()]
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
        out += [f"| {_cell(row['page'])} | {row['level']} | {_cell(row['rendered_label'])} | {_cell(row['expected_label'])} | {_ROW_WORDS[_row_status(row)]} | {_cell(row['text'][:60])} |"
                for row in review['numbering']]
        out.append('')
    unread_rows = _unread_list_paragraphs(review.get('paragraphs'))
    if unread_rows:
        out += ['## 인쇄된 번호를 확인하지 않은 문단', '', '| 종류 | 내용 |', '|---|---|']
        out += [f"| {_numbering_kind(item)} | {_cell(item['text'][:60])} |" for item in unread_rows]
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
        names = {'added': '추가', 'removed': '삭제', 'number_changed': '번호 변경', 'format_changed': '서식 변경'}
        for change in comparison['changes']:
            arrow = f" ({change.get('before') or '번호 없음'} → {change.get('after') or '번호 없음'})" if change['kind'] == 'number_changed' else (f" ({change['after']})" if change.get('after') else (f" ({change['before']})" if change.get('before') else ''))
            out.append(f"- {names[change['kind']]}: {change['text'][:60]}{arrow}")
        if not comparison['changes']:
            out.append('- 바뀐 문단 없음')
        out += ['', '## 보존 확인', '']
        words = {'unchanged': '동일', 'changed': '변경 감지', 'unproven': '확인 못함'}
        out += [f"- {_EVIDENCE_LABELS[name]}: {words[state]}" for name, state in comparison['evidence'].items()]
        out += [f"- {_CONTROL_LABELS.get(name, name)}: {item['before']}개 → {item['after']}개" for name, item in (comparison['controls'] or {}).items() if name in _CONTROL_LABELS]
        if comparison['nested_paragraphs_identical'] is None:
            out.append('- 표 셀 등 본문 밖 문단: 전체를 비교하지 못함')
        else:
            out.append(f"- 표 셀 등 본문 밖 문단 {comparison['nested_paragraph_count']}개: {'내용 동일' if comparison['nested_paragraphs_identical'] else '변경 감지'}")
        out.append('')
    rows = _format_table_rows(comparison) if comparison else []
    if rows:
        out += ['## 서식 변경 상세', '', '| 범위 | 항목 | 이전 | 이후 |', '|---|---|---|---|'] + rows[:_MAX_FORMAT_ROWS] + ['']
    persisted = review.get('persisted_format')
    if persisted:
        words = {'match': '일치', 'partial': '읽은 범위에서 일치', 'mismatch': '불일치', 'unverified': '비교하지 못함'}
        out += ['## 저장된 서식 확인', '', f"- 판정: {words[persisted['state']]}", f"- 비교한 문단: {persisted.get('paragraphs_checked')}개"]
        if persisted.get('path'):
            out.append(f"- 비교한 파일: {persisted['path']}")
        if persisted.get('reason'):
            out.append(f"- 이유: {persisted['reason']}")
        if persisted.get('mismatches'):
            out += ['', '| 범위 | 항목 | 실행 중 문서 | 저장 파일 |', '|---|---|---|---|']
            out += [f"| {_cell(item.get('text'))} | {_FORMAT_LABELS[name]} | {_format_word(name, value['before'])} | {_format_word(name, value['after'])} |"
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
