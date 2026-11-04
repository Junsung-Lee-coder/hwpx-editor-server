"""Existing headers and footers: an inventory, a binding by section and page scope, and exact native edits inside one of them.

Measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2 (owned documents, saved, reopened and printed to PDF):
- every header and footer is a native list of its own. List ids follow document order: list 1 belongs to the section
  definition, then each header, footer and table cell takes the next id where it stands. SetPos(list, 0, 0) puts the
  caret inside one without opening an editing view, and ParentCtrl then names 'head' or 'foot'. SetPos to a list id the
  document does not have returns True and leaves the caret where it was, so the caret is read back every time;
- inside such a list the same native actions as in the body edit its text and format: InsertText at a bare caret and
  Delete of a selected block (one Undo step each), CharShape and ParagraphShape over a selected block (one each). The page
  number field next to the edited text stays as it was. SetPos back to the body leaves the area, with nothing open;
- the HeaderFooter action over an existing header replaces it with a new empty one, and HeaderFooterModify with the caret
  at the header's anchor returns False, so neither is used;
- a section without a header or footer of its own shows the ones of the section before it.
The document's own HWPML2X text is the proof of every outcome; nothing here writes the file.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from app import native_format as nf, native_table as nt, native_text_edit as nte
from app.native_numbering import (
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    NativeNumberingReadFailure,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _action_confirmed,
    _get_pos as _native_pos,
    _get_selection as _native_selection,
    _multiset_delta,
    _native_method,
    _reject_unknown_keys,
    _require_confirm_layout,
    _sdk_read,
    _set_pos,
    _status_text,
)
from app.native_numbering_census import MAX_HWPML_CHARS, _DOCTYPE, _MAX_DEPTH, _Resolver, _Unresolved, _attr, _name
from app.native_paragraph_edit import _COMPARED

KINDS = ('header', 'footer')
SCOPES = ('both', 'odd', 'even')
_NATIVE_HOLDER = {'header': 'head', 'footer': 'foot'}
_HASH_HEX = 24
_BINDING_FIELDS = ('kind', 'section', 'apply_to', 'expected_area_hash', 'expected_affected_sections')
_TEXT_FIELDS = ('old_text', 'new_text', 'match', 'occurrence', 'expected_occurrences', 'paragraph_index', 'expected_text', 'format_from', 'confirm_layout')
_TARGET_FIELDS = ('match', 'paragraph_index', 'expected_text', 'confirm_layout')
_CHARACTER_FIELDS = ('font_family', 'size_pt', 'bold', 'italic', 'underline', 'text_color')
_PARAGRAPH_FIELDS = ('align', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt', 'line_spacing_type', 'line_spacing_value')
# A header or footer of a document holds at most a few lists' worth of text; the scan stops at the target.
_LIST_SCAN_MARGIN = 16
_LIST_SCAN_MAX = 20000
_REPORT_TEXT = 300
_PAGE_FIELDS = {'page': 'page_number', 'totalpage': 'total_pages'}
_MAY_EMPTY = frozenset({'charshapelist', 'facenamelist'})


# Requests.

def _binding(step: Mapping[str, Any], op: str) -> dict[str, Any]:
    _require_confirm_layout(step, op)
    kind, section, scope, expected = (step.get(name) for name in _BINDING_FIELDS[:4])
    if type(kind) is not str or kind not in KINDS:
        raise NativeNumberingInputError('kind must be header or footer')
    if type(section) is not int or not 0 <= section <= 10000:
        raise NativeNumberingInputError('section must be the section number header_footer_inventory reports, from 0')
    if type(scope) is not str or scope not in SCOPES:
        raise NativeNumberingInputError('apply_to must be both, odd or even')
    if type(expected) is not str or len(expected) != len('sha256:') + _HASH_HEX or not expected.startswith('sha256:') \
            or any(char not in '0123456789abcdef' for char in expected[len('sha256:'):]):
        raise NativeNumberingInputError('expected_area_hash must be the hash header_footer_inventory reported for this area')
    binding = {'kind': kind, 'section': section, 'apply_to': scope, 'expected_area_hash': expected}
    if 'expected_affected_sections' in step:
        affected = step['expected_affected_sections']
        if (type(affected) is not list or not 1 <= len(affected) <= 10001
                or any(type(item) is not int or not 0 <= item <= 10000 for item in affected)
                or affected != sorted(set(affected))):
            raise NativeNumberingInputError('expected_affected_sections must be a nonempty ascending unique array of exact section integers 0..10000')
        binding['expected_affected_sections'] = list(affected)
    return binding


def validate_inventory_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, _STEP_ENVELOPE_KEYS, 'header_footer_inventory step')
    return {}


def validate_text_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """The body command's own validation of the text fields, with a page that a header or footer does not have."""
    _reject_unknown_keys(step, (*_BINDING_FIELDS, *_TEXT_FIELDS, *_STEP_ENVELOPE_KEYS), 'header_footer_text_exact step')
    binding = _binding(step, 'header_footer_text_exact')
    text = {key: value for key, value in step.items() if key in _TEXT_FIELDS}
    request = nte.validate_replace_step({**text, 'expected_page': 1})
    request.pop('expected_page')
    return {**binding, **request}


