"""Reading and editing one exact table, with a model of every table and paragraph in the live document as its proof.

Native facts these rely on were measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2 on disposable synthetic documents.

A cell block grows from its first cell with TableCellBlock, TableCellBlockExtend and then TableRightCell and TableLowerCell, which go
to the cell in the next slot. A cell the block touches is taken whole. SetPos ends a block. get_selected_range names exactly the cells
of the block, covered slots excepted, so it is the proof of a selection.

A sparse CellBorder set changes only its items. On a block BorderType* are the outer edges of the block and TypeHorz and TypeVert its
inner lines. The side of a cell outside the block that faces an edge is written too, but only when that cell's whole side lies on the
edge; a merged neighbour wider than the edge keeps its value. NoNeighborCell=1 leaves the neighbour alone, and the PDF then does not
always show the line, so it is not offered. The left colour item is spelled BorderCorlorLeft, as the official table prints it, and
BorderColorLeft is ignored. A line of type 0 keeps its stored width and colour. Colours are 0x00BBGGRR. Black is left out of HWPML2X.

CellFill changes the fill alone only through the GetDefault set: a sparse set returns True and changes nothing. Type 1 with a face
colour is a fill, white included, and type 0 removes the fill altogether.

TableInsertRowColumn with Side 0 left, 1 right, 2 above, 3 below makes Count lines in one action and one Undo; a new line repeats the
merged shape and the border and fill of the line it was made from. Past the table's edge the new line takes the frame, and the old
line's side there takes the value of its other side. The per-side insert actions ignore Count. TableDeleteRow and
TableDeleteColumn work on the caret's line and do nothing on a block; TableDeleteCell on a block of whole lines removes them in one
action. A line through a merged cell shortens that cell and keeps its content, and a line holding the merged cell's first slot leaves
it where it was. A deleted line at the table's edge hands that edge's values to the cells that become the edge. A table's last line
cannot be deleted.

TableMergeCell joins the paragraphs of its cells in reading order with their styles. A cell holding one blank paragraph adds nothing.
The merged cell takes the first cell's border and fill, and a grid line no cell starts on any more disappears, so a merge can reduce
the table's rows or columns. TableSplitCell leaves all content in the first part. It divides width equally, so unmerging columns of
different widths adds a grid line rather than restoring the old ones. A merged cell split into as many rows as it spans keeps the row
lines. Into another number of rows the result follows rendered heights, which this code cannot predict, so that is refused.

Cell heights in HWPML2X follow layout and change when a document is rendered, so a table's hash leaves them out and no check rests on
them. Undo of an insert through a merged cell leaves that cell's stored width or height different; the history reports this.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from app import native_format as nf
from app.native_numbering import (
    ControlEnumerationUnproven,
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _action_confirmed,
    _control_ids,
    _multiset_delta,
    _reject_unknown_keys,
    _require_confirm_layout,
    _status_text,
)
from app.native_numbering_census import (
    CensusError,
    MAX_HWPML_CHARS,
    _DOCTYPE,
    _MAX_DEPTH,
    _Resolver,
    _Unresolved,
    _attr,
    _lower_attrs,
    _name,
)
from app.native_paragraph_edit import _COMPARED, _exception_class_label, evidence_of
from app.table_structure import cell_address, parse_cell_address

MAX_LINES = 50
MAX_SPLIT = 20
MAX_GRID = 500
MAX_REPORT_ROWS = 200
MAX_READ_CELLS = 2000
READ_TEXT = 2000
REVIEW_TEXT = 300
READ_BYTES = 2_500_000
REVIEW_BYTES = 1_400_000
HATCH_DEFAULT = 0x999999
NO_COLOR = 4294967295
_TABLE_ID = re.compile(r'tbl:([0-9]{1,20})')
_HASH = re.compile(r'sha256:[a-f0-9]{24}')
_COLOR = re.compile(r'#[0-9A-Fa-f]{6}')
_ADDRESS = re.compile(r'[A-Z]{1,3}[1-9][0-9]{0,3}')

LINE_TYPES = {
    'none': (0, 'None'), 'solid': (1, 'Solid'), 'dash': (2, 'Dash'), 'dot': (3, 'Dot'), 'dash_dot': (4, 'DashDot'), 'dash_dot_dot': (5, 'DashDotDot'),
    'long_dash': (6, 'LongDash'), 'circle': (7, 'Circle'), 'double': (8, 'DoubleSlim'), 'slim_thick': (9, 'SlimThick'), 'thick_slim': (10, 'ThickSlim'), 'slim_thick_slim': (11, 'SlimThickSlim'),
}
_LINE_PUBLIC = {stored: public for public, (_number, stored) in LINE_TYPES.items()}
LINE_WIDTHS = (0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
SIDES = ('left', 'right', 'top', 'bottom')
_OPPOSITE = {'left': 'right', 'right': 'left', 'top': 'bottom', 'bottom': 'top'}
_ITEM_SIDE = {'left': 'Left', 'right': 'Right', 'top': 'Top', 'bottom': 'Bottom'}
_EDGE_GROUPS = {
    'top': ('top',), 'bottom': ('bottom',), 'left': ('left',), 'right': ('right',),
    'outline': SIDES, 'inner_horizontal': ('inner_horizontal',), 'inner_vertical': ('inner_vertical',),
    'inner': ('inner_horizontal', 'inner_vertical'), 'all': (*SIDES, 'inner_horizontal', 'inner_vertical'),
}
ROW_ACTIONS = {'insert_row_above': 2, 'insert_row_below': 3}
COL_ACTIONS = {'insert_col_left': 0, 'insert_col_right': 1}
EDIT_ACTIONS = frozenset({*ROW_ACTIONS, *COL_ACTIONS, 'delete_row', 'delete_col', 'merge_cells', 'split_cell', 'unmerge_cell'})
_BOUND_FIELDS = ('table_id', 'expected_hash', 'expected_page', 'expected_rows', 'expected_cols')
_EDIT_FIELDS = ('action', 'row', 'col', 'count', 'cell', 'end_cell', 'split_rows', 'split_cols', 'expected_removed_cells')
_STYLE_FIELDS = ('scope', 'cell', 'end_cell', 'borders', 'fill')
_CREATE_FIELDS = ('match', 'expected_page', 'rows', 'cols', 'cells')
_CELL_VOLATILE = frozenset({'rowaddr', 'coladdr', 'rowspan', 'colspan', 'width', 'height', 'dirty', 'borderfill'})
_LIST_VOLATILE = frozenset({'linklistid', 'linklistidnext'})
RENDER_PROOF_WARNING = 'Render every affected page and review the saved file before accepting this edit.'


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=list).encode('utf-8', 'surrogatepass')).hexdigest()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _color_text(value: Any) -> str:
    number = _int(value, 0)
    return f'#{number & 0xFF:02X}{number >> 8 & 0xFF:02X}{number >> 16 & 0xFF:02X}'


def native_color(color: str) -> int:
    return nf._hex_to_native(color)  # noqa: SLF001


def _width_mm(value: Any) -> float | None:
    text = str(value or '').replace('mm', '').strip()
    try:
        return float(text)
    except ValueError:
        return None


# The model.

def _border_facts(element: ET.Element, resolver: _Resolver) -> dict[str, Any]:
    border: dict[str, Any] = {}
    fill: dict[str, Any] = {'kind': 'none'}
    rest: list[Any] = sorted((key, value) for key, value in _lower_attrs(element).items() if key != 'id')
    for child in element:
        name = _name(child)
        side = name[:-6] if name.endswith('border') else None
        if side in SIDES:
            kind = _attr(child, 'type') or ''
            border[side] = {'line': _LINE_PUBLIC.get(kind, kind.lower()), 'width_mm': _width_mm(_attr(child, 'width')), 'color': _color_text(_attr(child, 'color'))}
        elif name == 'fillbrush':
            brushes = list(child)
            if len(brushes) == 1 and _name(brushes[0]) == 'windowbrush' and (_attr(brushes[0], 'hatchstyle') or '-1').strip() in {'-1', ''}:
                face = _int(_attr(brushes[0], 'facecolor'))
                fill = {'kind': 'none'} if face == NO_COLOR else {'kind': 'solid', 'color': _color_text(face)}
            else:
                fill = {'kind': 'other', 'digest': _digest([resolver.digest(item) for item in brushes])[:16]}
        else:
            rest.append(resolver.digest(child))
    missing = [side for side in SIDES if side not in border]
    if missing:
        raise nf._ModelUnusable('a border definition does not name every side')  # noqa: SLF001
    return {'border': border, 'fill': fill, 'rest': _digest(rest)[:16]}


def _table_inst(table: ET.Element) -> str | None:
    for child in table:
        if _name(child) == 'shapeobject':
            value = _attr(child, 'instid')
            return value.strip() if value else None
    return None


def _shape_id(props: Any, cache: dict[int, str]) -> str:
    key = id(props)
    if key not in cache:
        cache[key] = _digest(props)[:16]
    return cache[key]


def _paragraph_facts(paragraph: ET.Element, chars: Mapping[str, Any], paras: Mapping[str, Any], resolver: _Resolver, cache: dict[int, str]) -> dict[str, Any]:
    ref = _attr(paragraph, 'parashape')
    shape = paras.get(ref.strip()) if ref is not None else None
    if shape is None:
        raise nf._ModelUnusable('a paragraph has no resolvable paragraph shape')  # noqa: SLF001
    atoms = nf._atoms(paragraph, chars, resolver)  # noqa: SLF001
    tables = iter([child for run in paragraph if _name(run) == 'text' for child in _text_objects(run) if _name(child) == 'table'])
    rows: list[Any] = []
    for atom in atoms:
        if 'char' in atom:
            rows.append(['c', atom['char'], _shape_id(atom['shape'], cache)])
        elif atom['control'] == 'table':
            element = next(tables, None)
            rows.append(['t', _table_inst(element) if element is not None else None, _shape_id(atom['shape'], cache)])
        else:
            rows.append(['o', atom['control'], atom.get('digest'), atom.get('data'), _shape_id(atom['shape'], cache)])
    end = next((chars.get((_attr(run, 'charshape') or '').strip()) for run in reversed(list(paragraph)) if _name(run) == 'text'), None)
    other_attrs = sorted((key, value) for key, value in _lower_attrs(paragraph).items() if key not in {'parashape', 'style', 'instid'})
    style = _attr(paragraph, 'style')
    try:
        style_digest = resolver.definition('stylelist', style) if style is not None else None
    except _Unresolved as exc:
        raise nf._ModelUnusable('a paragraph has no resolvable style') from exc  # noqa: SLF001
    return {
        'text': ''.join(atom['char'] for atom in atoms if 'char' in atom), 'atoms': atoms, 'shape': shape,
        'objects': [atom['control'] for atom in atoms if 'control' in atom and atom['control'] not in nf._JOINING_CONTROLS],  # noqa: SLF001
        'digest': _digest([rows, _shape_id(shape, cache), _shape_id(end, cache) if end is not None else None, other_attrs, style_digest]),
    }


def _text_objects(run: ET.Element) -> list[ET.Element]:
    items: list[ET.Element] = []
    for child in run:
        if _name(child) == 'char':
            items.extend(inner for inner in child if _name(inner) not in nf._INLINE_CHARACTERS)  # noqa: SLF001
        else:
            items.append(child)
    return items


def _frame_digest(table: ET.Element, resolver: _Resolver) -> str:
    tokens: list[Any] = sorted((key, value) for key, value in _lower_attrs(table).items() if key not in {'rowcount', 'colcount', 'borderfill'})
    border = _attr(table, 'borderfill')
    try:
        tokens.append(resolver.definition('borderfilllist', border) if border is not None else None)
        for child in table:
            name = _name(child)
            if name == 'row':
                continue
            if name == 'shapeobject':
                tokens.append(sorted(_lower_attrs(child).items()))
                tokens.extend(resolver.digest(item, prune=True) for item in child if _name(item) != 'size')
            else:
                tokens.append(resolver.digest(child, prune=True))
    except _Unresolved as exc:
        raise nf._ModelUnusable('a table has an unresolved definition') from exc  # noqa: SLF001
    return _digest(tokens)[:16]


def parse_table_model(text: str) -> dict[str, Any]:
    """Every table and paragraph of the body, in document order; a nested table's content belongs to that table, not to the cell holding it."""
    if type(text) is not str or len(text) > MAX_HWPML_CHARS or _DOCTYPE.search(text):
        raise nf._ModelUnusable('HWPML2X text is not usable as a document')  # noqa: SLF001
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise nf._ModelUnusable('HWPML2X text is not well-formed') from exc  # noqa: SLF001
    body = next((element for element in root.iter() if _name(element) == 'body'), None)
    if body is None:
        raise nf._ModelUnusable('HWPML2X text has no BODY')  # noqa: SLF001
    resolver = _Resolver(root, body)
    chars, paras, _fonts, _headings = nf._shape_tables(root, resolver)  # noqa: SLF001
    fills: dict[str, dict[str, Any]] = {}
    for item in resolver.items.get('borderfilllist', []):
        ident = _attr(item, 'id')
        if ident is not None:
            fills[ident.strip()] = _border_facts(item, resolver)
    cache: dict[int, str] = {}
    tables: list[dict[str, Any]] = []
    paragraphs: list[dict[str, Any]] = []
    margins: list[str] = []
    body_count = [0]

    def read_table(table: ET.Element, chain: tuple[str, ...], where: dict[str, Any], before_text: str, depth: int) -> None:
        inst = _table_inst(table)
        rows, cols = _int(_attr(table, 'rowcount'), -1), _int(_attr(table, 'colcount'), -1)
        if inst is None or rows < 1 or cols < 1:
            raise nf._ModelUnusable('a table has no identity or no grid')  # noqa: SLF001
        fact: dict[str, Any] = {'table_id': f'tbl:{inst}', 'inst_id': inst, 'index': len(tables), 'rows': rows, 'cols': cols, 'nested_in': where.get('table'), 'nested_cell': where.get('cell'), 'body_index': where.get('body_index'),
                                'before_text': before_text, 'frame': _frame_digest(table, resolver), 'cells': []}
        tables.append(fact)
        inside = (*chain, inst)
        for row_element in table:
            name = _name(row_element)
            if name != 'row':
                if name == 'shapeobject':  # A caption's paragraphs stay with the document, not with the grid.
                    visit(row_element, chain, depth + 1)
                continue
            for cell in row_element:
                if _name(cell) == 'cell':
                    fact['cells'].append(read_cell(cell, fact, inside, depth + 1))
        fact['cells'].sort(key=lambda item: (item['row'], item['col']))
        fact['state_sha256'] = table_state(fact)
        fact['expected_hash'] = 'sha256:' + fact['state_sha256'][:24]

    def read_cell(cell: ET.Element, table: dict[str, Any], chain: tuple[str, ...], depth: int) -> dict[str, Any]:
        row, col = _attr(cell, 'rowaddr'), _attr(cell, 'coladdr')
        if row is None or col is None:
            raise nf._ModelUnusable('a table cell has no address')  # noqa: SLF001
        ref = (_attr(cell, 'borderfill') or '').strip()
        if ref not in fills:
            raise nf._ModelUnusable('a table cell has no resolvable border')  # noqa: SLF001
        fact = {'row': _int(row) + 1, 'col': _int(col) + 1, 'row_span': _int(_attr(cell, 'rowspan'), 1), 'col_span': _int(_attr(cell, 'colspan'), 1), 'width': _int(_attr(cell, 'width')), 'height': _int(_attr(cell, 'height'))}
        fact['address'] = cell_address(fact['row'], fact['col'])
        fact.update(json.loads(json.dumps(fills[ref])))
        attrs: list[Any] = sorted((key, value) for key, value in _lower_attrs(cell).items() if key not in _CELL_VOLATILE)
        rows: list[dict[str, Any]] = []
        for child in cell:
            name = _name(child)
            if name == 'paralist':
                attrs.append(sorted((key, value) for key, value in _lower_attrs(child).items() if key not in _LIST_VOLATILE))
                for item in child:
                    if _name(item) == 'p':
                        rows.append(read_paragraph(item, table, chain, {'table': table['table_id'], 'cell': fact['address']}, depth + 1))
            elif name == 'cellmargin':
                attrs.append(sorted(_lower_attrs(child).items()))
            else:
                try:
                    attrs.append(resolver.digest(child, prune=True))
                except _Unresolved as exc:
                    raise nf._ModelUnusable('a table cell has an unresolved definition') from exc  # noqa: SLF001
        fact['paragraphs'] = rows
        fact['text'] = '\n'.join(item['text'] for item in rows)
        fact['objects'] = [name for item in rows for name in item['objects']]
        fact['content'] = _digest([item['digest'] for item in rows])[:24]
        fact['attrs'] = _digest(attrs)[:16]
        fact['blank'] = len(rows) == 1 and not rows[0]['atoms']
        return fact

    def read_paragraph(paragraph: ET.Element, table: dict[str, Any] | None, chain: tuple[str, ...], where: dict[str, Any], depth: int) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise nf._ModelUnusable('HWPML2X nesting is too deep')  # noqa: SLF001
        fact = _paragraph_facts(paragraph, chars, paras, resolver, cache)
        paragraphs.append({'chain': chain, 'digest': fact['digest'], 'text': fact['text'], 'body_index': where.get('body_index'), 'heading': _heading_of(fact['shape']), 'objects': fact['objects'], 'in_table': bool(where.get('cell')), 'atoms': fact['atoms']})
        own = _own_text_before_table(fact)
        previous = where.get('previous', '')
        for run in paragraph:
            if _name(run) != 'text':
                continue
            for child in _text_objects(run):
                if _name(child) == 'table':
                    read_table(child, chain, where, own or previous, depth + 1)
                elif _name(child) in {'header', 'footer'}:
                    margins.extend(''.join(item.itertext()) for item in child.iter() if _name(item) == 'char')
                else:
                    visit(child, chain, depth + 1)
        return fact

    def visit(element: ET.Element, chain: tuple[str, ...], depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise nf._ModelUnusable('HWPML2X nesting is too deep')  # noqa: SLF001
        previous = ''
        for child in element:
            name = _name(child)
            if name in {'header', 'footer'}:
                margins.extend(''.join(item.itertext()) for item in child.iter() if _name(item) == 'char')
            elif name == 'p':
                where: dict[str, Any] = {'previous': previous}
                if _name(element) == 'section':
                    where['body_index'] = body_count[0]
                    body_count[0] += 1
                fact = read_paragraph(child, None, chain, where, depth + 1)
                previous = fact['text'] or previous
            elif name == 'table':
                read_table(child, chain, {}, '', depth + 1)
            else:
                visit(child, chain, depth + 1)

    visit(body, (), 0)
    identities = [table['inst_id'] for table in tables]
    return {'tables': tables, 'paragraphs': paragraphs, 'duplicates': sorted({value for value in identities if identities.count(value) > 1}), 'header_footer_text': margins, 'fonts': _fonts}


def _heading_of(shape: Mapping[str, Any]) -> str:
    return str(shape['attrs'].get('headingtype') or 'None').lower()


def _own_text_before_table(fact: Mapping[str, Any]) -> str:
    text = ''
    for atom in fact['atoms']:
        if atom.get('control') == 'table':
            return text
        text += atom.get('char', '')
    return ''


def table_state(table: Mapping[str, Any]) -> str:
    """What a table's hash is over: its grid, each cell's place, width, text, styles, border and fill. Heights follow layout and stay out."""
    return _digest({'grid': [table['rows'], table['cols']], 'frame': table['frame'], 'cells': [
        [cell['row'], cell['col'], cell['row_span'], cell['col_span'], cell['width'], cell['border'], cell['fill'], cell['rest'], cell['attrs'], cell['content']] for cell in table['cells']]})


def _covering(table: Mapping[str, Any], row: int, col: int) -> dict[str, Any] | None:
    for cell in table['cells']:
        if cell['row'] <= row < cell['row'] + cell['row_span'] and cell['col'] <= col < cell['col'] + cell['col_span']:
            return cell
    return None


def _named(table: Mapping[str, Any], row: int, col: int) -> dict[str, Any] | None:
    return next((cell for cell in table['cells'] if cell['row'] == row and cell['col'] == col), None)


def public_summary(table: Mapping[str, Any], page: int | None) -> dict[str, Any]:
    cells = table['cells']
    return {
        'table_id': table['table_id'], 'index': table['index'], 'page': page, 'rows': table['rows'], 'cols': table['cols'], 'cell_count': len(cells),
        'merged_cells': sum(cell['row_span'] > 1 or cell['col_span'] > 1 for cell in cells), 'expected_hash': table['expected_hash'],
        'before_text': table['before_text'][:120], 'nested_in': table['nested_in'], 'nested_cell': table['nested_cell'],
        'first_cells': [{'address': cell['address'], 'text': cell['text'][:60]} for cell in cells[:8]],
    }


def _public_cell(cell: Mapping[str, Any]) -> dict[str, Any]:
    paragraphs = []
    for item in cell['paragraphs']:
        runs = [{key: value for key, value in row.items() if key not in {'rest', 'underline_style'}} for row in nf._segments(item['atoms'])]  # noqa: SLF001
        paragraphs.append({'text': item['text'][:READ_TEXT], 'runs': [{**run, 'text': run['text'][:READ_TEXT]} for run in runs], 'paragraph': nf.public_paragraph(item['shape']), 'objects': item['objects']})
    return {
        'address': cell['address'], 'row': cell['row'], 'col': cell['col'], 'row_span': cell['row_span'], 'col_span': cell['col_span'],
        'width_hu': cell['width'], 'height_hu': cell['height'], 'text': cell['text'][:READ_TEXT], 'text_truncated': len(cell['text']) > READ_TEXT,
        'paragraph_count': len(cell['paragraphs']), 'paragraphs': paragraphs, 'objects': cell['objects'], 'blank': cell['blank'],
        'border': cell['border'], 'fill': cell['fill'], 'content_sha256': cell['content'],
    }


def public_table(table: Mapping[str, Any], page: int | None, max_cells: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    spent = 0
    for cell in table['cells'][:max_cells]:
        row = _public_cell(cell)
        spent += len(json.dumps(row, ensure_ascii=False).encode('utf-8', 'surrogatepass'))
        if spent > READ_BYTES:
            break
        rows.append(row)
    return {**public_summary(table, page), 'cells': rows, 'cells_truncated': len(table['cells']) > len(rows)}


def review_rows(model: Mapping[str, Any], pages: Mapping[str, int | None]) -> tuple[list[dict[str, Any]], bool]:
    """Each table's values for a review: within a fixed size, saying when it is cut short."""
    rows: list[dict[str, Any]] = []
    spent = 0
    truncated = False
    for table in model['tables']:
        cells = [{'address': cell['address'], 'row': cell['row'], 'col': cell['col'], 'row_span': cell['row_span'], 'col_span': cell['col_span'], 'width': cell['width'],
                  'text': cell['text'][:REVIEW_TEXT], 'objects': cell['objects'], 'content': cell['content'], 'border': cell['border'], 'fill': cell['fill'], 'rest': cell['rest']} for cell in table['cells']]
        row = {'table_id': table['table_id'], 'index': table['index'], 'page': pages.get(table['inst_id']), 'rows': table['rows'], 'cols': table['cols'], 'state_sha256': table['state_sha256'],
               'before_text': table['before_text'][:120], 'nested_in': table['nested_in'], 'cells': cells}
        spent += len(json.dumps(row, ensure_ascii=False).encode('utf-8', 'surrogatepass'))
        if spent > REVIEW_BYTES:
            truncated = True
            break
        rows.append(row)
    return rows, truncated


# Requests.

def _table_id(value: Any) -> str:
    if type(value) is not str or _TABLE_ID.fullmatch(value) is None:
        raise NativeNumberingInputError('table_id must be a table identity from table_inventory, such as tbl:1011')
    return value


def _positive(step: Mapping[str, Any], name: str, *, high: int | None = None) -> int:
    value = step.get(name)
    if type(value) is not int or value < 1 or (high is not None and value > high):
        raise NativeNumberingInputError(f'{name} must be a positive whole number' + (f' of at most {high}' if high else ''))
    return value


def _validated_binding(step: Mapping[str, Any], name: str) -> dict[str, Any]:
    _require_confirm_layout(step, name)
    expected_hash = step.get('expected_hash')
    if type(expected_hash) is not str or _HASH.fullmatch(expected_hash) is None:
        raise NativeNumberingInputError('expected_hash must be the hash table_inventory or table_read reported')
    return {'table_id': _table_id(step.get('table_id')), 'expected_hash': expected_hash, 'expected_page': _positive(step, 'expected_page'),
            'expected_rows': _positive(step, 'expected_rows', high=MAX_GRID), 'expected_cols': _positive(step, 'expected_cols', high=MAX_GRID)}


def _address(step: Mapping[str, Any], name: str) -> tuple[int, int]:
    value = step.get(name)
    if type(value) is not str or _ADDRESS.fullmatch(value) is None:
        raise NativeNumberingInputError(f'{name} must be a cell address such as B3')
    return parse_cell_address(value)


def _refuse_fields(step: Mapping[str, Any], allowed: tuple[str, ...], fields: tuple[str, ...], what: str) -> None:
    stray = [name for name in fields if name not in allowed and step.get(name) is not None]
    if stray:
        raise NativeNumberingInputError(f'{what} does not take {", ".join(stray)}')


def validate_read_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, ('table_id', 'max_cells', *_STEP_ENVELOPE_KEYS), 'table_read step')
    return {'table_id': _table_id(step.get('table_id')), 'max_cells': _positive(step, 'max_cells', high=MAX_READ_CELLS) if step.get('max_cells') is not None else MAX_READ_CELLS}


def validate_inventory_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, _STEP_ENVELOPE_KEYS, 'table_inventory step')
    return {}


