"""Guarded body-paragraph insert and remove for numbered documents, plus a read-only numbering readback.

Native facts these rely on were observed on Hancom 2024 through pyhwpx 1.7.2: one ``InsertText`` holding ``\\r\\n``
creates one paragraph that inherits the neighbor's HeadingType and Level and is reverted by one Undo. Selecting from a
paragraph start to the next paragraph start and running one ``Delete`` removes it, keeps the next paragraph's shape and
is reverted by one Undo, but the next paragraph loses a PageBreak/ColumnBreak flag, so that case is refused. Removing the
only user of a character or paragraph shape also drops that shape from the document's definition list.

The same census protects both edits as numbering: mandatory evidence is read before the call, and any change beyond the
requested paragraph after the call is an unknown outcome that is never rolled back.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Callable, Mapping

from app.native_numbering_census import CensusError, CensusTextUnusable, _digest_tokens
from app.native_numbering import (
    ControlEnumerationLimit,
    ControlEnumerationUnproven,
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingReadFailure,
    NativeNumberingRefusal,
    STEP_MATCH_MAX_CHARS,
    _MutationReach,
    _STEP_ENVELOPE_KEYS,
    _action_confirmed,
    _census_hits,
    _control_ids,
    _exception_class_label,
    _get_pos,
    _get_selection,
    _locate_unique_body_paragraph,
    _mark_noncomplete,
    _multiset_delta,
    _native_method,
    _read_page_number_props,
    _read_protection,
    _read_target_state,
    _reject_unknown_keys,
    _require_complete,
    _require_confirm_layout,
    _require_haction,
    _set_pos,
    _status_text,
    _validated_census,
    single_use_shape_left,
)

TEXT_MAX_CHARS = 4000
_NO_MEDIA_SHA256 = _digest_tokens([])
_COMPARED = ('header_footer', 'styles_sha256', 'numberings_sha256', 'bindata_sha256', 'media_sha256', 'outline_shapes', 'page_restarts')


def _validated_match(step: Mapping[str, Any], name: str) -> dict[str, Any]:
    match = step.get('match')
    if not isinstance(match, str) or not match.strip():
        raise NativeNumberingInputError(f'{name} requires non-empty match text')
    match = match.strip()
    if len(match) > STEP_MATCH_MAX_CHARS or '\n' in match or '\r' in match:
        raise NativeNumberingInputError(f'match must be one line of at most {STEP_MATCH_MAX_CHARS} characters')
    expected_page = step.get('expected_page')
    if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page < 1:
        raise NativeNumberingInputError('expected_page must be a positive integer')
    return {'match': match, 'expected_page': expected_page}


def validate_insert_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, ('match', 'expected_page', 'text', 'position') + _STEP_ENVELOPE_KEYS, 'paragraph_insert_exact step')
    _require_confirm_layout(step, 'paragraph_insert_exact')
    validated = _validated_match(step, 'paragraph_insert_exact')
    text = step.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX_CHARS or any(ord(char) < 32 for char in text):
        raise NativeNumberingInputError(f'text must be one non-blank line of at most {TEXT_MAX_CHARS} characters')
    position = step.get('position', 'after')
    if position not in {'after', 'before'}:
        raise NativeNumberingInputError('position must be after or before')
    return {**validated, 'text': text, 'position': position}


def validate_remove_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, ('match', 'expected_page', 'expected_text', 'confirm_remove') + _STEP_ENVELOPE_KEYS, 'paragraph_remove_exact step')
    if step.get('confirm_remove') is not True:
        raise NativeNumberingInputError('paragraph_remove_exact requires confirm_remove=true')
    expected_text = step.get('expected_text')
    if not isinstance(expected_text, str) or not expected_text:
        raise NativeNumberingInputError('paragraph_remove_exact requires expected_text, the full text of the paragraph to remove')
    return {**_validated_match(step, 'paragraph_remove_exact'), 'expected_text': expected_text}


def _target(census: Mapping[str, Any], match: str) -> tuple[int, dict[str, Any]]:
    hits = _census_hits(census['paragraphs'], match, True)
    if len(hits) != 1:
        code = 'target_not_found' if not hits else 'target_ambiguous'
        raise NativeNumberingRefusal(f'the match identifies {len(hits)} paragraphs, not exactly one', code=code)
    paragraph = census['paragraphs'][hits[0]]
    if paragraph.get('body_index') is None:
        raise NativeNumberingRefusal('the target is not a body paragraph (for example a table cell)', code='target_not_in_body')
    return hits[0], paragraph


def _located(hwp: Any, match: str, paragraph: Mapping[str, Any]) -> tuple[int, int]:
    list_id, para = _locate_unique_body_paragraph(hwp, match, match_case=True)
    if para != paragraph['body_index']:
        raise NativeNumberingRefusal('the native target position disagrees with the document census', code='target_unlocatable')
    return list_id, para


def _shared_problems(before: Mapping[str, Any], after: Mapping[str, Any], *, removed_paragraph: Mapping[str, Any] | None = None) -> list[str]:
    problems = [f'{field} changed' for field in _COMPARED if before.get(field) != after.get(field)]
    for name in sorted(set(before['definitions']) | set(after['definitions'])):
        removed, added = _multiset_delta(before['definitions'].get(name, []), after['definitions'].get(name, []))
        if not removed and not added:
            continue
        if removed_paragraph is not None and not added and name in {'parashapelist', 'charshapelist'}:
            own, field = (([removed_paragraph.get('parashape_sha256')], 'parashape_sha256') if name == 'parashapelist'
                          else (removed_paragraph.get('charshape_sha256s') or [], 'charshape_sha256s'))
            if single_use_shape_left(removed, own, after['paragraphs'], field):
                continue
        problems.append(f'{name} changed')
    return problems


def _same_content(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in (
        'text', 'heading', 'level', 'section', 'content_sha256', 'numbering_ref_sha256', 'inline_controls', 'page_break', 'column_break',
    ))


def _control_sequence(hwp: Any) -> list[str]:
    try:
        return [name for _control, name in _control_ids(hwp)]
    except ControlEnumerationUnproven as exc:
        raise NativeNumberingRefusal('the document controls could not be enumerated before the edit', code='control_enumeration_unavailable') from exc


def _finish(reach: _MutationReach, problems: list[str], what: str) -> None:
    if problems:
        raise NativeNumberingMutationError(
            f'{what} outcome is not as requested: ' + '; '.join(problems[:5]) + '; nothing was rolled back'
        )


def _run(reach: _MutationReach, body: Callable[[], dict[str, Any]], what: str) -> dict[str, Any]:
    try:
        return body()
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        raise NativeNumberingMutationError(
            f'{what} failed with an exception of class {_exception_class_label(exc)} after the native call was issued; '
            'the document state is not proven and nothing was rolled back'
        ) from exc
    except BaseException as exc:
        if reach.reached:
            _mark_noncomplete(exc)
        raise


def insert_paragraph(
    hwp: Any, *, match: str, text: str, position: str, protection: Callable[[], Any], on_mutating: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        haction = _require_haction(hwp, 'GetDefault', 'Execute')
        before = _read_protection(protection)
        _require_complete(before)
        index, paragraph = _target(before, match)
        if text in {item['text'] for item in before['paragraphs']}:
            raise NativeNumberingRefusal('the new text already exists as a paragraph, so the result could not be told apart', code='target_ambiguous')
        if position == 'before' and paragraph['body_index'] == 0:
            raise NativeNumberingRefusal('inserting before the first body paragraph would split the section definition', code='target_at_document_start')
        if position == 'before' and (paragraph['page_break'] or paragraph['column_break']):
            raise NativeNumberingRefusal('inserting before a paragraph that starts a page or column is refused', code='neighbor_has_break')
        controls = _control_sequence(hwp)
        list_id, para = _located(hwp, match, paragraph)
        _set_pos(hwp, (list_id, para, 0))
        if position == 'after':
            _native_method(hwp, 'MoveParaEnd')()
        caret = _get_pos(hwp)
        if caret[:2] != (list_id, para) or _get_selection(hwp) != ():
            raise NativeNumberingRefusal('the caret is not at the requested paragraph edge', code='target_unlocatable')
        hset = hwp.HParameterSet.HInsertText
        default = haction.GetDefault('InsertText', hset.HSet)
        if not _action_confirmed(default):
            raise NativeNumberingRefusal(f'native InsertText defaults returned {_status_text(default)}', code='native_defaults_unavailable')
        hset.Text = ('\r\n' + text) if position == 'after' else (text + '\r\n')
        if _read_target_state(hwp) != (caret, ()):
            raise NativeNumberingRefusal('the caret moved while the insertion was prepared', code='target_moved')
        reach.issue()
        result = haction.Execute('InsertText', hset.HSet)
        if not _action_confirmed(result):
            raise NativeNumberingMutationError(f'native InsertText returned {_status_text(result)}; nothing was rolled back')
        after = _read_protection(protection)
        problems = _shared_problems(before, after)
        if [name for _control, name in _control_ids(hwp)] != controls:
            problems.append('the document control sequence changed')
        # Cells of the target's own table are listed right after it, so the new paragraph is found by body position.
        new_body_index = paragraph['body_index'] + (1 if position == 'after' else 0)
        new_index = next((number for number, item in enumerate(after['paragraphs']) if item.get('body_index') == new_body_index), None)
        if len(after['paragraphs']) != len(before['paragraphs']) + 1 or new_index is None:
            problems.append('the paragraph count did not grow by exactly one')
        else:
            added = after['paragraphs'][new_index]
            if added['text'] != text:
                problems.append('the new paragraph does not hold exactly the requested text')
            if (added['heading'], added['level']) != (paragraph['heading'], paragraph['level']):
                problems.append('the new paragraph did not inherit the neighbor numbering')
            rest = after['paragraphs'][:new_index] + after['paragraphs'][new_index + 1:]
            changed = [number for number, (left, right) in enumerate(zip(before['paragraphs'], rest)) if not _same_content(left, right)]
            if changed:
                problems.append(f'paragraph(s) {changed[:5]} outside the new paragraph changed')
        _finish(reach, problems, 'paragraph insertion')
        return {
            'changed': True, 'status': 'inserted', 'position': position,
            'new_paragraph': {'text': text, 'heading': paragraph['heading'], 'level': paragraph['level']},
            'native_actions': ['Execute:InsertText'],
            'protection': {'checked': True, 'compared': [*_COMPARED, 'definitions', 'paragraphs'], 'unproven': []},
        }

    return _run(reach, body, 'paragraph insertion')


def remove_paragraph(
    hwp: Any, *, match: str, expected_text: str, protection: Callable[[], Any], on_mutating: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        _require_haction(hwp, 'Run')
        before = _read_protection(protection)
        _require_complete(before)
        index, paragraph = _target(before, match)
        if paragraph['text'] != expected_text:
            raise NativeNumberingRefusal('the paragraph at this match does not hold exactly expected_text', code='target_text_mismatch')
        if paragraph['inline_controls']:
            raise NativeNumberingRefusal(
                f"the paragraph holds inline controls ({', '.join(sorted(set(paragraph['inline_controls'])))}), so removing it would remove them",
                code='target_has_controls',
            )
        following = next((item for item in before['paragraphs'] if item.get('body_index') == paragraph['body_index'] + 1), None)
        if following is None or following['section'] != paragraph['section']:
            raise NativeNumberingRefusal('the last paragraph of a section cannot be removed this way', code='target_is_last_paragraph')
        if paragraph['page_break'] or paragraph['column_break'] or following['page_break'] or following['column_break']:
            raise NativeNumberingRefusal('removing this paragraph would move a page or column break', code='neighbor_has_break')
        controls = _control_sequence(hwp)
        list_id, para = _located(hwp, match, paragraph)
        selected = hwp.select_text(para, 0, para + 1, 0, list_id)
        expected = (list_id, para, 0, list_id, para + 1, 0)
        if selected is not True or _get_selection(hwp) != expected:
            raise NativeNumberingRefusal('the paragraph could not be selected exactly', code='target_unlocatable')
        reach.issue()
        result = hwp.HAction.Run('Delete')
        if not _action_confirmed(result):
            raise NativeNumberingMutationError(f'native Delete returned {_status_text(result)}; nothing was rolled back')
        after = _read_protection(protection)
        problems = _shared_problems(before, after, removed_paragraph=paragraph)
        if [name for _control, name in _control_ids(hwp)] != controls:
            problems.append('the document control sequence changed')
        rest = before['paragraphs'][:index] + before['paragraphs'][index + 1:]
        if len(after['paragraphs']) != len(rest):
            problems.append('the paragraph count did not shrink by exactly one')
        else:
            changed = [number for number, (left, right) in enumerate(zip(rest, after['paragraphs'])) if not _same_content(left, right)]
            if changed:
                problems.append(f'paragraph(s) {changed[:5]} outside the removed paragraph changed')
        _finish(reach, problems, 'paragraph removal')
        return {
            'changed': True, 'status': 'removed',
            'removed_paragraph': {'text': paragraph['text'], 'heading': paragraph['heading'], 'level': paragraph['level']},
            'native_actions': ['Run:Delete'],
            'protection': {'checked': True, 'compared': [*_COMPARED, 'definitions', 'paragraphs'], 'unproven': []},
        }

    return _run(reach, body, 'paragraph removal')


_PARAGRAPH_LIMIT = 2000
# What a review may spend on run-by-run values; the adapter reads at most 4 MiB of response.
_FORMAT_READBACK_BYTES = 1536 * 1024
# In a review, a definition list the fully resolved document does not have: an inventory that was read and is empty.
EMPTY_INVENTORY = 'absent'
_READBACK_TEXT_LIMIT = 300


def numbering_readback(hwp: Any, *, protection: Callable[[], Any], format_model: Callable[[], Mapping[str, Any]] | None = None, format_rows: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Facts for a readable review; only in-memory reads run, and the caret is compared before and after.

    A document this code cannot use as evidence (too large, unresolved references, too many controls) is reported as
    unavailable and leaves the session as it was. An unexpected native exception propagates, and stays unknown.
    """
    try:
        state = _read_target_state(hwp)
    except NativeNumberingReadFailure:
        raise  # Unknown, so no later read follows.
    except NativeNumberingRefusal:
        state = None
    result: dict[str, Any] = {'available': True, 'reason': None, 'paragraphs': None, 'paragraphs_truncated': False, 'paragraph_total': None, 'evidence': None,
                              'page_restarts': None}
    try:
        census = _validated_census(_read_census(protection))
    except (CensusTextUnusable, _CensusUnusable):
        census = None
        result.update(available=False, reason='census_unavailable')
    if census is not None:
        result['evidence'] = {
            'header_footer': census['header_footer'], 'styles_sha256': census['styles_sha256'], 'media_sha256': census['media_sha256'],
            'numberings_sha256': census['numberings_sha256'], 'bindata_sha256': census['bindata_sha256'],
        }
        if census['paragraphs'] is not None and census['definitions'] is not None:
            # Every paragraph reference resolved, so nothing can be using a list that is not there. Storage without a list stays unproven.
            if census['numberings_sha256'] is None:
                result['evidence']['numberings_sha256'] = EMPTY_INVENTORY
            if census['bindata_sha256'] is None and census['media_sha256'] == _NO_MEDIA_SHA256:
                result['evidence']['bindata_sha256'] = EMPTY_INVENTORY
        result['page_restarts'] = [next((value for key, value in entry['attrs'] if key == 'number'), None) for entry in census['page_restarts'] or []]
        if census['paragraphs'] is None:
            result.update(available=False, reason='paragraphs_unresolved')
        else:
            result['paragraph_total'] = len(census['paragraphs'])
            result['paragraphs_truncated'] = len(census['paragraphs']) > _PARAGRAPH_LIMIT
            result['paragraphs'] = [
                {
                    'text': item['text'][:_READBACK_TEXT_LIMIT], 'heading': item['heading'], 'level': item['level'],
                    'in_body': item.get('body_index') is not None, 'content_sha256': item['content_sha256'],
                    'numbering_ref_sha256': item.get('numbering_ref_sha256'),
                }
                for item in census['paragraphs'][:_PARAGRAPH_LIMIT]
            ]
            _add_format(result, census['paragraphs'], format_model, format_rows)
    try:
        chain = _control_ids(hwp)
    except ControlEnumerationUnproven:
        result.update(controls=None, controls_reason='control_limit', page_numbers=None)
    else:
        controls: dict[str, int] = {}
        page_numbers: list[dict[str, Any] | None] = []
        for control, name in chain:
            controls[name] = controls.get(name, 0) + 1
            if name == 'pgnp':
                props = _read_page_number_props(control, allow_unset_new_number=True)
                page_numbers.append(None if props is None else {'draw_pos': props['DrawPos'], 'side_char': props['SideChar']})
        result.update(controls=controls, page_numbers=page_numbers)
    result.update(page_count=hwp.PageCount, document_is_modified=hwp.IsModified, caret_unchanged=state is not None and _read_target_state(hwp) == state)
    return result


