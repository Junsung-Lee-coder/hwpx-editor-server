"""Guarded body-paragraph insert and remove for numbered documents, plus a read-only numbering readback.

Native facts these rely on were observed on Hancom 2024 through pyhwpx 1.7.2: one ``InsertText`` holding ``\\r\\n``
creates one paragraph that inherits the neighbor's HeadingType and Level and is reverted by one Undo. Selecting from a
paragraph start to the next paragraph start and running one ``Delete`` removes it, keeps the next paragraph's shape and
is reverted by one Undo, but the next paragraph loses a PageBreak/ColumnBreak flag, so that case is refused.

The same census protects both edits as numbering: mandatory evidence is read before the call, and any change beyond the
requested paragraph after the call is an unknown outcome that is never rolled back.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from app.native_numbering import (
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingRefusal,
    STEP_MATCH_MAX_CHARS,
    _MutationReach,
    _STEP_ENVELOPE_KEYS,
    _action_confirmed,
    _census_hits,
    _exception_class_label,
    _get_pos,
    _get_selection,
    _locate_unique_body_paragraph,
    _mark_noncomplete,
    _native_method,
    _read_protection,
    _read_target_state,
    _reject_unknown_keys,
    _require_complete,
    _require_confirm_layout,
    _require_haction,
    _set_pos,
    _status_text,
)

TEXT_MAX_CHARS = 4000
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
    _reject_unknown_keys(step, ('match', 'expected_page', 'confirm_remove') + _STEP_ENVELOPE_KEYS, 'paragraph_remove_exact step')
    if step.get('confirm_remove') is not True:
        raise NativeNumberingInputError('paragraph_remove_exact requires confirm_remove=true')
    return _validated_match(step, 'paragraph_remove_exact')


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


def _shared_problems(before: Mapping[str, Any], after: Mapping[str, Any], *, allowed_shape_removal: bool) -> list[str]:
    problems = [f'{field} changed' for field in _COMPARED if before.get(field) != after.get(field)]
    for name in sorted(set(before['definitions']) | set(after['definitions'])):
        left, right = before['definitions'].get(name, []), after['definitions'].get(name, [])
        if left == right:
            continue
        removed = [entry for entry in left if entry not in right]
        added = [entry for entry in right if entry not in left]
        if not (allowed_shape_removal and name == 'parashapelist' and len(removed) <= 1 and not added):
            problems.append(f'{name} changed')
    return problems


def _same_content(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ('text', 'heading', 'level', 'section', 'content_sha256'))


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
        problems = _shared_problems(before, after, allowed_shape_removal=False)
        new_index = index + 1 if position == 'after' else index
        if len(after['paragraphs']) != len(before['paragraphs']) + 1:
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
    hwp: Any, *, match: str, protection: Callable[[], Any], on_mutating: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        _require_haction(hwp, 'Run')
        before = _read_protection(protection)
        _require_complete(before)
        index, paragraph = _target(before, match)
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
        problems = _shared_problems(before, after, allowed_shape_removal=True)
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


_PAGE_POSITIONS = {'bottomcenter': 'bottom-center', 'bottomleft': 'bottom-left', 'bottomright': 'bottom-right',
                   'topcenter': 'top-center', 'topleft': 'top-left', 'topright': 'top-right'}
_PARAGRAPH_LIMIT = 2000
_READBACK_TEXT_LIMIT = 300


def numbering_readback(hwp: Any, *, protection: Callable[[], Any]) -> dict[str, Any]:
    """Facts for a readable review. Only the existing in-memory readers run; the caret is compared before and after."""
    from app.native_numbering import _control_ids, _validated_census

    state = _read_target_state(hwp)
    census = _validated_census(protection())
    if not census['paragraphs']:
        raise RuntimeError('the document census has no paragraph evidence')
    controls: dict[str, int] = {}
    page_numbers = []
    for control, name in _control_ids(hwp):
        controls[name] = controls.get(name, 0) + 1
        if name == 'pgnp':
            props = control.Properties
            page_numbers.append({'draw_pos': props.Item('DrawPos'), 'side_char': props.Item('SideChar')})
    restarts = [next((value for key, value in entry['attrs'] if key == 'number'), None) for entry in census['page_restarts'] or []]
    paragraphs = [
        {
            'text': item['text'][:_READBACK_TEXT_LIMIT], 'heading': item['heading'], 'level': item['level'],
            'in_body': item.get('body_index') is not None, 'content_sha256': item['content_sha256'],
        }
        for item in census['paragraphs'][:_PARAGRAPH_LIMIT]
    ]
    return {
        'paragraphs': paragraphs,
        'paragraphs_truncated': len(census['paragraphs']) > _PARAGRAPH_LIMIT,
        'controls': controls,
        'page_numbers': page_numbers,
        'page_restarts': restarts,
        'evidence': {
            'header_footer': census['header_footer'], 'styles_sha256': census['styles_sha256'], 'media_sha256': census['media_sha256'],
            'numberings_sha256': census['numberings_sha256'], 'bindata_sha256': census['bindata_sha256'],
        },
        'page_count': hwp.PageCount,
        'document_is_modified': hwp.IsModified,
        'caret_unchanged': state is not None and _read_target_state(hwp) == state,
    }