def validate_edit_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_BOUND_FIELDS, *_EDIT_FIELDS, *_STEP_ENVELOPE_KEYS), 'table_edit_exact step')
    bound = _validated_binding(step, 'table_edit_exact')
    action = step.get('action')
    if type(action) is not str or action not in EDIT_ACTIONS:
        raise NativeNumberingInputError('action must be one of ' + ', '.join(sorted(EDIT_ACTIONS)))
    request: dict[str, Any] = {**bound, 'action': action}
    name = f'table_edit_exact action {action}'
    fields = _EDIT_FIELDS[1:]
    if action in ROW_ACTIONS or action == 'delete_row':
        allowed = ('row', 'count', 'expected_removed_cells') if action == 'delete_row' else ('row', 'count')
        _refuse_fields(step, allowed, fields, name)
        request.update(row=_positive(step, 'row', high=MAX_GRID), count=_positive(step, 'count', high=MAX_LINES) if step.get('count') is not None else 1)
    elif action in COL_ACTIONS or action == 'delete_col':
        allowed = ('col', 'count', 'expected_removed_cells') if action == 'delete_col' else ('col', 'count')
        _refuse_fields(step, allowed, fields, name)
        request.update(col=_positive(step, 'col', high=MAX_GRID), count=_positive(step, 'count', high=MAX_LINES) if step.get('count') is not None else 1)
    elif action == 'merge_cells':
        _refuse_fields(step, ('cell', 'end_cell'), fields, name)
        request.update(cell=_address(step, 'cell'), end_cell=_address(step, 'end_cell'))
    elif action == 'split_cell':
        _refuse_fields(step, ('cell', 'split_rows', 'split_cols'), fields, name)
        rows = _positive(step, 'split_rows', high=MAX_SPLIT) if step.get('split_rows') is not None else 1
        cols = _positive(step, 'split_cols', high=MAX_SPLIT) if step.get('split_cols') is not None else 1
        if rows * cols < 2:
            raise NativeNumberingInputError('split_cell needs split_rows or split_cols of at least 2')
        request.update(cell=_address(step, 'cell'), split_rows=rows, split_cols=cols)
    else:
        _refuse_fields(step, ('cell',), fields, name)
        request.update(cell=_address(step, 'cell'))
    if action in {'delete_row', 'delete_col'}:
        request['expected_removed_cells'] = _validated_removed(step.get('expected_removed_cells'))
    return request


