from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

HP_NS = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HS_NS = 'http://www.hancom.co.kr/hwpml/2011/section'
NS = {'hp': HP_NS, 'hs': HS_NS}
SECTION_RE = re.compile(r'^Contents/section\d+\.xml$')
HEADING_RE = re.compile(r'^\s*(?:[0-9]+(?:\.[0-9]+)*[.)]?|[가-하][.)]|[A-Za-z][.)]|[Ⅰ-Ⅻ]+[.)]?|[①-⑳]|[■●•◦▪※□▶▷])\s+')


class LocalCliDocumentError(RuntimeError):
    pass


def _text_of(elem: ET.Element) -> str:
    parts: list[str] = []
    for node in elem.iter():
        if node.tag == f'{{{HP_NS}}}t' and node.text:
            parts.append(node.text)
    return ''.join(parts)


def _normalize_space(value: str) -> str:
    return ' '.join((value or '').split()).strip()


def _normalized_hash(value: str) -> str:
    normalized = _normalize_space(value).casefold()
    return 'sha256:' + hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ''):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _section_index(section_name: str) -> int | None:
    match = re.search(r'section(\d+)\.xml$', str(section_name or ''))
    if not match:
        return None
    return int(match.group(1))


def _column_name(col_1based: int) -> str:
    value = col_1based
    letters: list[str] = []
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters.append(chr(ord('A') + remainder))
    return ''.join(reversed(letters)) or '?'


def _cell_addr(row_1based: int | None, col_1based: int | None) -> str | None:
    if row_1based is None or col_1based is None or row_1based <= 0 or col_1based <= 0:
        return None
    return f'{_column_name(col_1based)}{row_1based}'


def _section_paths(zf: zipfile.ZipFile) -> list[str]:
    return sorted(name for name in zf.namelist() if SECTION_RE.match(name))


def _table_context_from_node(node: ET.Element, inherited: dict[str, Any] | None) -> dict[str, Any] | None:
    table = dict(inherited or {})
    if node.tag == f'{{{HP_NS}}}tbl':
        target_id = node.get('id') or node.get('instId') or node.get('ctrlid') or node.get('no')
        if target_id:
            table['target_id'] = str(target_id)
        if node.attrib:
            table['proof_hash'] = _normalized_hash(' '.join(f'{key}={value}' for key, value in sorted(node.attrib.items())))
    if node.tag == f'{{{HP_NS}}}tc':
        row = _as_int(node.get('rowAddr') or node.get('row'))
        col = _as_int(node.get('colAddr') or node.get('col'))
        if row is not None:
            table['row_1based'] = row + 1 if row >= 0 else row
        if col is not None:
            table['col_1based'] = col + 1 if col >= 0 else col
        row_span = _as_int(node.get('rowSpan'))
        col_span = _as_int(node.get('colSpan'))
        if row_span is not None:
            table.setdefault('row_span', row_span)
        if col_span is not None:
            table.setdefault('col_span', col_span)
    for child in node:
        if child.tag != f'{{{HP_NS}}}cellAddr':
            continue
        row = _as_int(child.get('rowAddr') or child.get('row'))
        col = _as_int(child.get('colAddr') or child.get('col'))
        if row is not None:
            table['row_1based'] = row + 1 if row >= 0 else row
        if col is not None:
            table['col_1based'] = col + 1 if col >= 0 else col
    addr = _cell_addr(_as_int(table.get('row_1based')), _as_int(table.get('col_1based')))
    if addr:
        table['cell_addr'] = addr
    return table or None


def _iter_paragraphs_with_context(root: ET.Element) -> list[tuple[ET.Element, dict[str, Any] | None]]:
    paragraphs: list[tuple[ET.Element, dict[str, Any] | None]] = []

    def walk(node: ET.Element, table_context: dict[str, Any] | None = None) -> None:
        current_table = table_context
        if node.tag in {f'{{{HP_NS}}}tbl', f'{{{HP_NS}}}tc'}:
            current_table = _table_context_from_node(node, table_context)
        if node.tag == f'{{{HP_NS}}}p':
            paragraphs.append((node, dict(current_table) if current_table else None))
        for child in node:
            walk(child, current_table)

    walk(root)
    return paragraphs


