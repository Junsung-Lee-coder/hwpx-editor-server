"""Character and paragraph formatting on one exact range, with a per-character model of the live document as its proof.

Native facts these rely on were measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2. A native position counts 8 for a tab
or an inline control and 1 for any other character. A set from ``CreateAction(name).CreateSet()`` holding only the requested
items changes only those items, on every run of a mixed block, and one ``Execute`` is one Undo step. ``ParagraphShape`` on a
bare caret merges into the previous caret edit of the same paragraph in the Undo history and on a block does not, so a
paragraph is always reached through a block: its text, or for a paragraph without text the block from its end to the next
paragraph's start, which leaves that next paragraph alone. Margins and non-percent line spacing in HParaShape are 200 units to
the point, as the saved layout and the PDF confirm; HWPX stores half of that in its ``hp:case`` branch.

A numbered paragraph's printed mark takes the character shape of the paragraph's last text run unless its definition names a
shape, so a block reaching the paragraph end changes the mark as well. A sparse ``CharShape`` on the bare caret at the paragraph
end writes a trailing empty run holding the old values without touching text; it prints, saves and reopens as the old mark and
is one more Undo step. Formatting the end again changes that run too, so the old values are written back each time. The
endpoints these commands accept for size, indents and line spacing were stored exactly, saved and reopened.

Before each command the target is found again from the document text and reselected, and the block's own HWPML2X must hold
exactly the requested text. After the action every character and paragraph is compared with what was requested; anything else
is an unknown outcome that is never rolled back.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from app.native_numbering import (
    ControlEnumerationLimit,
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    STEP_MATCH_MAX_CHARS,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _action_confirmed,
    _control_ids,
    _get_selection,
    _multiset_delta,
    _native_method,
    _read_target_state,
    _reject_unknown_keys,
    _require_confirm_layout,
    _require_motion_result,
    _set_pos,
    _status_text,
)
from app.native_numbering_census import (
    CensusError,
    MAX_HWPML_CHARS,
    PAGE_NUMBER_ELEMENTS,
    _DOCTYPE,
    _INLINE_CHARACTERS,
    _MAX_DEPTH,
    _Resolver,
    _Unresolved,
    _attr,
    _is_bare_page_restart,
    _lower_attrs,
    _name,
    parse_hwpml2x_census,
)
from app.native_paragraph_edit import _COMPARED, _exception_class_label, evidence_of

TEXT_MAX_CHARS = 4000
LANGUAGES = ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User')
_ALIGN_NATIVE = {'justify': 0, 'left': 1, 'right': 2, 'center': 3}
_ALIGN_PUBLIC = {'Justify': 'justify', 'Left': 'left', 'Right': 'right', 'Center': 'center', 'Distribute': 'distribute', 'DistributeSpace': 'distribute_space'}
_SPACING_NATIVE = {'percent': 0, 'fixed': 1, 'between_lines': 2, 'at_least': 3}
_SPACING_STORED = {'percent': 'Percent', 'fixed': 'Fixed', 'between_lines': 'BetweenLines', 'at_least': 'AtLeast'}
_SPACING_PUBLIC = {stored: public for public, stored in _SPACING_STORED.items()}
_PARA_UNITS_PER_POINT = 200
_INDENT_MAX_PT = 570.2
_LINE_MAX_PT = 841.8
_FIND_HITS_MAX = 64
_READBACK_RUN_TEXT = 200
_READBACK_RUNS = 100
_SECTION_CELL_CHAIN = frozenset({'body', 'section', 'p', 'text', 'table', 'row', 'cell', 'paralist'})
_CONTROL_WIDTH = 8
_CHARACTER_FIELDS = ('font_family', 'size_pt', 'bold', 'italic', 'underline', 'text_color')
_MARK_KINDS = {'number': 'number', 'bullet': 'bullet', 'outline': 'outline'}
_UNDERLINE_NATIVE = {'none': 0, 'bottom': 1, 'top': 3}
_UNSET_CHAR_SHAPE = '4294967295'
# The controls of a section's first paragraph, which a reader never sees as objects in its text.
_SECTION_CONTROLS = frozenset({'secdef', 'coldef', 'header', 'footer'})
_PARAGRAPH_FIELDS = ('align', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt', 'line_spacing_type', 'line_spacing_value')
_TARGET_FIELDS = ('match', 'through_match', 'paragraph_index', 'expected_text', 'expected_page')


class _ModelUnusable(CensusError):
    """The HWPML2X text does not resolve to characters with known shapes, so it cannot prove a formatting edit."""


def _number(value: Any, name: str) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise NativeNumberingInputError(f'{name} must be a finite number')
        return float(value)
    except OverflowError as exc:
        raise NativeNumberingInputError(f'{name} must be a finite number') from exc


def _tenths(value: Any, name: str, low: float, high: float, *, exclusive_low: bool = False) -> float:
    """Hancom's dialogs and the 1/100 pt and 1/200 pt fields hold these in steps of 0.1 pt; finer input would be silently rounded."""
    number = _number(value, name)
    if number > high or number < low or (exclusive_low and number == low) or abs(number * 10 - round(number * 10)) > 1e-9:
        raise NativeNumberingInputError(f'{name} must be {low} to {high} points in steps of 0.1')
    return round(number, 1)


def _validated_target(step: Mapping[str, Any], name: str) -> dict[str, Any]:
    _require_confirm_layout(step, name)
    expected_page = step.get('expected_page')
    if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page < 1:
        raise NativeNumberingInputError('expected_page must be a positive integer')
    match, through = step.get('match'), step.get('through_match')
    index, expected_text = step.get('paragraph_index'), step.get('expected_text')
    target: dict[str, Any] = {'expected_page': expected_page}
    if match is not None:
        if index is not None or expected_text is not None:
            raise NativeNumberingInputError('give match or paragraph_index with expected_text, not both')
        for label, value in (('match', match), ('through_match', through)):
            if value is None:
                continue
            # Never stripped: spaces and tabs are part of the requested range.
            if (not isinstance(value, str) or not value or len(value) > STEP_MATCH_MAX_CHARS or not value.strip(' \t\n  ')
                    or any(ord(char) < 32 and char not in '\t\n' for char in value)):
                raise NativeNumberingInputError(f'{label} must be exact text of at most {STEP_MATCH_MAX_CHARS} characters without other control characters')
        target.update(match=match, through_match=through)
        return target
    if through is not None:
        raise NativeNumberingInputError('through_match needs match')
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or not isinstance(expected_text, str) or len(expected_text) > TEXT_MAX_CHARS:
        raise NativeNumberingInputError('give match, or paragraph_index with the paragraph\'s full expected_text')
    target.update(paragraph_index=index, expected_text=expected_text)
    return target