def _validated_removed(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_READ_CELLS:
        raise NativeNumberingInputError('expected_removed_cells must be a list of the cells that hold content')
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {'address', 'text', 'objects'} or type(item.get('address')) is not str or _ADDRESS.fullmatch(item['address']) is None or type(item.get('text')) is not str:
            raise NativeNumberingInputError('each expected removed cell gives its address and full text, and objects when it holds any')
        objects = item.get('objects', [])
        if not isinstance(objects, list) or any(type(name) is not str for name in objects):
            raise NativeNumberingInputError('objects must be a list of object names from table_read')
        rows.append({'address': item['address'], 'text': item['text'], 'objects': list(objects)})
    return rows


def _validated_borders(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 7:
        raise NativeNumberingInputError('borders must be a list of edge groups')
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {'edges', 'line', 'width_mm', 'color'}:
            raise NativeNumberingInputError('a border group gives edges, line, and width_mm and color unless the line is none')
        edges = item.get('edges')
        if not isinstance(edges, list) or not edges or any(type(edge) is not str or edge not in _EDGE_GROUPS for edge in edges):
            raise NativeNumberingInputError('edges must name ' + ', '.join(sorted(_EDGE_GROUPS)))
        parts = [part for edge in edges for part in _EDGE_GROUPS[edge]]
        if len(set(parts)) != len(parts) or seen & set(parts):
            raise NativeNumberingInputError('an edge may be named once')
        seen |= set(parts)
        line = item.get('line')
        if type(line) is not str or line not in LINE_TYPES:
            raise NativeNumberingInputError('line must be one of ' + ', '.join(sorted(LINE_TYPES)))
        group: dict[str, Any] = {'edges': parts, 'line': line}
        if line == 'none':
            if item.get('width_mm') is not None or item.get('color') is not None:
                raise NativeNumberingInputError('removing a line takes no width_mm or color')
        else:
            width, color = item.get('width_mm'), item.get('color')
            if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or float(width) not in LINE_WIDTHS:
                raise NativeNumberingInputError('width_mm must be one of ' + ', '.join(f'{number:g}' for number in LINE_WIDTHS))
            if type(color) is not str or _COLOR.fullmatch(color) is None:
                raise NativeNumberingInputError('color must be #RRGGBB')
            group.update(width_mm=float(width), color=color.upper())
        groups.append(group)
    return groups


def validate_style_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_BOUND_FIELDS, *_STYLE_FIELDS, *_STEP_ENVELOPE_KEYS), 'table_style_exact step')
    request = _validated_binding(step, 'table_style_exact')
    scope = step.get('scope')
    if type(scope) is not str or scope not in {'cell', 'range', 'table'}:
        raise NativeNumberingInputError('scope must be cell, range or table')
    request['scope'] = scope
    if scope == 'table':
        if step.get('cell') is not None or step.get('end_cell') is not None:
            raise NativeNumberingInputError('a table scope takes no cell address')
    else:
        request['cell'] = _address(step, 'cell')
        if scope == 'range':
            request['end_cell'] = _address(step, 'end_cell')
        elif step.get('end_cell') is not None:
            raise NativeNumberingInputError('a cell scope takes no end_cell')
    if step.get('borders') is None and step.get('fill') is None:
        raise NativeNumberingInputError('table_style_exact needs borders, fill or both')
    if step.get('borders') is not None:
        request['borders'] = _validated_borders(step['borders'])
    fill = step.get('fill')
    if fill is not None:
        if fill != 'none' and (type(fill) is not str or _COLOR.fullmatch(fill) is None):
            raise NativeNumberingInputError("fill must be #RRGGBB, or none for no fill")
        request['fill'] = fill if fill == 'none' else fill.upper()
    return request


