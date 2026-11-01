"""Local, read-only HWPX document reading helpers (pure stdlib).

Everything here is *local static evidence* derived from the OWPML XML inside a
``.hwpx`` zip. It is not Hancom-rendered proof: there are no page numbers, no
layout, and no field/numbering evaluation.

``load_document(path)`` returns a plain dict; every other public function is a
pure function over that dict (or over the chunk list for ``search_chunks``).
"""

from __future__ import annotations

import html
import os
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .static_inspector import _local_name

EVIDENCE = 'local-static-hwpx'
EVIDENCE_NOTE = 'local static HWPX XML reading; not Hancom-rendered proof (no pagination/layout)'

MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_ENTRY_BYTES = 100 * 1024 * 1024

_SECTION_RE = re.compile(r'^Contents/section(\d+)\.xml$', re.IGNORECASE)
_STYLE_OUTLINE_RE = re.compile(r'^\s*(?:개요|outline)\s*(\d+)\s*$', re.IGNORECASE)
_NOTE_ELEMENTS = {'header': 'headers', 'footer': 'footers', 'footnote': 'footnotes', 'endnote': 'endnotes'}
_SENTENCE_END_RE = re.compile(r'(?<=[.!?。？！])\s+')
_QUOTED_RE = re.compile(r'"([^"]+)"')


class HwpxReadError(ValueError):
    """Raised when an input cannot be read as a bounded, well-formed HWPX package."""


# --------------------------------------------------------------------------- loading


def _check_entry_name(name: str) -> None:
    normalized = name.replace('\\', '/')
    if normalized.startswith('/') or re.match(r'^[A-Za-z]:', normalized):
        raise HwpxReadError(f'unsafe zip entry with absolute path: {name!r}')
    if '..' in PurePosixPath(normalized).parts:
        raise HwpxReadError(f'unsafe zip entry with parent traversal: {name!r}')


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        with zf.open(info) as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise HwpxReadError(f'cannot read zip entry {info.filename!r}: {exc}') from exc
    if len(data) > MAX_ENTRY_BYTES:
        raise HwpxReadError(f'zip entry too large: {info.filename!r} exceeds {MAX_ENTRY_BYTES} bytes')
    return data


def _parse(data: bytes, name: str) -> ET.Element:
    head = data[:4096].lower()
    if b'<!doctype' in head or b'<!entity' in data.lower():
        raise HwpxReadError(f'refusing XML with DOCTYPE/ENTITY declarations: {name!r}')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise HwpxReadError(f'malformed XML in {name!r}: {exc}') from exc


def _section_order(zf: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo]) -> list[str]:
    numeric = sorted(
        (int(match.group(1)), name) for name in infos if (match := _SECTION_RE.match(name))
    )
    fallback = [name for _, name in numeric]
    hpf = infos.get('Contents/content.hpf')
    if hpf is None:
        return fallback
    try:
        root = _parse(_read_entry(zf, hpf), hpf.filename)
    except HwpxReadError:
        return fallback
    hrefs: dict[str, str] = {}
    for elem in root.iter():
        if _local_name(elem.tag) == 'item' and elem.get('id') and elem.get('href'):
            href = elem.get('href', '')
            hrefs[elem.get('id', '')] = href if href.startswith('Contents/') else 'Contents/' + href
    ordered: list[str] = []
    for elem in root.iter():
        if _local_name(elem.tag) == 'itemref':
            href = hrefs.get(elem.get('idref', ''))
            if href and _SECTION_RE.match(href) and href in infos and href not in ordered:
                ordered.append(href)
    if not ordered:
        return fallback
    # Keep any section file the spine forgot, in numeric order, after the spine ones.
    return ordered + [name for name in fallback if name not in ordered]