def load_paragraph_records(hwpx_path: Path) -> list[dict[str, Any]]:
    if not hwpx_path.exists() or not hwpx_path.is_file():
        raise LocalCliDocumentError(f'HWPX file not found: {hwpx_path}')

    paragraphs: list[dict[str, Any]] = []
    global_index = 0
    try:
        with zipfile.ZipFile(hwpx_path) as zf:
            section_paths = _section_paths(zf)
            if not section_paths:
                raise LocalCliDocumentError('No Contents/section*.xml entries found in the HWPX package.')
            for section_name in section_paths:
                root = ET.fromstring(zf.read(section_name))
                section_paragraph_index = 0
                for para, table_context in _iter_paragraphs_with_context(root):
                    text = _normalize_space(_text_of(para))
                    if not text:
                        continue
                    section_paragraph_index += 1
                    global_index += 1
                    paragraphs.append(
                        {
                            'global_index': global_index,
                            'section': section_name,
                            'section_paragraph_index': section_paragraph_index,
                            'text': text,
                            'inside_table': bool(table_context),
                            'table': table_context or None,
                        }
                    )
    except zipfile.BadZipFile as exc:
        raise LocalCliDocumentError(f'Invalid HWPX package: {hwpx_path}') from exc
    except ET.ParseError as exc:
        raise LocalCliDocumentError(f'Failed to parse XML inside HWPX package: {hwpx_path}') from exc
    return paragraphs


def load_plain_text_records(text: str, *, section: str = 'live-text') -> list[dict[str, Any]]:
    """Build lightweight paragraph records from Hancom's live text stream.

    The local CLI `find` path must not SaveAs a temporary HWPX, because Hancom can
    make that temporary file the active document. `GetTextFile(UNICODE, '')` gives
    us enough ordered text for navigation match lists without touching disk.
    """

    paragraphs: list[dict[str, Any]] = []
    global_index = 0
    section_paragraph_index = 0
    for raw_line in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = _normalize_space(raw_line)
        if not line:
            continue
        section_paragraph_index += 1
        global_index += 1
        paragraphs.append(
            {
                'global_index': global_index,
                'section': section,
                'section_paragraph_index': section_paragraph_index,
                'text': line,
                'inside_table': False,
                'table': None,
            }
        )
    return paragraphs


def _excerpt(text: str, query: str, *, radius: int = 44) -> str:
    normalized_text = _normalize_space(text)
    normalized_query = _normalize_space(query)
    if not normalized_query:
        return normalized_text[: radius * 2].strip()

    lower_text = normalized_text.casefold()
    lower_query = normalized_query.casefold()
    idx = lower_text.find(lower_query)
    if idx < 0:
        return normalized_text[: radius * 2].strip()

    start = max(0, idx - radius)
    end = min(len(normalized_text), idx + len(normalized_query) + radius)
    prefix = '…' if start > 0 else ''
    suffix = '…' if end < len(normalized_text) else ''
    return f'{prefix}{normalized_text[start:end]}{suffix}'.strip()


def _is_heading_record(paragraph: dict[str, Any]) -> bool:
    if paragraph.get('inside_table'):
        return False
    text = _normalize_space(str(paragraph.get('text') or ''))
    if not text or len(text) > 90:
        return False
    return bool(HEADING_RE.match(text))


def _nearby_headings(paragraphs: list[dict[str, Any]], index: int, *, limit: int = 3) -> list[str]:
    headings: list[str] = []
    for prior in reversed(paragraphs[max(0, index - 12) : index]):
        if not _is_heading_record(prior):
            continue
        heading = _normalize_space(str(prior.get('text') or ''))
        if heading and heading not in headings:
            headings.append(heading)
        if len(headings) >= limit:
            break
    return list(reversed(headings))


def _structured_context(paragraphs: list[dict[str, Any]], index: int, *, radius: int) -> dict[str, Any]:
    radius = max(0, int(radius))
    before_records = paragraphs[max(0, index - radius) : index]
    after_records = paragraphs[index + 1 : min(len(paragraphs), index + radius + 1)]
    current = paragraphs[index] if 0 <= index < len(paragraphs) else {}
    return {
        'around': radius,
        'before': [_normalize_space(str(item.get('text') or '')) for item in before_records if item.get('text')],
        'current': _normalize_space(str(current.get('text') or '')),
        'after': [_normalize_space(str(item.get('text') or '')) for item in after_records if item.get('text')],
    }


def _location_payload(paragraph: dict[str, Any]) -> dict[str, Any]:
    section = str(paragraph.get('section') or '')
    return {
        'section': section,
        'section_index': _section_index(section),
        'section_paragraph_index': paragraph.get('section_paragraph_index'),
        'global_paragraph_index': paragraph.get('global_index'),
        'page_candidate': paragraph.get('page_candidate') or paragraph.get('page'),
    }


