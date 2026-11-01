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

MAX_PACKAGE_MEMBERS = 4096
_HANGUL = '가나다라마바사아자차카타파하'
_POSITIONS = {1: '위 왼쪽', 2: '위 가운데', 3: '위 오른쪽', 4: '아래 왼쪽', 5: '아래 가운데', 6: '아래 오른쪽'}
_EVIDENCE_LABELS = {
    'header_footer': '머리말·꼬리말', 'styles_sha256': '스타일', 'media_sha256': '그림·바이너리 데이터',
    'numberings_sha256': '번호 정의', 'bindata_sha256': '바이너리 데이터 목록',
}
_CONTROL_LABELS = {'tbl': '표', 'gso': '그림·개체', 'head': '머리말', 'foot': '꼬리말', 'pgnp': '쪽 번호', 'nwno': '새 쪽 번호'}


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


def rendered_numbering(paragraphs: list[dict[str, Any]], page_texts: list[str]) -> list[dict[str, Any]]:
    """Find each numbered paragraph in the native PDF's text, in order, and read the label printed before it."""
    lines = [(number, _compact(line)) for number, text in enumerate(page_texts, start=1) for line in text.splitlines() if line.strip()]
    expected = iter(expected_labels(paragraphs))
    cursor = 0
    rows: list[dict[str, Any]] = []
    for item in paragraphs:
        if item['heading'] != 'OUTLINE' or not item.get('in_body'):
            continue
        wanted = _compact(item['text'])[:24]
        row = {'level': item['level'] + 1, 'text': item['text'], 'expected_label': next(expected), 'rendered_label': None, 'page': None}
        if wanted:
            for index in range(cursor, len(lines)):
                position = lines[index][1].find(wanted)
                if position >= 0:
                    row['rendered_label'] = lines[index][1][:position] or None
                    row['page'] = lines[index][0]
                    cursor = index + 1
                    break
        row['matches'] = row['expected_label'] is not None and row['rendered_label'] == row['expected_label']
        rows.append(row)
    return rows


def rendered_page_numbers(readback: dict[str, Any], page_texts: list[str]) -> dict[str, Any]:
    controls = readback.get('page_numbers') or []
    if not controls:
        return {'present': False, 'pages': []}
    control = controls[0]
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
    return {'present': True, 'position': _POSITIONS.get(control.get('draw_pos')), 'start_number': start, 'side_char': 'dash' if dash else 'none',
            'pages': pages, 'all_found': all(page['found'] for page in pages)}


