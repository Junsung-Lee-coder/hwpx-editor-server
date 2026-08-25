from __future__ import annotations

from typing import Any, Callable, Mapping, TypeVar
import unicodedata

from app.command_packages.commands.where.run import _style_summary
from app.edit_ops import _get_pos, _get_selected_pos, _get_selected_text, _select_text, _set_pos

T = TypeVar('T')


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    # `context` accepts no targeting fields. It observes the current live edit position only.
    return step


def _warn(warnings: list[str], message: str) -> None:
    if message and message not in warnings:
        warnings.append(message)


def _safe(name: str, func: Callable[[], T], default: T, warnings: list[str]) -> T:
    try:
        return func()
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        _warn(warnings, f'{name} unavailable: {type(exc).__name__}: {exc}')
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_text(value: Any, *, max_chars: int = 500) -> str | None:
    if value in (None, ''):
        return None
    text = str(value).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    cleaned: list[str] = []
    for ch in text:
        if ch in {'\n', '\t'}:
            cleaned.append(ch)
        elif unicodedata.category(ch).startswith('C'):
            cleaned.append(' ')
        else:
            cleaned.append(ch)
    result = ''.join(cleaned).strip()
    if len(result) > max_chars:
        return result[:max_chars] + '…'
    return result


def _clean_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, tuple):
        return [_clean_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, max_chars=800)
    return value


def _pos_parts(cursor: Mapping[str, Any]) -> dict[str, Any]:
    pos = cursor.get('pos')
    if not (isinstance(pos, list) and len(pos) >= 3):
        return {}
    try:
        return {
            'list_id': int(pos[0]),
            'paragraph_index': int(pos[1]),
            'offset': int(pos[2]),
        }
    except Exception:
        return {}


def _block_context(location: Mapping[str, Any]) -> dict[str, Any]:
    cursor = _as_dict(location.get('cursor'))
    cell_ref = _as_dict(cursor.get('cell_ref'))
    cell_addr = cursor.get('cell_addr') or cell_ref.get('addr')
    inside_table = bool(location.get('caret_in_table_cell') or cursor.get('is_cell') or location.get('cur_field_state') == 1)
    block_type = 'table_cell' if inside_table else ('paragraph' if location.get('current_paragraph_preview') else 'unknown')
    return {
        'inside_table': inside_table,
        'current_block_type': block_type,
        'cell': {
            'addr': cell_addr,
            'row_1based': cell_ref.get('row_1based'),
            'col_1based': cell_ref.get('col_1based'),
            'row_index': cell_ref.get('row_index'),
            'col_index': cell_ref.get('col_index'),
            'col_letters': cell_ref.get('col_letters'),
        } if inside_table or cell_addr else None,
        'field_name': cursor.get('field_name'),
        **_pos_parts(cursor),
    }


