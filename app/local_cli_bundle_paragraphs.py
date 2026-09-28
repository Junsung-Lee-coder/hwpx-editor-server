"""Command-bundle style and paragraph operations for LocalCliService."""

from __future__ import annotations

import re
from typing import Any


from app.edit_ops import (
    _capture_nearby_text_context,
    _delete_selection,
    _get_pos,
    _get_selected_pos,
    _get_selected_text,
    _move_doc_begin,
    _normalize_visible_text,
    _preview_text,
    _select_paragraph_with_trailing_break_for_current_selection,
    _set_pos,
    _snapshot_cursor_context,
)
from app.local_cli_runtime import (
    LocalCliRuntimeError,
    snapshot_live_location,
)
from app.local_cli_service_support import (
    _remove_visible_spaces,
)


class LocalCliBundleParagraphsMixin:
    """Command-bundle style and paragraph operations for LocalCliService."""

    def _style_parameter_snapshot(self, hwp: Any, action_name: str, set_name: str, keys: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {'available': False, 'values': {}, 'method': None, 'error': None}
        haction = getattr(hwp, 'HAction', None)
        hparameter_set = getattr(hwp, 'HParameterSet', None)
        pset = getattr(hparameter_set, set_name, None) if hparameter_set is not None else None
        hset = getattr(pset, 'HSet', None) if pset is not None else None
        get_default = getattr(haction, 'GetDefault', None) if haction is not None else None
        try:
            if callable(get_default) and pset is not None and hset is not None:
                get_default(action_name, hset)
                result['available'] = True
                result['method'] = f'HAction.GetDefault({action_name}, HParameterSet.{set_name})'
            else:
                result['error'] = f'{action_name}/{set_name} parameter set unavailable'
                return result
            values: dict[str, Any] = {}
            for key in keys:
                try:
                    value = getattr(pset, key, None)
                except Exception as exc:
                    values[key] = f'<error: {exc}>'
                    continue
                if value is None or isinstance(value, (str, int, float, bool)):
                    values[key] = value
                else:
                    values[key] = str(value)
            result['values'] = values
            return result
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            return result

    def _bundle_style_inspect(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = str(step.get('match') or '').strip() or None
        keep_position = bool(step.get('keep_position'))
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        match_evidence: dict[str, Any] | None = None
        warnings: list[str] = []
        try:
            if match:
                _move_doc_begin(hwp)
                found = False
                find_method = getattr(hwp, 'find', None)
                if callable(find_method):
                    try:
                        found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
                    except TypeError:
                        found = bool(find_method(match))
                if not found:
                    raise LocalCliRuntimeError(f'style-inspect match not found: {match!r}')
                snapshot = _snapshot_cursor_context(hwp)
                match_evidence = {
                    'found': True,
                    'pos': snapshot.get('pos'),
                    'selection_mode': snapshot.get('selection_mode'),
                    'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                    'page': self._bundle_page_evidence(hwp).get('page'),
                }

            location = snapshot_live_location(
                hwp=hwp,
                source_filename='style-inspect',
                working_copy_id='',
            )
            char_raw = self._style_parameter_snapshot(
                hwp,
                'CharShape',
                'HCharShape',
                ('Height', 'FaceNameHangul', 'FaceNameLatin', 'FaceNameHanja', 'FaceNameJapanese', 'FaceNameOther', 'Bold', 'Italic', 'Underline'),
            )
            para_raw = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('AlignType', 'LeftMargin', 'RightMargin', 'Indent', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            char_values = char_raw.get('values') if isinstance(char_raw.get('values'), dict) else {}
            para_values = para_raw.get('values') if isinstance(para_raw.get('values'), dict) else {}
            face_name = char_values.get('FaceNameHangul') or char_values.get('FaceNameLatin')
            height = char_values.get('Height')
            font_size_pt = None
            if isinstance(height, (int, float)):
                # Hancom commonly stores char height in 1/100 pt; keep raw too.
                font_size_pt = round(float(height) / 100.0, 2) if float(height) > 100 else float(height)
            if not char_raw.get('available'):
                warnings.append(str(char_raw.get('error') or 'character style unavailable'))
            if not para_raw.get('available'):
                warnings.append(str(para_raw.get('error') or 'paragraph style unavailable'))
            return {
                'schema_version': 'local-cli/style-inspect/v1',
                'read_only': True,
                'match': match,
                'match_evidence': match_evidence,
                'position': self._bundle_compact_location(location),
                'selection': {
                    'selection_summary': location.get('selection_summary'),
                    'has_selection': location.get('has_selection'),
                },
                'character': {
                    'font_size_pt': font_size_pt,
                    'height_raw': height,
                    'face_name': face_name,
                    'bold': char_values.get('Bold'),
                },
                'paragraph': {
                    'align': para_values.get('AlignType'),
                    'left_margin': para_values.get('LeftMargin'),
                    'indent': para_values.get('Indent'),
                    'line_spacing': para_values.get('LineSpacing'),
                    'line_spacing_type': para_values.get('LineSpacingType'),
                },
                'list': {
                    'enabled': None,
                    'kind': None,
                    'level': None,
                    'marker': None,
                    'note': 'list/bullet marker state is best-effort and not exposed by this first-pass primitive',
                },
                'raw': {
                    'char_shape': char_raw,
                    'para_shape': para_raw,
                },
                'warnings': warnings,
            }
        finally:
            if match and not keep_position and original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _find_text_on_expected_page(self, hwp: Any, *, match: str, expected_page: int) -> dict[str, Any]:
        _move_doc_begin(hwp)
        find_method = getattr(hwp, 'find', None)
        if not callable(find_method):
            raise LocalCliRuntimeError('exact paragraph primitive requires hwp.find')
        seen_positions: set[tuple[int, int, int]] = set()
        candidates: list[dict[str, Any]] = []
        for _ in range(200):
            try:
                found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
            except TypeError:
                found = bool(find_method(match))
            if not found:
                break
            snapshot = _snapshot_cursor_context(hwp)
            raw_pos = snapshot.get('pos') or []
            try:
                pos_key = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
            except Exception:
                pos_key = (len(seen_positions), -1, -1)
            if pos_key in seen_positions:
                break
            seen_positions.add(pos_key)
            page = self._bundle_page_evidence(hwp).get('page')
            candidates.append({'page': page, 'pos': list(pos_key), 'snapshot': snapshot})
            if page is not None and int(page) == int(expected_page):
                return {'page': page, 'pos': list(pos_key), 'snapshot': snapshot, 'candidates': candidates}
        pages = [item.get('page') for item in candidates]
        raise LocalCliRuntimeError(f'exact paragraph primitive could not find {match!r} on expected_page={expected_page}; candidate_pages={pages!r}')

    def _find_paragraph_delete_target(
        self,
        hwp: Any,
        *,
        match: str,
        expected_page: int,
        occurrence_on_page: int,
        expected_previous_contains: str | None,
        expected_next_contains: str | None,
    ) -> dict[str, Any]:
        _move_doc_begin(hwp)
        find_method = getattr(hwp, 'find', None)
        if not callable(find_method):
            raise LocalCliRuntimeError('paragraph_delete_exact requires hwp.find')
        seen_positions: set[tuple[int, int, int]] = set()
        candidates: list[dict[str, Any]] = []
        matches_on_page: list[dict[str, Any]] = []
        for _ in range(500):
            try:
                found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
            except TypeError:
                found = bool(find_method(match))
            if not found:
                break
            snapshot = _snapshot_cursor_context(hwp)
            raw_pos = snapshot.get('pos') or []
            try:
                pos_key = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
            except Exception:
                pos_key = (len(seen_positions), -1, -1)
            if pos_key in seen_positions:
                break
            seen_positions.add(pos_key)
            page = self._bundle_page_evidence(hwp).get('page')
            context = _capture_nearby_text_context(hwp)
            selected_text = ''
            try:
                selected_text = _get_selected_text(hwp, keep_select=True)
            except Exception:
                selected_text = ''
            item = {
                'page': page,
                'pos': list(pos_key),
                'snapshot': snapshot,
                'context': context,
                'selected_text_preview': _preview_text(selected_text, limit=120),
                'selected_text_hash': self._text_proof_hash(selected_text),
            }
            candidates.append(item)
            if page is None or int(page) != int(expected_page):
                continue
            previous_preview = str(context.get('previous_paragraph_preview') or '')
            next_preview = str(context.get('next_paragraph_preview') or '')
            if expected_previous_contains and expected_previous_contains not in previous_preview:
                continue
            if expected_next_contains and expected_next_contains not in next_preview:
                continue
            matches_on_page.append(item)
            if len(matches_on_page) == occurrence_on_page:
                return {**item, 'candidates': candidates, 'filtered_match_count': len(matches_on_page)}
        pages = [item.get('page') for item in candidates]
        contexts = [
            {
                'page': item.get('page'),
                'pos': item.get('pos'),
                'previous': (item.get('context') or {}).get('previous_paragraph_preview'),
                'current': (item.get('context') or {}).get('current_paragraph_preview'),
                'next': (item.get('context') or {}).get('next_paragraph_preview'),
            }
            for item in candidates[:20]
        ]
        raise LocalCliRuntimeError(
            f'paragraph_delete_exact could not find occurrence_on_page={occurrence_on_page} for {match!r} '
            f'on expected_page={expected_page}; candidate_pages={pages!r}; contexts={contexts!r}'
        )

    def _bundle_paragraph_delete_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_delete_exact requires positive expected_page')
        if not bool(step.get('confirm_remove')):
            raise LocalCliRuntimeError('paragraph_delete_exact requires confirm_remove=true')
        occurrence_on_page = int(step.get('occurrence_on_page') or 1)
        expected_previous_contains = str(step.get('expected_previous_contains') or '').strip() or None
        expected_next_contains = str(step.get('expected_next_contains') or '').strip() or None
        max_page_after = int(step.get('max_page_after') or expected_page)

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_lines = [_normalize_visible_text(line) for line in re.split(r'[\r\n]+', before_text)]
            before_exact_line_count = sum(1 for line in before_lines if line == _normalize_visible_text(match))
            if before_exact_line_count <= 0:
                raise LocalCliRuntimeError(
                    f'paragraph_delete_exact could not prove an exact visible line {match!r} before mutation; refusing cleanup'
                )

            target = self._find_paragraph_delete_target(
                hwp,
                match=match,
                expected_page=expected_page,
                occurrence_on_page=occurrence_on_page,
                expected_previous_contains=expected_previous_contains,
                expected_next_contains=expected_next_contains,
            )
            context_before = _capture_nearby_text_context(hwp)
            _select_paragraph_with_trailing_break_for_current_selection(hwp)
            selected_range = _get_selected_pos(hwp)
            selected_text = _get_selected_text(hwp, keep_select=True)
            selected_normalized = _normalize_visible_text(selected_text)
            if _normalize_visible_text(match) not in selected_normalized:
                raise LocalCliRuntimeError(
                    'paragraph_delete_exact selected text does not contain the target match; '
                    f'match={match!r}; selected={_preview_text(selected_text, limit=160)!r}; range={selected_range!r}'
                )
            if len(selected_text) > 5000:
                raise LocalCliRuntimeError('paragraph_delete_exact selected more than 5,000 chars; refusing possible overselection')
            _delete_selection(hwp)

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_lines = [_normalize_visible_text(line) for line in re.split(r'[\r\n]+', after_text)]
            after_exact_line_count = sum(1 for line in after_lines if line == _normalize_visible_text(match))
            if after_exact_line_count != max(0, before_exact_line_count - 1):
                undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                try:
                    if callable(undo):
                        undo('Undo')
                except Exception:
                    pass
                raise LocalCliRuntimeError(
                    'paragraph_delete_exact post-proof failed: exact visible line count did not decrease by one; '
                    f'before={before_exact_line_count} after={after_exact_line_count}; undo attempted'
                )
            if expected_next_contains:
                after_next_target = self._find_text_on_expected_page(hwp, match=expected_next_contains, expected_page=min(max_page_after, expected_page))
                if after_next_target.get('page') is None or int(after_next_target.get('page')) > max_page_after:
                    raise LocalCliRuntimeError(
                        f'paragraph_delete_exact next guard exceeded max_page_after={max_page_after}: {after_next_target.get("page")}'
                    )
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-delete-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-delete-exact',
                'match': match,
                'expected_page': expected_page,
                'occurrence_on_page': occurrence_on_page,
                'target_before': target,
                'context_before': context_before,
                'context_after': context_after,
                'selected_range': list(selected_range) if isinstance(selected_range, (list, tuple)) else selected_range,
                'selected_text_preview': _preview_text(selected_text, limit=160),
                'selected_text_hash': self._text_proof_hash(selected_text),
                'exact_line_count_before': before_exact_line_count,
                'exact_line_count_after': after_exact_line_count,
                'visible_text_hash_before': self._text_proof_hash(before_text),
                'visible_text_hash_after': self._text_proof_hash(after_text),
                'warnings': ['Destructive paragraph removal; rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_join_previous_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_join_previous_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_join_previous_exact requires confirm_layout=true')
        delete_back_count = int(step.get('delete_back_count') or 1)
        if not (1 <= delete_back_count <= 5):
            raise LocalCliRuntimeError('paragraph_join_previous_exact delete_back_count must be 1..5')
        max_page_after = int(step.get('max_page_after') or expected_page)
        expected_previous_contains = str(step.get('expected_previous_contains') or '').strip() or None

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_visible_no_space = _remove_visible_spaces(before_text)

            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_join_previous_exact expected active selection after find')
            _, slist, spara, spos, _elist, _epara, _epos = selected
            _set_pos(hwp, int(slist), int(spara), 0)
            context_before = _capture_nearby_text_context(hwp)
            if expected_previous_contains:
                previous_preview = str(context_before.get('previous_paragraph_preview') or '')
                if expected_previous_contains not in previous_preview:
                    raise LocalCliRuntimeError(
                        f'paragraph_join_previous_exact previous paragraph guard failed: expected {expected_previous_contains!r}; got {previous_preview!r}'
                    )

            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('paragraph_join_previous_exact requires HAction.Run')
            raw_results = []
            for _i in range(delete_back_count):
                raw_results.append(run('DeleteBack'))

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_visible_no_space = _remove_visible_spaces(after_text)
            if before_visible_no_space and after_visible_no_space != before_visible_no_space:
                undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                try:
                    if callable(undo):
                        for _i in range(delete_back_count):
                            undo('Undo')
                except Exception:
                    pass
                raise LocalCliRuntimeError('paragraph_join_previous_exact changed visible text; undo attempted and mutation refused')

            after_target = self._find_text_on_expected_page(hwp, match=match, expected_page=min(expected_page, max_page_after))
            after_page = after_target.get('page')
            if after_page is None or int(after_page) > max_page_after:
                raise LocalCliRuntimeError(f'paragraph_join_previous_exact target page after mutation exceeds max_page_after={max_page_after}: {after_page}')
            _set_pos(hwp, int(after_target['pos'][0]), int(after_target['pos'][1]), 0)
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-join-previous-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-join-previous',
                'match': match,
                'expected_page': expected_page,
                'delete_back_count': delete_back_count,
                'target_before': target,
                'target_after': after_target,
                'context_before': context_before,
                'context_after': context_after,
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'raw_results': [bool(item) if item is not None else None for item in raw_results],
                'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_join_next_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_join_next_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_join_next_exact requires confirm_layout=true')
        delete_count = int(step.get('delete_count') or 1)
        if not (1 <= delete_count <= 5):
            raise LocalCliRuntimeError('paragraph_join_next_exact delete_count must be 1..5')
        next_match = str(step.get('next_match') or '').strip() or None
        max_next_page_after = int(step.get('max_next_page_after') or expected_page)
        expected_next_contains = str(step.get('expected_next_contains') or '').strip() or None

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_visible_no_space = _remove_visible_spaces(before_text)

            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_join_next_exact expected active selection after find')
            _, _slist, _spara, _spos, elist, epara, epos = selected
            _set_pos(hwp, int(elist), int(epara), int(epos))
            moved_to_line_end = False
            if bool(step.get('move_to_line_end')):
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                if not callable(run):
                    raise LocalCliRuntimeError('paragraph_join_next_exact requires HAction.Run')
                run('MoveLineEnd')
                moved_to_line_end = True
            context_before = _capture_nearby_text_context(hwp)
            if expected_next_contains:
                next_preview = str(context_before.get('next_paragraph_preview') or '')
                if expected_next_contains not in next_preview:
                    raise LocalCliRuntimeError(
                        f'paragraph_join_next_exact next paragraph guard failed: expected {expected_next_contains!r}; got {next_preview!r}'
                    )

            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('paragraph_join_next_exact requires HAction.Run')
            raw_results = []
            for _i in range(delete_count):
                raw_results.append(run('Delete'))
            inserted_line_break = False
            if bool(step.get('insert_line_break')):
                raw_results.append(run('BreakLine'))
                inserted_line_break = True

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_visible_no_space = _remove_visible_spaces(after_text)
            if before_visible_no_space and after_visible_no_space != before_visible_no_space:
                try:
                    for _i in range(delete_count):
                        run('Undo')
                except Exception:
                    pass
                diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
                before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
                after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
                raise LocalCliRuntimeError(
                    'paragraph_join_next_exact changed visible text; undo attempted and mutation refused; '
                    f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                    f'before_frag={before_frag!r} after_frag={after_frag!r}'
                )

            after_target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            after_next = None
            if next_match:
                # The page target may move upward after a successful join, so search the expected page first,
                # then permit a bounded target page through the same exact finder.
                try:
                    after_next = self._find_text_on_expected_page(hwp, match=next_match, expected_page=max_next_page_after)
                except LocalCliRuntimeError:
                    after_next = self._find_text_on_expected_page(hwp, match=next_match, expected_page=expected_page)
                next_page = after_next.get('page')
                if next_page is None or int(next_page) > max_next_page_after:
                    raise LocalCliRuntimeError(f'paragraph_join_next_exact next_match page after mutation exceeds max_next_page_after={max_next_page_after}: {next_page}')
            _set_pos(hwp, int(after_target['pos'][0]), int(after_target['pos'][1]), int(after_target['pos'][2]))
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-join-next-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-join-next',
                'match': match,
                'expected_page': expected_page,
                'delete_count': delete_count,
                'inserted_line_break': inserted_line_break,
                'moved_to_line_end': moved_to_line_end,
                'target_before': target,
                'target_after': after_target,
                'next_after': after_next,
                'context_before': context_before,
                'context_after': context_after,
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'raw_results': [bool(item) if item is not None else None for item in raw_results],
                'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_style_apply_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires confirm_layout=true')
        desired: dict[str, Any] = {}
        if step.get('keep_with_next') is not None:
            desired['KeepWithNext'] = 1 if bool(step.get('keep_with_next')) else 0
        if step.get('widow_orphan') is not None:
            desired['WidowOrphan'] = 1 if bool(step.get('widow_orphan')) else 0
        if step.get('pagebreak_before') is not None:
            desired['PagebreakBefore'] = int(step.get('pagebreak_before'))
        if not desired:
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires at least one style field')

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            text_occurrence_count = None
            try:
                if hasattr(hwp, 'get_text_file'):
                    text_occurrence_count = str(hwp.get_text_file('UNICODE', '') or '').count(match)
                elif hasattr(hwp, 'GetTextFile'):
                    text_occurrence_count = str(hwp.GetTextFile('UNICODE', '') or '').count(match)
            except Exception:
                text_occurrence_count = None
            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            target_snapshot = self._bundle_compact_snapshot(hwp)
            page = target.get('page')
            if page is None:
                raise LocalCliRuntimeError('paragraph_style_apply_exact cannot prove target page')
            before_style = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('KeepWithNext', 'WidowOrphan', 'PagebreakBefore', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            set_para = getattr(hwp, 'set_para', None)
            if not callable(set_para):
                raise LocalCliRuntimeError('paragraph_style_apply_exact requires pyhwpx set_para')
            raw = set_para(**desired)
            after_style = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('KeepWithNext', 'WidowOrphan', 'PagebreakBefore', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            before_values = before_style.get('values') if isinstance(before_style.get('values'), dict) else {}
            after_values = after_style.get('values') if isinstance(after_style.get('values'), dict) else {}
            changed = {
                key: {'before': before_values.get(key), 'after': after_values.get(key)}
                for key in desired
                if before_values.get(key) != after_values.get(key)
            }
            satisfied = {key: after_values.get(key) for key in desired if after_values.get(key) == desired.get(key)}
            if len(satisfied) != len(desired):
                raise LocalCliRuntimeError(
                    f'paragraph_style_apply_exact did not observe desired style values; desired={desired!r}; before={before_values!r}; after={after_values!r}'
                )
            after_snapshot = self._bundle_compact_snapshot(hwp)
            return {
                'schema_version': 'local-cli/paragraph-style-apply-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-style-apply',
                'match': match,
                'expected_page': expected_page,
                'text_occurrence_count': text_occurrence_count,
                'target': {'page': page, 'snapshot': target_snapshot, 'finder': target},
                'operation': {'desired': desired, 'raw_result': bool(raw) if raw is not None else None},
                'before_style': before_style,
                'after_style': after_style,
                'changed_style': changed,
                'after': after_snapshot,
                'warnings': ['This primitive mutates one unique matched paragraph style only; rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
