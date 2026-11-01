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
import zipfile
import zlib

MAX_PACKAGE_MEMBERS = 4096
MAX_PACKAGE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_TEXT_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
_HANGUL = '가나다라마바사아자차카타파하'
_POSITIONS = {1: '위 왼쪽', 2: '위 가운데', 3: '위 오른쪽', 4: '아래 왼쪽', 5: '아래 가운데', 6: '아래 오른쪽'}
_EVIDENCE_LABELS = {
    'header_footer': '머리말·꼬리말', 'styles_sha256': '스타일', 'media_sha256': '그림·바이너리 데이터',
    'numberings_sha256': '번호 정의', 'bindata_sha256': '바이너리 데이터 목록',
}
_CONTROL_LABELS = {'tbl': '표', 'gso': '그림·개체', 'head': '머리말', 'foot': '꼬리말', 'pgnp': '쪽 번호', 'nwno': '새 쪽 번호'}


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
        row = {'level': item['level'] + 1, 'text': item['text'], 'expected_label': next(expected), 'rendered_label': None, 'page': None}
        # The first printed line of a long paragraph can hold only the start of its text.
        for wanted in (whole[:24], whole[:10]) if whole else ():
            found = next((index for index in range(cursor, len(lines)) if wanted in lines[index][1]), None)
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
                elif (before[left]['heading'], before[left]['level']) != (after[right]['heading'], after[right]['level']):
                    changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_label, 'after': new_label})
                else:
                    uncompared += 1
                if before[left]['content_sha256'] != after[right]['content_sha256']:
                    changes.append({'kind': 'format_changed', 'text': after[right]['text']})
            continue
        # What runs off the end of a truncated list says nothing about the document.
        if not (baseline.get('paragraphs_truncated') and i2 == len(before)):
            changes.extend({'kind': 'removed', 'text': before[index]['text'], 'before': old_labels[index][0]} for index in range(i1, i2))
        if not (current.get('paragraphs_truncated') and j2 == len(after)):
            changes.extend({'kind': 'added', 'text': after[index]['text'], 'after': new_labels[index][0]} for index in range(j1, j2))
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
            labels.append((None, True))
    return labels


def package_inventory(path: Path) -> dict[str, Any]:
    """Independent facts from the HWPX file itself, within fixed limits and with every member's checksum read."""
    bindata: dict[str, str] = {}
    texts: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PACKAGE_MEMBERS or sum(info.file_size for info in infos) > MAX_PACKAGE_TOTAL_BYTES:
                raise PackageUnreadable('package_limit', 'The HWPX package is larger than this inspection reads.')
            for info in infos:
                kept = bytearray() if re.fullmatch(r'Contents/(section\d+|header)\.xml', info.filename) else None
                if kept is not None and info.file_size > MAX_PACKAGE_TEXT_BYTES:
                    raise PackageUnreadable('package_limit', 'An HWPX text member is larger than this inspection reads.')
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
    first = f"{review['page_count']}쪽, 번호 문단 {len(rows)}개" if structure_read else f"{review['page_count']}쪽, 번호 문단은 확인하지 못함"
    if all_seen_match:
        first += ' — 렌더링된 번호가 모두 기대와 일치합니다.'
    elif rows and readable and statuses.count('match') and 'mismatch' not in statuses:
        first += f" — 확인한 {statuses.count('match')}개는 일치, {len(rows) - statuses.count('match')}개는 확인하지 못했습니다."
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
        if comparison.get('numbers_not_compared'):
            lines.append(f"번호 {comparison['numbers_not_compared']}개는 인쇄된 번호를 읽지 못한 쪽이 있어 수준만 비교했습니다.")
        changed = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'changed']
        unproven = [_EVIDENCE_LABELS[name] for name, value in comparison['evidence'].items() if value == 'unproven']
        if changed:
            lines.append('보존 확인: 변경 감지 — ' + ', '.join(changed))
        if unproven:
            lines.append('보존 확인: 확인 못함 — ' + ', '.join(unproven))
        if not changed and not unproven:
            lines.append('보존 확인: 머리말·꼬리말, 스타일, 그림 데이터, 번호 정의 동일')
    saved = review.get('saved_output')
    if isinstance(saved, dict) and saved.get('retrieved') is False:
        lines.append('최신 저장본을 산출물 폴더로 복사하지 못했습니다. hwpx_save를 다시 호출하세요. 복사 전에는 닫기를 거부합니다.')
    lines.append('저장 안 된 변경 있음' if review['document_is_modified'] else '저장 안 된 변경 없음')
    return '\n'.join(lines)