def _table_payload(paragraph: dict[str, Any]) -> dict[str, Any]:
    raw_table = paragraph.get('table') if isinstance(paragraph.get('table'), dict) else {}
    table = dict(raw_table)
    for key in ('target_id', 'proof_hash', 'cell_addr', 'row_1based', 'col_1based', 'row_count', 'col_count'):
        if key in paragraph and key not in table:
            table[key] = paragraph[key]
    return table


def find_matches(
    paragraphs: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 50,
    around: int = 0,
    with_page: bool = False,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_space(query)
    if not normalized_query:
        raise LocalCliDocumentError('find text must not be empty')

    matches: list[dict[str, Any]] = []
    lowered_query = normalized_query.casefold()
    for paragraph_index, paragraph in enumerate(paragraphs):
        text = str(paragraph.get('text') or '')
        if lowered_query not in text.casefold():
            continue
        table = _table_payload(paragraph)
        inside_table = bool(paragraph.get('inside_table') or table)
        page_candidate = paragraph.get('page_candidate') or paragraph.get('page')
        warnings = list(paragraph.get('warnings') or []) if isinstance(paragraph.get('warnings'), list) else []
        if with_page and page_candidate in (None, ''):
            warnings.append('page candidate is approximate/unavailable for this find result; verify with rendered proof before mutation.')
        normalized_hash = _normalized_hash(text)
        location = _location_payload(paragraph)
        matches.append(
            {
                'schema_version': 'local-cli/find-match/v2',
                'section': paragraph['section'],
                'section_paragraph_index': paragraph['section_paragraph_index'],
                'global_paragraph_index': paragraph['global_index'],
                'location': location,
                'page_candidate': page_candidate,
                'text': text,
                'excerpt': _excerpt(text, normalized_query),
                'normalized_hash': normalized_hash,
                'identity': {
                    'normalized_hash': normalized_hash,
                    'global_paragraph_index': paragraph.get('global_index'),
                    'section': paragraph.get('section'),
                    'section_paragraph_index': paragraph.get('section_paragraph_index'),
                    'table_cell_addr': table.get('cell_addr'),
                },
                'inside_table': inside_table,
                'table': table,
                'context': _structured_context(paragraphs, paragraph_index, radius=around),
                'nearby_headings': _nearby_headings(paragraphs, paragraph_index),
                'warnings': warnings,
                'read_only': True,
                'selection_mutated': False,
            }
        )
        if len(matches) >= limit:
            break

    for idx, match in enumerate(matches, start=1):
        match['number'] = idx
        match['match_index'] = idx
    return matches


def resolve_match_target(raw_target: str, *, cached_matches: list[dict[str, Any]] | None) -> tuple[str, int | None]:
    target = _normalize_space(str(raw_target or ''))
    if not target:
        raise LocalCliDocumentError('target must not be empty')
    if target.isdigit():
        match_number = int(target)
        if match_number <= 0:
            raise LocalCliDocumentError('match number must be 1 or greater')
        return target, match_number
    return target, None


def build_context(paragraphs: list[dict[str, Any]], match: dict[str, Any], *, radius: int = 1) -> dict[str, Any]:
    paragraph_index = int(match['global_paragraph_index']) - 1
    start = max(0, paragraph_index - radius)
    end = min(len(paragraphs), paragraph_index + radius + 1)
    context_paragraphs = paragraphs[start:end]
    context_text = '\n'.join(item['text'] for item in context_paragraphs if item.get('text'))
    return {
        'number': match.get('number'),
        'match_index': match.get('match_index') or match.get('number'),
        'section': match.get('section'),
        'section_paragraph_index': match.get('section_paragraph_index'),
        'global_paragraph_index': match.get('global_paragraph_index'),
        'location': match.get('location'),
        'page_candidate': match.get('page_candidate'),
        'inside_table': match.get('inside_table'),
        'table': match.get('table'),
        'normalized_hash': match.get('normalized_hash'),
        'excerpt': match.get('excerpt'),
        'context': context_text,
        'structured_context': _structured_context(paragraphs, paragraph_index, radius=radius),
        'nearby_headings': _nearby_headings(paragraphs, paragraph_index),
        'warnings': match.get('warnings') or [],
        'read_only': True,
        'selection_mutated': False,
    }