def _add_format(result: dict[str, Any], paragraphs: list[dict[str, Any]], format_model: Callable[[], Mapping[str, Any]] | None, format_rows: Callable[[Mapping[str, Any]], dict[str, Any]] | None) -> None:
    """Each paragraph's position and its character and paragraph values; a document that cannot show them says so and stays readable."""
    for index, row in enumerate(result['paragraphs']):
        row['index'] = index
    if format_model is None or format_rows is None:
        return
    try:
        rows = format_model()['paragraphs']
    except CensusError:  # A returned text this reader cannot use; a raising native read is not this.
        rows = None
    if rows is None or [item['text'] for item in rows] != [item['text'] for item in paragraphs]:
        result['format_reason'] = 'format_unavailable'
        return
    spent = 0
    for row, item in zip(result['paragraphs'], rows):
        fact = format_rows(item)
        spent += len(json.dumps(fact['runs'], ensure_ascii=False).encode('utf-8', 'surrogatepass'))
        if spent > _FORMAT_READBACK_BYTES:
            fact = {**fact, 'runs': [], 'runs_truncated': True}
        row['context'] = item['context']
        row['format'] = fact


class _CensusUnusable(Exception):
    """The native read returned, but the result does not have the shape of a document census."""


def _read_census(protection: Callable[[], Any]) -> Any:
    raw = protection()
    if not isinstance(raw, Mapping):
        raise _CensusUnusable('census is not an object')
    return raw