def validate_character_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, _TARGET_FIELDS + _CHARACTER_FIELDS + ('number_mark',) + _STEP_ENVELOPE_KEYS, 'character_format_exact step')
    target = _validated_target(step, 'character_format_exact')
    number_mark = step.get('number_mark', 'preserve')
    if type(number_mark) is not str or number_mark not in {'preserve', 'follow'}:
        raise NativeNumberingInputError('number_mark must be preserve or follow')
    target['number_mark'] = number_mark
    wanted: dict[str, Any] = {}
    if step.get('font_family') is not None:
        face = step['font_family']
        if not isinstance(face, str) or not face.strip() or face != face.strip() or len(face) > 64 or face.startswith('@') or any(ord(char) < 32 for char in face):
            raise NativeNumberingInputError('font_family must be a font family name')
        wanted['font_family'] = face
    if step.get('size_pt') is not None:
        wanted['size_pt'] = _tenths(step['size_pt'], 'size_pt', 0.1, 4096.0)
    for name in ('bold', 'italic', 'underline'):
        if step.get(name) is not None:
            if type(step[name]) is not bool:
                raise NativeNumberingInputError(f'{name} must be true or false')
            wanted[name] = step[name]
    if step.get('text_color') is not None:
        color = step['text_color']
        if not isinstance(color, str) or re.fullmatch(r'#[0-9A-Fa-f]{6}', color) is None:
            raise NativeNumberingInputError('text_color must be #RRGGBB')
        wanted['text_color'] = color.upper()
    if not wanted:
        raise NativeNumberingInputError('character_format_exact needs at least one property')
    return {**target, 'properties': wanted}


def validate_paragraph_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, _TARGET_FIELDS + _PARAGRAPH_FIELDS + _STEP_ENVELOPE_KEYS, 'paragraph_format_exact step')
    target = _validated_target(step, 'paragraph_format_exact')
    wanted: dict[str, Any] = {}
    if step.get('align') is not None:
        if type(step['align']) is not str or step['align'] not in _ALIGN_NATIVE:
            raise NativeNumberingInputError('align must be left, center, right or justify')
        wanted['align'] = step['align']
    for name in ('left_indent_pt', 'right_indent_pt'):
        if step.get(name) is not None:
            wanted[name] = _tenths(step[name], name, 0.0, _INDENT_MAX_PT)
    if step.get('first_line_indent_pt') is not None:
        wanted['first_line_indent_pt'] = _tenths(step['first_line_indent_pt'], 'first_line_indent_pt', -_INDENT_MAX_PT, _INDENT_MAX_PT)
    kind, value = step.get('line_spacing_type'), step.get('line_spacing_value')
    if (kind is None) != (value is None):
        raise NativeNumberingInputError('line_spacing_type and line_spacing_value go together')
    if kind is not None:
        if type(kind) is not str or kind not in _SPACING_NATIVE:
            raise NativeNumberingInputError('line_spacing_type must be percent, fixed, between_lines or at_least')
        if kind == 'percent':
            if not _number(value, 'line_spacing_value').is_integer() or not 1 <= value <= 500:
                raise NativeNumberingInputError('a percent line spacing is a whole number from 1 to 500')
            wanted.update(line_spacing_type=kind, line_spacing_value=int(value))
        else:
            wanted.update(line_spacing_type=kind, line_spacing_value=_tenths(value, 'line_spacing_value', 0.0, _LINE_MAX_PT, exclusive_low=kind != 'between_lines'))
    if not wanted:
        raise NativeNumberingInputError('paragraph_format_exact needs at least one property')
    return {**target, 'properties': wanted}


def _hex_to_native(color: str) -> int:
    red, green, blue = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return red | green << 8 | blue << 16