def assemble(*, session_id: str, review_id: str, readback: dict[str, Any], pdf: dict[str, Any] | None, pages: list[dict[str, Any]],
             texts: list[str] | None, baseline: dict[str, Any] | None, saved_output: dict[str, Any] | None, render_error: str | None) -> dict[str, Any]:
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
    current['coverage'] = {'paragraphs': paragraph_coverage, 'pages': page_coverage, 'text': text_coverage, 'controls': 'unverified' if controls is None else 'complete'}
    current['comparison'] = compare(baseline, current, current['numbering']) if baseline else None
    statuses = [row['status'] for row in current['numbering']]
    if 'unverified' in {paragraph_coverage['state'], page_coverage['state'], text_coverage['state']}:
        coverage_status = 'unverified'
    elif ('partial' in {paragraph_coverage['state'], page_coverage['state']} or controls is None or any(status != 'match' and status != 'mismatch' for status in statuses)
          or current['page_numbers'].get('state') in {'partial', 'unverified'} or (current['comparison'] or {}).get('state', 'complete') != 'complete'):
        coverage_status = 'partial'
    else:
        coverage_status = 'complete'
    current['coverage']['status'] = coverage_status
    mismatch = 'mismatch' in statuses or current['page_numbers'].get('state') == 'missing'
    current['review_status'] = 'mismatch' if mismatch else coverage_status
    current['numbering_all_match'] = False if 'mismatch' in statuses else (True if statuses and coverage_status != 'unverified' and paragraph_coverage['state'] == 'complete' and page_coverage['state'] == 'complete' and all(status == 'match' for status in statuses) else None)
    current['summary_text'] = summarize(current)
    return current


_ROW_WORDS = {'match': '예', 'mismatch': '아니요', 'outside_rendered_pages': '렌더링 범위 밖', 'unverified': '확인 못함'}


def _cell(text: Any) -> str:
    return str('' if text is None else text).replace('|', '\\|').replace('\n', ' ')


def to_markdown(review: dict[str, Any]) -> str:
    out = [f"# 문서 확인 요약: {review['document']}", '', review['summary_text'].replace('\n', '  \n'), '']
    coverage = review.get('coverage')
    if coverage:
        out += ['## 확인 범위', '', f"- 판정: {status_label(review['review_status'])}", f"- 문단: {coverage['paragraphs']['reviewed']}개 읽음 (전체 {coverage['paragraphs']['total'] if coverage['paragraphs']['total'] is not None else '알 수 없음'})",
                f"- 쪽: {coverage['pages']['rendered']}쪽 렌더링 (전체 {coverage['pages']['total'] if coverage['pages']['total'] is not None else '알 수 없음'})",
                f"- PDF 글자: {'읽음' if coverage['text']['state'] == 'complete' else '읽지 못함'}", '']
    if review['numbering']:
        out += ['## 번호 문단 (한글이 만든 PDF 기준)', '', '| 쪽 | 수준 | 화면 번호 | 기대 번호 | 일치 | 내용 |', '|---|---|---|---|---|---|']
        out += [f"| {_cell(row['page'])} | {row['level']} | {_cell(row['rendered_label'])} | {_cell(row['expected_label'])} | {_ROW_WORDS[_row_status(row)]} | {_cell(row['text'][:60])} |"
                for row in review['numbering']]
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