_HISTORY_CHANGE_LIMIT = 50


def history_evidence(hwp: Any, protection: Callable[[], Any]) -> dict[str, Any] | None:
    """What Undo/Redo is compared with. None means this document cannot be used as evidence, which no read exception can mean."""
    try:
        census = _validated_census(protection())
    except CensusTextUnusable:
        return None
    if census['paragraphs'] is None:
        return None
    try:
        controls = [name for _control, name in _control_ids(hwp)]
    except ControlEnumerationLimit:
        return None
    return evidence_of(census, controls)


def evidence_of(census: Mapping[str, Any], controls: list[str]) -> dict[str, Any]:
    return {
        'paragraphs': [{**{key: item[key] for key in ('text', 'heading', 'level', 'content_sha256')}, 'end_charshape_sha256': item.get('end_charshape_sha256')} for item in census['paragraphs']],
        'controls': controls,
        'digests': {name: census[name] for name in ('header_footer', 'styles_sha256', 'numberings_sha256', 'media_sha256', 'page_restarts')},
    }


def history_confirmation(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]:
    """What this Undo/Redo actually changed, from the document, so the reply says more than that the call returned."""
    if before is None or after is None:
        return {'available': False, 'document_changed': None}
    old, new = before['paragraphs'], after['paragraphs']
    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, [item['text'] for item in old], [item['text'] for item in new], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != 'equal':
            changes.extend({'kind': 'removed', 'text': item['text'][:120]} for item in old[i1:i2])
            changes.extend({'kind': 'added', 'text': item['text'][:120]} for item in new[j1:j2])
            continue
        for left, right in zip(old[i1:i2], new[j1:j2]):
            if (left['heading'], left['level']) != (right['heading'], right['level']):
                changes.append({'kind': 'numbering_changed', 'text': right['text'][:120], 'before': {'heading': left['heading'], 'level': left['level']},
                                'after': {'heading': right['heading'], 'level': right['level']}})
            elif left['content_sha256'] != right['content_sha256']:
                changes.append({'kind': 'format_changed', 'text': right['text'][:120]})
            if (left['heading'], left['level']) == (right['heading'], right['level']) and left.get('end_charshape_sha256') != right.get('end_charshape_sha256'):
                changes.append({'kind': 'number_mark_changed', 'text': right['text'][:120]})
    digests = sorted(name for name in before['digests'] if before['digests'][name] != after['digests'][name])
    controls_changed = before['controls'] != after['controls']
    result: dict[str, Any] = {
        'available': True,
        'document_changed': bool(changes or digests or controls_changed),
        'paragraph_count': {'before': len(old), 'after': len(new)},
        'changes': changes[:_HISTORY_CHANGE_LIMIT],
        'changed_evidence': digests,
    }
    if controls_changed:
        result['controls'] = {'before': before['controls'], 'after': after['controls']}
    return result
