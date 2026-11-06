"""Existing native named fields: an inventory by name and occurrence, and filling one existing click-here field.

These are the document's own data fields (click-here fields, 누름틀, and fields named on table cells or text boxes), not
the literal [[BR::NAME]] placeholders of the authoring templates, and not the temporary cell-address fields the table
helpers use internally.

Official desktop contract (HwpAutomation_2504, pp. 16-19; ParameterSetTable_2504 49 FieldCtrl):
- GetFieldList(number, option) is BSTR: names joined by 0x02, none after the last. number 1 (hwpFieldNumber) suffixes
  each with ``{{n}}``, counted from 0 per name in document order; number 2 (hwpFieldCount) gives each name once with
  ``{{count}}``. option 0 is every field, 1 cell fields, 2 click-here fields, 8 text box fields.
- GetFieldText(fieldlist) is BSTR. ``name{{n}}`` is the n-th field of that name. A missing field and a field without
  text both give an empty string, so existence is proven from the list, never from the text. A tab is 0x09 and a
  paragraph break CR/LF. The manual marks the end of each field's text with 0x02; on Hancom 13.0.0.3457 a read of one
  field returned its text with no 0x02 after it (tests/fixtures/native_fields), so one trailing 0x02 is taken as that
  mark and a text without it is taken whole.
- PutFieldText(fieldlist, textlist) is VOID. Without ``{{n}}`` it writes EVERY field of that name, and a field that
  does not exist is silently ignored, so a write always names its occurrence and is proven by reading back.
- FieldExist(field) is BOOL. The manual says the filled text takes the field's own character shape.
The pinned wrapper pyhwpx 1.7.2 is not used for these: its put_field_text reads a spreadsheet for a field string
ending in .xlsx/.xls and joins lists and frames on its own, and its get_field_list defaults to number 1. The calls go
to the wrapped type library object (``hwp.hwp``) with exact BSTR arguments, and each return is checked by its
documented type: a list or text that is not a string, or a FieldExist that is not a bool, is never coerced, and a
PutFieldText that returns anything but None is an unproven outcome.

Proof of a fill is the field list and the document's own HWPML2X text before and after. The field list must read back
with the target holding the new text and every other field as before; a mismatch there is an unknown outcome at once,
with no further native read. Then every definition, control, table, header, footer and paragraph must be as it was,
except that exactly one paragraph holds, between one FIELDBEGIN and its FIELDEND, the new text where the old text was,
with those two controls unchanged, in one character shape: the field's own (``format_source`` 'field') or the one the
whole previous text had ('previous_text'). A character shape or font that only the old text used may be gone. When the
field stands in a header or footer, that whole area is compared too, its frame and every paragraph of its text boxes,
tables and other lists included, and its tables; only the field's own paragraph may differ (see _area_problems). Before
the write, the paragraph lists of the format model, the census and the table model must agree, or the fill is refused.

The document's control sequence is taken from that same HWPML2X text, not from a walk of the live control list: the
text holds every control with its data, and the proof compares them all (see _read).

Hancom 13.0.0.3457 writes a click-here field in HWPML2X as a FIELDBEGIN with a unique InstId and a FieldId shared by
the document's fields, the text, and a FIELDEND with the same FieldId and no InstId (tests/fixtures/native_fields): a
begin pairs with its end only by their order, so no id is used as an identity. No native fill has been measured yet:
if a fill renews any data of the field's own controls, the proof fails closed as an unknown outcome.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

from app import field_rules, native_format as nf, native_table as nt, native_text_edit as nte
from app.native_numbering import (
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _get_pos,
    _get_selection,
    _multiset_delta,
    _reject_unknown_keys,
    _require_confirm_layout,
    _sdk_read,
)
from app.native_paragraph_edit import _COMPARED

SEPARATOR = '\x02'
HWP_FIELD_NUMBER = 1
HWP_FIELD_COUNT = 2
OPTION_ALL = 0
KIND_OPTIONS = (('click_here', 2), ('cell', 1), ('text_box', 8))
KINDS = ('click_here', 'cell', 'text_box', 'mixed', 'unclassified')
FILLABLE_KINDS = frozenset({'click_here'})
NAME_MAX_CHARS = field_rules.NAME_MAX_CHARS
TEXT_MAX_CHARS = field_rules.TEXT_MAX_CHARS
MAX_TEXT_READS = 1000
MAX_ROWS = 1000
REPORT_TEXT = 2000
_HASH_HEX = 24
_NUMBERED = re.compile(r'(.*)\{\{([0-9]+)\}\}', re.S)
_INVENTORY_FIELDS = ('name',)
_FILL_FIELDS = ('name', 'occurrence', 'expected_occurrences', 'expected_hash', 'new_text')
# Hancom drops a character shape, and a font only it used, when no text uses it any more; nothing else may come or go.
_MAY_EMPTY = frozenset({'charshapelist', 'facenamelist'})
_CENSUS_PARAGRAPH_KEYS = ('heading', 'level', 'section', 'numbering_ref_sha256', 'inline_controls', 'page_break', 'column_break', 'end_charshape_sha256')
_LINE_BREAKS = field_rules.LINE_BREAKS


class _CensusUnusable(Exception):
    """A field list or text the official contract does not allow; never a missing or empty field."""


# Requests.

def _name(value: Any) -> str:
    problem = field_rules.name_problem(value)
    if problem is not None:
        raise NativeNumberingInputError(problem)
    return value


def _text(value: Any) -> str:
    problem = field_rules.text_problem(value)
    if problem is not None:
        raise NativeNumberingInputError(problem)
    return value


def _positive(step: Mapping[str, Any], name: str) -> int | None:
    value = step.get(name)
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 100000:
        raise NativeNumberingInputError(f'{name} must be a positive whole number')
    return value


def validate_inventory_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_INVENTORY_FIELDS, *_STEP_ENVELOPE_KEYS), 'field_inventory step')
    return {'name': _name(step['name'])} if step.get('name') is not None else {}


def validate_fill_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_FILL_FIELDS, *_STEP_ENVELOPE_KEYS), 'field_text_exact step')
    _require_confirm_layout(step, 'field_text_exact')
    request: dict[str, Any] = {'name': _name(step.get('name')), 'new_text': _text(step.get('new_text'))}
    expected = step.get('expected_hash')
    if type(expected) is not str or not re.fullmatch(r'sha256:[0-9a-f]{%d}' % _HASH_HEX, expected):
        raise NativeNumberingInputError('expected_hash must be the hash field_inventory reported for this field')
    request['expected_hash'] = expected
    occurrence, count = _positive(step, 'occurrence'), _positive(step, 'expected_occurrences')
    if (occurrence is None) != (count is None):
        raise NativeNumberingInputError('occurrence and expected_occurrences are given together: which field of this name, and how many there are now')
    if occurrence is not None:
        if occurrence > count:
            raise NativeNumberingInputError('occurrence cannot be greater than expected_occurrences')
        request.update(occurrence=occurrence, expected_occurrences=count)
    return request


# The SDK's field census.

def _com(hwp: Any) -> Any:
    com = _sdk_read(lambda: getattr(hwp, 'hwp', None))
    if com is None or type(com) in (str, bytes, int, float, bool):
        raise NativeNumberingRefusal('the native field API is unavailable on this machine', code='native_unavailable')
    return com


def _method(com: Any, name: str) -> Callable[..., Any]:
    method = _sdk_read(lambda: getattr(com, name, None))
    if not callable(method):
        raise NativeNumberingRefusal(f'the native {name} is unavailable on this machine', code='native_unavailable')
    return method


def _call(com: Any, name: str, *args: Any) -> Any:
    method = _method(com, name)
    return _sdk_read(lambda: method(*args))


def _bstr(value: Any, what: str) -> str:
    if type(value) is not str:
        raise _CensusUnusable(f'{what} did not return text')
    return value


def _numbered(value: str) -> list[tuple[str, int]]:
    """GetFieldList(hwpFieldNumber): every field once, ``name{{n}}`` with n counting each name from 0."""
    if value == '':
        return []
    rows: list[tuple[str, int]] = []
    seen: dict[str, int] = {}
    for item in value.split(SEPARATOR):
        match = _NUMBERED.fullmatch(item)
        if match is None:
            raise _CensusUnusable('a numbered field list item has no {{n}}')
        name, number = match.group(1), int(match.group(2))
        if number != seen.get(name, 0):
            raise _CensusUnusable('a numbered field list does not count a name from 0 in order')
        seen[name] = number + 1
        rows.append((name, number))
    return rows


def _counted(value: str) -> dict[str, int]:
    """GetFieldList(hwpFieldCount): each name once with ``{{count}}``."""
    if value == '':
        return {}
    counts: dict[str, int] = {}
    for item in value.split(SEPARATOR):
        match = _NUMBERED.fullmatch(item)
        if match is None or match.group(1) in counts or int(match.group(2)) < 1:
            raise _CensusUnusable('a counted field list item is not a name with its count')
        counts[match.group(1)] = int(match.group(2))
    return counts


def _kind(total: int, per_kind: Mapping[str, int]) -> str:
    present = [kind for kind, count in per_kind.items() if count]
    if len(present) == 1 and per_kind[present[0]] == total:
        return present[0]
    if len(present) > 1 and sum(per_kind.values()) == total:
        return 'mixed'
    return 'unclassified'


def _addressable(name: str) -> bool:
    """A name a request may give: the same rule as the request's own name (so a longer name is listed, never fillable)."""
    return field_rules.name_problem(name) is None