def _header_outline_maps(root: ET.Element | None) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    para_levels: dict[str, int] = {}
    style_levels: dict[str, int] = {}
    style_names: dict[str, str] = {}
    if root is None:
        return para_levels, style_levels, style_names
    for elem in root.iter():
        name = _local_name(elem.tag)
        if name == 'paraPr' and elem.get('id') is not None:
            for child in elem.iter():
                if _local_name(child.tag) != 'heading':
                    continue
                if (child.get('type') or '').upper() != 'OUTLINE':
                    continue
                try:
                    level = int(child.get('level', '0'))
                except ValueError:
                    continue
                para_levels[elem.get('id', '')] = level + 1
                break
        elif name == 'style' and elem.get('id') is not None:
            style_id = elem.get('id', '')
            style_names[style_id] = elem.get('name') or elem.get('engName') or ''
            for candidate in (elem.get('name'), elem.get('engName')):
                match = _STYLE_OUTLINE_RE.match(candidate or '')
                if match:
                    style_levels[style_id] = max(1, int(match.group(1)))
                    break
    return para_levels, style_levels, style_names


def _t_text(elem: ET.Element) -> str:
    parts = [elem.text or '']
    for child in elem:
        name = _local_name(child.tag)
        if name == 'tab':
            parts.append('\t')
        elif name == 'lineBreak':
            parts.append('\n')
        else:
            parts.append(''.join(child.itertext()))
        parts.append(child.tail or '')
    return ''.join(parts)


class _Loader:
    def __init__(self, para_levels: dict[str, int], style_levels: dict[str, int]) -> None:
        self.para_levels = para_levels
        self.style_levels = style_levels
        self.paragraphs: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = []
        self.counts = {value: 0 for value in _NOTE_ELEMENTS.values()}
        self.counts['other_sublists'] = 0
        self.section_counter = 0

    def _collect(self, elem: ET.Element, parts: list[str], tables: list[ET.Element]) -> None:
        for child in elem:
            name = _local_name(child.tag)
            lowered = name.lower()
            if name == 't':
                parts.append(_t_text(child))
            elif name == 'tbl':
                tables.append(child)
            elif lowered in _NOTE_ELEMENTS:
                self.counts[_NOTE_ELEMENTS[lowered]] += 1
            elif name in {'subList', 'p'}:
                # Text boxes / drawing objects: not body flow text in this reader.
                self.counts['other_sublists'] += 1
            else:
                self._collect(child, parts, tables)

    def paragraph(self, p: ET.Element, section_index: int, table_path: list[dict[str, int]], top_level: bool) -> None:
        parts: list[str] = []
        nested: list[ET.Element] = []
        self._collect(p, parts, nested)
        text = ''.join(parts)
        para_pr = p.get('paraPrIDRef')
        style = p.get('styleIDRef')
        level: int | None = None
        if not table_path:
            if para_pr is not None and para_pr in self.para_levels:
                level = self.para_levels[para_pr]
            elif style is not None and style in self.style_levels:
                level = self.style_levels[style]
        inner = table_path[-1] if table_path else None
        record = {
            'global_index': len(self.paragraphs),
            'section_index': section_index,
            'section_paragraph_index': self.section_counter,
            'text': text,
            'para_pr_id': para_pr,
            'style_id': style,
            'outline_level': level,
            'in_table': bool(table_path),
            'table_index': inner['table_index'] if inner else None,
            'row': inner['row'] if inner else None,
            'col': inner['col'] if inner else None,
            'table_path': [dict(item) for item in table_path],
        }
        self.section_counter += 1
        self.paragraphs.append(record)
        if top_level:
            self.blocks.append({'type': 'paragraph', 'paragraph_index': record['global_index']})
        for tbl in nested:
            self.table(tbl, section_index, table_path, top_level)

    def table(self, tbl: ET.Element, section_index: int, table_path: list[dict[str, int]], top_level: bool) -> None:
        table_index = len(self.tables)
        record: dict[str, Any] = {
            'table_index': table_index,
            'section_index': section_index,
            'depth': len(table_path),
            'parent_table_index': table_path[-1]['table_index'] if table_path else None,
            'row_count': 0,
            'col_count': 0,
            'rows': [],
            'first_paragraph_index': None,
        }
        self.tables.append(record)
        if top_level:
            self.blocks.append({'type': 'table', 'table_index': table_index})
        cells: dict[tuple[int, int], list[int]] = {}
        row_pos = 0
        for tr in tbl:
            if _local_name(tr.tag) != 'tr':
                continue
            col_pos = 0
            for tc in tr:
                if _local_name(tc.tag) != 'tc':
                    continue
                row, col = row_pos, col_pos
                for child in tc:
                    if _local_name(child.tag) == 'cellAddr':
                        try:
                            row = int(child.get('rowAddr', row))
                            col = int(child.get('colAddr', col))
                        except ValueError:
                            pass
                        break
                cell_path = table_path + [{'table_index': table_index, 'row': row, 'col': col}]
                start = len(self.paragraphs)
                for child in tc:
                    if _local_name(child.tag) == 'subList':
                        for p in child:
                            if _local_name(p.tag) == 'p':
                                self.paragraph(p, section_index, cell_path, False)
                cells.setdefault((row, col), []).extend(range(start, len(self.paragraphs)))
                if record['first_paragraph_index'] is None and len(self.paragraphs) > start:
                    record['first_paragraph_index'] = start
                col_pos += 1
            row_pos += 1
        rows = max((r for r, _ in cells), default=-1) + 1
        cols = max((c for _, c in cells), default=-1) + 1
        grid = [['' for _ in range(cols)] for _ in range(rows)]
        for (r, c), indexes in cells.items():
            texts = [self.paragraphs[i]['text'] for i in indexes if self.paragraphs[i]['text'].strip()]
            grid[r][c] = '\n'.join(texts)
        record['row_count'] = rows
        record['col_count'] = cols
        record['rows'] = grid