def validate_create_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_CREATE_FIELDS, *_STEP_ENVELOPE_KEYS), 'table_create_exact step')
    _require_confirm_layout(step, 'table_create_exact')
    match = step.get('match')
    if type(match) is not str or not match.strip() or len(match) > 500 or any(ord(char) < 32 for char in match):
        raise NativeNumberingInputError('match must be one line of exact text of at most 500 characters')
    request: dict[str, Any] = {'match': match, 'expected_page': _positive(step, 'expected_page'), 'rows': _positive(step, 'rows', high=MAX_LINES), 'cols': _positive(step, 'cols', high=MAX_LINES)}
    cells = step.get('cells')
    if cells is not None:
        if not isinstance(cells, list) or any(not isinstance(row, list) or any(type(value) is not str or len(value) > 4000 for value in row) for row in cells):
            raise NativeNumberingInputError('cells must be rows of text')
        request['cells'] = cells
    return request


# The target.

def resolve_table(model: Mapping[str, Any], table_id: str) -> dict[str, Any]:
    table = next((item for item in model['tables'] if item['table_id'] == table_id), None)
    if table is None:
        raise NativeNumberingRefusal('there is no table with this identity; list them again with table_inventory', code='table_not_found')
    if table['inst_id'] in model['duplicates']:
        raise NativeNumberingRefusal('two tables report this identity, so which one is meant cannot be proven', code='table_identity_ambiguous')
    return table


def require_binding(table: Mapping[str, Any], request: Mapping[str, Any], page: int) -> None:
    if (table['rows'], table['cols']) != (request['expected_rows'], request['expected_cols']):
        raise NativeNumberingRefusal(f"the table has {table['rows']} rows and {table['cols']} columns, not {request['expected_rows']} and {request['expected_cols']}; read it again", code='table_grid_mismatch')
    if table['expected_hash'] != request['expected_hash']:
        raise NativeNumberingRefusal('the table changed since this hash was read; read it again with table_read', code='table_state_stale')
    if page != request['expected_page']:
        refusal = NativeNumberingRefusal('the table is not on expected_page', code='target_wrong_page')
        refusal.found_page = page
        raise refusal


def _rect_of(table: Mapping[str, Any], first: tuple[int, int], last: tuple[int, int]) -> dict[str, Any]:
    """The cells of a rectangle given by its corners, refused when a corner is outside or a merged cell crosses its edge."""
    if not all(1 <= row <= table['rows'] and 1 <= col <= table['cols'] for row, col in (first, last)):
        raise NativeNumberingRefusal(f"a cell of this range is outside the {table['rows']} by {table['cols']} table", code='cell_out_of_range')
    top, bottom, left, right = min(first[0], last[0]), max(first[0], last[0]), min(first[1], last[1]), max(first[1], last[1])
    touched = [cell for cell in table['cells'] if cell['row'] <= bottom and cell['row'] + cell['row_span'] - 1 >= top and cell['col'] <= right and cell['col'] + cell['col_span'] - 1 >= left]
    for cell in touched:
        if cell['row'] < top or cell['row'] + cell['row_span'] - 1 > bottom or cell['col'] < left or cell['col'] + cell['col_span'] - 1 > right:
            raise NativeNumberingRefusal(f"the merged cell {cell['address']} crosses the edge of this range; include all of it or none of it", code='range_cuts_merged_cell')
    if not touched or _covering(table, top, left) is None or _named(table, top, left) is None:
        raise NativeNumberingRefusal('this range does not begin at a cell of the table', code='cell_out_of_range')
    return {'top': top, 'bottom': bottom, 'left': left, 'right': right, 'cells': touched}


def _whole_cell(table: Mapping[str, Any], at: tuple[int, int]) -> dict[str, Any]:
    if not (1 <= at[0] <= table['rows'] and 1 <= at[1] <= table['cols']):
        raise NativeNumberingRefusal(f"{cell_address(*at)} is outside the {table['rows']} by {table['cols']} table", code='cell_out_of_range')
    cell = _named(table, *at)
    if cell is None:
        owner = _covering(table, *at)
        raise NativeNumberingRefusal(f"{cell_address(*at)} is part of the merged cell {owner['address'] if owner else 'unknown'}; use that address", code='cell_covered_by_merge')
    return cell


# What a structure edit must produce. An entry says where each cell must end up and what must hold there.

def _kept(cell: Mapping[str, Any], row: int, col: int, row_span: int, col_span: int) -> dict[str, Any]:
    return {'kind': 'kept', 'source': cell, 'row': row, 'col': col, 'row_span': row_span, 'col_span': col_span, 'exact_width': (row_span, col_span) == (cell['row_span'], cell['col_span']), 'border': {}}


def _new(row: int, col: int, row_span: int, col_span: int, source: Mapping[str, Any], kind: str = 'new') -> dict[str, Any]:
    return {'kind': kind, 'source': source, 'row': row, 'col': col, 'row_span': row_span, 'col_span': col_span}


def _collapsed(entries: list[dict[str, Any]], rows: int, cols: int) -> tuple[list[dict[str, Any]], int, int]:
    """Hancom drops a grid line no cell starts on."""
    for axis, span, total in (('row', 'row_span', rows), ('col', 'col_span', cols)):
        starts = sorted({entry[axis] for entry in entries})
        number = {line: index for index, line in enumerate(starts, start=1)}
        for entry in entries:
            end = next((line for line in starts if line >= entry[axis] + entry[span]), None)
            new_end = number[end] if end is not None else len(starts) + 1
            new_span = new_end - number[entry[axis]]
            if new_span != entry[span] and entry['kind'] == 'kept':
                entry['exact_width'] = False
            entry[axis], entry[span] = number[entry[axis]], new_span
        if axis == 'row':
            rows = len(starts)
        else:
            cols = len(starts)
    return entries, rows, cols