def address(name: str, index: int) -> str:
    return f'{name}{{{{{index}}}}}'


def _field_hash(name: str, index: int, count: int, kind: str, text: str | None) -> str:
    value = json.dumps(['field', name, index, count, kind, text], ensure_ascii=False)
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()[:_HASH_HEX]


def read_census(hwp: Any, *, only: str | None = None) -> dict[str, Any]:
    """Every listed field in document order with its kind and, where it can be addressed, its text.

    The text reads cover the first MAX_TEXT_READS addressable fields in document order. ``only`` reads the texts of one
    name among those and no further, so a field the whole census cannot read is never read for that name alone; the
    list, counts and kinds are always read whole."""
    com = _com(hwp)
    rows = _numbered(_bstr(_call(com, 'GetFieldList', HWP_FIELD_NUMBER, OPTION_ALL), 'GetFieldList'))
    counts = _counted(_bstr(_call(com, 'GetFieldList', HWP_FIELD_COUNT, OPTION_ALL), 'GetFieldList'))
    tallies: dict[str, int] = {}
    for name, _number in rows:
        tallies[name] = tallies.get(name, 0) + 1
    if tallies != counts:
        raise _CensusUnusable('the numbered and counted field lists disagree')
    per_kind: dict[str, dict[str, int]] = {}
    for kind, option in KIND_OPTIONS:
        listed = _counted(_bstr(_call(com, 'GetFieldList', HWP_FIELD_COUNT, option), 'GetFieldList'))
        if any(name not in counts or count > counts[name] for name, count in listed.items()):
            raise _CensusUnusable(f'the {kind} field list names a field the whole list does not have')
        per_kind[kind] = listed
    kinds = {name: _kind(total, {kind: per_kind[kind].get(name, 0) for kind, _option in KIND_OPTIONS}) for name, total in counts.items()}
    occurrences: list[dict[str, Any]] = []
    complete, slots = True, 0
    for name, index in rows:
        row: dict[str, Any] = {'name': name, 'index': index, 'occurrences': counts[name], 'kind': kinds[name], 'addressable': _addressable(name),
                               'readable': False, 'text': None, 'reason': None}
        slot = None
        if row['addressable']:
            slot, slots = slots, slots + 1
        if only is not None and name != only:
            row['reason'] = 'not_read'
        elif not row['addressable']:
            row['reason'] = 'field_name_unaddressable'
            complete = False
        elif slot >= MAX_TEXT_READS:
            row['reason'] = 'field_census_budget'
            complete = False
        else:
            raw = _bstr(_call(com, 'GetFieldText', address(name, index)), 'GetFieldText')
            value = raw[:-1] if raw.endswith(SEPARATOR) else raw
            if SEPARATOR in value:
                row['reason'] = 'field_text_unreadable'
                complete = False
            else:
                row.update(readable=True, text=value)
        row['hash'] = _field_hash(name, index, counts[name], kinds[name], row['text'])
        occurrences.append(row)
    return {'occurrences': occurrences, 'counts': counts, 'kinds': kinds, 'complete': complete}