def compare(baseline: dict[str, Any], current: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Paragraph changes and preservation against an earlier review of the same document or its saved copy."""
    before, after = baseline['paragraphs'], current['paragraphs']
    old_labels, new_labels = _labels_by_paragraph(before, baseline.get('numbering') or []), _labels_by_paragraph(after, rows)
    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, [item['text'] for item in before], [item['text'] for item in after], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            for left, right in zip(range(i1, i2), range(j1, j2)):
                if old_labels[left] != new_labels[right]:
                    changes.append({'kind': 'number_changed', 'text': after[right]['text'], 'before': old_labels[left], 'after': new_labels[right]})
                if before[left]['content_sha256'] != after[right]['content_sha256']:
                    changes.append({'kind': 'format_changed', 'text': after[right]['text']})
            continue
        changes.extend({'kind': 'removed', 'text': before[index]['text'], 'before': old_labels[index]} for index in range(i1, i2))
        changes.extend({'kind': 'added', 'text': after[index]['text'], 'after': new_labels[index]} for index in range(j1, j2))
    evidence = {name: 'unchanged' if baseline['evidence'].get(name) == current['evidence'].get(name) else 'changed' for name in _EVIDENCE_LABELS}
    controls = {name: {'before': baseline['controls'].get(name, 0), 'after': current['controls'].get(name, 0)}
                for name in sorted(set(baseline['controls']) | set(current['controls']))}
    cells_before = [item['text'] for item in before if not item.get('in_body')]
    cells_after = [item['text'] for item in after if not item.get('in_body')]
    return {
        'baseline': baseline.get('review_id'),
        'changes': changes,
        'evidence': evidence,
        'controls': controls,
        'nested_paragraphs_identical': cells_before == cells_after,
        'nested_paragraph_count': len(cells_after),
    }


def _labels_by_paragraph(paragraphs: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str | None]:
    labels = iter(row.get('rendered_label') for row in rows)
    return [next(labels, None) if item['heading'] == 'OUTLINE' and item.get('in_body') else None for item in paragraphs]


def package_inventory(path: Path) -> dict[str, Any]:
    """Independent facts from the HWPX file itself; no Hancom involved."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_MEMBERS:
            raise ValueError('HWPX package has too many members')
        sections = [info.filename for info in infos if re.fullmatch(r'Contents/section\d+\.xml', info.filename)]
        section_text = b''.join(archive.read(name) for name in sections).decode('utf-8', 'replace')
        return {
            'member_count': len(infos),
            'bindata': {info.filename: hashlib.sha256(archive.read(info.filename)).hexdigest() for info in infos if info.filename.startswith('BinData/')},
            'sections': len(sections),
            'tables': len(re.findall(r'<hp:tbl\b', section_text)),
            'pictures': len(re.findall(r'<hp:pic\b', section_text)),
            'headers': len(re.findall(r'<hp:header\b', section_text)),
            'footers': len(re.findall(r'<hp:footer\b', section_text)),
            'page_number_controls': len(re.findall(r'<hp:pageNum\b', section_text)),
            'outline_paragraph_shapes': _outline_shape_count(archive),
        }


def _outline_shape_count(archive: zipfile.ZipFile) -> int | None:
    try:
        return len(re.findall(r'<hh:heading type="OUTLINE"', archive.read('Contents/header.xml').decode('utf-8', 'replace')))
    except KeyError:
        return None


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


def summarize(review: dict[str, Any]) -> str:
    """Short answer first: is the numbering what was asked for, did anything else change, is it saved."""
    rows = review['numbering']
    readable = review.get('rendered_text_available') is not False
    lines = [f"{review['page_count']}쪽, 번호 문단 {len(rows)}개" + (' — 렌더링된 번호가 모두 기대와 일치합니다.' if readable and rows and all(row['matches'] for row in rows) else '')]
    if not readable:
        lines.append('PDF 글자를 읽을 수 없어 번호와 쪽 번호를 확인하지 못했습니다. 미리보기 이미지로 직접 확인하세요.')
    if review.get('pages_truncated'):
        lines.append(f"미리보기는 앞 {review['pages_rendered']}쪽까지만 만들었습니다.")
    wrong = [row for row in rows if not row['matches']] if readable else []
    if wrong:
        lines.append(f'번호가 기대와 다른 문단 {len(wrong)}개: ' + ', '.join(f"{row['text'][:20]}({row['rendered_label']} / 기대 {row['expected_label']})" for row in wrong[:5]))
    pages = review['page_numbers']
    if pages.get('present') and readable:
        lines.append('쪽 번호: ' + ('모든 쪽에서 확인됨' if pages['all_found'] else '일부 쪽에서 확인되지 않음') + f" ({pages['position']}, {pages['start_number']}부터)")
    comparison = review.get('comparison')
    if comparison:
        counts: dict[str, int] = {}
        for change in comparison['changes']:
            counts[change['kind']] = counts.get(change['kind'], 0) + 1
        names = {'added': '추가', 'removed': '삭제', 'number_changed': '번호 변경', 'format_changed': '서식 변경'}
        lines.append(f"{_short_id(comparison['baseline'], review)} 대비: " + (', '.join(f'{names[key]} {value}건' for key, value in counts.items()) or '바뀐 문단 없음'))
        changed = [_EVIDENCE_LABELS[name] for name, state in comparison['evidence'].items() if state != 'unchanged']
        lines.append('보존 확인: ' + ('변경 감지 — ' + ', '.join(changed) if changed else '머리말·꼬리말, 스타일, 그림 데이터, 번호 정의 동일'))
    lines.append('저장 안 된 변경 있음' if review['document_is_modified'] else '저장 안 된 변경 없음')
    return '\n'.join(lines)


def _cell(text: Any) -> str:
    return str('' if text is None else text).replace('|', '\\|').replace('\n', ' ')


def to_markdown(review: dict[str, Any]) -> str:
    out = [f"# 문서 확인 요약: {review['document']}", '', review['summary_text'].replace('\n', '  \n'), '']
    if review['numbering']:
        out += ['## 번호 문단 (한글이 만든 PDF 기준)', '', '| 쪽 | 수준 | 화면 번호 | 기대 번호 | 일치 | 내용 |', '|---|---|---|---|---|---|']
        out += [f"| {_cell(row['page'])} | {row['level']} | {_cell(row['rendered_label'])} | {_cell(row['expected_label'])} | {'예' if row['matches'] else '아니요'} | {_cell(row['text'][:60])} |"
                for row in review['numbering']]
        out.append('')
    if review['page_numbers'].get('present'):
        out += ['## 쪽 번호', '']
        out += [f"- {page['page']}쪽: {page['expected']} — {'확인' if page['found'] else '찾지 못함'}" for page in review['page_numbers']['pages']]
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
        out += [f"- {_EVIDENCE_LABELS[name]}: {'동일' if state == 'unchanged' else '변경 감지'}" for name, state in comparison['evidence'].items()]
        out += [f"- {_CONTROL_LABELS.get(name, name)}: {item['before']}개 → {item['after']}개" for name, item in comparison['controls'].items() if name in _CONTROL_LABELS]
        out += [f"- 표 셀 등 본문 밖 문단 {comparison['nested_paragraph_count']}개: {'내용 동일' if comparison['nested_paragraphs_identical'] else '변경 감지'}", '']
    saved = review.get('saved_output')
    out += ['## 파일', '', f"- PDF: {review['pdf']['path']}"]
    out += [f"- {page['page']}쪽 미리보기: {page['path']}" for page in review['pages']]
    if saved:
        out.append(f"- 저장된 문서: {saved['path']}")
    out.append('')
    return '\n'.join(out)