def validate_format_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_BINDING_FIELDS, *_TARGET_FIELDS, *_CHARACTER_FIELDS, *_PARAGRAPH_FIELDS, *_STEP_ENVELOPE_KEYS), 'header_footer_format_exact step')
    binding = _binding(step, 'header_footer_format_exact')
    target = {key: step[key] for key in ('match', 'paragraph_index', 'expected_text') if step.get(key) is not None}
    character = {key: step[key] for key in _CHARACTER_FIELDS if step.get(key) is not None}
    paragraph = {key: step[key] for key in _PARAGRAPH_FIELDS if step.get(key) is not None}
    if not character and not paragraph:
        raise NativeNumberingInputError('header_footer_format_exact needs at least one character or paragraph property')
    request: dict[str, Any] = {**binding, 'character': None, 'paragraph': None}
    checked = None
    if character:
        checked = nf.validate_character_step({**target, **character, 'expected_page': 1, 'confirm_layout': True})
        request['character'] = checked['properties']
    if paragraph:
        checked = nf.validate_paragraph_step({**target, **paragraph, 'expected_page': 1, 'confirm_layout': True})
        request['paragraph'] = checked['properties']
    request.update({key: checked[key] for key in ('match', 'paragraph_index', 'expected_text') if key in checked})
    if checked.get('through_match') is not None:
        raise NativeNumberingInputError('a header or footer range stays in one paragraph; through_match is not taken')
    return request


# The document's areas.

def _area_hash(kind: str, section: int, scope: str, digest: str) -> str:
    value = json.dumps([kind, section, scope, digest], ensure_ascii=False)
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()[:_HASH_HEX]


def _fields_of(element: ET.Element, paragraphs: list[ET.Element]) -> list[dict[str, Any]]:
    fields = []
    for index, paragraph in enumerate(paragraphs):
        for item in paragraph.iter():
            if _name(item) == 'autonum':
                kind = (_attr(item, 'numbertype') or '').strip().lower()
                fields.append({'kind': _PAGE_FIELDS.get(kind, kind or 'number'), 'paragraph_index': index})
    return fields