def _census(hwp: Any, **options: Any) -> dict[str, Any]:
    """The census before any write: an unusable answer stops cleanly, since nothing was changed."""
    try:
        return read_census(hwp, **options)
    except _CensusUnusable as exc:
        raise NativeNumberingRefusal(f'The document field list is not usable as evidence: {exc}.', code='field_census_unusable') from exc


def _fill_refusal(row: Mapping[str, Any]) -> str | None:
    if not row['addressable']:
        return 'field_name_unaddressable'
    if not row['readable']:
        return row['reason'] if row['reason'] != 'not_read' else None
    if row['kind'] not in FILLABLE_KINDS:
        return 'field_kind_unsupported'
    if any(char in _LINE_BREAKS for char in row['text']):
        return 'field_multi_paragraph'
    return None


def _cut(value: str) -> tuple[str, bool]:
    return value[:REPORT_TEXT], len(value) > REPORT_TEXT


def public_field(row: Mapping[str, Any]) -> dict[str, Any]:
    text = row['text']
    refusal = _fill_refusal(row)
    return {'name': row['name'][:NAME_MAX_CHARS], 'name_truncated': len(row['name']) > NAME_MAX_CHARS, 'occurrence': row['index'] + 1,
            'occurrences': row['occurrences'], 'native_address': address(row['name'], row['index']) if row['addressable'] else None, 'kind': row['kind'],
            'text': text[:REPORT_TEXT] if text is not None else None, 'text_truncated': text is not None and len(text) > REPORT_TEXT,
            'empty': (text == '') if row['readable'] else None, 'readable': row['readable'],
            'fillable': row['readable'] and refusal is None, 'fill_refusal': refusal, 'hash': row['hash']}