def load_document(path: str | Path) -> dict[str, Any]:
    """Load a ``.hwpx`` file into a plain dict (read-only, bounded)."""
    file_path = Path(path).expanduser()
    try:
        size = os.stat(file_path).st_size
    except OSError as exc:
        raise HwpxReadError(f'cannot read file {str(file_path)!r}: {exc.strerror or exc}') from exc
    if not file_path.is_file():
        raise HwpxReadError(f'not a regular file: {str(file_path)!r}')
    if size > MAX_FILE_BYTES:
        raise HwpxReadError(f'file too large: {size} bytes exceeds {MAX_FILE_BYTES} bytes')
    try:
        zf = zipfile.ZipFile(file_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise HwpxReadError(f'not a readable HWPX zip package: {exc}') from exc
    with zf:
        infos: dict[str, zipfile.ZipInfo] = {}
        for info in zf.infolist():
            _check_entry_name(info.filename)
            if info.file_size > MAX_ENTRY_BYTES:
                raise HwpxReadError(f'zip entry too large: {info.filename!r} declares {info.file_size} bytes')
            infos[info.filename] = info
        section_names = _section_order(zf, infos)
        if not section_names:
            raise HwpxReadError('no Contents/sectionN.xml body sections found')
        header_info = infos.get('Contents/header.xml')
        header_root = _parse(_read_entry(zf, header_info), header_info.filename) if header_info else None
        para_levels, style_levels, style_names = _header_outline_maps(header_root)
        loader = _Loader(para_levels, style_levels)
        sections: list[dict[str, Any]] = []
        for section_index, name in enumerate(section_names):
            root = _parse(_read_entry(zf, infos[name]), name)
            loader.section_counter = 0
            first = len(loader.paragraphs)
            for child in root:
                if _local_name(child.tag) == 'p':
                    loader.paragraph(child, section_index, [], True)
            sections.append({
                'section_index': section_index,
                'entry': name,
                'paragraph_start': first,
                'paragraph_count': len(loader.paragraphs) - first,
            })
    offset = 0
    for index, para in enumerate(loader.paragraphs):
        if index:
            offset += 1  # '\n' separator
        para['char_start'] = offset
        offset += len(para['text'])
        para['char_end'] = offset
    return {
        'evidence': EVIDENCE,
        'path': str(file_path),
        'file_name': file_path.name,
        'sections': sections,
        'paragraphs': loader.paragraphs,
        'tables': loader.tables,
        'blocks': loader.blocks,
        'text': '\n'.join(para['text'] for para in loader.paragraphs),
        'control_counts': loader.counts,
        'style_names': style_names,
    }


# --------------------------------------------------------------------------- outline / index


def outline(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Table of contents: body headings (outside tables) with 1-based level."""
    items = []
    for para in doc['paragraphs']:
        level = para.get('outline_level')
        if level is None or para['in_table'] or not para['text'].strip():
            continue
        items.append({
            'level': level,
            'text': re.sub(r'\s+', ' ', para['text']).strip(),
            'paragraph_index': para['global_index'],
            'section_index': para['section_index'],
            'char_start': para['char_start'],
        })
    return items


def _heading_paths(doc: dict[str, Any]) -> list[list[str]]:
    paths: list[list[str]] = []
    stack: list[tuple[int, str]] = []
    for para in doc['paragraphs']:
        level = para.get('outline_level')
        if level is not None and not para['in_table'] and para['text'].strip():
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, re.sub(r'\s+', ' ', para['text']).strip()))
        paths.append([text for _, text in stack])
    return paths


def position_index(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-paragraph position record with offsets into ``doc['text']``."""
    rows = []
    for para in doc['paragraphs']:
        rows.append({
            'global_index': para['global_index'],
            'section_index': para['section_index'],
            'section_paragraph_index': para['section_paragraph_index'],
            'char_start': para['char_start'],
            'char_end': para['char_end'],
            'in_table': para['in_table'],
            'table_index': para['table_index'],
            'row': para['row'],
            'col': para['col'],
            'outline_level': para['outline_level'],
            'text_length': len(para['text']),
        })
    return rows


# --------------------------------------------------------------------------- chunking / search


def _split_long(start: int, text: str, max_chars: int) -> list[tuple[int, int]]:
    """Split one oversized paragraph into (start, end) spans, preferring sentence then whitespace."""
    spans: list[tuple[int, int]] = []
    pos = 0
    length = len(text)
    while length - pos > max_chars:
        window_end = pos + max_chars
        window = text[pos:window_end]
        cut = -1
        for match in _SENTENCE_END_RE.finditer(window):
            if match.start() > 0:
                cut = match.start()
        if cut <= 0:
            ws = max(window.rfind(' '), window.rfind('\n'), window.rfind('\t'))
            cut = ws if ws > 0 else max_chars
        spans.append((start + pos, start + pos + cut))
        pos += cut
        while pos < length and text[pos].isspace():
            pos += 1
    if pos < length:
        spans.append((start + pos, start + length))
    return spans


def chunk_document(doc: dict[str, Any], max_chars: int = 2000, overlap_paragraphs: int = 1) -> list[dict[str, Any]]:
    """Split the body into ordered chunks without cutting paragraphs (unless one is oversized)."""
    if max_chars < 1:
        raise ValueError('max_chars must be >= 1')
    if overlap_paragraphs < 0:
        raise ValueError('overlap_paragraphs must be >= 0')
    full = doc['text']
    units: list[tuple[int, int, int]] = []  # (paragraph_index, char_start, char_end)
    for para in doc['paragraphs']:
        if not para['text'].strip():
            continue
        if len(para['text']) <= max_chars:
            units.append((para['global_index'], para['char_start'], para['char_end']))
        else:
            for s, e in _split_long(para['char_start'], para['text'], max_chars):
                units.append((para['global_index'], s, e))
    heading_paths = _heading_paths(doc)
    chunks: list[dict[str, Any]] = []
    i = 0
    carry: list[int] = []
    while i < len(units):
        members = list(carry)
        # Drop overlap units from the front until the next new unit fits.
        while members and units[i][2] - units[members[0]][1] > max_chars:
            members.pop(0)
        overlap_count = len(members)
        members.append(i)
        i += 1
        while i < len(units) and units[i][2] - units[members[0]][1] <= max_chars:
            members.append(i)
            i += 1
        first, last = units[members[0]], units[members[-1]]
        chunks.append({
            'id': f'c{len(chunks) + 1:04d}',
            'paragraph_start': first[0],
            'paragraph_end': last[0],
            'char_start': first[1],
            'char_end': last[2],
            'text': full[first[1]:last[2]],
            'heading_path': heading_paths[first[0]] if heading_paths else [],
            'overlap_units': overlap_count,
        })
        # Overlap is counted in units (whole paragraphs, or pieces of an oversized one).
        # Forward progress is guaranteed because each chunk consumes at least one new unit.
        carry = members[-overlap_paragraphs:] if overlap_paragraphs else []
    return chunks


def _parse_query(query: str) -> list[str]:
    phrases = [phrase.strip().lower() for phrase in _QUOTED_RE.findall(query) if phrase.strip()]
    rest = _QUOTED_RE.sub(' ', query).replace('"', ' ')
    terms = [term.lower() for term in rest.split() if term]
    return phrases + terms


def search_chunks(chunks: list[dict[str, Any]], query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Case-insensitive AND search over chunks; quoted text is an exact phrase."""
    needles = _parse_query(query)
    if not needles or limit < 1:
        return []
    hits = []
    for order, chunk in enumerate(chunks):
        lowered = chunk['text'].lower()
        if not all(needle in lowered for needle in needles):
            continue
        score = sum(lowered.count(needle) for needle in needles)
        first_hit = min(lowered.find(needle) for needle in needles)
        start = max(0, first_hit - 60)
        end = min(len(chunk['text']), first_hit + 100)
        snippet = re.sub(r'\s+', ' ', chunk['text'][start:end]).strip()
        if start > 0:
            snippet = '…' + snippet
        if end < len(chunk['text']):
            snippet += '…'
        hits.append((-score, order, {
            'id': chunk['id'],
            'score': score,
            'snippet': snippet,
            'paragraph_start': chunk['paragraph_start'],
            'paragraph_end': chunk['paragraph_end'],
            'char_start': chunk['char_start'],
            'hit_char_offset': chunk['char_start'] + first_hit,
            'heading_path': chunk['heading_path'],
        }))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in hits[:limit]]


# --------------------------------------------------------------------------- export / stats


def _cell_inline(text: str) -> str:
    return text.replace('\t', ' ').replace('\n', ' ')


def export_text(doc: dict[str, Any]) -> str:
    """Plain text: one line per body paragraph; tables as tab-separated rows."""
    lines: list[str] = []
    for block in doc['blocks']:
        if block['type'] == 'paragraph':
            lines.append(doc['paragraphs'][block['paragraph_index']]['text'])
        else:
            for row in doc['tables'][block['table_index']]['rows']:
                lines.append('\t'.join(_cell_inline(cell) for cell in row))
    return '\n'.join(lines) + ('\n' if lines else '')


def _html_text(text: str) -> str:
    return html.escape(text, quote=True).replace('\n', '<br>')


def export_html(doc: dict[str, Any]) -> str:
    """Self-contained escaped HTML (no scripts); headings by outline level."""
    toc = outline(doc)
    title = toc[0]['text'] if toc else doc.get('file_name') or 'HWPX document'
    body: list[str] = []
    for block in doc['blocks']:
        if block['type'] == 'paragraph':
            para = doc['paragraphs'][block['paragraph_index']]
            if not para['text'].strip():
                continue
            level = para.get('outline_level')
            tag = f'h{min(max(level, 1), 6)}' if level else 'p'
            body.append(f'<{tag}>{_html_text(para["text"])}</{tag}>')
        else:
            table = doc['tables'][block['table_index']]
            rows = ''.join(
                '<tr>' + ''.join(f'<td>{_html_text(cell)}</td>' for cell in row) + '</tr>'
                for row in table['rows']
            )
            body.append(f'<table>{rows}</table>')
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="generator" content="hwpx_reader {EVIDENCE}">\n'
        f'<title>{html.escape(title, quote=True)}</title>\n'
        '<style>body{font-family:sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.6}'
        'table{border-collapse:collapse;margin:1rem 0}td{border:1px solid #999;padding:.25rem .5rem;vertical-align:top}'
        'p{white-space:pre-wrap}</style>\n</head>\n<body>\n'
        f'<!-- evidence: {EVIDENCE}; not Hancom-rendered proof -->\n'
        + '\n'.join(body)
        + '\n</body>\n</html>\n'
    )


def word_count(doc: dict[str, Any]) -> dict[str, Any]:
    """Character/word/structure counts over body paragraphs (headers/footers/notes excluded)."""
    texts = [para['text'] for para in doc['paragraphs']]
    joined = ''.join(texts)
    return {
        'characters_with_spaces': len(joined),
        'characters_without_spaces': sum(1 for ch in joined if not ch.isspace()),
        'words': sum(len(text.split()) for text in texts),
        'paragraphs': len(texts),
        'non_empty_paragraphs': sum(1 for text in texts if text.strip()),
        'table_paragraphs': sum(1 for para in doc['paragraphs'] if para['in_table']),
        'tables': len(doc['tables']),
        'sections': len(doc['sections']),
        'headings': len(outline(doc)),
        'excluded_controls': dict(doc.get('control_counts', {})),
    }