def _plan_insert(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    on_rows = request['action'] in ROW_ACTIONS
    line = request['row'] if on_rows else request['col']
    total = table['rows'] if on_rows else table['cols']
    count = request['count']
    if line > total:
        raise NativeNumberingRefusal(f"{'row' if on_rows else 'column'} {line} is outside the table", code='cell_out_of_range')
    if total + count > MAX_GRID:
        raise NativeNumberingRefusal(f'the table would pass {MAX_GRID} lines', code='table_too_large')
    axis, span, other, other_span = ('row', 'row_span', 'col', 'col_span') if on_rows else ('col', 'col_span', 'row', 'row_span')
    side = {**ROW_ACTIONS, **COL_ACTIONS}[request['action']]
    before = side in {0, 2}
    at = line if before else line + 1
    # The caret names the line: its cell must start on it (before) or end on it (after), and be the plainest one there.
    candidates = [cell for cell in table['cells'] if (cell[axis] == line if before else cell[axis] + cell[span] - 1 == line)]
    if not candidates:
        raise NativeNumberingRefusal(f"no cell {'starts' if before else 'ends'} on this line, because merged cells cross it; choose the line next to them", code='line_inside_merged_cell')
    caret = min(candidates, key=lambda cell: (cell[span], cell[other_span], cell[other]))
    source_line = line if before else line
    entries: list[dict[str, Any]] = []
    mirrored: list[dict[str, Any]] = []
    for cell in table['cells']:
        start, length = cell[axis], cell[span]
        where = {'row': cell['row'], 'col': cell['col'], 'row_span': cell['row_span'], 'col_span': cell['col_span']}
        if start >= at:
            where[axis] = start + count
        elif start + length > at:
            where[span] = length + count
        entries.append(_kept(cell, where['row'], where['col'], where['row_span'], where['col_span']))
        if start <= source_line < start + length and not (start < at < start + length):
            mirrored.append(cell)
    moved_edges: list[dict[str, Any]] = []
    outer_side = {0: 'left', 1: 'right', 2: 'top', 3: 'bottom'}[side]
    if (before and at == 1) or (not before and at == total + 1):
        # The frame goes to the new line. The old line's side there takes the value of its other side.
        for entry in entries:
            cell = entry['source']
            if cell in mirrored:
                value = cell['border'][_OPPOSITE[outer_side]]
                entry['border'][outer_side] = value
                if value != cell['border'][outer_side]:
                    moved_edges.append({'cell': cell_address(entry['row'], entry['col']), 'side': outer_side, 'before': cell['border'][outer_side], 'after': value})
    added: list[str] = []
    for number in range(count):
        for cell in mirrored:
            where = {axis: at + number, span: 1, other: cell[other], other_span: cell[other_span]}
            entry = _new(where['row'], where['col'], where['row_span'], where['col_span'], cell)
            entries.append(entry)
            added.append(cell_address(entry['row'], entry['col']))
    rows, cols = (table['rows'] + count, table['cols']) if on_rows else (table['rows'], table['cols'] + count)
    caret_after = (caret['row'] + count, caret['col']) if side == 2 else (caret['row'], caret['col'] + count) if side == 0 else (caret['row'], caret['col'])
    return {'action': request['action'], 'kind': 'insert', 'rows': rows, 'cols': cols, 'entries': entries, 'caret': caret, 'native': ('insert', side, count), 'caret_after': caret_after,
            'changes': {'inserted_lines': count, 'line_kind': 'row' if on_rows else 'column', 'new_cells': added, 'outer_edge_moved': moved_edges[:MAX_REPORT_ROWS], 'widened_cells': [entry['source']['address'] for entry in entries if entry['kind'] == 'kept' and (entry['row_span'], entry['col_span']) != (entry['source']['row_span'], entry['source']['col_span'])]}}


def _plan_delete(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    on_rows = request['action'] == 'delete_row'
    first = request['row'] if on_rows else request['col']
    total = table['rows'] if on_rows else table['cols']
    count = request['count']
    if first > total or first + count - 1 > total:
        raise NativeNumberingRefusal(f"these {'rows' if on_rows else 'columns'} are outside the table", code='cell_out_of_range')
    if count >= total:
        raise NativeNumberingRefusal(f"this would delete every {'row' if on_rows else 'column'} of the table", code='table_would_be_empty')
    axis, span, other, other_span = ('row', 'row_span', 'col', 'col_span') if on_rows else ('col', 'col_span', 'row', 'row_span')
    last = first + count
    entries: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    shortened: list[str] = []
    moved_edges: list[dict[str, Any]] = []
    at_edge = first == 1 or last == total + 1
    outer_side = ('top' if on_rows else 'left') if first == 1 else ('bottom' if on_rows else 'right')
    outer_line, inner_line = (1, last) if first == 1 else (total, first - 1)
    for cell in table['cells']:
        start, end = cell[axis], cell[axis] + cell[span]
        if start >= first and end <= last:
            removed.append(cell)
            continue
        remaining = end - start - max(0, min(end, last) - max(start, first))
        where = {'row': cell['row'], 'col': cell['col'], 'row_span': cell['row_span'], 'col_span': cell['col_span']}
        where[span] = remaining
        where[axis] = start if start < first else first if start < last else start - count
        if remaining != cell[span]:
            shortened.append(cell['address'])
        entry = _kept(cell, where['row'], where['col'], where['row_span'], where['col_span'])
        if at_edge and start <= inner_line < end:
            # The cells that become the edge take the deleted edge's values, side by side.
            sources = [_covering(table, *((outer_line, slot) if on_rows else (slot, outer_line))) for slot in range(cell[other], cell[other] + cell[other_span])]
            values = [item['border'][outer_side] for item in sources if item is not None and not (item[axis] <= inner_line < item[axis] + item[span])]
            if values and all(value == values[0] for value in values):
                entry['border'][outer_side] = values[0]
                if values[0] != cell['border'][outer_side]:
                    moved_edges.append({'cell': cell_address(entry['row'], entry['col']), 'side': outer_side, 'before': cell['border'][outer_side], 'after': values[0]})
            elif values:
                entry['border'][outer_side] = None  # Several values along one side: which one it took is not predicted.
        entries.append(entry)
    if count == 1:
        candidates = [cell for cell in table['cells'] if cell[axis] == first and cell[span] == 1 and cell in removed]
        if not candidates:
            candidates = [cell for cell in table['cells'] if cell[axis] == first]
        if not candidates:
            raise NativeNumberingRefusal('no cell starts on this line, because merged cells cross it', code='line_inside_merged_cell')
        caret = min(candidates, key=lambda cell: (cell[other_span], cell[other]))
        native: tuple = ('delete_line', 'TableDeleteRow' if on_rows else 'TableDeleteColumn')
        rect = None
    else:
        first_cell = _named(table, first, 1) if on_rows else _named(table, 1, first)
        corners = ((first, 1), (last - 1, table['cols'])) if on_rows else ((1, first), (table['rows'], last - 1))
        try:
            rect = _rect_of(table, *corners)
        except NativeNumberingRefusal as exc:
            if exc.code != 'range_cuts_merged_cell':
                raise
            raise NativeNumberingRefusal(f'{exc}. Delete these lines one at a time, or unmerge that cell first.', code='merged_cell_crosses_lines') from None
        if first_cell is None:
            raise NativeNumberingRefusal('this block of lines does not begin at a cell', code='merged_cell_crosses_lines')
        caret = first_cell
        native = ('delete_block',)
    stated = [{'address': cell['address'], 'text': cell['text'], 'objects': cell['objects']} for cell in removed if not cell['blank']]
    wanted = sorted(([item['address'], item['text'], sorted(item['objects'])] for item in request['expected_removed_cells']), key=str)
    if wanted != sorted(([item['address'], item['text'], sorted(item['objects'])] for item in stated), key=str):
        raise NativeNumberingRefusal('expected_removed_cells must list every cell with content this removes, with its full text and objects as table_read gives them: ' + ', '.join(item['address'] for item in stated)[:200], code='removed_content_mismatch')
    entries, rows, cols = _collapsed(entries, *((table['rows'] - count, table['cols']) if on_rows else (table['rows'], table['cols'] - count)))
    return {'action': request['action'], 'kind': 'delete', 'rows': rows, 'cols': cols, 'entries': entries, 'caret': caret, 'native': native, 'rect': rect, 'removed_objects': [name for cell in removed for name in cell['objects']],
            'changes': {'deleted_lines': count, 'line_kind': 'row' if on_rows else 'column', 'removed_cells': [{key: item[key] for key in ('address', 'text')} | ({'objects': item['objects']} if item['objects'] else {}) for item in stated],
                        'removed_blank_cells': [cell['address'] for cell in removed if cell['blank']], 'shortened_cells': shortened, 'outer_edge_moved': moved_edges[:MAX_REPORT_ROWS]}}


def _plan_merge(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    rect = _rect_of(table, request['cell'], request['end_cell'])
    cells = sorted(rect['cells'], key=lambda cell: (cell['row'], cell['col']))
    if len(cells) < 2:
        raise NativeNumberingRefusal('a merge needs at least two cells', code='range_too_small')
    first = cells[0]
    entries = [_kept(cell, cell['row'], cell['col'], cell['row_span'], cell['col_span']) for cell in table['cells'] if cell not in cells]
    merged = _new(rect['top'], rect['left'], rect['bottom'] - rect['top'] + 1, rect['right'] - rect['left'] + 1, first, kind='merged')
    kept_paragraphs = [item for cell in cells if not cell['blank'] for item in cell['paragraphs']]
    merged['paragraph_digests'] = [item['digest'] for item in kept_paragraphs] or [cells[0]['paragraphs'][0]['digest']]
    entries.append(merged)
    entries, rows, cols = _collapsed(entries, table['rows'], table['cols'])
    return {'action': request['action'], 'kind': 'merge', 'rows': rows, 'cols': cols, 'entries': entries, 'caret': first, 'native': ('merge',), 'rect': rect,
            'changes': {'merged': {'cell': first['address'], 'from': [cell['address'] for cell in cells], 'paragraphs': len(merged['paragraph_digests']), 'dropped_blank_cells': [cell['address'] for cell in cells if cell['blank']]},
                        'removed_grid_lines': {'rows': table['rows'] - rows, 'cols': table['cols'] - cols}}}


def _column_edges(table: Mapping[str, Any]) -> list[int] | None:
    """Each grid column's left edge, from the cells' widths; None when the cells do not say."""
    widths: dict[int, int] = {}
    for _round in range(table['cols'] + 1):
        moved = False
        for cell in table['cells']:
            missing = [col for col in range(cell['col'], cell['col'] + cell['col_span']) if col not in widths]
            if len(missing) == 1:
                known = sum(widths[col] for col in range(cell['col'], cell['col'] + cell['col_span']) if col in widths)
                widths[missing[0]] = cell['width'] - known
                moved = True
        if not moved:
            break
    if len(widths) != table['cols'] or any(value <= 0 for value in widths.values()):
        return None
    edges = [0]
    for col in range(1, table['cols'] + 1):
        edges.append(edges[-1] + widths[col])
    return edges


def _plan_split(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    cell = _whole_cell(table, request['cell'])
    unmerge = request['action'] == 'unmerge_cell'
    if unmerge:
        if cell['row_span'] == 1 and cell['col_span'] == 1:
            raise NativeNumberingRefusal(f"{cell['address']} is not a merged cell; use split_cell to divide it", code='cell_not_merged')
        rows_wanted, cols_wanted = cell['row_span'], cell['col_span']
    else:
        rows_wanted, cols_wanted = request['split_rows'], request['split_cols']
        if cell['row_span'] > 1 and rows_wanted != 1 and rows_wanted != cell['row_span']:
            raise NativeNumberingRefusal(f"{cell['address']} spans {cell['row_span']} rows; it can be split into exactly that many rows, because any other number follows rendered heights this cannot predict", code='merged_split_rows_unsupported')
    if table['rows'] + rows_wanted - 1 > MAX_GRID or table['cols'] + cols_wanted - 1 > MAX_GRID:
        raise NativeNumberingRefusal(f'the table would pass {MAX_GRID} lines', code='table_too_large')
    edges = _column_edges(table)
    if edges is None:
        raise NativeNumberingRefusal('the column widths of this table cannot be worked out from its cells, so the result of a split cannot be predicted', code='table_geometry_unknown')
    x0, x1 = edges[cell['col'] - 1], edges[cell['col'] - 1 + cell['col_span']]
    cuts = [x0 + (x1 - x0) * part / cols_wanted for part in range(cols_wanted + 1)]
    snapped = [next((edge for edge in edges if abs(edge - cut) <= 2), cut) for cut in cuts]
    grid = sorted({*edges, *snapped})
    extra_rows = 0 if rows_wanted == cell['row_span'] or rows_wanted == 1 else rows_wanted - 1
    entries: list[dict[str, Any]] = []
    target_top = cell['row']
    # A cell keeping its rows through a rows_wanted==1 split of a merged cell stays one part per column over all its rows.
    part_rows = cell['row_span'] if rows_wanted == 1 else 1
    for other in table['cells']:
        if other is cell:
            continue
        low, high = grid.index(edges[other['col'] - 1]), grid.index(edges[other['col'] - 1 + other['col_span']])
        row, row_span = other['row'], other['row_span']
        if other['row'] > target_top:
            row += extra_rows
        elif other['row'] + other['row_span'] > target_top and extra_rows:
            row_span += extra_rows
        entries.append(_kept(other, row, low + 1, row_span, high - low))
    parts: list[str] = []
    for row_part in range(rows_wanted):
        for col_part in range(cols_wanted):
            low, high = grid.index(snapped[col_part]), grid.index(snapped[col_part + 1])
            entry = _new(target_top + row_part * (1 if part_rows == 1 else part_rows), low + 1, part_rows, high - low, cell, kind='first_part' if not parts else 'part')
            entries.append(entry)
            parts.append(cell_address(entry['row'], entry['col']))
    rows, cols = table['rows'] + extra_rows, len(grid) - 1
    return {'action': request['action'], 'kind': 'split', 'rows': rows, 'cols': cols, 'entries': entries, 'caret': cell, 'native': ('split', rows_wanted if rows_wanted > 1 else 0, cols_wanted if cols_wanted > 1 else 0),
            'changes': {'split': {'cell': cell['address'], 'parts': parts, 'content_in': parts[0]}, 'added_grid_lines': {'rows': rows - table['rows'], 'cols': cols - table['cols']}}}


def plan_edit(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    action = request['action']
    if action in ROW_ACTIONS or action in COL_ACTIONS:
        return _plan_insert(table, request)
    if action in {'delete_row', 'delete_col'}:
        return _plan_delete(table, request)
    if action == 'merge_cells':
        return _plan_merge(table, request)
    return _plan_split(table, request)


def edit_problems(table: Mapping[str, Any], plan: Mapping[str, Any], after: Mapping[str, Any] | None) -> list[str]:
    """The observed table against every entry of the plan: where each cell is, what it holds and how it looks."""
    if after is None:
        return ['the table is gone']
    problems: list[str] = []
    if (after['rows'], after['cols']) != (plan['rows'], plan['cols']):
        problems.append(f"the table has {after['rows']} by {after['cols']} lines, expected {plan['rows']} by {plan['cols']}")
    if after['frame'] != table['frame']:
        problems.append("the table's own properties changed")
    observed = {(cell['row'], cell['col']): cell for cell in after['cells']}
    wanted = {(entry['row'], entry['col']): entry for entry in plan['entries']}
    for key in sorted(set(observed) | set(wanted)):
        where = cell_address(*key)
        got, want = observed.get(key), wanted.get(key)
        if got is None:
            problems.append(f'no cell at {where}')
        elif want is None:
            problems.append(f'an unplanned cell at {where}')
        elif (got['row_span'], got['col_span']) != (want['row_span'], want['col_span']):
            problems.append(f"{where} spans {got['row_span']} by {got['col_span']}, expected {want['row_span']} by {want['col_span']}")
        else:
            problems.extend(_entry_problems(where, want, got))
    return problems


def _entry_problems(where: str, want: Mapping[str, Any], got: Mapping[str, Any]) -> list[str]:
    source, kind = want['source'], want['kind']
    problems: list[str] = []
    if kind == 'kept':
        if got['content'] != source['content']:
            problems.append(f'{where} does not hold the content of {source["address"]}')
        for name in ('fill', 'rest', 'attrs'):
            if got[name] != source[name]:
                problems.append(f'{where} changed its {name}')
        for side in SIDES:
            expected = want['border'].get(side, source['border'][side])
            if expected is not None and got['border'][side] != expected:
                problems.append(f'{where} changed its {side} line')
        if want['exact_width'] and got['width'] != source['width']:
            problems.append(f'{where} changed its width')
    elif kind == 'merged':
        if [item['digest'] for item in got['paragraphs']] != want['paragraph_digests']:
            problems.append(f'{where} does not hold exactly the merged paragraphs in order')
        for name in ('border', 'fill', 'rest', 'attrs'):
            if got[name] != source[name]:
                problems.append(f"{where} did not take the {name} of {source['address']}")
    elif kind == 'first_part':
        if got['content'] != source['content']:
            problems.append(f'{where} does not hold the content of the split cell')
    else:
        if not got['blank']:
            problems.append(f'{where} should be a new empty cell')
        if got['fill'] != source['fill']:
            problems.append(f'{where} did not take the fill of {source["address"]}')
    return problems


# Borders and fill.

def _target_rect(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    if request['scope'] == 'table':
        return _rect_of(table, (1, 1), (table['rows'], table['cols']))
    if request['scope'] == 'cell':
        cell = _whole_cell(table, request['cell'])
        return {'top': cell['row'], 'bottom': cell['row'] + cell['row_span'] - 1, 'left': cell['col'], 'right': cell['col'] + cell['col_span'] - 1, 'cells': [cell]}
    return _rect_of(table, request['cell'], request['end_cell'])


def _after_line(before: Mapping[str, Any], group: Mapping[str, Any]) -> dict[str, Any]:
    if group['line'] == 'none':
        return {**before, 'line': 'none'}
    return {'line': group['line'], 'width_mm': group['width_mm'], 'color': group['color']}


def _border_items(groups: list[dict[str, Any]]) -> dict[str, int]:
    items: dict[str, int] = {}
    for group in groups:
        number = LINE_TYPES[group['line']][0]
        for edge in group['edges']:
            if edge in SIDES:
                type_item, width_item = f'BorderType{_ITEM_SIDE[edge]}', f'BorderWidth{_ITEM_SIDE[edge]}'
                color_item = 'BorderCorlorLeft' if edge == 'left' else f'BorderColor{_ITEM_SIDE[edge]}'
            else:
                axis = 'Horz' if edge == 'inner_horizontal' else 'Vert'
                type_item, width_item, color_item = f'Type{axis}', f'Width{axis}', f'Color{axis}'
            items[type_item] = number
            if group['line'] != 'none':
                items[width_item] = LINE_WIDTHS.index(group['width_mm'])
                items[color_item] = native_color(group['color'])
    return items


def plan_style(table: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    rect = _target_rect(table, request)
    cells = sorted(rect['cells'], key=lambda cell: (cell['row'], cell['col']))
    selected = {id(cell) for cell in cells}
    expected = {(cell['row'], cell['col']): {'border': json.loads(json.dumps(cell['border'])), 'fill': cell['fill']} for cell in table['cells']}
    edge_rows: list[dict[str, Any]] = []
    not_written: list[dict[str, Any]] = []
    groups = request.get('borders') or []
    by_edge = {edge: group for group in groups for edge in group['edges']}

    def on_outer(cell: Mapping[str, Any], side: str) -> bool:
        return {'left': cell['col'] == rect['left'], 'right': cell['col'] + cell['col_span'] - 1 == rect['right'], 'top': cell['row'] == rect['top'], 'bottom': cell['row'] + cell['row_span'] - 1 == rect['bottom']}[side]

    def write(cell: Mapping[str, Any], side: str, group: Mapping[str, Any], role: str) -> None:
        old = cell['border'][side]
        new = _after_line(old, group)
        expected[(cell['row'], cell['col'])]['border'][side] = new
        if new != old:
            edge_rows.append({'cell': cell['address'], 'side': side, 'role': role, 'before': old, 'after': new})

    for name in ('inner_horizontal', 'inner_vertical'):
        if name in by_edge:
            found = any(cell['row'] + cell['row_span'] - 1 < rect['bottom'] for cell in cells) if name == 'inner_horizontal' else any(cell['col'] + cell['col_span'] - 1 < rect['right'] for cell in cells)
            if not found:
                raise NativeNumberingRefusal(f'this target has no {name} line', code='edge_not_in_target')
    for cell in cells:
        for side in SIDES:
            if on_outer(cell, side):
                if side in by_edge:
                    write(cell, side, by_edge[side], 'requested')
            else:
                name = 'inner_horizontal' if side in {'top', 'bottom'} else 'inner_vertical'
                if name in by_edge:
                    write(cell, side, by_edge[name], 'requested')
    for side in SIDES:
        if side not in by_edge:
            continue
        for other in table['cells']:
            if id(other) in selected:
                continue
            facing = {'top': other['row'] + other['row_span'] == rect['top'], 'bottom': other['row'] == rect['bottom'] + 1, 'left': other['col'] + other['col_span'] == rect['left'], 'right': other['col'] == rect['right'] + 1}[side]
            if not facing:
                continue
            if side in {'top', 'bottom'}:
                low, high, first, last = other['col'], other['col'] + other['col_span'] - 1, rect['left'], rect['right']
            else:
                low, high, first, last = other['row'], other['row'] + other['row_span'] - 1, rect['top'], rect['bottom']
            if high < first or low > last:
                continue
            if low >= first and high <= last:
                write(other, _OPPOSITE[side], by_edge[side], 'shared_neighbour')
            else:
                not_written.append({'cell': other['address'], 'side': _OPPOSITE[side], 'reason': 'wider_than_edge'})
    fill_rows: list[dict[str, Any]] = []
    fill = request.get('fill')
    if fill is not None:
        new_fill = {'kind': 'none'} if fill == 'none' else {'kind': 'solid', 'color': fill}
        for cell in cells:
            if cell['fill']['kind'] == 'other':
                raise NativeNumberingRefusal(f"{cell['address']} has a pattern, gradation or picture fill, which this command does not replace", code='fill_kind_unsupported')
            expected[(cell['row'], cell['col'])]['fill'] = new_fill
            if cell['fill'] != new_fill:
                fill_rows.append({'cell': cell['address'], 'before': cell['fill'], 'after': new_fill})
    return {'rect': rect, 'cells': cells, 'expected': expected, 'border_items': _border_items(groups) if edge_rows else {}, 'fill': fill if fill_rows else None,
            'changes': {'edges': edge_rows[:MAX_REPORT_ROWS], 'edges_total': len(edge_rows), 'fills': fill_rows[:MAX_REPORT_ROWS], 'fills_total': len(fill_rows), 'neighbours_not_written': not_written[:MAX_REPORT_ROWS]}}


def style_problems(table: Mapping[str, Any], plan: Mapping[str, Any], after: Mapping[str, Any] | None) -> list[str]:
    if after is None:
        return ['the table is gone']
    problems: list[str] = []
    if (after['rows'], after['cols'], after['frame']) != (table['rows'], table['cols'], table['frame']):
        problems.append('the table grid or properties changed')
    observed = {(cell['row'], cell['col']): cell for cell in after['cells']}
    for cell in table['cells']:
        key, where = (cell['row'], cell['col']), cell['address']
        got, want = observed.get(key), plan['expected'][(cell['row'], cell['col'])]
        if got is None:
            problems.append(f'no cell at {where}')
            continue
        for name in ('row_span', 'col_span', 'width', 'content', 'attrs', 'rest'):
            if got[name] != cell[name]:
                problems.append(f'{where} changed its {name}')
        if got['border'] != want['border']:
            sides = [side for side in SIDES if got['border'][side] != want['border'][side]]
            problems.append(f"{where} has other lines than expected on {', '.join(sides)}")
        if got['fill'] != want['fill']:
            problems.append(f'{where} has another fill than expected')
    if len(after['cells']) != len(table['cells']):
        problems.append('the number of cells changed')
    return problems


# The document around the target.

_STRUCTURE_LISTS = frozenset({'borderfilllist', 'charshapelist', 'parashapelist', 'facenamelist', 'tabdeflist'})


def document_problems(before: Mapping[str, Any], after: Mapping[str, Any], target: Mapping[str, Any] | None, *, allowed_lists: frozenset[str], removed_objects: tuple[str, ...] = (), allow_new_paragraphs: bool = False, allow_new_table: bool = False) -> list[str]:
    census_before, census_after = before['census'], after['census']
    compared = [*_COMPARED, *([] if allow_new_paragraphs else ['frame'])]
    if 'picture' in removed_objects:
        compared = [name for name in compared if name not in {'bindata_sha256', 'media_sha256'}]
    problems = [f'{name} changed' for name in compared if census_before.get(name) != census_after.get(name)]
    for name in sorted(set(census_before['definitions']) | set(census_after['definitions'])):
        removed, added = _multiset_delta(census_before['definitions'].get(name, []), census_after['definitions'].get(name, []))
        if (removed or added) and name not in allowed_lists:
            problems.append(f'{name} changed')
    if sorted(before['controls']) != sorted(after['controls']) and not removed_objects and not allow_new_table:
        problems.append('the document controls changed')
    inst = target['inst_id'] if target is not None else None
    old = [row for row in before['model']['paragraphs'] if inst not in row['chain']]
    new = [row for row in after['model']['paragraphs'] if inst not in row['chain']]
    if not allow_new_table and [row['digest'] for row in old] != [row['digest'] for row in new]:
        problems.append('a paragraph outside the table changed')
    others_before = {item['table_id']: item['state_sha256'] for item in before['model']['tables'] if inst is None or item['table_id'] != target['table_id']}
    others_after = {item['table_id']: item['state_sha256'] for item in after['model']['tables'] if inst is None or item['table_id'] != target['table_id']}
    if allow_new_table:
        others_after = {key: value for key, value in others_after.items() if key in others_before}
    if others_before != others_after:
        problems.append('another table changed')
    return problems


def new_table_paragraph_problems(before: Mapping[str, Any], after: Mapping[str, Any], anchor: Mapping[str, Any], new_inst: str) -> list[str]:
    """A new table adds, right after the anchor, the paragraph holding it and one blank paragraph, as measured; every other paragraph stays in place."""
    old = before['model']['paragraphs']
    new = [row for row in after['model']['paragraphs'] if new_inst not in row['chain']]
    at = next((number for number, row in enumerate(old) if row is anchor), None)
    if at is None or len(new) != len(old) + 2:
        return ['the new table did not add exactly its own two paragraphs']
    problems: list[str] = []
    holder, blank = new[at + 1], new[at + 2]
    if holder['objects'] != ['table'] or holder['text'] or holder['body_index'] is None:
        problems.append('the new table is not in its own paragraph after the anchor')
    if blank['objects'] or blank['text'] or blank['body_index'] is None:
        problems.append('the paragraph after the new table is not blank')
    if [row['digest'] for row in old] != [row['digest'] for row in (*new[:at + 1], *new[at + 3:])]:
        problems.append('a paragraph outside the new table changed')
    return problems


def _inst_of(control: Any) -> str | None:
    getter = getattr(control, 'GetCtrlInstID', None)
    value = getter() if callable(getter) else None
    return str(value) if value not in (None, '') else None


def table_controls(hwp: Any) -> dict[str, Any]:
    """Table controls by native identity, read fresh; a walk that cannot finish stops the command, as any other unreadable evidence does."""
    found: dict[str, Any] = {}
    try:
        controls = _control_ids(hwp)
    except ControlEnumerationUnproven as exc:
        raise NativeNumberingRefusal('the document controls could not be enumerated', code='control_enumeration_unavailable') from exc
    for control, name in controls:
        if name == 'tbl':
            inst = _inst_of(control)
            if inst is not None:
                found[inst] = control
    return found


def table_pages(hwp: Any, page_of: Callable[[], int], *, only: str | None = None) -> dict[str, int | None]:
    """The page each table starts on, by visiting its anchor; the caller gives the caret back."""
    pages: dict[str, int | None] = {}
    setter = getattr(hwp, 'set_pos_by_set', None)
    for inst, control in table_controls(hwp).items():
        if only is not None and inst != only:
            continue
        anchor = getattr(control, 'GetAnchorPos', None)
        if not callable(anchor) or not callable(setter):
            pages[inst] = None
            continue
        setter(anchor(0))
        pages[inst] = page_of()
    return pages


def _run(hwp: Any, name: str) -> Any:
    return hwp.HAction.Run(name)


def _parent_inst(hwp: Any) -> str | None:
    parent = hwp.ParentCtrl
    return _inst_of(parent) if parent is not None else None


def _in_table(hwp: Any, inst: str) -> bool:
    return hwp.is_cell() is True and _parent_inst(hwp) == inst


def enter_table(hwp: Any, inst: str) -> None:
    """Enter the table found by identity and prove the caret is in it, with nothing selected."""
    _run(hwp, 'Cancel')
    control = table_controls(hwp).get(inst)
    if control is None:
        raise NativeNumberingRefusal('this table was not found among the document controls', code='target_table_not_entered')
    hwp.set_pos_by_set(control.GetAnchorPos(0))
    hwp.FindCtrl()
    _run(hwp, 'ShapeObjTextBoxEdit')
    if not _in_table(hwp, inst) or hwp.SelectionMode != 0:
        _run(hwp, 'Cancel')
        raise NativeNumberingRefusal('the caret did not enter this table in a plain state', code='target_table_not_entered')


def goto_cell(hwp: Any, inst: str, address: str, cell_count: int) -> None:
    """From the first cell, cell by cell, with the address read at each step. pyhwpx goto_addr keeps an address cache across edits, so it is not used."""
    _run(hwp, 'TableColBegin')
    _run(hwp, 'TableColPageUp')
    # A walk meets a tall cell once per row it covers, so this bound holds for any shape of table.
    for _ in range(cell_count * 2 + MAX_GRID + 2):
        if hwp.get_cell_addr() == address:
            break
        if _run(hwp, 'TableRightCell') is not True:
            raise NativeNumberingRefusal(f'the caret could not reach {address}', code='target_cell_unreachable')
    else:
        raise NativeNumberingRefusal(f'the caret could not reach {address}', code='target_cell_unreachable')
    if not _in_table(hwp, inst) or hwp.get_cell_addr() != address or hwp.SelectionMode != 0:
        raise NativeNumberingRefusal(f'the caret is not provably on {address} of this table', code='target_cell_unreachable')


def _block_moves(table: Mapping[str, Any], first: Mapping[str, Any], rect: Mapping[str, Any]) -> list[str]:
    """What walks a block from its first cell to its far corner, slot by slot as the native moves go."""
    moves: list[str] = []
    row, col = first['row'], first['col']
    cell = first
    while cell['col'] + cell['col_span'] - 1 < rect['right']:
        col = cell['col'] + cell['col_span']
        cell = _covering(table, row, col)
        moves.append('TableRightCell')
    while cell['row'] + cell['row_span'] - 1 < rect['bottom']:
        row = cell['row'] + cell['row_span']
        cell = _covering(table, row, col)
        moves.append('TableLowerCell')
    return moves


def select_block(hwp: Any, inst: str, table: Mapping[str, Any], rect: Mapping[str, Any]) -> None:
    cells = sorted(rect['cells'], key=lambda cell: (cell['row'], cell['col']))
    first = cells[0]
    goto_cell(hwp, inst, first['address'], len(table['cells']))
    _run(hwp, 'TableCellBlock')
    _run(hwp, 'TableCellBlockExtend')
    for move in _block_moves(table, first, rect):
        _run(hwp, move)
    selected = hwp.get_selected_range()
    if not isinstance(selected, list) or sorted(selected) != sorted(cell['address'] for cell in cells) or hwp.SelectionMode == 0 or _parent_inst(hwp) != inst:
        _run(hwp, 'Cancel')
        raise NativeNumberingRefusal('the block on screen is not exactly the requested cells', code='selection_unverified')


def _issue(reach: _MutationReach, call: Callable[[], Any], name: str) -> None:
    reach.issue()
    result = call()
    if not _action_confirmed(result):
        raise NativeNumberingMutationError(f'native {name} returned {_status_text(result)}; nothing was rolled back')


def _execute_border(hwp: Any, items: Mapping[str, int], reach: _MutationReach) -> None:
    action = hwp.CreateAction('CellBorder')
    pset = action.CreateSet()
    for name, value in items.items():
        pset.SetItem(name, value)
    _issue(reach, lambda: action.Execute(pset), 'CellBorder')


def _execute_fill(hwp: Any, fill: str, reach: _MutationReach) -> None:
    pset = hwp.HParameterSet.HCellBorderFill
    if not _action_confirmed(hwp.HAction.GetDefault('CellFill', pset.HSet)):
        _run(hwp, 'Cancel')
        raise NativeNumberingRefusal('the current cell fill could not be read', code='selection_unverified')
    attr = pset.FillAttr
    if fill == 'none':
        attr.type = 0
    else:
        attr.type = 1
        attr.WinBrushFaceColor = native_color(fill)
        attr.WinBrushHatchColor = HATCH_DEFAULT
        attr.WinBrushFaceStyle = -1
        attr.WindowsBrush = 1
    _issue(reach, lambda: hwp.HAction.Execute('CellFill', pset.HSet), 'CellFill')


def _execute_edit(hwp: Any, inst: str, table: Mapping[str, Any], plan: Mapping[str, Any], reach: _MutationReach) -> list[str]:
    native = plan['native']
    kind = native[0]
    if kind == 'merge':
        select_block(hwp, inst, table, plan['rect'])
        _issue(reach, lambda: _run(hwp, 'TableMergeCell'), 'TableMergeCell')
        return ['Run:TableMergeCell']
    if kind == 'delete_block':
        select_block(hwp, inst, table, plan['rect'])
        _issue(reach, lambda: hwp.TableDeleteCell(remain_cell=False), 'TableDeleteCell')
        return ['Run:TableDeleteCell']
    goto_cell(hwp, inst, plan['caret']['address'], len(table['cells']))
    if kind == 'delete_line':
        _issue(reach, lambda: _run(hwp, native[1]), native[1])
        return [f'Run:{native[1]}']
    if kind == 'insert':
        pset = hwp.HParameterSet.HTableInsertLine
        if not _action_confirmed(hwp.HAction.GetDefault('TableInsertRowColumn', pset.HSet)):
            raise NativeNumberingRefusal('the table insert settings could not be read', code='selection_unverified')
        pset.Side, pset.Count = native[1], native[2]
        _issue(reach, lambda: hwp.HAction.Execute('TableInsertRowColumn', pset.HSet), 'TableInsertRowColumn')
        return ['Execute:TableInsertRowColumn']
    _issue(reach, lambda: hwp.TableSplitCell(Rows=native[1], Cols=native[2], DistributeHeight=0, Merge=0), 'TableSplitCell')
    return ['Execute:TableSplitCell']


def _public_request(request: Mapping[str, Any], names: set[str] | None = None) -> dict[str, Any]:
    """What was asked for, in the caller's terms: a parsed cell goes back to its address."""
    return {key: cell_address(*value) if key in {'cell', 'end_cell'} else value for key, value in request.items() if key not in _BOUND_FIELDS and (names is None or key in names)}


def _report_table(table: Mapping[str, Any], page: int | None) -> dict[str, Any]:
    return {'table_id': table['table_id'], 'page': page, 'rows': table['rows'], 'cols': table['cols'], 'cell_count': len(table['cells']), 'expected_hash': table['expected_hash']}


def _state_sha(side: Mapping[str, Any]) -> str:
    return nf.state_digest(evidence_of(side['census'], side['controls']))


def _finish(hwp: Any, restore: Callable[[], None]) -> None:
    _run(hwp, 'Cancel')
    restore()
    if hwp.SelectionMode != 0:
        raise NativeNumberingMutationError('a block was left on screen after the table edit; nothing was rolled back')


def apply_edit(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int], restore: Callable[[], None], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = read()
        table = resolve_table(before['model'], request['table_id'])
        inst = table['inst_id']
        page = table_pages(hwp, page_of, only=inst).get(inst)
        require_binding(table, request, page)
        plan = plan_edit(table, request)
        enter_table(hwp, inst)
        native_actions = _execute_edit(hwp, inst, table, plan, reach)
        caret_address = hwp.get_cell_addr() if plan['kind'] == 'insert' else None
        _run(hwp, 'Cancel')
        after = read()
        new_table = next((item for item in after['model']['tables'] if item['table_id'] == table['table_id']), None)
        problems = edit_problems(table, plan, new_table)
        problems += document_problems(before, after, table, allowed_lists=_STRUCTURE_LISTS, removed_objects=tuple(plan.get('removed_objects', ())))
        if plan['kind'] == 'insert' and caret_address != cell_address(*plan['caret_after']):
            problems.append(f"the caret ended on {caret_address}, so the new lines are not where they were asked for")
        if problems:
            raise NativeNumberingMutationError('table edit outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        _finish(hwp, restore)
        return {
            'target': {'table_id': table['table_id'], 'page': page, 'cells': len(table['cells'])}, 'requested': _public_request(request),
            'before': _report_table(table, page), 'after': _report_table(new_table, page), 'changes': plan['changes'],
            'result': {'changed': True, 'status': 'edited', 'native_actions': native_actions},
            'undo': {'native_editing_actions': len(native_actions), 'pre_state_sha256': _state_sha(before), 'post_state_sha256': _state_sha(after)},
        }

    return _guarded(reach, body, 'table edit')


def apply_style(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int], restore: Callable[[], None], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = read()
        table = resolve_table(before['model'], request['table_id'])
        inst = table['inst_id']
        page = table_pages(hwp, page_of, only=inst).get(inst)
        require_binding(table, request, page)
        plan = plan_style(table, request)
        target = {'table_id': table['table_id'], 'page': page, 'scope': request['scope'], 'cells': len(plan['cells']), 'addresses': [cell['address'] for cell in plan['cells']][:MAX_REPORT_ROWS]}
        requested = _public_request(request, {'scope', 'cell', 'end_cell', 'borders', 'fill'})
        if not plan['border_items'] and plan['fill'] is None:
            return {'target': target, 'requested': requested, 'before': _report_table(table, page), 'after': _report_table(table, page), 'changes': plan['changes'],
                    'result': {'changed': False, 'status': 'already_set', 'native_actions': []}, 'undo': {'native_editing_actions': 0, 'pre_state_sha256': None, 'post_state_sha256': None}}
        native_actions: list[str] = []
        if plan['border_items']:
            enter_table(hwp, inst)
            select_block(hwp, inst, table, plan['rect'])
            _execute_border(hwp, plan['border_items'], reach)
            native_actions.append('Execute:CellBorder')
        if plan['fill'] is not None:
            # Entered and selected again: nothing the first action or its reads did to the caret is trusted.
            if reach.reached:
                try:
                    _run(hwp, 'Cancel')
                    enter_table(hwp, inst)
                    select_block(hwp, inst, table, plan['rect'])
                except NativeNumberingRefusal as exc:
                    raise NativeNumberingMutationError(f'the first of two actions ran and the second could not be aimed: {exc}; nothing was rolled back') from exc
            else:
                enter_table(hwp, inst)
                select_block(hwp, inst, table, plan['rect'])
            _execute_fill(hwp, plan['fill'], reach)
            native_actions.append('Execute:CellFill')
        _run(hwp, 'Cancel')
        after = read()
        new_table = next((item for item in after['model']['tables'] if item['table_id'] == table['table_id']), None)
        problems = style_problems(table, plan, new_table)
        problems += document_problems(before, after, table, allowed_lists=frozenset({'borderfilllist'}))
        if problems:
            raise NativeNumberingMutationError('table style outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        _finish(hwp, restore)
        return {'target': target, 'requested': requested, 'before': _report_table(table, page), 'after': _report_table(new_table, page), 'changes': plan['changes'],
                'result': {'changed': True, 'status': 'styled', 'native_actions': native_actions},
                'undo': {'native_editing_actions': len(native_actions), 'pre_state_sha256': _state_sha(before), 'post_state_sha256': _state_sha(after)}}

    return _guarded(reach, body, 'table style')


def apply_create(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int], restore: Callable[[], None], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = read()
        cells = request.get('cells')
        if cells is not None and (len(cells) != request['rows'] or any(len(row) != request['cols'] for row in cells)):
            raise NativeNumberingRefusal('cells must hold exactly rows rows of cols texts', code='cells_shape_mismatch')
        if cells is not None and not any(value.strip() for row in cells for value in row):
            raise NativeNumberingRefusal('cells holds no text; leave it out for an empty table', code='cells_shape_mismatch')
        anchor = _create_anchor(before['model'], request['match'])
        at = (0, anchor['body_index'], _anchor_end(anchor))
        hwp.set_pos(*at)
        page = page_of()
        if page != request['expected_page']:
            refusal = NativeNumberingRefusal('the paragraph is not on expected_page', code='target_wrong_page')
            refusal.found_page = page
            raise refusal
        reach.issue()
        result = hwp.create_table(request['rows'], request['cols'], True, header=False)
        if not _action_confirmed(result):
            raise NativeNumberingMutationError(f'native TableCreate returned {_status_text(result)}; nothing was rolled back')
        native_actions = ['Execute:TableCreate']
        filled = False
        if cells is not None:
            _fill_new_table(hwp, cells)
            filled = True
            native_actions.append('Fields:cell text')
        _run(hwp, 'Cancel')
        after = read()
        known = {item['table_id'] for item in before['model']['tables']}
        new_tables = [item for item in after['model']['tables'] if item['table_id'] not in known]
        problems = document_problems(before, after, None, allowed_lists=frozenset({'borderfilllist', 'charshapelist', 'parashapelist', 'facenamelist'}), allow_new_paragraphs=True, allow_new_table=True)
        if len(new_tables) != 1:
            problems.append('the document does not hold exactly one new table')
        else:
            problems += _new_table_problems(new_tables[0], request, cells)
            problems += new_table_paragraph_problems(before, after, anchor, new_tables[0]['inst_id'])
        if problems:
            raise NativeNumberingMutationError('table creation outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        new_inst = new_tables[0]['inst_id']
        new_page = table_pages(hwp, page_of, only=new_inst).get(new_inst)
        hwp.set_pos(*at)
        return {'target': {'kind': 'after_paragraph', 'paragraph': anchor['text'][:120], 'body_index': anchor['body_index'], 'page': page}, 'requested': {key: request.get(key) for key in ('rows', 'cols')},
                'table': public_summary(new_tables[0], new_page), 'result': {'changed': True, 'status': 'created', 'native_actions': native_actions},
                'changes': {'new_table': new_tables[0]['table_id'], 'cell_text_filled': filled},
                'undo': {'native_editing_actions': None if filled else 1, 'pre_state_sha256': None if filled else _state_sha(before), 'post_state_sha256': None if filled else _state_sha(after)}}

    return _guarded(reach, body, 'table creation')


def _create_anchor(model: Mapping[str, Any], match: str) -> Mapping[str, Any]:
    hits = [row for row in model['paragraphs'] if match in row['text']]
    if not hits:
        if any(match in text for text in model['header_footer_text']):
            raise NativeNumberingRefusal('the text is in a header or footer', code='target_in_header_footer')
        raise NativeNumberingRefusal('the text was not found in the document', code='target_not_found')
    if len(hits) > 1:
        raise NativeNumberingRefusal(f'the text occurs in {len(hits)} paragraphs; give more of it so that it identifies one', code='target_ambiguous')
    anchor = hits[0]
    if anchor['body_index'] is None:
        raise NativeNumberingRefusal('a new table goes after a body paragraph, and this text is inside a table or another object', code='target_not_in_body')
    if anchor['heading'] not in {'none', ''}:
        raise NativeNumberingRefusal('a new table takes the paragraph shape of the paragraph before it, so after a numbered paragraph it would take a number', code='target_numbered')
    return anchor


def _anchor_end(anchor: Mapping[str, Any]) -> int:
    return nf._end_position({'atoms': anchor['atoms']})  # noqa: SLF001


def _fill_new_table(hwp: Any, cells: list[list[str]]) -> None:
    """The existing native table path: fields over the whole table, written in cell order and then named away."""
    name = '__hwpx_table_cells__'
    _run(hwp, 'TableCellBlockExtendAbs')
    _run(hwp, 'TableCellBlockExtend')
    hwp.set_cur_field_name(name)
    try:
        hwp.put_field_text(name, [value for row in cells for value in row])
    finally:
        hwp.set_cur_field_name('')


def _new_table_problems(table: Mapping[str, Any], request: Mapping[str, Any], cells: list[list[str]] | None) -> list[str]:
    problems: list[str] = []
    if (table['rows'], table['cols']) != (request['rows'], request['cols']) or len(table['cells']) != request['rows'] * request['cols']:
        problems.append('the new table does not have the requested rows and columns')
    if cells is None:
        if not all(cell['blank'] for cell in table['cells']):
            problems.append('the new table is not empty')
    else:
        wanted = {cell_address(row + 1, col + 1): value for row, values in enumerate(cells) for col, value in enumerate(values)}
        if {cell['address']: cell['text'] for cell in table['cells']} != wanted:
            problems.append('the new table does not hold the requested text')
    return problems


def _guarded(reach: _MutationReach, body: Callable[[], dict[str, Any]], what: str) -> dict[str, Any]:
    try:
        return body()
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        raise NativeNumberingMutationError(
            f'{what} failed with an exception of class {_exception_class_label(exc)} after the native call was issued; the document state is not proven and nothing was rolled back'
        ) from exc


__all__ = [
    'CensusError', 'EDIT_ACTIONS', 'LINE_TYPES', 'LINE_WIDTHS', 'apply_create', 'apply_edit', 'apply_style', 'parse_table_model', 'public_summary', 'public_table', 'resolve_table',
    'review_rows', 'table_pages', 'validate_create_step', 'validate_edit_step', 'validate_inventory_step', 'validate_read_step', 'validate_style_step',
]
