"""Replacing one exact range of existing text, and the text of whole existing table cells, keeping the replaced text's format.

Native facts these rely on were measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2 on disposable synthetic documents.

InsertText (the official parameter set InsertText, item Text) at a bare caret gives the new text the character shape of the
character to its left, and in a blank paragraph that of its own empty run. Over a selected block it replaces the block but
takes the shape of the character before the block: a replaced bold word came out plain and the first word of a 16 pt title
came out 10 pt. So the new text goes in at the end of the old range, right after its last character, and the old range is
then selected and deleted; the new text has the shape of the old range's last character, which for a range of one format is
that format. Delete on a block removes exactly its characters and leaves the caret at the block's start. A block that starts
right after a paragraph's section, column, header and footer controls, or right after an inline picture, leaves those in
place. InsertText and Delete are one Undo step each: a replacement is two, two cells are four, a blank cell filled by
InsertText alone is one. The rest of the paragraph, its paragraph shape and every other paragraph, control, table, header,
footer and definition were unchanged after save and reopen.

The old text is selected and its block read before anything changes, and again after the insertion; afterwards the whole
document is compared with what was asked for. Anything else is an unknown outcome that is never rolled back.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from app import native_format as nf, native_table as nt
from app.native_numbering import (
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    STEP_MATCH_MAX_CHARS,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _action_confirmed,
    _get_pos,
    _get_selection,
    _multiset_delta,
    _native_method,
    _read_target_state,
    _reject_unknown_keys,
    _require_confirm_layout,
    _sdk_read,
    _set_pos,
    _status_text,
)
from app.native_paragraph_edit import _COMPARED
from app.table_structure import cell_address

TEXT_MAX_CHARS = 2000
MAX_CELL_WRITES = 100
REPORT_TEXT = 300
FORMAT_FROM = ('replaced_text', 'last_character')
# Controls measured to stay in place when a block starting right after them is deleted.
_MEASURED_BEFORE = frozenset({'secdef', 'coldef', 'header', 'footer', 'picture'})
_REPLACE_FIELDS = ('old_text', 'new_text', 'match', 'occurrence', 'expected_occurrences', 'paragraph_index', 'expected_text', 'expected_page', 'format_from', 'confirm_layout')
_CELL_KEYS = frozenset({'address', 'expected_text', 'text'})


# Requests.

def _line_text(value: Any, name: str, *, max_chars: int) -> str:
    if type(value) is not str or not value or len(value) > max_chars or any(ord(char) < 32 or char in '  ' for char in value):
        raise NativeNumberingInputError(f'{name} must be 1 to {max_chars} characters on one line, without tabs or other control characters')
    return value


def _count(text: str, phrase: str) -> list[int]:
    return [offset for offset in range(len(text) - len(phrase) + 1) if text.startswith(phrase, offset)]


def _positive(step: Mapping[str, Any], name: str) -> int | None:
    value = step.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise NativeNumberingInputError(f'{name} must be a positive whole number')
    return value


def validate_replace_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_REPLACE_FIELDS, *_STEP_ENVELOPE_KEYS), 'text_replace_exact step')
    _require_confirm_layout(step, 'text_replace_exact')
    page = _positive(step, 'expected_page')
    if page is None:
        raise NativeNumberingInputError('expected_page must be a positive whole number')
    old = _line_text(step.get('old_text'), 'old_text', max_chars=STEP_MATCH_MAX_CHARS)
    new = _line_text(step.get('new_text'), 'new_text', max_chars=TEXT_MAX_CHARS)
    if new == old:
        raise NativeNumberingInputError('new_text is the same as old_text, so nothing would change')
    request: dict[str, Any] = {'old_text': old, 'new_text': new, 'expected_page': page}
    occurrence, expected = _positive(step, 'occurrence'), _positive(step, 'expected_occurrences')
    if (occurrence is None) != (expected is None):
        raise NativeNumberingInputError('occurrence and expected_occurrences are given together: which one, and how many there are now')
    if occurrence is not None:
        if occurrence > expected:
            raise NativeNumberingInputError('occurrence cannot be greater than expected_occurrences')
        request.update(occurrence=occurrence, expected_occurrences=expected)
    match, index, text = step.get('match'), step.get('paragraph_index'), step.get('expected_text')
    if index is not None or text is not None:
        if match is not None:
            raise NativeNumberingInputError('give match, or paragraph_index with expected_text, not both')
        if type(index) is not int or index < 0 or type(text) is not str or len(text) > nf.TEXT_MAX_CHARS:
            raise NativeNumberingInputError("paragraph_index needs the paragraph's full current expected_text")
        request.update(paragraph_index=index, expected_text=text)
    elif match is not None:
        match = _line_text(match, 'match', max_chars=STEP_MATCH_MAX_CHARS)
        if len(_count(match, old)) != 1:
            raise NativeNumberingInputError('match must contain old_text exactly once')
        request['match'] = match
    format_from = step.get('format_from', 'replaced_text')
    if format_from not in FORMAT_FROM:
        raise NativeNumberingInputError('format_from must be replaced_text or last_character')
    request['format_from'] = format_from
    return request


def validate_cell_text_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*nt._BOUND_FIELDS, 'cells', 'confirm_layout', *_STEP_ENVELOPE_KEYS), 'table_cell_text_exact step')  # noqa: SLF001
    bound = nt._validated_binding(step, 'table_cell_text_exact')  # noqa: SLF001
    cells = step.get('cells')
    if not isinstance(cells, list) or not 1 <= len(cells) <= MAX_CELL_WRITES:
        raise NativeNumberingInputError(f'cells must list 1 to {MAX_CELL_WRITES} cells')
    seen: set[str] = set()
    planned: list[dict[str, Any]] = []
    for item in cells:
        if not isinstance(item, Mapping) or set(item) != _CELL_KEYS:
            raise NativeNumberingInputError('each cell is {address, expected_text, text}')
        address = item['address']
        at = nt._address({'address': address}, 'address')  # noqa: SLF001
        if address in seen:
            raise NativeNumberingInputError(f'{address} is listed twice')
        seen.add(address)
        expected = item['expected_text']
        if type(expected) is not str or len(expected) > TEXT_MAX_CHARS:
            raise NativeNumberingInputError(f'expected_text of {address} must be the cell\'s whole current text as table_read reports it')
        text = _line_text(item['text'], f'text of {address}', max_chars=TEXT_MAX_CHARS)
        if text == expected:
            raise NativeNumberingInputError(f'the text of {address} is the same as its expected_text, so nothing would change')
        planned.append({'address': address, 'at': at, 'expected_text': expected, 'text': text})
    return {**bound, 'cells': planned}


# Shared native steps.

def _insert(hwp: Any, text: str) -> None:
    action = _native_method(hwp, 'CreateAction')('InsertText')
    pset = _sdk_read(lambda: action.CreateSet())
    _sdk_read(lambda: pset.SetItem('Text', text))
    result = action.Execute(pset)
    if not _action_confirmed(result):
        raise NativeNumberingMutationError(f'native InsertText returned {_status_text(result)}; nothing was rolled back')


def _delete(hwp: Any) -> None:
    result = hwp.HAction.Run('Delete')
    if not _action_confirmed(result):
        raise NativeNumberingMutationError(f'native Delete returned {_status_text(result)}; nothing was rolled back')


def _select_words(hwp: Any, block: tuple[int, ...], words: list[str], *, after_mutation: bool) -> None:
    """Select a block of one paragraph by position and prove it holds exactly the words."""
    list_id, para, first, _list, _para, last = block
    selected = _sdk_read(lambda: hwp.select_text(para, first, para, last, list_id))
    if selected is not True or _get_selection(hwp) != block or nf._block_words(hwp) != words:  # noqa: SLF001
        if after_mutation:
            raise NativeNumberingMutationError('the old text could not be selected again exactly after the insertion; nothing was rolled back')
        raise NativeNumberingRefusal('the requested range could not be selected exactly', code='selection_unverified')


def _caret_at(hwp: Any, at: tuple[int, int, int], *, after_mutation: bool, what: str) -> None:
    _set_pos(hwp, at)
    if _read_target_state(hwp) != (at, ()):
        if after_mutation:
            raise NativeNumberingMutationError(f'the caret is not {what}; nothing was rolled back')
        raise NativeNumberingRefusal(f'the caret could not be put {what}', code='selection_unverified')


def _uniform(atoms: list[dict[str, Any]]) -> bool:
    return all(atom['shape'] == atoms[0]['shape'] for atom in atoms)


# Hancom drops a character shape, and a font only it used, when no text uses it any more; nothing else may come or go.
_MAY_EMPTY = frozenset({'charshapelist', 'facenamelist'})


def _definition_problems(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    census_before, census_after = before['census'], after['census']
    problems = [f'{field} changed' for field in (*_COMPARED, 'frame') if census_before.get(field) != census_after.get(field)]
    for name in sorted(set(census_before['definitions']) | set(census_after['definitions'])):
        removed, added = _multiset_delta(census_before['definitions'].get(name, []), census_after['definitions'].get(name, []))
        if added or (removed and name not in _MAY_EMPTY):
            problems.append(f'{name} changed')
    if before['controls'] != after['controls']:
        problems.append('the document control sequence changed')
    return problems


# One exact range.

def _resolve(model: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[dict[str, Any], int, int]:
    """The paragraph, the first and past-last atom of the old text, and which of how many occurrences it is."""
    paragraphs = model['paragraphs']
    old = request['old_text']
    if 'paragraph_index' in request:
        index = request['paragraph_index']
        if index >= len(paragraphs):
            raise NativeNumberingRefusal('there is no paragraph with this index', code='target_not_found')
        if paragraphs[index]['text'] != request['expected_text']:
            raise NativeNumberingRefusal('the paragraph at this index does not hold exactly expected_text', code='target_text_mismatch')
        hits = [(index, offset) for offset in _count(paragraphs[index]['text'], old)]
        where = 'in this paragraph'
    else:
        phrase = request.get('match') or old
        inner = phrase.index(old)
        hits = [(paragraph['index'], offset + inner) for paragraph in paragraphs for offset in _count(paragraph['text'], phrase)]
        if not hits and any(phrase in text for text in model['header_footer_text']):
            raise NativeNumberingRefusal('the text is in a header or footer, which this command does not edit', code='target_in_header_footer')
        where = 'in the document'
    if not hits:
        raise NativeNumberingRefusal('the text was not found ' + where, code='target_not_found')
    if 'occurrence' in request:
        if len(hits) != request['expected_occurrences']:
            raise NativeNumberingRefusal(f"the text occurs {len(hits)} times {where}, not {request['expected_occurrences']}; read the document again", code='target_count_mismatch')
        number = request['occurrence']
    elif len(hits) > 1:
        raise NativeNumberingRefusal(f'the text occurs {len(hits)} times {where}; give occurrence with expected_occurrences, a longer match, or paragraph_index with expected_text', code='target_ambiguous')
    else:
        number = 1
    index, offset = hits[number - 1]
    paragraph = paragraphs[index]
    nf._require_context(paragraph)  # noqa: SLF001
    chars = nf._char_indexes(paragraph)  # noqa: SLF001
    return {'paragraph': paragraph, 'first': chars[offset], 'last': chars[offset + len(old) - 1] + 1}, number, len(hits)


def _require_range(row: Mapping[str, Any], format_from: str) -> dict[str, Any]:
    nf._require_no_control_inside([row])  # noqa: SLF001
    atoms = row['paragraph']['atoms']
    first, last = row['first'], row['last']
    before = atoms[first - 1].get('control') if first else None
    if before is not None and before not in _MEASURED_BEFORE:
        raise NativeNumberingRefusal(f'the text starts right after a control this command was not measured to leave alone ({before}); start at least one character later', code='target_beside_unproven_control')
    span = atoms[first:last]
    if not _uniform(span) and format_from != 'last_character':
        parts = len(nf._segments(span))  # noqa: SLF001
        raise NativeNumberingRefusal(f'the text to replace has {parts} character formats; replace each part on its own, or give format_from "last_character" so the new text takes the format of its last character', code='target_mixed_format')
    return span[-1]['shape']


def _model_texts(side: Mapping[str, Any]) -> list[str]:
    return [item['text'] for item in side['model']['paragraphs']]


def _replace_problems(before: Mapping[str, Any], after: Mapping[str, Any], row: Mapping[str, Any], new: str, shape: Mapping[str, Any]) -> list[str]:
    problems = _definition_problems(before, after)
    target = row['paragraph']['index']
    old_census, new_census = before['census']['paragraphs'], after['census']['paragraphs']
    old_model, new_model = before['model']['paragraphs'], after['model']['paragraphs']
    old_tables, new_tables = before['tables']['paragraphs'], after['tables']['paragraphs']
    if not (len(old_census) == len(new_census) == len(old_model) == len(new_model) == len(old_tables) == len(new_tables)):
        return [*problems, 'the paragraph count changed']
    for number, (left, right) in enumerate(zip(old_census, new_census)):
        fields = ('heading', 'level', 'section', 'numbering_ref_sha256', 'inline_controls', 'page_break', 'column_break', 'end_charshape_sha256')
        if any(left.get(key) != right.get(key) for key in fields) or (number != target and left['text'] != right['text']):
            problems.append(f'paragraph {number} outside the request changed')
    for number, (left, right) in enumerate(zip(old_tables, new_tables)):
        if number != target and left['digest'] != right['digest']:
            problems.append(f'paragraph {number} outside the request changed')
    holder = old_tables[target]
    tables_before = {item['table_id']: item for item in before['tables']['tables']}
    tables_after = {item['table_id']: item for item in after['tables']['tables']}
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
        problems += _cell_problems(table, other, {holder['cell']})
    left, right = old_model[target], new_model[target]
    first, last = row['first'], row['last']
    expected = [*left['atoms'][:first], *({'char': char, 'shape': shape} for char in new), *left['atoms'][last:]]
    if left['shape'] != right['shape'] or left['end_shape'] != right['end_shape']:
        problems.append('the paragraph changed its paragraph format or its last run')
    if len(expected) != len(right['atoms']):
        problems.append('the paragraph does not hold exactly the old text with the range replaced')
    else:
        for want, got in zip(expected, right['atoms']):
            same = want.get('char') == got.get('char') and want.get('control') == got.get('control') and want['shape'] == got['shape']
            # A table's own content is compared by its state above; its stored heights follow layout.
            if same and want.get('control') not in (None, 'table'):
                same = want.get('digest') == got.get('digest') and want.get('data') == got.get('data')
            if not same:
                problems.append('the paragraph does not hold exactly the old text with the range replaced in the expected format')
                break
    return problems


def apply_replace(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int],
                  on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = nf._read(hwp, read)  # noqa: SLF001
        if not (_model_texts(before) == [item['text'] for item in before['census']['paragraphs']] == [item['text'] for item in before['tables']['paragraphs']]):
            raise NativeNumberingRefusal('The document text and its paragraph list disagree.', code='preservation_unprovable')
        row, number, count = _resolve(before['model'], request)
        shape = _require_range(row, request['format_from'])
        paragraph = row['paragraph']
        list_id, para = nf._native_list(hwp, [row])  # noqa: SLF001
        first, last = nf._position_of(paragraph, row['first']), nf._position_of(paragraph, row['last'])  # noqa: SLF001
        # The old text, selected as Hancom selects it (a block ending right before an invisible control takes it too) and read.
        proof_block, words = nf._character_block([row], list_id, para)  # noqa: SLF001
        nf._select(hwp, proof_block, words, page_of, request['expected_page'])  # noqa: SLF001
        _caret_at(hwp, (list_id, para, last), after_mutation=False, what='at the end of the old text')
        reach.issue()
        _insert(hwp, request['new_text'])
        _select_words(hwp, (list_id, para, first, list_id, para, last), [request['old_text']], after_mutation=True)
        _delete(hwp)
        if _read_target_state(hwp) != ((list_id, para, first), ()):
            raise NativeNumberingMutationError('the caret is not at the start of the new text after the deletion; nothing was rolled back')
        after = nf._read(hwp, read)  # noqa: SLF001
        problems = _replace_problems(before, after, row, request['new_text'], shape)
        if problems:
            raise NativeNumberingMutationError('text replacement outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        end = (list_id, para, first + len(request['new_text']))
        _caret_at(hwp, end, after_mutation=True, what='after the new text')
        new_text = after['model']['paragraphs'][paragraph['index']]['text']
        return {
            'target': {'paragraph_index': paragraph['index'], 'context': paragraph['context'], 'occurrence': number, 'occurrences': count,
                       'page': request['expected_page'], 'start': nf._char_indexes(paragraph).index(row['first'])},  # noqa: SLF001
            'requested': {'old_text': request['old_text'], 'new_text': request['new_text'], 'format_from': request['format_from']},
            'result': {'changed': True, 'status': 'replaced', 'paragraph_before': paragraph['text'][:REPORT_TEXT], 'paragraph_after': new_text[:REPORT_TEXT],
                       'paragraph_truncated': max(len(paragraph['text']), len(new_text)) > REPORT_TEXT, 'format': nf.public_character(shape),
                       'native_actions': ['Execute:InsertText', 'Run:Delete'], 'caret_after': list(end)},
            'undo': {'native_editing_actions': 2, 'pre_state_sha256': nf._state_sha(before), 'post_state_sha256': nf._state_sha(after)},  # noqa: SLF001
        }

    return nt._guarded(reach, body, 'text replacement')  # noqa: SLF001


# Whole table cells.

def _cell_problems(table: Mapping[str, Any], other: Mapping[str, Any], changing: set[str]) -> list[str]:
    """Everything of a table but the content of the named cells, which the caller checks."""
    problems: list[str] = []
    if (table['rows'], table['cols'], table['frame']) != (other['rows'], other['cols'], other['frame']):
        problems.append(f"table {table['table_id']} changed its grid or frame")
    old = {cell['address']: cell for cell in table['cells']}
    new = {cell['address']: cell for cell in other['cells']}
    if set(old) != set(new):
        return [*problems, f"table {table['table_id']} changed its cells"]
    for address, cell in old.items():
        now = new[address]
        for key in ('row_span', 'col_span', 'width', 'border', 'fill', 'rest', 'attrs'):
            if cell[key] != now[key]:
                problems.append(f'{address} changed its {key}')
        if address not in changing and cell['content'] != now['content']:
            problems.append(f'{address} changed its content')
    return problems


def _plan_cell(table: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    cell = nt._whole_cell(table, item['at'])  # noqa: SLF001
    address = cell['address']
    if cell['text'] != item['expected_text']:
        raise NativeNumberingRefusal(f'{address} does not hold exactly expected_text; read the table again', code='cell_text_mismatch')
    if len(cell['paragraphs']) != 1:
        raise NativeNumberingRefusal(f"{address} has {len(cell['paragraphs'])} paragraphs; its text is replaced only as one paragraph, so change its paragraphs with text_replace_exact", code='cell_multi_paragraph')
    paragraph = cell['paragraphs'][0]
    if any('char' not in atom for atom in paragraph['atoms']) or cell['all_objects']:
        raise NativeNumberingRefusal(f'{address} holds a table, picture or other object, which this command does not remove', code='cell_has_objects')
    atoms = paragraph['atoms']
    if atoms and not _uniform(atoms):
        raise NativeNumberingRefusal(f'{address} has text in more than one character format; replace each part with text_replace_exact', code='cell_mixed_format')
    shape = atoms[-1]['shape'] if atoms else paragraph['end_shape']
    if shape is None:
        raise NativeNumberingRefusal(f'the character format of {address} cannot be read', code='preservation_unprovable')
    end = nf._position_of({'atoms': atoms}, len(atoms))  # noqa: SLF001
    return {'address': address, 'old': cell['text'], 'text': item['text'], 'shape': shape, 'end': end, 'blank': not atoms, 'paragraph_shape': paragraph['shape']}


def _cell_list(hwp: Any, inst: str, table: Mapping[str, Any], address: str, *, after_mutation: bool) -> int:
    nt.goto_cell(hwp, inst, address, len(table['cells']))
    list_id, para, _pos = _get_pos(hwp)
    if para != 0 or list_id == 0:
        if after_mutation:
            raise NativeNumberingMutationError(f'the caret is not in the paragraph of {address}; nothing was rolled back')
        raise NativeNumberingRefusal(f'the caret is not in the paragraph of {address}', code='target_cell_unreachable')
    return list_id


def _written_problems(plans: list[dict[str, Any]], new_table: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    by_address = {cell['address']: cell for cell in new_table['cells']}
    for plan in plans:
        cell = by_address.get(plan['address'])
        if cell is None or len(cell['paragraphs']) != 1:
            problems.append(f"{plan['address']} does not hold one paragraph")
            continue
        paragraph = cell['paragraphs'][0]
        if cell['text'] != plan['text'] or any('char' not in atom or atom['shape'] != plan['shape'] for atom in paragraph['atoms']):
            problems.append(f"{plan['address']} does not hold exactly the requested text in its format")
        if paragraph['shape'] != plan['paragraph_shape']:
            problems.append(f"{plan['address']} changed its paragraph format")
    return problems


def apply_cell_text(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int], restore: Callable[[], None],
                    on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = read()
        table = nt.resolve_table(before['model'], request['table_id'])
        inst = table['inst_id']
        page = nt.table_pages(hwp, page_of, only=inst).get(inst)
        nt.require_binding(table, request, page)
        plans = [_plan_cell(table, item) for item in request['cells']]
        nt.enter_table(hwp, inst)
        # Every cell is reached and its text proven before the first edit, so a refusal still changes nothing.
        for plan in plans:
            plan['list'] = _cell_list(hwp, inst, table, plan['address'], after_mutation=False)
            if not plan['blank']:
                _caret_at(hwp, (plan['list'], 0, 0), after_mutation=False, what=f"at the start of {plan['address']}")
                _select_words(hwp, (plan['list'], 0, 0, plan['list'], 0, plan['end']), [plan['old']], after_mutation=False)
            nt._run(hwp, 'Cancel')  # noqa: SLF001
        native_actions: list[str] = []
        for plan in plans:
            started = reach.reached
            listed = _cell_list(hwp, inst, table, plan['address'], after_mutation=started)
            if listed != plan['list']:
                raise (NativeNumberingMutationError(f"{plan['address']} is in another list than before; nothing was rolled back") if started
                       else NativeNumberingRefusal(f"{plan['address']} could not be reached again", code='target_cell_unreachable'))
            _caret_at(hwp, (listed, 0, plan['end']), after_mutation=started, what=f"at the end of {plan['address']}")
            reach.issue()
            _insert(hwp, plan['text'])
            native_actions.append(f"{plan['address']}:Execute:InsertText")
            if not plan['blank']:
                _select_words(hwp, (listed, 0, 0, listed, 0, plan['end']), [plan['old']], after_mutation=True)
                _delete(hwp)
                native_actions.append(f"{plan['address']}:Run:Delete")
                if _read_target_state(hwp) != ((listed, 0, 0), ()):
                    raise NativeNumberingMutationError(f"the caret is not at the start of the new text in {plan['address']}; nothing was rolled back")
        nt._run(hwp, 'Cancel')  # noqa: SLF001
        after = read()
        new_table = next((item for item in after['model']['tables'] if item['table_id'] == table['table_id']), None)
        problems = nt.document_problems(before, after, table, allowed_lists=frozenset())
        if new_table is None:
            problems.append('the table is not in the document after the edit')
        else:
            problems += _cell_problems(table, new_table, {plan['address'] for plan in plans})
            problems += _written_problems(plans, new_table)
        if problems:
            raise NativeNumberingMutationError('table cell text outcome is not as requested: ' + '; '.join(problems[:6]) + '; nothing was rolled back')
        nt._finish(hwp, restore)  # noqa: SLF001
        return {
            'target': {'table_id': table['table_id'], 'page': page, 'cells': len(table['cells'])},
            'requested': {'cells': [{'address': plan['address'], 'expected_text': plan['old'][:REPORT_TEXT], 'text': plan['text'][:REPORT_TEXT]} for plan in plans]},
            'before': nt._report_table(table, page), 'after': nt._report_table(new_table, page),  # noqa: SLF001
            'changes': [{'address': plan['address'], 'before': plan['old'][:REPORT_TEXT], 'after': plan['text'][:REPORT_TEXT]} for plan in plans],
            'result': {'changed': True, 'status': 'written', 'native_actions': native_actions,
                       'format': {plan['address']: nf.public_character(plan['shape']) for plan in plans}},
            'undo': {'native_editing_actions': len(native_actions), 'pre_state_sha256': nt._state_sha(before), 'post_state_sha256': nt._state_sha(after)},  # noqa: SLF001
            '_details': (nt._detail_of(before), nt._detail_of(after)),  # noqa: SLF001
        }

    return nt._guarded(reach, body, 'table cell text')  # noqa: SLF001


__all__ = ['FORMAT_FROM', 'MAX_CELL_WRITES', 'apply_cell_text', 'apply_replace', 'validate_cell_text_step', 'validate_replace_step', 'cell_address']