def inventory(hwp: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """The fields in document order: every one is counted; the first MAX_ROWS are listed, and any further ones are named as omitted."""
    only = request.get('name')
    census = _census(hwp, only=only)
    rows = [row for row in census['occurrences'] if only is None or row['name'] == only]
    listed = rows[:MAX_ROWS]
    omitted = len(rows) - len(listed)
    complete = census['complete'] and all(row['readable'] for row in rows) and not omitted
    return {'count': len(rows), 'listed': len(listed), 'truncated': omitted > 0, 'document_field_count': len(census['occurrences']), 'names': len(census['counts']),
            'fields': [public_field(row) for row in listed],
            'coverage': {'complete': complete, 'method': 'GetFieldList(hwpFieldNumber) and GetFieldText of each name{{n}}; kinds from GetFieldList(hwpFieldCount) per option',
                         'not_listed': 'a field without a name, which GetFieldList does not return',
                         'rows_omitted': omitted, 'row_limit': MAX_ROWS, 'text_read_limit': MAX_TEXT_READS,
                         'unread': sorted({row['reason'] for row in rows if not row['readable'] and row['reason']})}}


def resolve(census: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    name = request['name']
    count = census['counts'].get(name)
    if count is None:
        raise NativeNumberingRefusal('the document has no field of this name; list the fields again with field_inventory', code='field_not_found')
    if 'occurrence' in request:
        if request['expected_occurrences'] != count:
            raise NativeNumberingRefusal(f'the document has {count} field(s) of this name, not {request["expected_occurrences"]}; list them again', code='field_count_mismatch')
        index = request['occurrence'] - 1
    elif count > 1:
        raise NativeNumberingRefusal(f'the document has {count} fields of this name; give occurrence with expected_occurrences so that one is filled and the '
                                     'others stay', code='field_occurrence_required')
    else:
        index = 0
    row = next(item for item in census['occurrences'] if item['name'] == name and item['index'] == index)
    refusal = _fill_refusal(row)
    if refusal is not None:
        messages = {'field_kind_unsupported': f"this field is a {row['kind']} field; this command fills click-here fields (a cell's own text: table_cell_text_exact)",
                    'field_multi_paragraph': 'the field holds more than one paragraph, which this command does not write',
                    'field_text_unreadable': 'the text of this field could not be read as one value',
                    'field_census_budget': 'the document has more fields than one read covers',
                    'field_name_unaddressable': 'this field name cannot be addressed by occurrence'}
        raise NativeNumberingRefusal(messages.get(refusal, 'this field cannot be filled'), code=refusal)
    if row['hash'] != request['expected_hash']:
        raise NativeNumberingRefusal('this field is not as it was when it was listed; list it again with field_inventory', code='field_hash_mismatch')
    return row


# The document text.

def _paragraphs(side: Mapping[str, Any]) -> list[tuple[str, Any, Mapping[str, Any]]]:
    """Every paragraph the HWPML2X text holds: the model's (body, table cells, other lists) and each header's and footer's."""
    rows: list[tuple[str, Any, Mapping[str, Any]]] = [('model', item['index'], item) for item in side['model']['paragraphs']]
    for found in side['areas']['areas']:
        rows.extend(('area', (found['order'], item['index']), item) for item in found['paragraphs'])
    return rows


def _field_spans(atoms: list[Mapping[str, Any]], text: str) -> list[tuple[int, int]]:
    """Each FIELDBEGIN directly followed by exactly this text and then a FIELDEND: (begin, end) atom indexes."""
    spans = []
    for begin, atom in enumerate(atoms):
        end = begin + 1 + len(text)
        if atom.get('control') != 'fieldbegin' or end >= len(atoms) or atoms[end].get('control') != 'fieldend':
            continue
        inner = atoms[begin + 1:end]
        if all('char' in item for item in inner) and ''.join(item['char'] for item in inner) == text:
            spans.append((begin, end))
    return spans


def _require_text_in_document(side: Mapping[str, Any], text: str) -> None:
    if not any(_field_spans(paragraph['atoms'], text) for _kind, _key, paragraph in _paragraphs(side)):
        raise NativeNumberingRefusal('no field in the document text holds exactly the text the field list reads for this field, so its edit could not be '
                                     'proven; nothing was written', code='field_text_unprovable')


def _definition_problems(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    old, new = before['census'], after['census']
    problems = [f'{field} changed' for field in (*(item for item in _COMPARED if item != 'header_footer'), 'frame') if old.get(field) != new.get(field)]
    for name in sorted(set(old['definitions']) | set(new['definitions'])):
        removed, added = _multiset_delta(old['definitions'].get(name, []), new['definitions'].get(name, []))
        if added or (removed and name not in _MAY_EMPTY):
            problems.append(f'{name} changed')
    if before['controls'] != after['controls']:
        problems.append('the document control sequence changed')
    return problems


def _span_problems(left: Mapping[str, Any], right: Mapping[str, Any], old: str, new: str) -> tuple[list[str], str | None]:
    """The one paragraph holds the old text with the new text in place of the old between one field's controls, nothing else changed."""
    if left['shape'] != right['shape'] or left.get('end_shape') != right.get('end_shape') or left.get('mark') != right.get('mark'):
        return ['the paragraph of the field changed its paragraph format, last run or number mark'], None
    before, after = left['atoms'], right['atoms']
    found = []
    for begin, end in _field_spans(before, old):
        tail = begin + 1 + len(new)
        if len(after) != len(before) - len(old) + len(new) or before[:begin + 1] != after[:begin + 1] or before[end:] != after[tail:]:
            continue
        middle = after[begin + 1:tail]
        if all('char' in item for item in middle) and ''.join(item['char'] for item in middle) == new:
            found.append((begin, middle))
    if len(found) != 1:
        return ['the paragraph does not hold exactly the old text with one field\'s text replaced by the new text and its field controls unchanged'], None
    begin, middle = found[0]
    if not middle:
        return [], 'none'
    shape = middle[0]['shape']
    if any(item['shape'] != shape for item in middle):
        return ['the filled text is in more than one character format'], None
    if shape == before[begin]['shape']:
        return [], 'field'
    old_atoms = before[begin + 1:begin + 1 + len(old)]
    if old_atoms and all(item['shape'] == old_atoms[0]['shape'] for item in old_atoms) and shape == old_atoms[0]['shape']:
        return [], 'previous_text'
    return ['the filled text has a character format other than the field\'s'], None


def _document_problems(before: Mapping[str, Any], after: Mapping[str, Any], old: str, new: str) -> tuple[list[str], dict[str, Any] | None, str | None]:
    problems = _definition_problems(before, after)
    left, right = _paragraphs(before), _paragraphs(after)
    if [(kind, key) for kind, key, _item in left] != [(kind, key) for kind, key, _item in right]:
        return [*problems, 'the paragraphs or the headers and footers changed in number or order'], None, None
    areas_before, areas_after = before['areas']['areas'], after['areas']['areas']
    if [(item['kind'], item['section'], item['apply_to']) for item in areas_before] != [(item['kind'], item['section'], item['apply_to']) for item in areas_after]:
        return [*problems, 'the set of headers and footers changed'], None, None
    changed = [(kind, key, one, two) for (kind, key, one), (_kind, _key, two) in zip(left, right) if one != two]
    if len(changed) != 1:
        return [*problems, f'{len(changed)} paragraphs changed where exactly one field\'s paragraph should'], None, None
    kind, key, one, two = changed[0]
    spans, source = _span_problems(one, two, old, new)
    problems += spans
    old_census, new_census = before['census'], after['census']
    old_tables, new_tables = before['tables'], after['tables']
    target = key if kind == 'model' else None
    if len(old_census['paragraphs']) != len(new_census['paragraphs']) or len(old_tables['paragraphs']) != len(new_tables['paragraphs']):
        return [*problems, 'the paragraph count changed'], None, None
    for number, (a, b) in enumerate(zip(old_census['paragraphs'], new_census['paragraphs'])):
        if any(a.get(field) != b.get(field) for field in _CENSUS_PARAGRAPH_KEYS) or (number != target and a.get('text') != b.get('text')):
            problems.append(f'paragraph {number} outside the field changed')
    for number, (a, b) in enumerate(zip(old_tables['paragraphs'], new_tables['paragraphs'])):
        if number != target and a.get('digest') != b.get('digest'):
            problems.append(f'paragraph {number} outside the field changed')
    holder = old_tables['paragraphs'][target] if target is not None else {}
    tables_before = {item['table_id']: item for item in old_tables['tables']}
    tables_after = {item['table_id']: item for item in new_tables['tables']}
    if set(tables_before) != set(tables_after):
        problems.append('the set of tables changed')
    for table_id, table in tables_before.items():
        other = tables_after.get(table_id)
        if other is None:
            continue
        if table_id != holder.get('table_id'):
            if table['state_sha256'] != other['state_sha256']:
                problems.append(f'table {table_id} changed')
            continue
        problems += nte._cell_problems(table, other, {holder['cell']})  # noqa: SLF001
    old_hf, new_hf = old_census.get('header_footer') or [], new_census.get('header_footer') or []
    area = key[0] if kind == 'area' else None
    if len(old_hf) != len(new_hf) or any(a != b and number != area for number, (a, b) in enumerate(zip(old_hf, new_hf))):
        problems.append('a header or footer outside the field changed')
    problems += _area_problems(areas_before, areas_after, key if kind == 'area' else None)
    margin_before = {item['table_id']: item['state_sha256'] for item in old_tables.get('margin_tables', ())}
    margin_after = {item['table_id']: item['state_sha256'] for item in new_tables.get('margin_tables', ())}
    if margin_before != margin_after:
        problems.append('a table in a header or footer changed')
    if kind == 'model':
        location = {'context': one['context'], 'paragraph_index': key}
    else:
        found = areas_before[key[0]]
        location = {'context': found['kind'], 'area_id': found['area_id'], 'paragraph_index': key[1]}
    location['paragraph_before'], location['paragraph_before_truncated'] = _cut(one['text'])
    location['paragraph_after'], location['paragraph_after_truncated'] = _cut(two['text'])
    return problems, location, source


def _area_problems(before: list[Mapping[str, Any]], after: list[Mapping[str, Any]], target: tuple[int, int] | None) -> list[str]:
    """Every header and footer whole, the text boxes, tables and other lists inside them included.

    Each area's frame and every paragraph in it must be as it was. The one exception is the field's own paragraph when it
    stands in an area: its text and runs are bounded by _span_problems, and its own settings (InstId apart) must stay; the
    paragraphs of what it holds, such as a text box beside the field, are compared like any other."""
    problems = []
    for one, two in zip(before, after):
        if one['shell'] != two['shell'] or [(item['top'], item['nested']) for item in one['every_paragraph']] != [(item['top'], item['nested']) for item in two['every_paragraph']]:
            problems.append(f"{one['area_id']} changed its paragraphs in number, order or nesting")
            continue
        for a, b in zip(one['every_paragraph'], two['every_paragraph']):
            own = target is not None and one['order'] == target[0] and a['top'] == target[1] and not a['nested']
            if (a['settings'] if own else a['content']) != (b['settings'] if own else b['content']):
                problems.append(f"{one['area_id']} changed outside the field" if not own else f"the field's paragraph in {one['area_id']} changed its own settings")
                break
    return problems


def _census_problems(before: Mapping[str, Any], after: Mapping[str, Any], target: Mapping[str, Any], new: str) -> list[str]:
    def shape(census: Mapping[str, Any]) -> list[tuple[str, int, str]]:
        return [(row['name'], row['index'], row['kind']) for row in census['occurrences']]

    if shape(before) != shape(after) or before['counts'] != after['counts']:
        return ['the document field list changed in names, number or kinds']
    problems = []
    for one, two in zip(before['occurrences'], after['occurrences']):
        if (one['name'], one['index']) == (target['name'], target['index']):
            if not two['readable'] or two['text'] != new:
                problems.append('the field does not read back with the new text')
        elif one['readable'] and (not two['readable'] or two['text'] != one['text']):
            problems.append(f"another field ({address(one['name'], one['index'])}) changed")
    return problems


# The fill.

def _bare_caret(hwp: Any) -> tuple[int, int, int]:
    caret = _get_pos(hwp)
    selection = _get_selection(hwp)
    if selection is None or selection != ():
        raise NativeNumberingRefusal('the caret holds a selection or could not be read plainly; cancel the selection first', code='selection_unverified')
    return caret


def _text_controls(side: Mapping[str, Any]) -> list[str]:
    """The document's controls in order, by kind, as its own HWPML2X text holds them (body, nested lists, headers and footers)."""
    return [atom['control'] for _kind, _key, paragraph in _paragraphs(side) for atom in paragraph['atoms'] if 'control' in atom]


def _read(read: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    """One HWPML2X read of the whole document, and its control sequence from that same text.

    The fill does not walk the live control list (HeadCtrl, Next, CtrlID) as the other engines' native_format._read does.
    Its proof already compares, in the same text, every control of every paragraph by kind, place and full-attribute
    digest: every paragraph but the field's must be equal, the field's paragraph must be equal outside the field's text,
    and that text must be characters only (_document_problems, _span_problems). So any control added, removed, moved,
    renewed or changed is caught without the walk, and the control sequence compared and hashed here comes from the same
    single read as the rest of the proof. A read that fails is still an unknown outcome (the mixin's
    NativeNumberingReadFailure), and no further native call follows it."""
    side = read()
    return {**side, 'controls': _text_controls(side)}


def _report(target: Mapping[str, Any], new: str, *, changed: bool, after: Mapping[str, Any] | None, location: Mapping[str, Any] | None,
            source: str | None, caret: tuple[int, ...], undo: Mapping[str, Any]) -> dict[str, Any]:
    later = next((row for row in after['occurrences'] if (row['name'], row['index']) == (target['name'], target['index'])), None) if after else None
    observed = later['text'] if later is not None else target['text']
    next_hash = later['hash'] if later is not None else target['hash']
    count = target['occurrences']
    binding = {'name': target['name'], 'expected_hash': next_hash, **({'occurrence': target['index'] + 1, 'expected_occurrences': count} if count > 1 else {})}
    return {
        'field': {'name': target['name'], 'occurrence': target['index'] + 1, 'occurrences': count, 'native_address': address(target['name'], target['index']),
                  'kind': target['kind'], 'hash_before': target['hash'], 'hash_after': next_hash},
        'requested': {'new_text': new},
        'result': {'changed': changed, 'status': 'filled' if changed else 'already_set',
                   'text_before': _cut(target['text'])[0], 'text_before_truncated': _cut(target['text'])[1],
                   'text_after': _cut(observed)[0], 'text_after_truncated': _cut(observed)[1], 'native_calls': ['PutFieldText'] if changed else [],
                   'siblings_kept': sum(1 for row in (after or {'occurrences': []})['occurrences'] if (row['name'], row['index']) != (target['name'], target['index'])),
                   'location': dict(location) if location else None, 'format_source': source,
                   'paragraph_before': location['paragraph_before'] if location else None,
                   'paragraph_before_truncated': location['paragraph_before_truncated'] if location else False,
                   'paragraph_after': location['paragraph_after'] if location else None,
                   'paragraph_after_truncated': location['paragraph_after_truncated'] if location else False},
        'next_binding': binding,
        'caret': list(caret),
        'undo': dict(undo),
    }


def apply_fill(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    """Fill one existing click-here field with one PutFieldText of its own ``name{{n}}``, then prove the outcome."""
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        caret = _bare_caret(hwp)
        before = _read(read)
        texts = [item['text'] for item in before['model']['paragraphs']]
        if texts != [item['text'] for item in before['census']['paragraphs']] or texts != [item['text'] for item in before['tables']['paragraphs']]:
            # The proof compares the census and table paragraphs by the format model's index: they must be the same list.
            raise NativeNumberingRefusal('The document text and its paragraph list disagree, so a fill could not be proven; nothing was written.',
                                         code='preservation_unprovable')
        census = _census(hwp)
        target = resolve(census, request)
        com = _com(hwp)
        exists = _call(com, 'FieldExist', target['name'])
        if exists is False:
            raise NativeNumberingRefusal('the official field check does not find this field', code='field_not_found')
        if exists is not True:
            raise NativeNumberingRefusal('the official field check did not answer with a BOOL', code='field_census_unusable')
        _require_text_in_document(before, target['text'])
        new = request['new_text']
        if target['text'] == new:
            return _report(target, new, changed=False, after=None, location=None, source=None, caret=caret,
                           undo={'native_editing_actions': 0, 'pre_state_sha256': None, 'post_state_sha256': None})
        put = _method(com, 'PutFieldText')
        reach.issue()
        returned = put(address(target['name'], target['index']), new)
        if returned is not None:
            raise NativeNumberingMutationError(f'PutFieldText is documented void but returned a {type(returned).__name__} value, so the outcome is not proven; '
                                               'nothing was rolled back')
        try:
            after_census = read_census(hwp)
        except _CensusUnusable as exc:
            raise NativeNumberingMutationError(f'the field list after the fill is not usable ({exc}); nothing was rolled back') from exc
        problems = _census_problems(census, after_census, target, new)
        if problems:
            # Already known not to be as requested: stop now, with no further native read.
            raise NativeNumberingMutationError('field fill outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        after = _read(read)
        problems, location, source = _document_problems(before, after, target['text'], new)
        if problems:
            raise NativeNumberingMutationError('field fill outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        return _report(target, new, changed=True, after=after_census, location=location, source=source, caret=_get_pos(hwp),
                       undo={'native_editing_actions': 1, 'pre_state_sha256': nf._state_sha(before), 'post_state_sha256': nf._state_sha(after)})  # noqa: SLF001

    return nt._guarded(reach, body, 'field fill')  # noqa: SLF001