def _character_items(wanted: Mapping[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = {}
    if 'font_family' in wanted:
        for language in LANGUAGES:
            items[f'FaceName{language}'] = wanted['font_family']
            items[f'FontType{language}'] = 1
    if 'size_pt' in wanted:
        items['Height'] = round(wanted['size_pt'] * 100)
    if 'bold' in wanted:
        items['Bold'] = 1 if wanted['bold'] else 0
    if 'italic' in wanted:
        items['Italic'] = 1 if wanted['italic'] else 0
    if 'underline' in wanted:
        items['UnderlineType'] = 1 if wanted['underline'] else 0
    if 'text_color' in wanted:
        items['TextColor'] = _hex_to_native(wanted['text_color'])
    return items


def _paragraph_items(wanted: Mapping[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = {}
    if 'align' in wanted:
        items['AlignType'] = _ALIGN_NATIVE[wanted['align']]
    for name, item in (('left_indent_pt', 'LeftMargin'), ('right_indent_pt', 'RightMargin'), ('first_line_indent_pt', 'Indentation')):
        if name in wanted:
            items[item] = round(wanted[name] * _PARA_UNITS_PER_POINT)
    if 'line_spacing_type' in wanted:
        items['LineSpacingType'] = _SPACING_NATIVE[wanted['line_spacing_type']]
        items['LineSpacing'] = wanted['line_spacing_value'] if wanted['line_spacing_type'] == 'percent' else round(wanted['line_spacing_value'] * _PARA_UNITS_PER_POINT)
    return items


# The document as characters with resolved shapes.

def _element_facts(element: ET.Element) -> dict[str, str]:
    return {key: value for key, value in _lower_attrs(element).items() if key != 'id'}


def _by_content(facts: dict[str, str], name: str, definitions: Mapping[str, str]) -> None:
    if name in facts:
        facts[name] = definitions.get(facts[name].strip(), facts[name])


def _char_props(element: ET.Element, fonts: Mapping[tuple[str, str], tuple[str, str]], fills: Mapping[str, str]) -> dict[str, Any]:
    props: dict[str, Any] = {'attrs': _element_facts(element), 'fonts': {}, 'font_types': {}, 'children': {}, 'bold': False, 'italic': False, 'underline': None}
    _by_content(props['attrs'], 'borderfillid', fills)
    for child in element:
        name = _name(child)
        if name == 'fontid':
            for language, ref in _lower_attrs(child).items():
                face = fonts.get((language, ref.strip()))
                if face is None:
                    raise _ModelUnusable('a character shape names a font that is not listed')
                props['fonts'][language], props['font_types'][language] = face
        elif name in {'bold', 'italic'}:
            props[name] = True
        elif name == 'underline':
            props['underline'] = _element_facts(child)
        else:
            props['children'][name] = _element_facts(child)
    return props


def _para_props(element: ET.Element, fills: Mapping[str, str], tabs: Mapping[str, str], numbers: Mapping[str, str]) -> dict[str, Any]:
    props: dict[str, Any] = {'attrs': _element_facts(element), 'children': {}}
    _by_content(props['attrs'], 'tabdef', tabs)
    _by_content(props['attrs'], 'heading', numbers)
    for child in element:
        props['children'][_name(child)] = _element_facts(child)
    if 'paramargin' not in props['children']:
        raise _ModelUnusable('a paragraph shape has no margin definition')
    if 'paraborder' in props['children']:
        _by_content(props['children']['paraborder'], 'borderfill', fills)
    return props


def _definition_digests(root: ET.Element, kind: str, resolver: _Resolver) -> dict[str, str]:
    """Definitions by content, so a renumbered one is not a changed one; one that cannot be resolved stays by its id."""
    digests: dict[str, str] = {}
    for element in root.iter():
        if _name(element) == kind and _attr(element, 'id') is not None:
            try:
                digests[_attr(element, 'id').strip()] = resolver.item_digest(element)
            except _Unresolved:
                continue
    return digests


def _shape_tables(root: ET.Element, resolver: _Resolver) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str], dict[str, tuple[str, str, str]]]:
    fills, tabs = _definition_digests(root, 'borderfill', resolver), _definition_digests(root, 'tabdef', resolver)
    numbers, bullets = _definition_digests(root, 'numbering', resolver), _definition_digests(root, 'bullet', resolver)
    headings: dict[str, tuple[str, str, str]] = {}
    fonts: dict[tuple[str, str], tuple[str, str]] = {}
    names: set[str] = set()
    for face in root.iter():
        if _name(face) != 'fontface':
            continue
        language = (_attr(face, 'lang') or '').strip().lower()
        for font in face:
            font_id, font_name = _attr(font, 'id'), _attr(font, 'name')
            if _name(font) == 'font' and font_id is not None and font_name is not None:
                fonts[(language, font_id.strip())] = (font_name, (_attr(font, 'type') or '').strip().lower())
                if (_attr(font, 'type') or '').strip().lower() == 'ttf':
                    names.add(font_name)
    chars: dict[str, dict[str, Any]] = {}
    paras: dict[str, dict[str, Any]] = {}
    for element in root.iter():
        kind = _name(element)
        if kind == 'charshape' and _attr(element, 'id') is not None:
            chars[_attr(element, 'id').strip()] = _char_props(element, fonts, fills)
        elif kind == 'parashape' and _attr(element, 'id') is not None:
            heading = (_attr(element, 'headingtype') or '').strip().lower()
            shape_id = _attr(element, 'id').strip()
            paras[shape_id] = _para_props(element, fills, tabs, bullets if heading == 'bullet' else numbers)
            headings[shape_id] = (heading, (_attr(element, 'level') or '0').strip(), (_attr(element, 'heading') or '').strip())
    return chars, paras, names, headings


def _object_digest(element: ET.Element, resolver: _Resolver) -> str | None:
    """An object by its resolved content, nested paragraphs apart, as the census sees it; a page number or bare restart is not one."""
    if _name(element) in PAGE_NUMBER_ELEMENTS or _is_bare_page_restart(element):
        return ''
    try:
        return resolver.digest(element, prune=True, strip=True, strip_restart=True)
    except _Unresolved:
        return None


def _atoms(paragraph: ET.Element, chars: Mapping[str, dict[str, Any]], resolver: _Resolver) -> list[dict[str, Any]]:
    """Everything in a paragraph in native order: one item per character or control, with its width and character shape."""
    atoms: list[dict[str, Any]] = []
    for run in paragraph:
        if _name(run) != 'text':
            continue
        ref = _attr(run, 'charshape')
        shape = chars.get(ref.strip()) if ref is not None else None
        if shape is None:
            raise _ModelUnusable('a text run has no resolvable character shape')

        def add(text: str | None) -> None:
            atoms.extend({'char': char, 'width': 1, 'shape': shape} for char in text or '')

        for child in run:
            if _name(child) != 'char':
                atoms.append({'control': _name(child), 'width': _CONTROL_WIDTH, 'shape': shape, 'digest': _object_digest(child, resolver)})
                add(child.tail)
                continue
            add(child.text)
            for inner in child:
                name = _name(inner)
                if name in _INLINE_CHARACTERS:
                    atoms.append({'char': _INLINE_CHARACTERS[name], 'width': _CONTROL_WIDTH if name == 'tab' else 1, 'shape': shape})
                else:
                    atoms.append({'control': name, 'width': None, 'shape': shape, 'digest': _object_digest(inner, resolver)})
                add(inner.tail)
            add(child.tail)
    return atoms


class _MarkDefinitions:
    """Where each numbered paragraph's mark takes its look from, read from the document's own definitions."""

    def __init__(self, root: ET.Element, chars: Mapping[str, dict[str, Any]]) -> None:
        self.chars = chars
        self.numberings: dict[str, dict[str, str | None]] = {}
        self.bullets: dict[str, str | None] = {}
        for element in root.iter():
            kind, ident = _name(element), _attr(element, 'id')
            if ident is None:
                continue
            heads = [item for item in element if _name(item) == 'parahead']
            if kind == 'numbering':
                self.numberings[ident.strip()] = {(_attr(item, 'level') or '').strip(): _attr(item, 'charshape') for item in heads}
            elif kind == 'bullet':
                self.bullets[ident.strip()] = _attr(heads[0], 'charshape') if heads else None

    def of(self, heading: tuple[str, str, str] | None, outline: str | None) -> dict[str, Any] | None:
        """None for a paragraph without a mark. A definition this code cannot find is 'unknown', never assumed to inherit."""
        if heading is None or heading[0] not in _MARK_KINDS:
            return None
        kind, level, reference = heading
        found = True
        named: str | None = None
        if kind == 'bullet':
            found = reference in self.bullets
            named = self.bullets.get(reference)
        else:
            levels = self.numberings.get(outline if kind == 'outline' else reference)
            number = str(int(level) + 1) if level.isdigit() else None
            found = levels is not None and number in levels
            named = levels.get(number) if found else None
        if not found:
            return {'kind': kind, 'source': 'unknown', 'shape': None}
        if named is None or named.strip() == _UNSET_CHAR_SHAPE:
            return {'kind': kind, 'source': 'paragraph_end', 'shape': None}
        shape = self.chars.get(named.strip())
        return {'kind': kind, 'source': 'definition', 'shape': shape} if shape is not None else {'kind': kind, 'source': 'unknown', 'shape': None}


def parse_format_model(text: str) -> dict[str, Any]:
    """The census's paragraphs, in the census's order, as characters with shapes, plus the words of headers and footers."""
    if type(text) is not str or len(text) > MAX_HWPML_CHARS or _DOCTYPE.search(text):
        raise _ModelUnusable('HWPML2X text is not usable as a document')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _ModelUnusable('HWPML2X text is not well-formed') from exc
    body = next((element for element in root.iter() if _name(element) == 'body'), None)
    if body is None:
        raise _ModelUnusable('HWPML2X text has no BODY')
    resolver = _Resolver(root, body)
    chars, paras, fonts, headings = _shape_tables(root, resolver)
    marks = _MarkDefinitions(root, chars)
    paragraphs: list[dict[str, Any]] = []
    margins: list[str] = []
    body_count = [0]

    def outside(element: ET.Element) -> None:
        for item in element.iter():
            if _name(item) == 'char':
                margins.append(''.join(item.itertext()))

    current_outline: list[str | None] = [None]

    def outline_of(_element: ET.Element, _chain: tuple[str, ...]) -> str | None:
        return current_outline[0]

    def visit(element: ET.Element, chain: tuple[str, ...], depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise _ModelUnusable('HWPML2X nesting is too deep')
        for child in element:
            name = _name(child)
            if name == 'section':
                current_outline[0] = next(((_attr(item, 'outlineshape') or '').strip() for item in child.iter() if _name(item) == 'secdef'), None)
            if name in {'header', 'footer'}:
                outside(child)
            elif name == 'p':
                ref = _attr(child, 'parashape')
                shape = paras.get(ref.strip()) if ref is not None else None
                if shape is None:
                    raise _ModelUnusable('a paragraph has no resolvable paragraph shape')
                in_body = _name(element) == 'section'
                context = 'body' if in_body else 'table_cell' if 'cell' in chain and set(chain) <= _SECTION_CELL_CHAIN else 'other'
                atoms = _atoms(child, chars, resolver)
                end = next((chars.get((_attr(run, 'charshape') or '').strip()) for run in reversed(list(child)) if _name(run) == 'text'), None)
                paragraphs.append({'index': len(paragraphs), 'context': context, 'body_index': body_count[0] if in_body else None, 'shape': shape, 'atoms': atoms,
                                   'text': ''.join(atom['char'] for atom in atoms if 'char' in atom), 'end_shape': end,
                                   'mark': marks.of(headings.get(ref.strip()), outline_of(element, chain))})
                if in_body:
                    body_count[0] += 1
                visit(child, chain + (name,), depth + 1)
            else:
                visit(child, chain + (name,), depth + 1)

    visit(body, ('body',), 0)
    return {'paragraphs': paragraphs, 'header_footer_text': margins, 'fonts': fonts}


def live_model_reader(hwp: Any) -> Callable[[], dict[str, Any]]:
    def read() -> dict[str, Any]:
        return parse_format_model(hwp.get_text_file('HWPML2X', ''))

    return read


def _color_text(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 0xFFFFFF:
        return None
    return f'#{number & 0xFF:02X}{number >> 8 & 0xFF:02X}{number >> 16 & 0xFF:02X}'


def _underline_type(props: Mapping[str, Any]) -> str:
    return ((props['underline'] or {}).get('type') or 'none').lower()


def public_character(props: Mapping[str, Any]) -> dict[str, Any]:
    fonts = props['fonts']
    distinct = set(fonts.values())
    height = props['attrs'].get('height')
    underline = _underline_type(props)
    return {
        'font_family': next(iter(distinct)) if len(distinct) == 1 else dict(fonts),
        'size_pt': int(height) / 100 if height is not None and height.strip().lstrip('-').isdigit() else None,
        'bold': props['bold'], 'italic': props['italic'],
        'underline': underline != 'none',
        'text_color': _color_text(props['attrs'].get('textcolor')),
        # underline=true means a bottom underline; any other position is named so it cannot pass for one.
        **({} if underline in {'none', 'bottom'} else {'underline_type': underline}),
    }


def _points(value: Any) -> float | None:
    try:
        return int(value) / _PARA_UNITS_PER_POINT
    except (TypeError, ValueError):
        return None


def public_paragraph(props: Mapping[str, Any]) -> dict[str, Any]:
    margin = props['children']['paramargin']
    kind = _SPACING_PUBLIC.get(margin.get('linespacingtype') or '')
    spacing = margin.get('linespacing')
    return {
        'align': _ALIGN_PUBLIC.get(props['attrs'].get('align') or ''),
        'left_indent_pt': _points(margin.get('left')), 'right_indent_pt': _points(margin.get('right')), 'first_line_indent_pt': _points(margin.get('indent')),
        'line_spacing_type': kind,
        'line_spacing_value': (int(spacing) if spacing is not None and spacing.lstrip('-').isdigit() else None) if kind == 'percent' else _points(spacing),
    }


def _short_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def _char_remainder(props: Mapping[str, Any]) -> str:
    """Everything in a character shape that public_character does not name, by content."""
    return _short_digest({'attrs': {key: value for key, value in props['attrs'].items() if key not in {'height', 'textcolor'}}, 'children': props['children']})


def _underline_style(props: Mapping[str, Any]) -> dict[str, str]:
    """An underline's shape and color exist only while it is on; turning it on or off is the named change."""
    if _underline_type(props) == 'none':
        return {}
    return {'underline_style': _short_digest({key: value for key, value in props['underline'].items() if key != 'type'})}


def _para_remainder(props: Mapping[str, Any]) -> str:
    """Everything in a paragraph shape that public_paragraph does not name, by content."""
    children = {name: {key: value for key, value in facts.items() if name != 'paramargin' or key not in {'left', 'right', 'indent', 'linespacing', 'linespacingtype'}} for name, facts in props['children'].items()}
    return _short_digest({'attrs': {key: value for key, value in props['attrs'].items() if key != 'align'}, 'children': children})


def _objects(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [atom for atom in atoms if 'control' in atom and atom['digest'] != '']


def public_mark(paragraph: Mapping[str, Any]) -> dict[str, Any] | None:
    """The look of the paragraph's printed mark as its definitions and last run determine it; not read from a rendering."""
    mark = paragraph['mark']
    if mark is None:
        return None
    shape = mark['shape'] if mark['source'] == 'definition' else paragraph['end_shape'] if mark['source'] == 'paragraph_end' else None
    return {'kind': mark['kind'], 'source': mark['source'], **(public_character(shape) if shape is not None else {})}


def _segments(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adjacent characters with one shape, in the terms a caller asked in."""
    rows: list[dict[str, Any]] = []
    current = None
    for atom in atoms:
        if 'char' not in atom:
            continue
        if current is not atom['shape']:
            current = atom['shape']
            rows.append({'text': '', **public_character(current), 'rest': _char_remainder(current), **_underline_style(current)})
        rows[-1]['text'] += atom['char']
    return rows


def readback_format(paragraph: Mapping[str, Any]) -> dict[str, Any]:
    """One paragraph for a review: within a fixed size, saying when it is cut short."""
    runs = _segments(paragraph['atoms'])
    objects = _objects(paragraph['atoms'])
    fact = {
        'runs': [{'text': row['text'][:_READBACK_RUN_TEXT], 'length': len(row['text']), **{key: value for key, value in row.items() if key != 'text'}} for row in runs[:_READBACK_RUNS]],
        'runs_truncated': len(runs) > _READBACK_RUNS,
        'paragraph': public_paragraph(paragraph['shape']),
        'paragraph_rest': _para_remainder(paragraph['shape']),
        'objects': [atom['control'] for atom in objects if atom['control'] not in _SECTION_CONTROLS],
        'objects_sha256': _short_digest([atom['digest'] for atom in objects]),
    }
    mark = public_mark(paragraph)
    if mark is not None:
        fact['mark'] = mark
    return fact


REPORT_ITEMS = 50
REPORT_TEXT = 200


def _bounded_rows(rows: list[dict[str, Any]], name: str, holder: dict[str, Any]) -> list[dict[str, Any]]:
    if len(rows) > REPORT_ITEMS:
        holder[f'{name}_cut'] = True
    return [{**row, **({'text': row['text'][:REPORT_TEXT]} if isinstance(row.get('text'), str) else {})} for row in rows[:REPORT_ITEMS]]


def public_report(step_result: Mapping[str, Any]) -> dict[str, Any]:
    """The range and values of one command for its caller, within fixed sizes, saying when it is cut short."""
    target = dict(step_result['target'])
    target['paragraphs'] = _bounded_rows(target['paragraphs'], 'paragraphs', target)
    result = dict(step_result['result'])
    if 'number_marks' in result:
        result['number_marks'] = _bounded_rows(result['number_marks'], 'number_marks', result)
    for side in ('before', 'after'):
        items: list[dict[str, Any]] = []
        for item in _bounded_rows(result[side], side, result):
            row = dict(item)
            if 'segments' in row:
                row['segments'] = _bounded_rows(row['segments'], 'segments', row)
            items.append(row)
        result[side] = items
    return {'target': target, 'requested': dict(step_result['requested']), 'result': result}


# The target.

def _hits(paragraphs: list[dict[str, Any]], phrase: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for paragraph in paragraphs:
        start = paragraph['text'].find(phrase)
        while start != -1:
            found.append((paragraph['index'], start))
            start = paragraph['text'].find(phrase, start + 1)
    return found


def _unique_hit(model: Mapping[str, Any], phrase: str) -> tuple[int, int]:
    hits = _hits(model['paragraphs'], phrase)
    if not hits:
        if any(phrase in text for text in model['header_footer_text']):
            raise NativeNumberingRefusal('the text is in a header or footer, which these commands do not edit', code='target_in_header_footer')
        raise NativeNumberingRefusal('the text was not found in the document', code='target_not_found')
    if len(hits) > 1:
        raise NativeNumberingRefusal(f'the text occurs {len(hits)} times; give more of it so that it identifies one place', code='target_ambiguous')
    return hits[0]


def _require_context(paragraph: Mapping[str, Any]) -> None:
    if paragraph['context'] == 'other':
        raise NativeNumberingRefusal('the text is inside an object other than a table cell, such as a text box, caption or note', code='target_unsupported_context')


def _char_indexes(paragraph: Mapping[str, Any]) -> list[int]:
    return [number for number, atom in enumerate(paragraph['atoms']) if 'char' in atom]


def _position_of(paragraph: Mapping[str, Any], atom_index: int) -> int:
    at = 0
    for atom in paragraph['atoms'][:atom_index]:
        if atom['width'] is None:
            raise NativeNumberingRefusal('an element of unknown native width comes before the text', code='target_unlocatable')
        at += atom['width']
    return at


def _end_position(paragraph: Mapping[str, Any]) -> int:
    return _position_of(paragraph, len(paragraph['atoms']))


def _resolve(model: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Rows of (paragraph, first and past-last atom) for the requested text, and what kind of target it was."""
    paragraphs = model['paragraphs']
    if 'paragraph_index' in request:
        index = request['paragraph_index']
        if index >= len(paragraphs):
            raise NativeNumberingRefusal('there is no paragraph with this index', code='target_not_found')
        paragraph = paragraphs[index]
        if paragraph['text'] != request['expected_text']:
            raise NativeNumberingRefusal('the paragraph at this index does not hold exactly expected_text', code='target_text_mismatch')
        _require_context(paragraph)
        chars = _char_indexes(paragraph)
        return [{'paragraph': paragraph, 'first': chars[0] if chars else None, 'last': chars[-1] + 1 if chars else None}], 'paragraph'
    first_index, first_offset = _unique_hit(model, request['match'])
    first = paragraphs[first_index]
    _require_context(first)
    last_index, last_offset, phrase = first_index, first_offset, request['match']
    if request.get('through_match') is not None:
        phrase = request['through_match']
        last_index, last_offset = _unique_hit(model, phrase)
        _require_context(paragraphs[last_index])
        if last_index != first_index and (first['context'] != 'body' or paragraphs[last_index]['context'] != 'body'):
            raise NativeNumberingRefusal('a range across paragraphs must stay in the body text', code='target_range_unsupported')
        if (last_index, last_offset + len(phrase)) <= (first_index, first_offset):
            raise NativeNumberingRefusal('through_match comes before match', code='target_range_unsupported')
    rows = []
    for index in range(first_index, last_index + 1):
        paragraph = paragraphs[index]
        chars = _char_indexes(paragraph)
        start = first_offset if index == first_index else 0
        stop = last_offset + len(phrase) if index == last_index else len(chars)
        rows.append({'paragraph': paragraph, 'first': chars[start] if start < len(chars) else None, 'last': chars[stop - 1] + 1 if stop else None})
    return rows, 'phrase'


def _require_no_control_inside(rows: list[dict[str, Any]]) -> None:
    for position, row in enumerate(rows):
        atoms = row['paragraph']['atoms']
        low = 0 if position else row['first'] or 0
        high = len(atoms) if position < len(rows) - 1 else row['last'] or 0
        if any('char' not in atom for atom in atoms[low:high]):
            raise NativeNumberingRefusal('the range would cross a table, picture or other object in the text', code='target_crosses_control')


def _native_list(hwp: Any, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """(list, first paragraph). A body paragraph has its census position; a cell's list is known only from a native search for its text."""
    head = rows[0]['paragraph']
    if head['context'] == 'body':
        return 0, head['body_index']
    wanted = head['text'] if len(head['text']) <= STEP_MATCH_MAX_CHARS else head['text'][:STEP_MATCH_MAX_CHARS]
    if not wanted.strip():
        raise NativeNumberingRefusal('a table cell paragraph without text cannot be located in the native document', code='target_unlocatable')
    mover = _native_method(hwp, 'MoveDocBegin')
    finder = _native_method(hwp, 'find')
    _require_motion_result(mover())
    found: list[tuple[int, int, int]] = []
    for _ in range(_FIND_HITS_MAX):
        matched = finder(wanted, direction='Forward', MatchCase=1, WholeWordOnly=0)
        if type(matched) is not bool:
            raise NativeNumberingRefusal('text search returned a non-Boolean result', code='target_unlocatable')
        if matched is False:
            break
        selection = _get_selection(hwp)
        if not selection or selection[0] != selection[3] or selection[1] != selection[4]:
            raise NativeNumberingRefusal('a text match could not be located by position', code='target_unlocatable')
        found.append((selection[0], selection[1], selection[2]))
        _set_pos(hwp, (selection[3], selection[4], selection[5]))
    else:
        raise NativeNumberingRefusal('text search did not terminate', code='target_unlocatable')
    cells = [hit for hit in found if hit[0] != 0 and hit[2] == _position_of(head, _char_indexes(head)[0])]
    if not cells:
        raise NativeNumberingRefusal('the table cell text could not be found in the native document', code='target_unlocatable')
    if len(cells) > 1:
        raise NativeNumberingRefusal('more than one table cell holds this whole text, so which one is meant cannot be proven', code='target_ambiguous')
    return cells[0][0], cells[0][1]


def _character_block(rows: list[dict[str, Any]], list_id: int, para: int) -> tuple[tuple[int, ...], list[str]]:
    first, last = rows[0], rows[-1]
    block = (list_id, para, _position_of(first['paragraph'], first['first']), list_id, para + len(rows) - 1, _position_of(last['paragraph'], last['last']))
    words = [''.join(atom['char'] for atom in row['paragraph']['atoms'][row['first'] or 0:row['last'] or 0]) for row in rows]
    return block, words


def _paragraph_block(rows: list[dict[str, Any]], list_id: int, para: int, model: Mapping[str, Any]) -> tuple[tuple[int, ...], list[str] | None]:
    """A block inside the target paragraph(s): text never crossing a control, or a paragraph's own end mark."""
    if len(rows) > 1:
        first_atoms = rows[0]['paragraph']['atoms']
        lead = next((number for number, atom in enumerate(first_atoms) if 'char' in atom), len(first_atoms))
        # A section's own controls come before the selected text and stay out of the block; anything a reader sees is refused.
        if lead == len(first_atoms) and lead or any(atom['control'] not in _SECTION_CONTROLS for atom in first_atoms[:lead]) or any('char' not in atom for atom in first_atoms[lead:]):
            raise NativeNumberingRefusal('a range of paragraphs holding a table, picture or other object is refused', code='target_has_controls')
        for row in rows[1:]:
            if any('char' not in atom for atom in row['paragraph']['atoms']):
                raise NativeNumberingRefusal('a range of paragraphs holding a table, picture or other object is refused', code='target_has_controls')
        end = _end_position(rows[-1]['paragraph'])
        return (list_id, para, _position_of(rows[0]['paragraph'], lead), list_id, para + len(rows) - 1, end), [row['paragraph']['text'] for row in rows]
    paragraph = rows[0]['paragraph']
    atoms = paragraph['atoms']
    first = next((number for number, atom in enumerate(atoms) if 'char' in atom), None)
    if first is None:
        following = next((item for item in model['paragraphs'] if paragraph['context'] == 'body' and item['body_index'] == paragraph['body_index'] + 1), None)
        if following is None:
            raise NativeNumberingRefusal('a paragraph without text and without a following paragraph cannot be selected', code='target_unselectable')
        return (list_id, para, _end_position(paragraph), list_id, para + 1, 0), None
    stop = first
    while stop < len(atoms) and 'char' in atoms[stop]:
        stop += 1
    return (list_id, para, _position_of(paragraph, first), list_id, para, _position_of(paragraph, stop)), [''.join(atom['char'] for atom in atoms[first:stop])]


def _block_words(hwp: Any) -> list[str] | None:
    """What the active block holds, in the notation of the census; None when the block cannot be read as a document."""
    try:
        census = parse_hwpml2x_census(hwp.get_text_file('HWPML2X', 'saveblock:true'))
    except CensusError:
        return None
    paragraphs = census['paragraphs']
    return None if paragraphs is None else [item['text'] for item in paragraphs]


def _require_page(found: int, expected: int) -> None:
    if found != expected:
        refusal = NativeNumberingRefusal('the target is not on expected_page', code='target_wrong_page')
        refusal.found_page = found
        raise refusal


def _select(hwp: Any, block: tuple[int, ...], words: list[str] | None, page_of: Callable[[], int], expected_page: int) -> None:
    list_id, start_para, start_pos, _end_list, end_para, end_pos = block
    _set_pos(hwp, (list_id, start_para, start_pos))
    _require_page(page_of(), expected_page)
    selected = hwp.select_text(start_para, start_pos, end_para, end_pos, list_id)
    if selected is not True or _get_selection(hwp) != block:
        raise NativeNumberingRefusal('the requested range could not be selected exactly', code='selection_unverified')
    if words is not None and _block_words(hwp) != words:
        raise NativeNumberingRefusal('the selected block does not hold exactly the requested text', code='selection_unverified')
    state = _read_target_state(hwp)
    if state is None or state[1] != block:
        raise NativeNumberingRefusal('the selection was not held after it was checked', code='selection_unverified')


def installed_font_families() -> set[str]:
    """Font families this Windows session offers Hancom, read on the calling thread; GDI lists vertical variants with a leading @."""
    import win32gui  # noqa: PLC0415 - Windows-only dependency of the locked runtime.

    names: set[str] = set()

    def collect(font: Any, _metric: Any, _kind: Any, _extra: Any) -> int:
        names.add(font.lfFaceName)
        return 1

    context = win32gui.GetDC(0)
    try:
        win32gui.EnumFontFamilies(context, None, collect, None)
    finally:
        win32gui.ReleaseDC(0, context)
    return {name for name in names if not name.startswith('@')}


def _require_font(face: str, model: Mapping[str, Any]) -> None:
    if face in model['fonts']:
        return
    try:
        available = installed_font_families()
    except Exception as exc:
        raise NativeNumberingRefusal('the fonts of this Windows session could not be listed, so this font cannot be proven available', code='font_availability_unknown') from exc
    if face not in available:
        # Hancom accepts any name and silently substitutes a font when it renders.
        raise NativeNumberingRefusal('this font is neither in the document nor offered by this Windows session', code='font_not_available')


def _char_after(before: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    expected = json.loads(json.dumps(before))
    if 'font_family' in wanted:
        expected['fonts'] = {language: wanted['font_family'] for language in expected['fonts']}
        expected['font_types'] = {language: 'ttf' for language in expected['font_types']}
    if 'size_pt' in wanted:
        expected['attrs']['height'] = str(round(wanted['size_pt'] * 100))
    if 'text_color' in wanted:
        expected['attrs']['textcolor'] = str(_hex_to_native(wanted['text_color']))
    for name in ('bold', 'italic'):
        if name in wanted:
            expected[name] = wanted[name]
    return expected


def _same_character(actual: Mapping[str, Any], expected: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    left, right = dict(actual), dict(expected)
    underline_left, underline_right = left.pop('underline'), right.pop('underline')
    if left != right:
        return False
    if 'underline' not in wanted:
        return underline_left == underline_right
    on = underline_left is not None and (underline_left.get('type') or '').lower() not in {'', 'none'}
    if on != wanted['underline']:
        return False
    # Turning it on keeps a shape or color the run already carried, and a run without one takes the native defaults.
    return not on or (underline_left.get('type') or '').lower() == 'bottom' and (
        underline_right is None or all(underline_left.get(key) == underline_right.get(key) for key in ('shape', 'color')))


def _para_after(before: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    expected = json.loads(json.dumps(before))
    margin = expected['children']['paramargin']
    if 'align' in wanted:
        expected['attrs']['align'] = {value: key for key, value in _ALIGN_PUBLIC.items()}[wanted['align']]
    for name, field in (('left_indent_pt', 'left'), ('right_indent_pt', 'right'), ('first_line_indent_pt', 'indent')):
        if name in wanted:
            margin[field] = str(round(wanted[name] * _PARA_UNITS_PER_POINT))
    if 'line_spacing_type' in wanted:
        margin['linespacingtype'] = _SPACING_STORED[wanted['line_spacing_type']]
        margin['linespacing'] = str(wanted['line_spacing_value'] if wanted['line_spacing_type'] == 'percent' else round(wanted['line_spacing_value'] * _PARA_UNITS_PER_POINT))
    return expected


def _already_character(rows: list[dict[str, Any]], wanted: Mapping[str, Any]) -> bool:
    for row in rows:
        for atom in row['paragraph']['atoms'][row['first'] or 0:row['last'] or 0]:
            public = public_character(atom['shape'])
            if any(public.get(key) != value for key, value in wanted.items() if key != 'underline'):
                return False
            if 'underline' in wanted and _underline_type(atom['shape']) != ('bottom' if wanted['underline'] else 'none'):
                return False
    return True


def _already_paragraph(rows: list[dict[str, Any]], wanted: Mapping[str, Any]) -> bool:
    return all(_para_after(row['paragraph']['shape'], wanted) == row['paragraph']['shape'] for row in rows)


def _reaches_end(row: Mapping[str, Any], *, last: bool) -> bool:
    """Whether the block runs through this paragraph's last run; only then does the paragraph end take the new values."""
    return not last or (bool(row['paragraph']['atoms']) and row['last'] == len(row['paragraph']['atoms']))


def _inherits_mark(paragraph: Mapping[str, Any]) -> bool:
    return paragraph['mark'] is not None and paragraph['mark']['source'] != 'definition'


def _restore_items(end: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    """The native items that put the paragraph end's old values back, only for what the formatting will change there."""
    items: dict[str, Any] = {}
    after = _char_after(end, wanted)
    if 'font_family' in wanted and after['fonts'] != end['fonts']:
        if set(end['font_types'].values()) != {'ttf'}:
            raise NativeNumberingRefusal('the number mark uses a font that cannot be written back; give number_mark=follow or end the range earlier', code='target_changes_number_mark')
        for language in LANGUAGES:
            items[f'FaceName{language}'] = end['fonts'][language.lower()]
            items[f'FontType{language}'] = 1
    if 'size_pt' in wanted and after['attrs'].get('height') != end['attrs'].get('height'):
        items['Height'] = int(end['attrs']['height'])
    if 'text_color' in wanted and after['attrs'].get('textcolor') != end['attrs'].get('textcolor'):
        items['TextColor'] = int(end['attrs']['textcolor'])
    for name, item in (('bold', 'Bold'), ('italic', 'Italic')):
        if name in wanted and after[name] != end[name]:
            items[item] = 1 if end[name] else 0
    if 'underline' in wanted and _underline_type(end) != ('bottom' if wanted['underline'] else 'none'):
        kind = _underline_type(end)
        if kind not in _UNDERLINE_NATIVE:
            raise NativeNumberingRefusal('the number mark has an underline that cannot be written back; give number_mark=follow or end the range earlier', code='target_changes_number_mark')
        items['UnderlineType'] = _UNDERLINE_NATIVE[kind]
    return items


def _mark_restore(rows: list[dict[str, Any]], wanted: Mapping[str, Any], policy: str) -> dict[str, Any] | None:
    """What must be written back at the paragraph end to keep the printed mark, or a refusal when one more action cannot do it."""
    plans = []
    for position, row in enumerate(rows):
        paragraph = row['paragraph']
        if _inherits_mark(paragraph) and paragraph['end_shape'] is not None and _reaches_end(row, last=position == len(rows) - 1):
            items = _restore_items(paragraph['end_shape'], wanted) if policy == 'preserve' else {}
            plans.append({'paragraph': paragraph, 'items': items})
    if policy == 'follow':
        return None
    plans = [plan for plan in plans if plan['items']]
    if len(rows) > 1 and plans:
        raise NativeNumberingRefusal('this range would change the number mark of a paragraph it runs through; format the paragraphs one at a time or give number_mark=follow', code='target_changes_number_mark')
    return plans[0] if plans else None


def _mark_report(rows: list[dict[str, Any]], before: Mapping[str, Any], after: Mapping[str, Any], kept: bool) -> list[dict[str, Any]]:
    """Each numbered target paragraph's mark before and after, from the document itself."""
    report = []
    for row in rows:
        index = row['paragraph']['index']
        old, new = public_mark(before['paragraphs'][index]), public_mark(after['paragraphs'][index])
        if old is None:
            continue
        item = {'paragraph_index': index, 'kind': old['kind'], 'source': old['source'], 'before': old, 'after': new, 'changed': old != new}
        if kept:
            item['kept_by'] = 'paragraph_end_action'
        report.append(item)
    return report


def _targeted_atoms(rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    return {row['paragraph']['index']: (row['first'] or 0, row['last'] or 0) for row in rows}


def _document_problems(before: Mapping[str, Any], after: Mapping[str, Any], rows: list[dict[str, Any]], *, allowed_lists: set[str]) -> list[str]:
    """The census facts outside the shape definitions the command may add to or empty."""
    census_before, census_after = before['census'], after['census']
    problems = [f'{field} changed' for field in (*_COMPARED, 'frame') if census_before.get(field) != census_after.get(field)]
    for name in sorted(set(census_before['definitions']) | set(census_after['definitions'])):
        removed, added = _multiset_delta(census_before['definitions'].get(name, []), census_after['definitions'].get(name, []))
        if (removed or added) and name not in allowed_lists:
            problems.append(f'{name} changed')
    if before['controls'] != after['controls']:
        problems.append('the document control sequence changed')
    old, new = census_before['paragraphs'], census_after['paragraphs']
    if len(old) != len(new):
        problems.append('the paragraph count changed')
        return problems
    targets = {row['paragraph']['index'] for row in rows}
    for number, (left, right) in enumerate(zip(old, new)):
        fields = ('text', 'heading', 'level', 'section', 'numbering_ref_sha256', 'inline_controls', 'page_break', 'column_break')
        if any(left.get(key) != right.get(key) for key in fields) or (number not in targets and (
                left.get('content_sha256') != right.get('content_sha256') or left.get('end_charshape_sha256') != right.get('end_charshape_sha256'))):
            problems.append(f'paragraph {number} outside the request changed')
    return problems


def _mark_problems(rows: list[dict[str, Any]], after: list[dict[str, Any]], wanted: Mapping[str, Any], policy: str) -> list[str]:
    """A mark that inherits the paragraph end stays as it was, unless the caller asked it to follow a range reaching that end."""
    problems: list[str] = []
    for position, row in enumerate(rows):
        paragraph = row['paragraph']
        if not _inherits_mark(paragraph):
            continue
        reached = _reaches_end(row, last=position == len(rows) - 1)
        expected = _char_after(paragraph['end_shape'], wanted) if policy == 'follow' and reached and paragraph['end_shape'] is not None else paragraph['end_shape']
        end = after[paragraph['index']]['end_shape']
        if end is None or expected is None or not _same_character(end, expected, wanted if policy == 'follow' and reached else {}):
            problems.append(f"paragraph {paragraph['index']} has a number mark other than requested")
    return problems


def _character_problems(before: Mapping[str, Any], after: Mapping[str, Any], rows: list[dict[str, Any]], wanted: Mapping[str, Any], policy: str = 'preserve') -> list[str]:
    problems = _document_problems(before, after, rows, allowed_lists={'charshapelist', 'facenamelist'})
    old, new = before['model']['paragraphs'], after['model']['paragraphs']
    if len(old) != len(new):
        return problems
    problems += _mark_problems(rows, new, wanted, policy)
    spans = _targeted_atoms(rows)
    for left, right in zip(old, new):
        if left['shape'] != right['shape']:
            problems.append(f"paragraph {left['index']} changed its paragraph shape")
        if len(left['atoms']) != len(right['atoms']):
            problems.append(f"paragraph {left['index']} changed its content")
            continue
        low, high = spans.get(left['index'], (0, 0))
        # A block starting at a section's first text takes the section's leading controls into its run (measured); they show nothing.
        lead = low if high > low and low and all(atom.get('control') in _SECTION_CONTROLS for atom in left['atoms'][:low]) else 0
        for number, (one, two) in enumerate(zip(left['atoms'], right['atoms'])):
            if one.get('char') != two.get('char') or one.get('control') != two.get('control'):
                problems.append(f"paragraph {left['index']} changed its content")
                break
            inside = low <= number < high or number < lead
            expected = _char_after(one['shape'], wanted) if inside else one['shape']
            if not _same_character(two['shape'], expected, wanted if inside else {}):
                problems.append(f"paragraph {left['index']} has a character format other than requested")
                break
    return problems


def _paragraph_problems(before: Mapping[str, Any], after: Mapping[str, Any], rows: list[dict[str, Any]], wanted: Mapping[str, Any]) -> list[str]:
    problems = _document_problems(before, after, rows, allowed_lists={'parashapelist'})
    old, new = before['model']['paragraphs'], after['model']['paragraphs']
    if len(old) != len(new):
        return problems
    targets = {row['paragraph']['index'] for row in rows}
    for left, right in zip(old, new):
        if left['end_shape'] != right['end_shape']:
            problems.append(f"paragraph {left['index']} changed its last run")
        expected = _para_after(left['shape'], wanted) if left['index'] in targets else left['shape']
        if right['shape'] != expected:
            problems.append(f"paragraph {left['index']} has a paragraph format other than requested")
        if len(left['atoms']) != len(right['atoms']) or any(
                one.get('char') != two.get('char') or one.get('control') != two.get('control') or one['shape'] != two['shape'] for one, two in zip(left['atoms'], right['atoms'])):
            problems.append(f"paragraph {left['index']} changed its characters")
    return problems


def state_digest(evidence: Mapping[str, Any]) -> str:
    """One value for what Undo and Redo are compared with, so a later history step can say whether it reached this state."""
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=list).encode('utf-8', 'surrogatepass')).hexdigest()


def _read(hwp: Any, read: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    state = read()
    try:
        controls = [name for _control, name in _control_ids(hwp)]
    except ControlEnumerationLimit as exc:
        raise NativeNumberingRefusal('the document controls could not be enumerated', code='control_enumeration_unavailable') from exc
    return {**state, 'controls': controls}


def _state_sha(side: Mapping[str, Any]) -> str:
    return state_digest(evidence_of(side['census'], side['controls']))


def _character_report(rows: list[dict[str, Any]], model: Mapping[str, Any]) -> list[dict[str, Any]]:
    report = []
    for row in rows:
        index = row['paragraph']['index']
        segments = _segments(model['paragraphs'][index]['atoms'][row['first'] or 0:row['last'] or 0])
        report.append({'paragraph_index': index, 'segments': [{key: value for key, value in item.items() if key not in {'rest', 'underline_style'}} for item in segments]})
    return report


def _paragraph_report(rows: list[dict[str, Any]], model: Mapping[str, Any]) -> list[dict[str, Any]]:
    report = []
    for row in rows:
        paragraph = model['paragraphs'][row['paragraph']['index']]
        report.append({'paragraph_index': paragraph['index'], 'text': paragraph['text'][:120], 'paragraph': public_paragraph(paragraph['shape'])})
    return report


def apply_format(
    hwp: Any,
    *,
    kind: str,
    request: Mapping[str, Any],
    read: Callable[[], Mapping[str, Any]],
    page_of: Callable[[], int],
    restore: tuple[int, int, int],
    on_mutating: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Find the range again, select and prove it, issue one action and compare the whole document with what was requested."""
    character = kind == 'character'
    action_name = 'CharShape' if character else 'ParagraphShape'
    wanted = request['properties']
    policy = request.get('number_mark', 'preserve')
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = _read(hwp, read)
        rows, target_kind = _resolve(before['model'], request)
        if character:
            if rows[0]['first'] is None or rows[-1]['last'] is None:
                raise NativeNumberingRefusal('the target has no text to format', code='target_empty')
            _require_no_control_inside(rows)
            if 'font_family' in wanted:
                _require_font(wanted['font_family'], before['model'])
            already = _already_character(rows, wanted)
            restoring = None if already else _mark_restore(rows, wanted, policy)
        else:
            already = _already_paragraph(rows, wanted)
            restoring = None
        list_id, para = _native_list(hwp, rows)
        block, words = _character_block(rows, list_id, para) if character else _paragraph_block(rows, list_id, para, before['model'])
        target = {
            'kind': target_kind,
            'native_block': list(block),
            'paragraphs': [{'paragraph_index': row['paragraph']['index'], 'context': row['paragraph']['context'],
                            'text': row['paragraph']['text'][:120]} for row in rows],
        }
        report = _character_report if character else _paragraph_report
        marks = (lambda later, kept=False: _mark_report(rows, before['model'], later, kept)) if character else (lambda later, kept=False: [])
        if already:
            # The caller gives back what it found, as it does after a refusal.
            _set_pos(hwp, (list_id, para, block[2]))
            _require_page(page_of(), request['expected_page'])
            return {'changed': False, 'status': 'already_set', 'target': target, 'requested': dict(wanted), 'before': report(rows, before['model']), 'after': report(rows, before['model']),
                    'number_marks': marks(before['model']), 'native_actions': [], 'pre_state_sha256': None, 'post_state_sha256': None}
        _select(hwp, block, words, page_of, request['expected_page'])
        creator = _native_method(hwp, 'CreateAction')
        action = creator(action_name)
        pset = action.CreateSet()
        for name, value in (_character_items(wanted) if character else _paragraph_items(wanted)).items():
            pset.SetItem(name, value)
        reach.issue()
        result = action.Execute(pset)
        if not _action_confirmed(result):
            raise NativeNumberingMutationError(f'native {action_name} returned {_status_text(result)}; nothing was rolled back')
        native_actions = [f'Execute:{action_name}']
        if restoring is not None:
            # The block ends here. On the bare caret at the paragraph's end no text can take these values.
            end_at = (list_id, para + len(rows) - 1, _end_position(restoring['paragraph']))
            _set_pos(hwp, end_at)
            if _read_target_state(hwp) != (end_at, ()):
                raise NativeNumberingMutationError('the caret did not reach the paragraph end after the formatting; nothing was rolled back')
            restore_action = creator('CharShape')
            restore_set = restore_action.CreateSet()
            for name, value in restoring['items'].items():
                restore_set.SetItem(name, value)
            restored = restore_action.Execute(restore_set)
            native_actions.append('Execute:CharShape:paragraph-end')
            if not _action_confirmed(restored):
                raise NativeNumberingMutationError(f'native CharShape at the paragraph end returned {_status_text(restored)}; nothing was rolled back')
        after = _read(hwp, read)
        problems = _character_problems(before, after, rows, wanted, policy) if character else _paragraph_problems(before, after, rows, wanted)
        if problems:
            raise NativeNumberingMutationError(f'{kind} formatting outcome is not as requested: ' + '; '.join(problems[:5]) + '; nothing was rolled back')
        _set_pos(hwp, restore)
        if _get_selection(hwp) != ():
            raise NativeNumberingMutationError('the selection did not end after the formatting; nothing was rolled back')
        return {'changed': True, 'status': 'formatted', 'target': target, 'requested': dict(wanted), 'before': report(rows, before['model']), 'after': report(rows, after['model']),
                'number_marks': marks(after['model'], restoring is not None), 'native_actions': native_actions, 'pre_state_sha256': _state_sha(before), 'post_state_sha256': _state_sha(after)}

    try:
        return body()
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        raise NativeNumberingMutationError(
            f'{kind} formatting failed with an exception of class {_exception_class_label(exc)} after the native call was issued; '
            'the document state is not proven and nothing was rolled back'
        ) from exc