def _nearby_text(location: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    nearby = _as_dict(location.get('nearby_context'))
    if nearby.get('capture_error'):
        _warn(warnings, f"nearby text unavailable: {nearby.get('capture_error')}")
    if any(nearby.get(f'{name}_paragraph_error') for name in ('previous', 'current', 'next')):
        _warn(warnings, 'nearby text window is partial; one or more paragraph reads failed')
    return {
        'method': 'best_effort_adjacent_paragraphs',
        'approximation': True,
        'before': _safe_text(nearby.get('previous_paragraph_preview'), max_chars=320),
        'current': _safe_text(nearby.get('current_paragraph_preview') or location.get('current_paragraph_preview'), max_chars=320),
        'after': _safe_text(nearby.get('next_paragraph_preview'), max_chars=320),
        'cursor_pos': nearby.get('cursor_pos'),
    }


def _run_probe_action(hwp: Any, action_name: str) -> dict[str, Any]:
    hwp_run = getattr(hwp, 'Run', None)
    if callable(hwp_run):
        raw = hwp_run(action_name)
        if raw is not False:
            return {'action': action_name, 'strategy': 'hwp.Run', 'raw_result': raw}

    haction = getattr(hwp, 'HAction', None)
    haction_run = getattr(haction, 'Run', None) if haction is not None else None
    if callable(haction_run):
        raw = haction_run(action_name)
        if raw is not False:
            return {'action': action_name, 'strategy': 'hwp.HAction.Run', 'raw_result': raw}

    method = getattr(hwp, action_name, None)
    if callable(method):
        raw = method()
        if raw is not False:
            return {'action': action_name, 'strategy': f'hwp.{action_name}', 'raw_result': raw}

    raise RuntimeError(f'Hancom action {action_name!r} returned false or is unavailable')


def _capture_probe_restore_state(hwp: Any) -> dict[str, Any]:
    state: dict[str, Any] = {'pos': list(_get_pos(hwp))}
    try:
        selected = _get_selected_pos(hwp)
        state['selected_pos'] = list(selected)
        state['had_selection'] = bool(selected and selected[0])
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        state['selected_pos_error'] = str(exc)
        state['had_selection'] = False
    return state


def _restore_probe_state(hwp: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    selected = state.get('selected_pos')
    if state.get('had_selection') and isinstance(selected, list) and selected:
        try:
            _select_text(hwp, tuple(selected))
            return {'restored': True, 'strategy': 'select_text(saved_selected_pos)'}
        except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
            fallback_error = str(exc)
        else:  # pragma: no cover - defensive only.
            fallback_error = ''
    else:
        fallback_error = ''

    pos = state.get('pos')
    if isinstance(pos, list) and len(pos) >= 3:
        _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
        result = {'restored': True, 'strategy': 'set_pos(saved_pos)'}
        if fallback_error:
            result['selection_restore_error'] = fallback_error
        return result
    raise RuntimeError(f'cannot restore live position from saved state: {state!r}')


def _read_selected_text_for_probe(hwp: Any) -> str:
    return _get_selected_text(hwp, keep_select=True)


def _current_paragraph_probe(hwp: Any) -> dict[str, Any]:
    state = _capture_probe_restore_state(hwp)
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    restore: dict[str, Any] = {'restored': False}
    try:
        pos = state.get('pos')
        if not (isinstance(pos, list) and len(pos) >= 3):
            raise RuntimeError(f'unexpected cursor position shape: {pos!r}')
        list_id, para, offset = int(pos[0]), int(pos[1]), int(pos[2])
        select_text = getattr(hwp, 'select_text', None)
        if callable(select_text):
            raw = select_text(para, 0, para, -1, list_id)
            actions.append({'action': 'select_text(paragraph)', 'strategy': 'hwp.select_text', 'raw_result': raw})
            if raw is False:
                raise RuntimeError('hwp.select_text returned false while selecting current paragraph')
        else:
            _select_text(hwp, (True, list_id, para, 0, list_id, para, -1))
            actions.append({'action': 'select_text(paragraph)', 'strategy': 'hwp.select_text(saved-range)'})
        selected_pos = list(_get_selected_pos(hwp))
        text = _read_selected_text_for_probe(hwp)
        return {
            'available': True,
            'method': 'save_position+select_text(current_paragraph)+get_selected_text+restore',
            'approximation': False,
            'text': text,
            'text_preview': _safe_text(text, max_chars=320),
            'text_len': len(text),
            'cursor_offset': offset,
            'selected_pos': selected_pos,
            'actions': actions,
            'restore': restore,
            'warnings': warnings,
        }
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        warnings.append(f'paragraph probe failed: {type(exc).__name__}: {exc}')
        return {
            'available': False,
            'method': 'save_position+select_text(current_paragraph)+get_selected_text+restore',
            'approximation': True,
            'error': str(exc),
            'actions': actions,
            'restore': restore,
            'warnings': warnings,
        }
    finally:
        try:
            restore.clear()
            restore.update(_restore_probe_state(hwp, state))
        except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
            restore.clear()
            restore.update({'restored': False, 'error': str(exc)})


def _current_visual_line_probe(hwp: Any) -> dict[str, Any]:
    state = _capture_probe_restore_state(hwp)
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    restore: dict[str, Any] = {'restored': False}
    try:
        pos = state.get('pos')
        if isinstance(pos, list) and len(pos) >= 3:
            _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
            actions.append({'action': 'set_pos(saved_pos)', 'strategy': 'clear_selection_before_line_probe'})
        actions.append(_run_probe_action(hwp, 'MoveLineBegin'))
        actions.append(_run_probe_action(hwp, 'MoveSelLineEnd'))
        selected_pos = list(_get_selected_pos(hwp))
        text = _read_selected_text_for_probe(hwp)
        return {
            'available': True,
            'method': 'save_position+MoveLineBegin+MoveSelLineEnd+get_selected_text+restore',
            'approximation': False,
            'text': text,
            'text_preview': _safe_text(text, max_chars=320),
            'text_len': len(text),
            'selected_pos': selected_pos,
            'actions': actions,
            'restore': restore,
            'warnings': warnings,
        }
    except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
        warnings.append(f'visual line probe failed: {type(exc).__name__}: {exc}')
        return {
            'available': False,
            'method': 'save_position+MoveLineBegin+MoveSelLineEnd+get_selected_text+restore',
            'approximation': True,
            'error': str(exc),
            'actions': actions,
            'restore': restore,
            'warnings': warnings,
        }
    finally:
        try:
            restore.clear()
            restore.update(_restore_probe_state(hwp, state))
        except Exception as exc:  # pragma: no cover - live Hancom failures are runtime-specific.
            restore.clear()
            restore.update({'restored': False, 'error': str(exc)})


def _selection_text_probes(hwp: Any, warnings: list[str]) -> dict[str, Any]:
    paragraph = _current_paragraph_probe(hwp)
    paragraph_restore = paragraph.get('restore') if isinstance(paragraph.get('restore'), Mapping) else {}
    if paragraph_restore.get('restored') is False:
        line = {
            'available': False,
            'method': 'save_position+MoveLineBegin+MoveSelLineEnd+get_selected_text+restore',
            'approximation': True,
            'error': 'skipped because paragraph probe could not restore the original live position',
            'actions': [],
            'restore': {'restored': None, 'skipped': True},
            'warnings': ['visual line probe skipped after paragraph probe restore failure'],
        }
    else:
        line = _current_visual_line_probe(hwp)

    for probe in (paragraph, line):
        for warning in probe.get('warnings') or []:
            _warn(warnings, str(warning))
        restore = probe.get('restore') if isinstance(probe.get('restore'), Mapping) else {}
        if restore and restore.get('restored') is False:
            _warn(warnings, f"selection probe restore failed: {restore.get('error') or 'unknown error'}")
        if restore and restore.get('selection_restore_error'):
            _warn(warnings, f"selection probe restored cursor position but not original selection: {restore.get('selection_restore_error')}")
    return {
        'read_only': True,
        'mutation': None,
        'paragraph': paragraph,
        'visual_line': line,
    }


def _paragraph_number_1based(paragraph_index: Any) -> int | None:
    try:
        value = int(paragraph_index)
    except Exception:
        return None
    if value < 0:
        return None
    return value + 1


def _paragraph_context(block: Mapping[str, Any], nearby_text: Mapping[str, Any]) -> dict[str, Any]:
    local_warnings: list[str] = []
    paragraph_index = block.get('paragraph_index')
    if paragraph_index in (None, ''):
        local_warnings.append('paragraph index unavailable from current position evidence')
    if nearby_text.get('current') in (None, ''):
        local_warnings.append('current paragraph preview unavailable')
    return {
        'list_id': block.get('list_id'),
        'paragraph_index': paragraph_index,
        'paragraph_number_1based': _paragraph_number_1based(paragraph_index),
        'offset': block.get('offset'),
        'current_paragraph_preview': nearby_text.get('current'),
        'previous_paragraph_preview': nearby_text.get('before'),
        'next_paragraph_preview': nearby_text.get('after'),
        'method': 'GetPos+best_effort_adjacent_paragraphs',
        'approximation': bool(nearby_text.get('approximation') or local_warnings),
        'warnings': local_warnings,
    }


def _merge_paragraph_probe(paragraph_context: dict[str, Any], probe: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(paragraph_context)
    if probe.get('available') is True:
        preview = _safe_text(probe.get('text'), max_chars=320)
        merged.update(
            {
                'current_paragraph_preview': preview,
                'current_paragraph_text': _safe_text(probe.get('text'), max_chars=800),
                'current_paragraph_text_len': probe.get('text_len'),
                'method': probe.get('method') or merged.get('method'),
                'approximation': False,
                'probe_restore': probe.get('restore'),
            }
        )
    return merged


def _first_present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return value
    nested = mapping.get('evidence')
    if isinstance(nested, Mapping):
        for key in keys:
            value = nested.get(key)
            if value not in (None, ''):
                return value
    return None


def _line_context(page_evidence: Mapping[str, Any], location: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, Any]:
    line_index = _first_present(page_evidence, ('line_index', 'visual_line_index'))
    line_number = _first_present(page_evidence, ('line_number', 'visual_line_number'))
    exact_visual_line = line_index not in (None, '') or line_number not in (None, '')
    local_warnings: list[str] = []
    method = 'page_evidence+current_position'
    if not exact_visual_line:
        method = 'page_evidence+paragraph_offset_only'
        local_warnings.append('exact visual line number unavailable from current Hancom evidence; offset_in_paragraph is not a visual line')
    return {
        'page_current': page_evidence.get('page'),
        'page_count': location.get('page_count'),
        'line_index': line_index,
        'line_number': line_number,
        'offset_in_paragraph': block.get('offset'),
        'method': method,
        'approximation': not exact_visual_line,
        'warnings': local_warnings,
    }


def _merge_visual_line_probe(line_context: dict[str, Any], probe: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(line_context)
    if probe.get('available') is True:
        text_available = probe.get('text') not in (None, '')
        merged.update(
            {
                'current_visual_line_preview': _safe_text(probe.get('text'), max_chars=320),
                'current_visual_line_text': _safe_text(probe.get('text'), max_chars=800),
                'current_visual_line_text_len': probe.get('text_len'),
                'method': probe.get('method') or merged.get('method'),
                'approximation': not text_available,
                'probe_restore': probe.get('restore'),
            }
        )
        warnings = [item for item in (merged.get('warnings') or []) if 'exact visual line number unavailable' not in str(item)]
        if not text_available:
            warnings.append('visual line probe returned an empty selected-text result')
        merged['warnings'] = warnings
    return merged


def _nearby_objects(service: Any, hwp: Any, location: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    selected = _safe(
        'selected control summary',
        lambda: service._bundle_selected_control_summary(hwp),  # noqa: SLF001 - package runtime composes server internals.
        {'available': False, 'error': 'unavailable'},
        warnings,
    )
    return {
        'current_selected_ctrl': location.get('current_selected_ctrl'),
        'parent_ctrl': location.get('parent_ctrl'),
        'selected_control': selected,
        'inventory_scope': 'current selection/parent only; no broad control scan',
    }


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    from app.local_cli_runtime import snapshot_live_location

    warnings: list[str] = []
    location = _safe(
        'live location snapshot',
        lambda: snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
        ),
        {},
        warnings,
    )
    page_evidence = _safe(
        'page evidence',
        lambda: service._bundle_page_evidence(handle.hwp),  # noqa: SLF001
        {'page': None, 'method': None},
        warnings,
    )
    style, style_warnings = _safe(
        'style summary',
        lambda: _style_summary(service, handle.hwp),
        ({}, ['style summary unavailable']),
        warnings,
    )
    for warning in style_warnings:
        _warn(warnings, warning)

    cursor = _as_dict(location.get('cursor'))
    block = _block_context(location)
    nearby_text = _nearby_text(location, warnings)
    paragraph_context = _paragraph_context(block, nearby_text)
    text_probes = _safe(
        'selection text probes',
        lambda: _selection_text_probes(handle.hwp, warnings),
        {},
        warnings,
    )
    probe_paragraph = _as_dict(text_probes.get('paragraph'))
    if probe_paragraph:
        paragraph_context = _merge_paragraph_probe(paragraph_context, probe_paragraph)
    for warning in paragraph_context.get('warnings') or []:
        _warn(warnings, warning)
    line_context = _line_context(page_evidence, location, block)
    probe_line = _as_dict(text_probes.get('visual_line'))
    if probe_line:
        line_context = _merge_visual_line_probe(line_context, probe_line)
    for warning in line_context.get('warnings') or []:
        _warn(warnings, warning)
    objects = _nearby_objects(service, handle.hwp, location, warnings)
    compact_location = _safe(
        'compact location',
        lambda: service._bundle_compact_location(location),  # noqa: SLF001
        {},
        warnings,
    )
    if not compact_location:
        _warn(warnings, 'where-compatible compact location is unavailable')
    if nearby_text.get('approximation'):
        _warn(warnings, 'nearby_text is a best-effort adjacent-paragraph window, not an exact visual line window')

    result = {
        'schema_version': manifest.get('version') or 'local-cli/context/v1-package',
        'label': step.get('label') or 'context:edit-position',
        'read_only': True,
        'location': compact_location,
        'page': {
            'current': page_evidence.get('page'),
            'method': page_evidence.get('method'),
            'page_count': location.get('page_count'),
            'evidence': page_evidence,
        },
        'current_cursor': cursor,
        'block_context': block,
        'paragraph_context': paragraph_context,
        'line_context': line_context,
        'selection_text_probes': text_probes,
        'nearby_text': nearby_text,
        'style_summary': style,
        'nearby_objects': objects,
        'structure_signals': {
            'cur_field_state': location.get('cur_field_state'),
            'selection_mode': location.get('selection_mode'),
            'has_selection': cursor.get('has_selection'),
            'inside_table': block.get('inside_table'),
            'cell_addr': _as_dict(block.get('cell')).get('addr') if isinstance(block.get('cell'), Mapping) else None,
            'page_count': location.get('page_count'),
        },
        'document': {
            'name': location.get('document_name'),
            'path': location.get('document_path'),
            'working_copy_id': location.get('working_copy_id'),
            'is_modified': location.get('document_is_modified'),
        },
        'warnings': warnings,
    }
    return _clean_value(result), False, warnings