def parse_areas(text: str) -> dict[str, Any]:
    """Every header and footer in document order, each with its section, page scope, paragraphs as the format model reads them, and hash."""
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
    chars, paras, _fonts, headings = nf._shape_tables(root, resolver)  # noqa: SLF001
    areas: list[dict[str, Any]] = []
    sections = [-1]

    def area(element: ET.Element, kind: str, section: int) -> dict[str, Any]:
        scope = (_attr(element, 'applypagetype') or '').strip().lower()
        lists = [child for child in element if _name(child) == 'paralist']
        if scope not in SCOPES or len(lists) != 1:
            raise nf._ModelUnusable('a header or footer has no page scope or no single paragraph list')  # noqa: SLF001
        elements = [child for child in lists[0] if _name(child) == 'p']
        paragraphs = []
        for index, item in enumerate(elements):
            ref = (_attr(item, 'parashape') or '').strip()
            shape = paras.get(ref)
            if shape is None:
                raise nf._ModelUnusable('a header or footer paragraph has no resolvable paragraph shape')  # noqa: SLF001
            atoms = nf._atoms(item, chars, resolver)  # noqa: SLF001
            end = next((chars.get((_attr(run, 'charshape') or '').strip()) for run in reversed(list(item)) if _name(run) == 'text'), None)
            nested = any(_name(inner) == 'p' for inner in item.iter() if inner is not item)
            paragraphs.append({'index': index, 'context': 'other' if nested else 'body', 'body_index': index, 'shape': shape, 'atoms': atoms,
                               'text': ''.join(atom['char'] for atom in atoms if 'char' in atom), 'end_shape': end,
                               'heading': headings.get(ref, ('', '0', ''))[0], 'mark': None})
        try:
            digest = resolver.digest(element)
        except _Unresolved as exc:
            raise nf._ModelUnusable('a header or footer does not resolve') from exc  # noqa: SLF001
        return {'order': len(areas), 'kind': kind, 'section': section, 'apply_to': scope, 'area_id': f'{kind}:s{section}:{scope}',
                'hash': _area_hash(kind, section, scope, digest), 'digest': digest, 'paragraphs': paragraphs, 'fields': _fields_of(element, elements)}

    def visit(element: ET.Element, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise nf._ModelUnusable('HWPML2X nesting is too deep')  # noqa: SLF001
        for child in element:
            name = _name(child)
            if name == 'section':
                sections[0] += 1
                visit(child, depth + 1)
            elif name in KINDS:
                areas.append(area(child, name, max(sections[0], 0)))
            else:
                visit(child, depth + 1)

    visit(body, 0)
    lists = sum(1 for element in body.iter() if _name(element) == 'paralist')
    return {'areas': areas, 'sections': max(sections[0] + 1, 1), 'list_bound': min(lists + _LIST_SCAN_MARGIN, _LIST_SCAN_MAX)}


def _own(model: Mapping[str, Any], kind: str, section: int) -> list[dict[str, Any]]:
    return [item for item in model['areas'] if item['kind'] == kind and item['section'] == section]


def _inherited_from(model: Mapping[str, Any], kind: str, section: int) -> int | None:
    """The section whose areas of this kind a section without any of its own shows (measured); None when it has its own or none comes before."""
    if _own(model, kind, section):
        return None
    for earlier in range(section - 1, -1, -1):
        if _own(model, kind, earlier):
            return earlier
    return None


def _shown_in(model: Mapping[str, Any], found: Mapping[str, Any]) -> list[int]:
    shown = [found['section']]
    for later in range(found['section'] + 1, model['sections']):
        if _inherited_from(model, found['kind'], later) != found['section']:
            break
        shown.append(later)
    return shown


def _public_paragraph(paragraph: Mapping[str, Any]) -> dict[str, Any]:
    segments = [{key: value for key, value in item.items() if key not in {'rest', 'underline_style'}} for item in nf._segments(paragraph['atoms'])]  # noqa: SLF001
    return {'paragraph_index': paragraph['index'], 'text': paragraph['text'][:_REPORT_TEXT], 'segments': segments, 'paragraph': nf.public_paragraph(paragraph['shape']),
            'controls': [atom['control'] for atom in paragraph['atoms'] if 'char' not in atom]}


def public_area(model: Mapping[str, Any], found: Mapping[str, Any]) -> dict[str, Any]:
    return {'area_id': found['area_id'], 'kind': found['kind'], 'section': found['section'], 'apply_to': found['apply_to'], 'hash': found['hash'],
            'text': '\n'.join(item['text'] for item in found['paragraphs'])[:_REPORT_TEXT], 'paragraphs': [_public_paragraph(item) for item in found['paragraphs']],
            'fields': list(found['fields']), 'shown_in_sections': _shown_in(model, found)}


def inventory(model: Mapping[str, Any]) -> dict[str, Any]:
    sections = []
    for section in range(model['sections']):
        sections.append({'section': section, **{kind: {'own': [item['apply_to'] for item in _own(model, kind, section)], 'inherited_from': _inherited_from(model, kind, section)}
                                                for kind in KINDS}})
    areas = [public_area(model, item) for item in model['areas']]
    return {'count': len(areas), 'areas': areas, 'sections': sections}


def resolve_area(model: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    kind, section, scope = request['kind'], request['section'], request['apply_to']
    if section >= model['sections']:
        raise NativeNumberingRefusal(f"the document has {model['sections']} section(s), numbered from 0", code='area_not_found')
    own = _own(model, kind, section)
    found = [item for item in own if item['apply_to'] == scope]
    if len(found) > 1:
        raise NativeNumberingRefusal(f'section {section} has {len(found)} {kind}s for {scope} pages, so which one is meant cannot be proven', code='area_ambiguous')
    if not found:
        if own:
            scopes = ', '.join(item['apply_to'] for item in own)
            raise NativeNumberingRefusal(f'section {section} has its own {kind} for {scopes} pages only, not for {scope} pages; name one of those', code='area_not_found')
        source = _inherited_from(model, kind, section)
        if source is not None:
            scopes = ', '.join(item['apply_to'] for item in _own(model, kind, source))
            raise NativeNumberingRefusal(f'section {section} has no {kind} of its own; its pages show the {kind} of section {source} ({scopes}). Edit that one with '
                                         f'section {source}, which changes every section that shows it', code='area_inherited')
        raise NativeNumberingRefusal(f'section {section} has no {kind}, and no section before it has one; this command edits only an existing {kind}', code='area_not_found')
    if found[0]['hash'] != request['expected_area_hash']:
        raise NativeNumberingRefusal('this area is not as it was when it was listed; list it again with header_footer_inventory', code='area_hash_mismatch')
    affected = _shown_in(model, found[0])
    expected_affected = request.get('expected_affected_sections')
    if expected_affected is None and len(affected) > 1:
        raise NativeNumberingRefusal('this area is shared; explicitly acknowledge every shown section with expected_affected_sections', code='affected_sections_required')
    if expected_affected is not None and expected_affected != affected:
        raise NativeNumberingRefusal('the acknowledged affected sections do not match the current full section set; list the areas again', code='affected_sections_mismatch')
    return found[0]


# The native list. An entered malformed read is not a target refusal: it stops before restoration or diagnostics.

def _get_pos(hwp: Any) -> tuple[int, int, int]:
    try:
        return _native_pos(hwp)
    except NativeNumberingRefusal as exc:
        raise NativeNumberingReadFailure('native area caret is not canonical', code='target_unlocatable') from exc


def _get_selection(hwp: Any) -> tuple[int, ...]:
    value = _native_selection(hwp)
    if value is None:
        raise NativeNumberingReadFailure('native area selection is not canonical', code='selection_unverified')
    return value


def _state(hwp: Any) -> tuple[tuple[int, int, int], tuple[int, ...]]:
    caret = _get_pos(hwp)
    selection = _get_selection(hwp)
    if _get_pos(hwp) != caret:
        raise NativeNumberingReadFailure('native area caret drifted during its read', code='selection_unverified')
    return caret, selection


def _cancel(hwp: Any) -> None:
    if _sdk_read(lambda: hwp.HAction.Run('Cancel')) is not True:
        raise NativeNumberingReadFailure('native area Cancel was not confirmed', code='selection_unverified')


def _words(hwp: Any) -> list[str]:
    value = nf._block_words(hwp)  # noqa: SLF001
    if value is None:
        raise NativeNumberingReadFailure('native area block is unreadable', code='selection_unverified')
    return value


def _select_area(hwp: Any, block: tuple[int, ...], words: list[str] | None, holder: str, *, mismatch_code: str = 'selection_unverified') -> None:
    lid, para, first, _lid, last_para, last = block
    _set_pos(hwp, (lid, para, first))
    selected = _sdk_read(lambda: hwp.select_text(para, first, last_para, last, lid))
    if selected is not True:
        raise NativeNumberingReadFailure('native area block selection was not confirmed', code='selection_unverified')
    if _get_selection(hwp) != block:
        raise NativeNumberingReadFailure('native area block identity is unproven', code='selection_unverified')
    if words is not None and _words(hwp) != words:
        raise NativeNumberingRefusal('native area block does not hold the requested words', code=mismatch_code)
    if _holder(hwp) != holder or _state(hwp)[1] != block:
        raise NativeNumberingReadFailure('native area scope or selection was lost', code='selection_unverified')


def _caret_at(hwp: Any, at: tuple[int, int, int], holder: str) -> None:
    _set_pos(hwp, at)
    if _holder(hwp) != holder or _state(hwp) != (at, ()):
        raise NativeNumberingReadFailure('native area caret or scope is unproven', code='selection_unverified')


def _holder(hwp: Any) -> str | None:
    parent = _sdk_read(lambda: hwp.ParentCtrl)
    if parent is None:
        return None
    value = _sdk_read(lambda: parent.CtrlID)
    if type(value) is not str or not value:
        raise NativeNumberingReadFailure('native ParentCtrl.CtrlID is not canonical', code='selection_unverified')
    return value


def _area_list(hwp: Any, model: Mapping[str, Any], found: Mapping[str, Any]) -> int:
    """The native list of the area: the n-th header or footer list in id order is the n-th area in document order, proven kind by kind and by its words."""
    wanted = model['areas'][:found['order'] + 1]
    lists: list[tuple[int, str]] = []
    for list_id in range(1, model['list_bound'] + 1):
        _set_pos(hwp, (list_id, 0, 0))
        if _get_pos(hwp)[0] != list_id:
            continue
        holder = _holder(hwp)
        if holder in _NATIVE_HOLDER.values():
            lists.append((list_id, holder))
            if len(lists) == len(wanted):
                break
    if len(lists) != len(wanted) or any(holder != _NATIVE_HOLDER[item['kind']] for (_list, holder), item in zip(lists, wanted)):
        raise NativeNumberingRefusal('the native lists of the headers and footers do not match the document text, so this area cannot be located', code='area_unlocatable')
    list_id = lists[-1][0]
    paragraphs = found['paragraphs']
    last = len(paragraphs) - 1
    end = nf._end_position(paragraphs[last])  # noqa: SLF001
    _select_area(hwp, (list_id, 0, 0, list_id, last, end), [item['text'] for item in paragraphs], _NATIVE_HOLDER[found['kind']], mismatch_code='area_unlocatable')
    _cancel(hwp)
    return list_id


def _leave(hwp: Any, caret: tuple[int, int, int]) -> None:
    """Back to where the caller had the caret: out of the area, with nothing selected."""
    _set_pos(hwp, caret)
    if _state(hwp) != (caret, ()):
        raise NativeNumberingMutationError('the caret did not go back to where it was after the edit; nothing was rolled back')


# Proof.

def _document_problems(before: Mapping[str, Any], after: Mapping[str, Any], found: Mapping[str, Any], grows: frozenset[str]) -> list[str]:
    """Everything but the one area is as it was; definitions may only gain in the lists the action writes, and lose a shape no text uses any more."""
    old, new = before['census'], after['census']
    problems = [f'{field} changed' for field in sorted((set(old) | set(new)) - {'header_footer', 'definitions'}) if old.get(field) != new.get(field)]
    for name in sorted(set(old['definitions']) | set(new['definitions'])):
        removed, added = _multiset_delta(old['definitions'].get(name, []), new['definitions'].get(name, []))
        if (added and name not in grows) or (removed and name not in _MAY_EMPTY | grows):
            problems.append(f'{name} changed')
    if before['controls'] != after['controls']:
        problems.append('the document control sequence changed')
    if before['model']['paragraphs'] != after['model']['paragraphs']:
        problems.append('the body or a table changed')
    areas, later = before['areas']['areas'], after['areas']['areas']
    if len(areas) != len(later) or len(old['header_footer'] or []) != len(new['header_footer'] or []):
        return [*problems, 'the set of headers and footers changed']
    for one, two in zip(areas, later):
        if (one['kind'], one['section'], one['apply_to']) != (two['kind'], two['section'], two['apply_to']):
            problems.append('the set of headers and footers changed')
        elif one['order'] != found['order'] and one['digest'] != two['digest']:
            problems.append(f"{one['area_id']} changed")
    if len(later[found['order']]['paragraphs']) != len(found['paragraphs']):
        problems.append('the area changed its paragraphs')
    return problems


def _other_paragraphs_kept(found: Mapping[str, Any], later: Mapping[str, Any], target: int) -> list[str]:
    for one, two in zip(found['paragraphs'], later['paragraphs']):
        if one['index'] != target and (one['atoms'] != two['atoms'] or one['shape'] != two['shape'] or one['end_shape'] != two['end_shape']):
            return [f"paragraph {one['index']} of the area changed"]
    return []


def _same_atom(want: Mapping[str, Any], got: Mapping[str, Any]) -> bool:
    if want.get('char') != got.get('char') or want.get('control') != got.get('control'):
        return False
    return 'control' not in want or (want.get('digest') == got.get('digest') and want.get('data') == got.get('data'))


def _text_problems(before: Mapping[str, Any], after: Mapping[str, Any], found: Mapping[str, Any], row: Mapping[str, Any], new: str, shape: Mapping[str, Any]) -> list[str]:
    problems = _document_problems(before, after, found, frozenset())
    later = after['areas']['areas'][found['order']] if len(after['areas']['areas']) > found['order'] else None
    if later is None or problems:
        return problems or ['the area is gone']
    target = row['paragraph']['index']
    problems += _other_paragraphs_kept(found, later, target)
    left, right = found['paragraphs'][target], later['paragraphs'][target]
    expected = [*left['atoms'][:row['first']], *({'char': char, 'shape': shape} for char in new), *left['atoms'][row['last']:]]
    if left['shape'] != right['shape'] or left['end_shape'] != right['end_shape']:
        problems.append('the paragraph changed its paragraph format or its last run')
    if len(expected) != len(right['atoms']) or any(not _same_atom(want, got) or want['shape'] != got['shape'] for want, got in zip(expected, right['atoms'])):
        problems.append('the paragraph does not hold exactly the old text with the range replaced in the expected format')
    return problems


def _format_problems(before: Mapping[str, Any], after: Mapping[str, Any], found: Mapping[str, Any], row: Mapping[str, Any],
                     character: Mapping[str, Any] | None, paragraph: Mapping[str, Any] | None) -> list[str]:
    grows = frozenset(({'charshapelist', 'facenamelist'} if character else set()) | ({'parashapelist'} if paragraph else set()))
    problems = _document_problems(before, after, found, grows)
    later = after['areas']['areas'][found['order']] if len(after['areas']['areas']) > found['order'] else None
    if later is None or problems:
        return problems or ['the area is gone']
    target = row['paragraph']['index']
    problems += _other_paragraphs_kept(found, later, target)
    left, right = found['paragraphs'][target], later['paragraphs'][target]
    expected_shape = nf._para_after(left['shape'], paragraph) if paragraph else left['shape']  # noqa: SLF001
    if right['shape'] != expected_shape:
        problems.append('the paragraph has a paragraph format other than requested')
    if len(left['atoms']) != len(right['atoms']):
        return [*problems, 'the paragraph changed its content']
    low, high = (row['first'] or 0, row['last'] or 0) if character else (0, 0)
    lead, trail = nf._joined(left['atoms'], low, high)  # noqa: SLF001
    for number, (one, two) in enumerate(zip(left['atoms'], right['atoms'])):
        if not _same_atom(one, two):
            return [*problems, 'the paragraph changed its content']
        inside = character is not None and (low <= number < high + trail or number < lead)
        expected = nf._char_after(one['shape'], character) if inside else one['shape']  # noqa: SLF001
        if not nf._same_character(two['shape'], expected, character if inside else {}):  # noqa: SLF001
            return [*problems, 'the paragraph has a character format other than requested']
    reaches_end = character is not None and high + trail >= len(left['atoms'])
    ends = [left['end_shape']]
    if reaches_end and left['end_shape'] is not None:
        ends.append(nf._char_after(left['end_shape'], character))  # noqa: SLF001
    if right['end_shape'] != left['end_shape'] and not (reaches_end and any(item is not None and nf._same_character(right['end_shape'], item, character) for item in ends[1:])):  # noqa: SLF001
        problems.append('the paragraph changed its last run')
    return problems


# Preservation reads. Keep this N1 policy local: entered unprovable control
# enumeration is UNKNOWN, not authority to restore the caret or reread evidence.
def _read(hwp: Any, read: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    try:
        return nf._read(hwp, read)  # noqa: SLF001
    except NativeNumberingReadFailure:
        raise
    except NativeNumberingRefusal as exc:
        if exc.code == 'control_enumeration_unavailable':
            raise NativeNumberingReadFailure('Header/footer preservation controls are unproven; no further native calls are permitted.', code='control_enumeration_unavailable') from exc
        raise


# Edits.

def _require_plain(paragraph: Mapping[str, Any]) -> None:
    if paragraph['context'] != 'body':
        raise NativeNumberingRefusal('the text is inside an object in the header or footer, such as a table or text box', code='target_unsupported_context')
    if paragraph['heading'] in {'number', 'bullet', 'outline'}:
        raise NativeNumberingRefusal('a numbered or bulleted paragraph in a header or footer is not edited by this command', code='target_numbered')


def _require_no_field_inside(row: Mapping[str, Any]) -> None:
    crossed = [atom['control'] for atom in row['paragraph']['atoms'][row['first'] or 0:row['last'] or 0] if 'char' not in atom]
    if 'autonum' in crossed:
        raise NativeNumberingRefusal('the range would cross the page number field; end it before the field or start after it', code='target_crosses_control')


def _area_report(model: Mapping[str, Any], found: Mapping[str, Any], later: Mapping[str, Any] | None) -> dict[str, Any]:
    return {'area_id': found['area_id'], 'kind': found['kind'], 'section': found['section'], 'apply_to': found['apply_to'], 'hash_before': found['hash'],
            'hash_after': later['hash'] if later is not None else found['hash'], 'shown_in_sections': _shown_in(model, found)}


def apply_text(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    """Insert the new text right after the old range and delete the old range, as the body command does, inside the area's own list."""
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        caret = _state(hwp)
        if caret is None or caret[1] != ():
            raise NativeNumberingRefusal('the caret could not be read plainly before the edit', code='selection_unverified')
        before = _read(hwp, read)
        found = resolve_area(before['areas'], request)
        local = {'paragraphs': found['paragraphs'], 'header_footer_text': []}
        row, number, count = nte._resolve(local, request)  # noqa: SLF001
        paragraph = row['paragraph']
        _require_plain(paragraph)
        _require_no_field_inside(row)
        shape = nte._require_range(row, request['format_from'])  # noqa: SLF001
        list_id = _area_list(hwp, before['areas'], found)
        para = paragraph['index']
        first, last = nf._position_of(paragraph, row['first']), nf._position_of(paragraph, row['last'])  # noqa: SLF001
        block = (list_id, para, first, list_id, para, last)
        holder = _NATIVE_HOLDER[found['kind']]
        _select_area(hwp, block, [request['old_text']], holder)
        _caret_at(hwp, (list_id, para, last), holder)
        reach.issue()
        nte._insert(hwp, request['new_text'])  # noqa: SLF001
        _select_area(hwp, block, [request['old_text']], holder)
        nte._delete(hwp)  # noqa: SLF001
        after = _read(hwp, read)
        problems = _text_problems(before, after, found, row, request['new_text'], shape)
        if problems:
            raise NativeNumberingMutationError('header or footer text replacement outcome is not as requested: ' + '; '.join(problems[:5]) + '; nothing was rolled back')
        _leave(hwp, caret[0])
        later = after['areas']['areas'][found['order']]
        new_text = later['paragraphs'][para]['text']
        return {
            'area': _area_report(before['areas'], found, later),
            'target': {'paragraph_index': para, 'occurrence': number, 'occurrences': count, 'start': nf._char_indexes(paragraph).index(row['first'])},  # noqa: SLF001
            'requested': {'old_text': request['old_text'], 'new_text': request['new_text'], 'format_from': request['format_from']},
            'result': {'changed': True, 'status': 'replaced', 'paragraph_before': paragraph['text'][:_REPORT_TEXT], 'paragraph_after': new_text[:_REPORT_TEXT],
                       'format': nf.public_character(shape), 'native_actions': ['Execute:InsertText', 'Run:Delete'], 'fields_kept': list(later['fields'])},
            'caret': list(caret[0]),
            'undo': {'native_editing_actions': 2, 'pre_state_sha256': nf._state_sha(before), 'post_state_sha256': nf._state_sha(after)},  # noqa: SLF001
        }

    return nt._guarded(reach, body, 'header or footer text replacement')  # noqa: SLF001


def _execute(hwp: Any, name: str, items: Mapping[str, Any]) -> None:
    action = _native_method(hwp, 'CreateAction')(name)
    pset = _sdk_read(lambda: action.CreateSet())
    for key, value in items.items():
        _sdk_read(lambda: pset.SetItem(key, value))
    result = action.Execute(pset)
    if not _action_confirmed(result):
        raise NativeNumberingMutationError(f'native {name} returned {_status_text(result)}; nothing was rolled back')


def _format_report(paragraph: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    segments = nf._segments(paragraph['atoms'][row['first'] or 0:row['last'] or 0])  # noqa: SLF001
    return {'text': ''.join(item['text'] for item in segments)[:_REPORT_TEXT], 'segments': [{key: value for key, value in item.items() if key not in {'rest', 'underline_style'}} for item in segments],
            'paragraph': nf.public_paragraph(paragraph['shape'])}


def apply_format(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    """Character format over the text and paragraph format of its paragraph, each by its one native action over a proven block."""
    reach = _MutationReach(on_mutating)
    character, paragraph_wanted = request['character'], request['paragraph']

    def body() -> dict[str, Any]:
        caret = _state(hwp)
        if caret is None or caret[1] != ():
            raise NativeNumberingRefusal('the caret could not be read plainly before the edit', code='selection_unverified')
        before = _read(hwp, read)
        found = resolve_area(before['areas'], request)
        local = {'paragraphs': found['paragraphs'], 'header_footer_text': []}
        rows, _kind = nf._resolve(local, {key: request[key] for key in ('match', 'paragraph_index', 'expected_text') if key in request})  # noqa: SLF001
        row = rows[0]
        paragraph = row['paragraph']
        _require_plain(paragraph)
        need_character = False
        if character:
            if row['first'] is None or row['last'] is None:
                raise NativeNumberingRefusal('the target has no text to format', code='target_empty')
            _require_no_field_inside(row)
            nf._require_no_control_inside(rows)  # noqa: SLF001
            if 'font_family' in character:
                nf._require_font(character['font_family'], before['model'])  # noqa: SLF001
            need_character = not nf._already_character(rows, character)  # noqa: SLF001
            if need_character:
                nf._require_measured_neighbours(rows)  # noqa: SLF001
        need_paragraph = bool(paragraph_wanted) and not nf._already_paragraph(rows, paragraph_wanted)  # noqa: SLF001
        target = {'paragraph_index': paragraph['index'], 'text': paragraph['text'][:_REPORT_TEXT]}
        requested = {'character': dict(character) if character else None, 'paragraph': dict(paragraph_wanted) if paragraph_wanted else None}
        if not need_character and not need_paragraph:
            return {'area': _area_report(before['areas'], found, None), 'target': target, 'requested': requested,
                    'result': {'changed': False, 'status': 'already_set', 'before': _format_report(paragraph, row), 'after': _format_report(paragraph, row), 'native_actions': []},
                    'caret': list(caret[0]), 'undo': {'native_editing_actions': 0, 'pre_state_sha256': None, 'post_state_sha256': None}}
        list_id = _area_list(hwp, before['areas'], found)
        holder = _NATIVE_HOLDER[found['kind']]
        actions: list[str] = []
        if need_character:
            block, words = nf._character_block(rows, list_id, paragraph['index'])  # noqa: SLF001
            _select_area(hwp, block, words, holder)
            reach.issue()
            _execute(hwp, 'CharShape', nf._character_items(character))  # noqa: SLF001
            actions.append('Execute:CharShape')
            _cancel(hwp)
        if need_paragraph:
            block, words = nf._paragraph_block(rows, list_id, paragraph['index'], local)  # noqa: SLF001
            _select_area(hwp, block, words, holder)
            reach.issue()
            _execute(hwp, 'ParagraphShape', nf._paragraph_items(paragraph_wanted))  # noqa: SLF001
            actions.append('Execute:ParagraphShape')
            _cancel(hwp)
        after = _read(hwp, read)
        problems = _format_problems(before, after, found, row, character if need_character else None, paragraph_wanted if need_paragraph else None)
        if problems:
            raise NativeNumberingMutationError('header or footer formatting outcome is not as requested: ' + '; '.join(problems[:5]) + '; nothing was rolled back')
        _leave(hwp, caret[0])
        later = after['areas']['areas'][found['order']]
        return {'area': _area_report(before['areas'], found, later), 'target': target, 'requested': requested,
                'result': {'changed': True, 'status': 'formatted', 'before': _format_report(paragraph, row), 'after': _format_report(later['paragraphs'][paragraph['index']], row),
                           'native_actions': actions},
                'caret': list(caret[0]),
                'undo': {'native_editing_actions': len(actions), 'pre_state_sha256': nf._state_sha(before), 'post_state_sha256': nf._state_sha(after)}}  # noqa: SLF001

    return nt._guarded(reach, body, 'header or footer formatting')  # noqa: SLF001
