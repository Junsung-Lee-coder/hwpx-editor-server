"""Command-bundle control, table, and cell operations for LocalCliService."""

from __future__ import annotations

import inspect
import json
import hashlib
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Mapping


from app.edit_ops import (
    _capture_nearby_text_context,
    _delete_ctrl,
    _enumerate_controls_headctrl,
    _get_ctrl_anchor_pos,
    _get_pos,
    _get_selected_pos,
    _get_selected_text,
    _move_doc_begin,
    _preview_text,
    _set_pos,
    _snapshot_cursor_context,
)
from app.local_cli_runtime import (
    LocalCliRuntimeError,
    LocalCliRuntimeHandle,
    insert_text_at_caret,
)
from app.local_cli_service_support import (
    _CELL_MARGIN_KEYS,
    _normalize_cell_margin_readback,
    _normalize_vertical_align_readback,
    _normalize_cell_addr_value,
    _valid_numeric_readback,
    _require_observed_cell_format_mutation,
    _remove_visible_spaces,
    _MACRO_MAX_STRING_CHARS,
    LocalCliMutationError,
)


class LocalCliBundleControlsMixin:
    """Command-bundle control, table, and cell operations for LocalCliService."""

    def _bundle_require_text(self, step: dict[str, Any], field_name: str, *, max_chars: int = _MACRO_MAX_STRING_CHARS) -> str:
        value = step.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise LocalCliRuntimeError(f'command-bundle {step.get("op")} requires non-empty {field_name}')
        if len(value) > max_chars:
            raise LocalCliRuntimeError(f'command-bundle {step.get("op")} {field_name} is too long')
        return value

    def _bundle_compact_snapshot(self, hwp: Any) -> dict[str, Any]:
        try:
            snapshot = _snapshot_cursor_context(hwp)
        except Exception:
            return {'error': 'native location snapshot unavailable'}
        return {
            'pos': snapshot.get('pos'),
            'cell_addr': snapshot.get('cell_addr'),
            'is_cell': snapshot.get('is_cell'),
            'has_selection': snapshot.get('has_selection'),
            'selection_mode': snapshot.get('selection_mode'),
            'current_paragraph_preview': None,
        }


    def _bundle_is_cell(self, hwp: Any) -> bool:
        is_cell_method = getattr(hwp, 'is_cell', None)
        if callable(is_cell_method):
            try:
                return bool(is_cell_method())
            except Exception:
                return False
        try:
            snapshot = self._bundle_compact_snapshot(hwp)
            return bool(snapshot.get('is_cell'))
        except Exception:
            return False

    def _bundle_current_cell_addr_tuple(self, hwp: Any) -> tuple[int, int] | None:
        getter = getattr(hwp, 'get_cell_addr', None)
        if not callable(getter):
            return None
        for args, kwargs in ((( ), {'as_': 'tuple'}), (( ), {'as_': 'str'}), (( ), {})):
            try:
                value = getter(*args, **kwargs)
            except Exception:
                continue
            if isinstance(value, tuple) and len(value) >= 2:
                try:
                    return (int(value[0]), int(value[1]))
                except Exception:
                    continue
            if isinstance(value, str):
                text = value.strip().upper()
                match = re.fullmatch(r'([A-Z]+)(\d+)', text)
                if match:
                    col = 0
                    for char in match.group(1):
                        col = col * 26 + (ord(char) - ord('A') + 1)
                    return (col - 1, int(match.group(2)) - 1)
        return None

    def _bundle_compact_location(self, location: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(location, dict):
            return {}
        return {
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
            'current_paragraph_preview': None,
            'caret_in_table_cell': location.get('caret_in_table_cell'),
            'document_is_modified': location.get('document_is_modified'),
        }

    def _bundle_page_evidence(self, hwp: Any) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        for name in ('current_page', 'get_current_page', 'GetCurrentPage'):
            method = getattr(hwp, name, None)
            if not callable(method):
                continue
            try:
                value = method()
                if isinstance(value, int) and value > 0:
                    return {'page': int(value), 'method': name}
            except Exception as exc:
                evidence[f'{name}_error'] = str(exc)
        for name in ('Page', 'CurrentPage'):
            try:
                value = getattr(hwp, name, None)
            except Exception as exc:
                evidence[f'{name}_error'] = str(exc)
                continue
            if isinstance(value, int) and value > 0:
                return {'page': int(value), 'method': name}
        key_indicator = getattr(hwp, 'KeyIndicator', None)
        if callable(key_indicator):
            try:
                raw = key_indicator()
                evidence['key_indicator_preview'] = self._macro_result_preview(raw)
                if isinstance(raw, (list, tuple)):
                    # pyhwpx/Hancom KeyIndicator returns several 1-based status
                    # values. In live checks, index 3 tracks the rendered page,
                    # while earlier integer fields can be constant status/list
                    # values. Prefer index 3 when present; fall back only after.
                    preferred_indexes = [3, 2, 1, 0]
                    for index in preferred_indexes:
                        if index >= len(raw):
                            continue
                        value = raw[index]
                        if isinstance(value, int) and value > 0:
                            return {'page': int(value), 'method': f'KeyIndicator[{index}]', 'raw_preview': evidence.get('key_indicator_preview')}
                        if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
                            return {'page': int(value.strip()), 'method': f'KeyIndicator[{index}]', 'raw_preview': evidence.get('key_indicator_preview')}
            except Exception as exc:
                evidence['KeyIndicator_error'] = str(exc)
        return {'page': None, 'method': None, **evidence}

    def _bundle_control_proof_item(self, hwp: Any, ctrl: Any, index: int) -> tuple[dict[str, Any], dict[str, Any], tuple[int, int, int] | None]:
        anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=1)
        snapshot: dict[str, Any] = {}
        page_evidence: dict[str, Any] = {'page': None, 'method': None}
        if anchor_pos is not None:
            _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
            snapshot = _snapshot_cursor_context(hwp)
            page_evidence = self._bundle_page_evidence(hwp)
        page = page_evidence.get('page')
        ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
        ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
        user_desc = self._bundle_control_scalar(ctrl, 'UserDesc')
        type_name = self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType')
        bounds = {
            key: self._bundle_control_scalar(ctrl, key)
            for key in ('X', 'Y', 'Width', 'Height', 'HorzRelTo', 'VertRelTo')
            if self._bundle_control_scalar(ctrl, key) not in (None, '')
        }
        proof_basis = {
            'index': index,
            'ctrl_id': ctrl_id,
            'ctrl_inst_id': ctrl_inst_id,
            'user_desc': user_desc,
            'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
            'bounds': bounds or None,
            'page': page,
            'type': type_name,
        }
        proof_hash = 'sha256:' + hashlib.sha256(json.dumps(proof_basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]
        stable_id = f"ctrl/{index}/{ctrl_id or 'unknown'}/{ctrl_inst_id or 'no-inst'}"
        item = {
            'target_id': stable_id,
            'index': index,
            'page': page,
            'page_evidence': page_evidence,
            'type': str(type_name or ctrl_id or 'control'),
            'ctrl_id': ctrl_id,
            'ctrl_inst_id': ctrl_inst_id,
            'bounds': bounds or None,
            'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
            'text_preview': _preview_text(str(user_desc or ''), limit=120) if user_desc else None,
            'proof_hash': proof_hash,
            'nearby': {
                'cell_addr': snapshot.get('cell_addr'),
                'field_name': snapshot.get('field_name'),
                'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
            },
        }
        return item, snapshot, anchor_pos

    def _bundle_find_anchor_evidence(self, hwp: Any, anchor: str | None) -> dict[str, Any] | None:
        if not anchor:
            return None
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            _move_doc_begin(hwp)
            found = False
            find_method = getattr(hwp, 'find', None)
            if callable(find_method):
                try:
                    found = bool(find_method(anchor, direction='Forward', MatchCase=1, WholeWordOnly=0))
                except TypeError:
                    found = bool(find_method(anchor))
            snapshot = _snapshot_cursor_context(hwp) if found else {}
            return {
                'anchor': anchor,
                'found': found,
                'pos': snapshot.get('pos'),
                'page': self._bundle_page_evidence(hwp).get('page') if found else None,
                'selection_mode': snapshot.get('selection_mode'),
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_control_scalar(self, ctrl: Any, attr: str) -> Any:
        method_fallbacks = {
            'CtrlInstID': ('GetCtrlInstID',),
        }
        try:
            value = getattr(ctrl, attr, None)
        except Exception:
            value = None
        if callable(value):
            value = None
        if value is None:
            for method_name in method_fallbacks.get(attr, ()):
                try:
                    method = getattr(ctrl, method_name, None)
                except Exception:
                    method = None
                if not callable(method):
                    continue
                try:
                    value = method()
                    break
                except Exception:
                    value = None
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _capture_control_map_signature(self, hwp: Any, *, max_controls: int = 2048) -> dict[str, Any]:
        """Capture a bounded control count/type signature without moving the caret."""

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        type_sequence: list[str] = []
        identity_sequence: list[dict[str, str]] = []
        type_counts: dict[str, int] = {}
        for ctrl in controls:
            ctrl_id = str(self._bundle_control_scalar(ctrl, 'CtrlID') or '')
            ctrl_inst_id = str(self._bundle_control_scalar(ctrl, 'CtrlInstID') or '')
            type_name = str(
                self._bundle_control_scalar(ctrl, 'Type')
                or self._bundle_control_scalar(ctrl, 'ShapeType')
                or ctrl_id
                or 'control'
            )
            type_sequence.append(type_name)
            identity_sequence.append({'ctrl_id': ctrl_id, 'ctrl_inst_id': ctrl_inst_id, 'type': type_name})
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
        sequence_payload = json.dumps(type_sequence, ensure_ascii=False, separators=(',', ':'))
        identity_payload = json.dumps(identity_sequence, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        return {
            'control_count': len(controls),
            'type_counts': dict(sorted(type_counts.items())),
            'type_sequence_sha256': hashlib.sha256(sequence_payload.encode('utf-8')).hexdigest(),
            'identity_sequence_sha256': hashlib.sha256(identity_payload.encode('utf-8')).hexdigest(),
            'enumeration_mode': enumeration_mode,
        }

    def _assert_control_map_unchanged(
        self,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        before_count = int(before.get('control_count') or 0)
        after_count = int(after.get('control_count') or 0)
        before_types = dict(before.get('type_counts') or {})
        after_types = dict(after.get('type_counts') or {})
        before_sequence = str(before.get('type_sequence_sha256') or '')
        after_sequence = str(after.get('type_sequence_sha256') or '')
        before_identity = str(before.get('identity_sequence_sha256') or '')
        after_identity = str(after.get('identity_sequence_sha256') or '')
        added_types = {
            key: int(after_types.get(key, 0)) - int(before_types.get(key, 0))
            for key in sorted(set(before_types) | set(after_types))
            if int(after_types.get(key, 0)) > int(before_types.get(key, 0))
        }
        removed_types = {
            key: int(before_types.get(key, 0)) - int(after_types.get(key, 0))
            for key in sorted(set(before_types) | set(after_types))
            if int(before_types.get(key, 0)) > int(after_types.get(key, 0))
        }
        passed = (
            before_count == after_count
            and before_types == after_types
            and before_sequence == after_sequence
            and before_identity == after_identity
        )
        proof = {
            'passed': passed,
            'operation': operation,
            'before_control_count': before_count,
            'after_control_count': after_count,
            'before_type_counts': before_types,
            'after_type_counts': after_types,
            'added_types': added_types,
            'removed_types': removed_types,
            'before_type_sequence_sha256': before_sequence,
            'after_type_sequence_sha256': after_sequence,
            'before_identity_sequence_sha256': before_identity,
            'after_identity_sequence_sha256': after_identity,
        }
        if not passed:
            raise LocalCliRuntimeError(
                'CONTROL_DRIFT: '
                f'{operation} changed the native control map '
                f'{before_count}->{after_count}; '
                f'added_types={json.dumps(added_types, ensure_ascii=False, sort_keys=True)}; '
                f'removed_types={json.dumps(removed_types, ensure_ascii=False, sort_keys=True)}; '
                f'identity_sha256={before_identity}->{after_identity}. '
                'The working copy was not saved; close without saving and retry with a structure-preserving primitive.'
            )
        return proof

    def _bundle_selected_control_summary(self, hwp: Any) -> dict[str, Any]:
        try:
            ctrl = getattr(hwp, 'CurSelectedCtrl', None)
        except Exception as exc:
            return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
        if ctrl is None:
            return {'available': False, 'control': None}
        summary = {
            'available': True,
            'ctrl_id': self._bundle_control_scalar(ctrl, 'CtrlID'),
            'ctrl_inst_id': self._bundle_control_scalar(ctrl, 'CtrlInstID'),
            'user_desc': self._bundle_control_scalar(ctrl, 'UserDesc'),
            'type': self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType'),
        }
        summary['shape_properties'] = self._shape_prop_snapshot(ctrl)
        return summary

    def _bundle_selected_control_matches_target(self, selected: dict[str, Any], target_item: dict[str, Any]) -> tuple[bool, str]:
        target_inst_id = str(target_item.get('ctrl_inst_id') or '').strip()
        selected_inst_id = str(selected.get('ctrl_inst_id') or '').strip()
        if target_inst_id and selected_inst_id:
            return selected_inst_id == target_inst_id, 'ctrl_inst_id'
        target_id = str(target_item.get('ctrl_id') or target_item.get('type') or '').strip()
        selected_id = str(selected.get('ctrl_id') or selected.get('type') or '').strip()
        if target_id and selected_id:
            return selected_id == target_id, 'ctrl_id-fallback'
        return False, 'unavailable'

    def _bundle_select_control_exact(self, hwp: Any, ctrl: Any, target_item: dict[str, Any]) -> dict[str, Any]:
        """Try documented pyhwpx control selection, preferring Hancom 2024 CtrlInstID.

        The returned proof is explicit about method strength.  Exact CtrlInstID
        selection is preferred, but older runtimes may only support weaker
        object/anchor fallback selection; those paths are reported instead of
        being hidden.
        """

        attempts: list[dict[str, Any]] = []
        target_inst_id = str(target_item.get('ctrl_inst_id') or self._bundle_control_scalar(ctrl, 'CtrlInstID') or '').strip()
        target_ctrl_id = str(target_item.get('ctrl_id') or '').strip()

        def _record_attempt(method: str, raw_result: Any = None, error: Exception | None = None, **extra: Any) -> dict[str, Any]:
            if error is not None:
                attempt = {'method': method, 'error': f'{type(error).__name__}: {error}', **extra}
                attempts.append(attempt)
                return attempt
            selected = self._bundle_selected_control_summary(hwp)
            matches, match_basis = self._bundle_selected_control_matches_target(selected, target_item)
            selected_available = bool(selected.get('available'))
            succeeded = matches or bool(raw_result) or (raw_result is None and selected_available)
            proof_strength = 'exact-ctrl-inst-id' if matches and match_basis == 'ctrl_inst_id' else ('fallback-selected-control' if matches else 'unverified-native-return')
            attempt = {
                'method': method,
                'result': bool(raw_result) if raw_result is not None else None,
                'selection_succeeded': succeeded,
                'selected_matches_target': matches,
                'match_basis': match_basis,
                'proof_strength': proof_strength,
                'selected_control': selected,
                **extra,
            }
            attempts.append(attempt)
            return attempt

        select_ctrl_exact = getattr(hwp, 'SelectCtrl', None)
        if target_inst_id and callable(select_ctrl_exact):
            for args in ((str(target_inst_id), 1), (str(target_inst_id),), (target_inst_id, 1), (target_inst_id,)):
                try:
                    raw = select_ctrl_exact(*args)
                    attempt = _record_attempt('SelectCtrl(GetCtrlInstID)', raw, ctrl_inst_id=target_inst_id, args=list(args))
                    if attempt.get('selected_matches_target'):
                        return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id, 'target_ctrl_id': target_ctrl_id, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
                except Exception as exc:
                    _record_attempt('SelectCtrl(GetCtrlInstID)', error=exc, ctrl_inst_id=target_inst_id, args=list(args))
        elif not target_inst_id:
            attempts.append({'method': 'SelectCtrl(GetCtrlInstID)', 'skipped': True, 'reason': 'target control has no CtrlInstID'})
        else:
            attempts.append({'method': 'SelectCtrl(GetCtrlInstID)', 'skipped': True, 'reason': 'hwp.SelectCtrl unavailable'})

        select_ctrl_object = getattr(hwp, 'select_ctrl', None)
        if callable(select_ctrl_object):
            try:
                raw = select_ctrl_object(ctrl)
                attempt = _record_attempt('select_ctrl(ctrl)', raw)
                if attempt.get('selection_succeeded'):
                    return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id or None, 'target_ctrl_id': target_ctrl_id or None, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
            except Exception as exc:
                _record_attempt('select_ctrl(ctrl)', error=exc)
        else:
            attempts.append({'method': 'select_ctrl(ctrl)', 'skipped': True, 'reason': 'hwp.select_ctrl unavailable'})

        anchor_pos = target_item.get('anchor_pos')
        if isinstance(anchor_pos, list) and len(anchor_pos) >= 3:
            try:
                _set_pos(hwp, int(anchor_pos[0]), int(anchor_pos[1]), int(anchor_pos[2]))
                find_ctrl = getattr(hwp, 'FindCtrl', None) or getattr(hwp, 'find_ctrl', None)
                if callable(find_ctrl):
                    raw = find_ctrl()
                    attempt = _record_attempt('anchor_pos+FindCtrl', raw, anchor_pos=anchor_pos)
                    if attempt.get('selection_succeeded'):
                        return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id or None, 'target_ctrl_id': target_ctrl_id or None, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
                else:
                    attempts.append({'method': 'anchor_pos+FindCtrl', 'skipped': True, 'reason': 'FindCtrl unavailable', 'anchor_pos': anchor_pos})
            except Exception as exc:
                _record_attempt('anchor_pos+FindCtrl', error=exc, anchor_pos=anchor_pos)
        else:
            attempts.append({'method': 'anchor_pos+FindCtrl', 'skipped': True, 'reason': 'target anchor_pos unavailable'})

        return {
            'selection_succeeded': False,
            'method_used': None,
            'proof_strength': 'unavailable',
            'target_ctrl_inst_id': target_inst_id or None,
            'target_ctrl_id': target_ctrl_id or None,
            'selected_control': self._bundle_selected_control_summary(hwp),
            'attempts': attempts,
        }

    def _bundle_resolve_control_target(self, hwp: Any, step: dict[str, Any], *, op_name: str, require_table: bool = False) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if expected_page <= 0:
            raise LocalCliRuntimeError(f'{op_name} requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'{op_name} section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'{op_name} around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'{op_name} target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if require_table and before_item.get('type') != 'tbl':
            raise LocalCliRuntimeError(f"{op_name} target must be a table control, got {before_item.get('type')!r}")
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(f"{op_name} proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}")
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'{op_name} cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'{op_name} expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'{op_name} page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')
        return {
            'target_id': target_id,
            'expected_hash': expected_hash,
            'expected_page': expected_page,
            'page_from': page_from,
            'page_to': page_to,
            'max_controls': max_controls,
            'section_anchor': section_anchor,
            'around': around,
            'anchor_evidence': anchor_evidence,
            'around_evidence': around_evidence,
            'controls': controls,
            'enumeration_mode': enumeration_mode,
            'target_ctrl': target_ctrl,
            'before_item': before_item,
            'target_anchor_pos': target_anchor_pos,
        }

    def _bundle_control_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        max_controls = int(step.get('max_controls') or 2048)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        expected_page = step.get('expected_page')
        target_id = str(step.get('target_id') or '').strip() or None
        expected_hash = str(step.get('expected_hash') or '').strip() or None
        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        anchor_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('section_anchor') or '').strip() or None)
        around_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('around') or '').strip() or None)
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        page_filter_unknown_count = 0
        try:
            for index, ctrl in enumerate(controls):
                anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=1)
                snapshot: dict[str, Any] = {}
                page_evidence: dict[str, Any] = {'page': None, 'method': None}
                if anchor_pos is not None:
                    try:
                        _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
                        snapshot = _snapshot_cursor_context(hwp)
                        page_evidence = self._bundle_page_evidence(hwp)
                    except Exception as exc:
                        snapshot = {'error': f'{type(exc).__name__}: {exc}'}
                page = page_evidence.get('page')
                if page_from is not None:
                    if page is None:
                        page_filter_unknown_count += 1
                    elif not (int(page_from) <= int(page) <= int(page_to)):
                        continue

                ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
                ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
                user_desc = self._bundle_control_scalar(ctrl, 'UserDesc')
                type_name = self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType')
                bounds = {
                    key: self._bundle_control_scalar(ctrl, key)
                    for key in ('X', 'Y', 'Width', 'Height', 'HorzRelTo', 'VertRelTo')
                    if self._bundle_control_scalar(ctrl, key) not in (None, '')
                }
                proof_basis = {
                    'index': index,
                    'ctrl_id': ctrl_id,
                    'ctrl_inst_id': ctrl_inst_id,
                    'user_desc': user_desc,
                    'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
                    'bounds': bounds or None,
                    'page': page,
                    'type': type_name,
                }
                proof_hash = 'sha256:' + hashlib.sha256(json.dumps(proof_basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]
                stable_id = f"ctrl/{index}/{ctrl_id or 'unknown'}/{ctrl_inst_id or 'no-inst'}"
                text_preview = _preview_text(str(user_desc or ''), limit=120) if user_desc else None
                item = {
                    'target_id': stable_id,
                    'index': index,
                    'page': page,
                    'page_evidence': page_evidence,
                    'type': str(type_name or ctrl_id or 'control'),
                    'ctrl_id': ctrl_id,
                    'ctrl_inst_id': ctrl_inst_id,
                    'bounds': bounds or None,
                    'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
                    'text_preview': text_preview,
                    'proof_hash': proof_hash,
                    'nearby': {
                        'cell_addr': snapshot.get('cell_addr'),
                        'field_name': snapshot.get('field_name'),
                        'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                    },
                    'source_evidence': {
                        'preexisting_control': True,
                        'editable_target': False,
                        'reason': 'inventory is read-only; use target id/hash as proof before any future edit primitive',
                    },
                }
                item['target_match'] = bool(
                    (target_id and (stable_id == target_id or str(ctrl_id or '') == target_id or str(ctrl_inst_id or '') == target_id))
                    and (not expected_hash or expected_hash == proof_hash)
                    and (not expected_page or page is None or int(expected_page) == int(page))
                )
                items.append(item)
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        if page_filter_unknown_count:
            warnings.append(
                f'page filter could not be applied to {page_filter_unknown_count} control(s) because page evidence was unavailable; those controls were retained.'
            )
        if target_id:
            matched = [item for item in items if item.get('target_match')]
            if len(matched) != 1:
                warnings.append(f'target proof is ambiguous or missing: matched {len(matched)} item(s) for target_id={target_id!r}.')
        return {
            'schema_version': 'local-cli/control-inventory/v1',
            'read_only': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': step.get('section_anchor'),
                'page_from': page_from,
                'page_to': page_to,
                'around': step.get('around'),
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'control_count_total': len(controls),
            'control_count_returned': len(items),
            'items': items,
            'warnings': warnings,
        }

    def _bundle_table_frame_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Read-only grouping of table/frame-like controls and co-anchored graphics.

        This is intentionally an inventory primitive, not a repair primitive. It
        exposes the native control shape properties and anchor groups needed to
        decide whether a later row/cell/frame-flow mutation can be targeted
        safely.
        """

        max_controls = int(step.get('max_controls') or 2048)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        expected_page = step.get('expected_page')
        target_id = str(step.get('target_id') or '').strip() or None
        expected_hash = str(step.get('expected_hash') or '').strip() or None
        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        anchor_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('section_anchor') or '').strip() or None)
        around_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('around') or '').strip() or None)
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        page_filter_unknown_count = 0

        try:
            for index, ctrl in enumerate(controls):
                item, snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                page = item.get('page')
                if page_from is not None:
                    if page is None:
                        page_filter_unknown_count += 1
                    elif not (int(page_from) <= int(page) <= int(page_to)):
                        continue

                shape_props = self._shape_prop_snapshot(ctrl)
                anchor_key = '/'.join(str(part) for part in anchor_pos) if anchor_pos is not None else None
                enriched = dict(item)
                enriched['anchor_key'] = anchor_key
                enriched['shape_properties'] = shape_props
                enriched['nearby'] = {
                    **dict(enriched.get('nearby') or {}),
                    'cell_addr': snapshot.get('cell_addr'),
                    'is_cell': snapshot.get('is_cell'),
                    'selection_mode': snapshot.get('selection_mode'),
                    'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                }
                enriched['target_match'] = bool(
                    (target_id and (item.get('target_id') == target_id or str(item.get('ctrl_id') or '') == target_id or str(item.get('ctrl_inst_id') or '') == target_id))
                    and (not expected_hash or expected_hash == item.get('proof_hash'))
                    and (not expected_page or page is None or int(expected_page) == int(page))
                )
                items.append(enriched)
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        groups_by_anchor: dict[str, dict[str, Any]] = {}
        for item in items:
            anchor_key = str(item.get('anchor_key') or 'unknown-anchor')
            group = groups_by_anchor.setdefault(
                anchor_key,
                {
                    'anchor_key': anchor_key,
                    'anchor_pos': item.get('anchor_pos'),
                    'pages': [],
                    'control_ids': [],
                    'types': [],
                    'items': [],
                    'has_table': False,
                    'has_graphic': False,
                },
            )
            page = item.get('page')
            if page is not None and page not in group['pages']:
                group['pages'].append(page)
            ctrl_id = item.get('ctrl_id') or item.get('type')
            if ctrl_id and ctrl_id not in group['types']:
                group['types'].append(ctrl_id)
            target = item.get('target_id')
            if target:
                group['control_ids'].append(target)
            group['items'].append({
                'target_id': item.get('target_id'),
                'page': item.get('page'),
                'type': item.get('type'),
                'ctrl_id': item.get('ctrl_id'),
                'proof_hash': item.get('proof_hash'),
                'shape_properties': item.get('shape_properties'),
                'text_preview': item.get('text_preview'),
            })
            ctrl_text = str(item.get('ctrl_id') or item.get('type') or '').lower()
            if ctrl_text == 'tbl' or 'table' in ctrl_text:
                group['has_table'] = True
            if ctrl_text in {'gso', 'pic'} or 'shape' in ctrl_text or 'graphic' in ctrl_text:
                group['has_graphic'] = True

        anchor_groups = list(groups_by_anchor.values())
        anchor_groups.sort(key=lambda group: (min(group.get('pages') or [999999]), str(group.get('anchor_key') or '')))
        for group in anchor_groups:
            group['pages'] = sorted(group.get('pages') or [])
            group['fit_risk'] = bool(group.get('has_table') and group.get('has_graphic'))
            if group['fit_risk']:
                group['fit_note'] = 'table and graphic share one anchor; direct graphic/table Width/Height may not repair row/cell/frame flow without a dedicated fit primitive'

        if page_filter_unknown_count:
            warnings.append(
                f'page filter could not be applied to {page_filter_unknown_count} control(s) because page evidence was unavailable; those controls were retained.'
            )
        if target_id:
            matched = [item for item in items if item.get('target_match')]
            if len(matched) != 1:
                warnings.append(f'target proof is ambiguous or missing: matched {len(matched)} item(s) for target_id={target_id!r}.')

        return {
            'schema_version': 'local-cli/table-frame-inventory/v1',
            'read_only': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': step.get('section_anchor'),
                'page_from': page_from,
                'page_to': page_to,
                'around': step.get('around'),
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'control_count_total': len(controls),
            'control_count_returned': len(items),
            'items': items,
            'anchor_groups': anchor_groups,
            'warnings': warnings,
            'next_tool_gap': 'A mutating row/cell/frame-flow fit primitive is still required before changing container layout; this command is read-only inventory only.',
        }

    def _shape_prop_item(self, prop: Any, key: str) -> Any:
        item = getattr(prop, 'Item', None)
        if callable(item):
            try:
                return item(key)
            except Exception:
                pass
        try:
            return getattr(prop, key)
        except Exception:
            return None

    def _shape_prop_set_item(self, prop: Any, key: str, value: Any) -> None:
        setter = getattr(prop, 'SetItem', None)
        if callable(setter):
            setter(key, value)
            return
        setattr(prop, key, value)

    def _shape_prop_snapshot(self, ctrl: Any) -> dict[str, Any]:
        try:
            prop = getattr(ctrl, 'Properties')
        except Exception as exc:
            return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
        keys = (
            'Width',
            'Height',
            'HorzRelTo',
            'VertRelTo',
            'HorzAlign',
            'VertAlign',
            'HorzOffset',
            'VertOffset',
            'TreatAsChar',
            'TextWrap',
            'FlowWithText',
        )
        values = {key: self._shape_prop_item(prop, key) for key in keys}
        return {
            'available': True,
            'values': {key: value for key, value in values.items() if value is not None},
        }

    def _mm_to_hwp_unit(self, hwp: Any, value_mm: float) -> int:
        method = getattr(hwp, 'MiliToHwpUnit', None) or getattr(hwp, 'mili_to_hwp_unit', None)
        if callable(method):
            return int(round(float(method(float(value_mm)))))
        # Hancom HWPUNIT is 7200 units per inch; 1 mm is about 283.465 units.
        return int(round(float(value_mm) * 7200.0 / 25.4))

    def _hwp_unit_to_mm(self, hwp: Any, value_hu: float) -> float:
        method = getattr(hwp, 'HwpUnitToMili', None) or getattr(hwp, 'hwp_unit_to_mili', None)
        if callable(method):
            return float(method(float(value_hu)))
        return float(value_hu) * 25.4 / 7200.0

    def _bundle_enter_table_cell_for_ctrl(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        select_cell = getattr(hwp, 'ShapeObjTableSelCell', None)
        text_box_edit = getattr(hwp, 'ShapeObjTextBoxEdit', None)
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
        ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
        target_item = {'ctrl_inst_id': ctrl_inst_id, 'ctrl_id': ctrl_id, 'anchor_pos': list(_get_ctrl_anchor_pos(hwp, ctrl, option=0) or [])}

        def _snapshot(label: str) -> dict[str, Any]:
            snap = self._bundle_compact_snapshot(hwp)
            attempts.append({'method': label, 'snapshot': snap})
            return snap

        def _try_text_edit(label: str) -> dict[str, Any]:
            if callable(text_box_edit):
                try:
                    raw = text_box_edit()
                    attempts.append({'method': f'{label}/ShapeObjTextBoxEdit()', 'result': bool(raw) if raw is not None else None})
                except Exception as exc:
                    attempts.append({'method': f'{label}/ShapeObjTextBoxEdit()', 'error': f'{type(exc).__name__}: {exc}'})
            elif callable(haction_run):
                try:
                    raw = haction_run('ShapeObjTextBoxEdit')
                    attempts.append({'method': f'{label}/HAction.Run(ShapeObjTextBoxEdit)', 'result': bool(raw) if raw is not None else None})
                except Exception as exc:
                    attempts.append({'method': f'{label}/HAction.Run(ShapeObjTextBoxEdit)', 'error': f'{type(exc).__name__}: {exc}'})
            else:
                attempts.append({'method': f'{label}/ShapeObjTextBoxEdit', 'skipped': True, 'reason': 'unavailable'})
            return _snapshot(f'{label}/after-text-edit')

        selection = self._bundle_select_control_exact(hwp, ctrl, target_item)
        attempts.append({'method': 'select-table-control', 'selection': selection})
        snap = _try_text_edit('exact-control-select') if selection.get('selection_succeeded') else _snapshot('exact-control-select/skipped-text-edit')
        if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
            return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}

        anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=0)
        if anchor_pos is not None:
            try:
                _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
                find_ctrl = getattr(hwp, 'FindCtrl', None) or getattr(hwp, 'find_ctrl', None)
                if callable(find_ctrl):
                    attempts.append({'method': 'anchor_pos+FindCtrl', 'result': bool(find_ctrl())})
                snap = _try_text_edit('anchor_pos+FindCtrl')
                if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
                    return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'anchor_pos+FindCtrl/ShapeObjTextBoxEdit', 'error': f'{type(exc).__name__}: {exc}'})

        if callable(select_cell):
            try:
                raw = select_cell()
                attempts.append({'method': 'ShapeObjTableSelCell diagnostic only (cell-block)', 'result': bool(raw) if raw is not None else None})
                snap = _snapshot('after-ShapeObjTableSelCell-diagnostic')
                if snap.get('is_cell'):
                    snap = _try_text_edit('cell-block-to-edit')
                    if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
                        return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'ShapeObjTableSelCell diagnostic only (cell-block)', 'error': f'{type(exc).__name__}: {exc}'})

        final = self._bundle_compact_snapshot(hwp)
        return {
            'is_cell': bool(final.get('is_cell')),
            'normal_edit_state': bool(final.get('is_cell')) and not final.get('has_selection') and int(final.get('selection_mode') or 0) == 0,
            'cell_addr': final.get('cell_addr'),
            'attempts': attempts,
        }

    def _bundle_native_cell_margin_readback(
        self,
        hwp: Any,
        *,
        expected_cell_addr: Any = None,
    ) -> dict[str, Any]:
        """Read fresh native four-side cell margins for the current cell.

        ``pyhwpx.get_cell_margin`` can expose a cached wrapper value.  The
        native action refresh is therefore part of this observation contract;
        a failed refresh makes the observation unavailable instead of allowing
        a stale value to serve as persistence evidence.
        """
        source = 'HParameterSet.HShapeObject.ShapeTableCell.Margin*'
        observed_addr: list[int] | None = None
        try:
            get_cell_addr = getattr(hwp, 'get_cell_addr', None)
            if not callable(get_cell_addr):
                raise LocalCliRuntimeError('native cell identity getter is unavailable')
            observed_addr = _normalize_cell_addr_value(get_cell_addr(as_='tuple'))
            if observed_addr is None:
                raise LocalCliRuntimeError('native cell identity is missing or invalid')
            expected_addr = _normalize_cell_addr_value(expected_cell_addr) if expected_cell_addr is not None else None
            if expected_cell_addr is not None and (expected_addr is None or observed_addr != expected_addr):
                raise LocalCliRuntimeError(
                    'native cell identity does not match the requested target; '
                    f'observed={observed_addr!r}; expected={expected_cell_addr!r}'
                )

            parameter_root = getattr(hwp, 'HParameterSet')
            shape = getattr(parameter_root, 'HShapeObject')
            hset = getattr(shape, 'HSet')
            get_default = getattr(getattr(hwp, 'HAction'), 'GetDefault', None)
            if not callable(get_default):
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault is unavailable')
            refresh_result = get_default('TablePropertyDialog', hset)
            if refresh_result is not True:
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault returned no positive success')
            cell = getattr(shape, 'ShapeTableCell')
            margins = {
                'left': getattr(cell, 'MarginLeft'),
                'right': getattr(cell, 'MarginRight'),
                'top': getattr(cell, 'MarginTop'),
                'bottom': getattr(cell, 'MarginBottom'),
            }
            normalized = _normalize_cell_margin_readback(margins)
            if normalized is None:
                raise LocalCliRuntimeError(f'native four-side cell-margin values are invalid: {margins!r}')
            return {
                'available': True,
                'refresh_succeeded': True,
                'cell_addr': observed_addr,
                'value': normalized,
                'source': source,
            }
        except Exception as exc:
            error = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{type(exc).__name__}: {exc}'
            return {
                'available': False,
                'refresh_succeeded': False,
                'cell_addr': observed_addr,
                'error': error,
                'source': source,
            }

    def _bundle_table_cell_metrics(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
            metrics: dict[str, Any] = {'enter': enter}
            if not enter.get('is_cell'):
                return {**metrics, 'available': False, 'error': 'could not place caret inside target table cell'}
            for key, method_name, kwargs in (
                ('cell_addr', 'get_cell_addr', {'as_': 'tuple'}),
                ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
                ('row_height_mm', 'get_row_height', {'as_': 'mm'}),
                ('table_height_hu', 'get_table_height', {'as_': 'hwpunit'}),
                ('table_height_mm', 'get_table_height', {'as_': 'mm'}),
                ('table_inside_margin_hu', 'get_table_inside_margin', {'as_': 'hwpunit'}),
                ('table_outside_margin_hu', 'get_table_outside_margin', {'as_': 'hwpunit'}),
            ):
                method = getattr(hwp, method_name, None)
                if not callable(method):
                    metrics[key] = {'unavailable': method_name}
                    continue
                try:
                    metrics[key] = method(**kwargs)
                except Exception as exc:
                    metrics[key] = {'error': f'{type(exc).__name__}: {exc}'}
            target_cell_addr = _normalize_cell_addr_value(enter.get('cell_addr'))
            if target_cell_addr is None:
                target_cell_addr = _normalize_cell_addr_value(metrics.get('cell_addr'))
            metrics['target_cell_addr'] = target_cell_addr
            margin_readback = self._bundle_native_cell_margin_readback(
                hwp,
                expected_cell_addr=target_cell_addr,
            )
            metrics['cell_margin_readback'] = margin_readback
            metrics['cell_margin_hu'] = (
                margin_readback.get('value')
                if margin_readback.get('available') is True
                else {'error': margin_readback.get('error', 'native margin readback unavailable')}
            )
            if margin_readback.get('cell_addr') is not None:
                metrics['cell_addr'] = margin_readback.get('cell_addr')
            char_raw = self._style_parameter_snapshot(
                hwp,
                'CharShape',
                'HCharShape',
                ('Height',),
            )
            para_raw = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('LineSpacing', 'LineSpacingType'),
            )
            char_values = char_raw.get('values') if isinstance(char_raw.get('values'), dict) else {}
            para_values = para_raw.get('values') if isinstance(para_raw.get('values'), dict) else {}
            metrics['char_height_raw'] = char_values.get('Height')
            metrics['para_line_spacing'] = para_values.get('LineSpacing')
            metrics['para_line_spacing_type'] = para_values.get('LineSpacingType')
            metrics['vertical_align'] = self._bundle_table_cell_vertical_align(hwp)
            metrics['style_snapshot'] = {'char_shape': char_raw, 'para_shape': para_raw}
            metrics['cell_addr_valid'] = _normalize_cell_addr_value(metrics.get('cell_addr')) is not None
            metrics['row_height_hu_valid'] = _valid_numeric_readback(metrics.get('row_height_hu'))
            metrics['cell_margin_hu_valid'] = _normalize_cell_margin_readback(metrics.get('cell_margin_hu')) is not None
            metrics['cell_margin_hu_available'] = bool(
                metrics['cell_margin_hu_valid'] and margin_readback.get('refresh_succeeded') is True
            )
            metrics['cell_identity_matches_target'] = bool(
                target_cell_addr is not None
                and _normalize_cell_addr_value(metrics.get('cell_addr')) == target_cell_addr
            )
            metrics['vertical_align_available'] = _normalize_vertical_align_readback(metrics.get('vertical_align')) is not None
            metrics['available'] = (
                bool(metrics['cell_addr_valid'])
                and bool(metrics['row_height_hu_valid'])
                and bool(metrics['cell_margin_hu_available'])
                and bool(metrics['cell_identity_matches_target'])
            )
            return metrics
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    @staticmethod
    def _bundle_table_cell_vertical_align(hwp: Any) -> dict[str, Any]:
        """Read the native vertical alignment of the current table cell.

        Hancom exposes this value after ``TablePropertyDialog`` populates
        ``HShapeObject.ShapeTableCell``. The COM value is an integer enum:
        0=top, 1=center, 2=bottom.
        """
        try:
            parameter_root = getattr(hwp, 'HParameterSet')
            shape = getattr(parameter_root, 'HShapeObject')
            get_default = getattr(getattr(hwp, 'HAction'), 'GetDefault', None)
            if not callable(get_default):
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault is unavailable')
            default_result = get_default('TablePropertyDialog', getattr(shape, 'HSet'))
            if default_result is not True:
                raise LocalCliRuntimeError(
                    'TablePropertyDialog GetDefault did not return positive success; '
                    f'result={default_result!r}'
                )
            cell = getattr(shape, 'ShapeTableCell')
            raw_value = getattr(cell, 'VertAlign')
            if isinstance(raw_value, bool):
                raise LocalCliRuntimeError('TablePropertyDialog VertAlign is boolean, not a native enum')
            if not isinstance(raw_value, int) or raw_value not in {0, 1, 2}:
                raise LocalCliRuntimeError(
                    'TablePropertyDialog VertAlign enum is invalid (not an integral native enum); '
                    f'value={raw_value!r}'
                )
            value = raw_value
            names = {0: 'top', 1: 'center', 2: 'bottom'}
            if value not in names:
                raise LocalCliRuntimeError(f'TablePropertyDialog VertAlign enum is invalid: {value!r}')
            return {
                'available': True,
                'refresh_succeeded': True,
                'value': value,
                'name': names.get(value),
                'source': 'HParameterSet.HShapeObject.ShapeTableCell.VertAlign',
            }
        except Exception as exc:
            return {
                'available': False,
                'refresh_succeeded': False,
                'error': f'{type(exc).__name__}: {exc}',
                'source': 'HParameterSet.HShapeObject.ShapeTableCell.VertAlign',
            }

    def _bundle_exact_control_select_proof(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='exact_control_select_proof')
        before_snapshot = self._bundle_compact_snapshot(hwp)
        selection = self._bundle_select_control_exact(hwp, resolved['target_ctrl'], resolved['before_item'])
        after_snapshot = self._bundle_compact_snapshot(hwp)
        if not selection.get('selection_succeeded'):
            raise LocalCliRuntimeError(
                'exact_control_select_proof could not select/prove the target control; '
                f'attempts={selection.get("attempts")!r}'
            )
        warnings = []
        if selection.get('proof_strength') != 'exact-ctrl-inst-id':
            warnings.append('Exact CtrlInstID selection was unavailable or unverified; fallback selection proof was used.')
        return {
            'schema_version': 'local-cli/exact-control-select-proof/v1',
            'read_only': True,
            'mutation': None,
            'succeeded': True,
            'enumeration_mode': resolved['enumeration_mode'],
            'scope': {
                'section_anchor': resolved['section_anchor'],
                'page_from': resolved['page_from'],
                'page_to': resolved['page_to'],
                'around': resolved['around'],
            },
            'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
            'target_proof': {
                'target_id': resolved['target_id'],
                'expected_hash': resolved['expected_hash'],
                'expected_page': resolved['expected_page'],
                'matched_before': resolved['before_item'],
                'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
            },
            'selection_proof': selection,
            'method_used': selection.get('method_used'),
            'proof_strength': selection.get('proof_strength'),
            'before': before_snapshot,
            'after': after_snapshot,
            'warnings': warnings,
        }

    def _bundle_metric_probe(self, hwp: Any, method_name: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
        method = getattr(hwp, method_name, None)
        if not callable(method):
            return {'available': False, 'method': method_name, 'error': 'unavailable'}
        try:
            value = method(**(kwargs or {}))
        except Exception as exc:
            return {'available': False, 'method': method_name, 'error': f'{type(exc).__name__}: {exc}'}
        return {
            'available': True,
            'method': method_name,
            'value': self._macro_result_preview(value),
            'raw_type': type(value).__name__,
        }

    def _bundle_call_cell_navigation(self, hwp: Any, action_name: str) -> dict[str, Any]:
        py_method = getattr(hwp, action_name, None)
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        attempts: list[dict[str, Any]] = []
        if callable(py_method):
            try:
                raw = py_method()
                return {
                    'method': f'{action_name}()',
                    'result': bool(raw) if raw is not None else None,
                    'raw_type': type(raw).__name__,
                    'attempts': [{'method': f'{action_name}()', 'result': bool(raw) if raw is not None else None}],
                }
            except Exception as exc:
                attempts.append({'method': f'{action_name}()', 'error': f'{type(exc).__name__}: {exc}'})
        if callable(haction_run):
            try:
                raw = haction_run(action_name)
                attempts.append({'method': f'HAction.Run({action_name})', 'result': bool(raw) if raw is not None else None})
                return {
                    'method': f'HAction.Run({action_name})',
                    'result': bool(raw) if raw is not None else None,
                    'raw_type': type(raw).__name__,
                    'attempts': attempts,
                }
            except Exception as exc:
                attempts.append({'method': f'HAction.Run({action_name})', 'error': f'{type(exc).__name__}: {exc}'})
        if not attempts:
            attempts.append({'method': action_name, 'error': 'unavailable'})
        return {'method': None, 'result': None, 'attempts': attempts}

    def _bundle_call_move_pos(self, hwp: Any, move_id: int) -> dict[str, Any]:
        method = getattr(hwp, 'move_pos', None) or getattr(hwp, 'MovePos', None)
        if not callable(method):
            return {'method': 'move_pos', 'move_id': move_id, 'error': 'unavailable'}
        try:
            raw = method(move_id)
        except Exception as exc:
            return {'method': 'move_pos', 'move_id': move_id, 'error': f'{type(exc).__name__}: {exc}'}
        return {'method': 'move_pos', 'move_id': move_id, 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__}

    def _bundle_navigation_probe_once(
        self,
        hwp: Any,
        ctrl: Any,
        *,
        label: str,
        kind: str,
        action_name: str | None = None,
        move_id: int | None = None,
    ) -> dict[str, Any]:
        enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_cell = self._bundle_current_cell_addr_tuple(hwp)
        if not enter.get('is_cell') or not enter.get('normal_edit_state'):
            return {
                'label': label,
                'kind': kind,
                'enter': enter,
                'before': before_snapshot,
                'before_cell': list(before_cell) if before_cell is not None else None,
                'skipped': True,
                'reason': 'could not enter target table in normal edit state',
            }
        if kind == 'action' and action_name:
            call = self._bundle_call_cell_navigation(hwp, action_name)
        elif kind == 'move_pos' and move_id is not None:
            call = self._bundle_call_move_pos(hwp, move_id)
        else:
            call = {'error': 'invalid navigation probe request'}
        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_cell = self._bundle_current_cell_addr_tuple(hwp)
        moved = before_cell is not None and after_cell is not None and before_cell != after_cell
        return {
            'label': label,
            'kind': kind,
            'enter': enter,
            'before': before_snapshot,
            'before_cell': list(before_cell) if before_cell is not None else None,
            'call': call,
            'after': after_snapshot,
            'after_cell': list(after_cell) if after_cell is not None else None,
            'moved': moved,
        }

    def _bundle_sha256_text(self, value: str) -> str:
        return 'sha256:' + hashlib.sha256(str(value).encode('utf-8', errors='replace')).hexdigest()

    def _bundle_sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return 'sha256:' + digest.hexdigest()

    def _bundle_bindata_manifest(self, path: Path) -> dict[str, Any]:
        if not path.exists() or not path.is_file():
            raise LocalCliRuntimeError(f'BinData manifest source is missing: {path}')
        items: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(path, 'r') as archive:
                for name in sorted(info.filename for info in archive.infolist() if info.filename.startswith('BinData/')):
                    data = archive.read(name)
                    items.append(
                        {
                            'name': name,
                            'size': len(data),
                            'sha256': hashlib.sha256(data).hexdigest(),
                        }
                    )
        except Exception as exc:
            raise LocalCliRuntimeError(f'Could not read HWPX BinData manifest from {path}: {type(exc).__name__}: {exc}') from exc
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {
            'available': True,
            'path': str(path),
            'count': len(items),
            'items': items,
            'manifest_hash': self._bundle_sha256_text(canonical),
        }

    def _bundle_table_inventory_signature(
        self,
        *,
        target_id: str,
        rows: int,
        cols: int,
        document_text_hash: str,
        text_char_count: int,
        nonempty_line_count: int,
        div0_count: int,
    ) -> dict[str, Any]:
        payload = {
            'target_id': target_id,
            'rows': rows,
            'cols': cols,
            'addresses_seen_zero_based_col_row': [[col, row] for row in range(rows) for col in range(cols)],
            'document_text_hash': document_text_hash,
            'text_char_count': text_char_count,
            'nonempty_line_count': nonempty_line_count,
            'div0_count': div0_count,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {**payload, 'inventory_hash': self._bundle_sha256_text(canonical)}

    def _bundle_table_width_snapshot(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('table_column_width_exact could not enter the target native table')

            def _read(method_name: str, **kwargs: Any) -> Any:
                method = getattr(hwp, method_name, None)
                if not callable(method):
                    raise LocalCliRuntimeError(f'table_column_width_exact requires pyhwpx {method_name}')
                try:
                    return method(**kwargs)
                except Exception as exc:
                    raise LocalCliRuntimeError(f'table_column_width_exact {method_name} failed: {type(exc).__name__}: {exc}') from exc

            entry_addr_raw = _read('get_cell_addr', as_='tuple')
            entry_addr = list(entry_addr_raw) if isinstance(entry_addr_raw, (tuple, list)) else [entry_addr_raw]
            rows = int(_read('get_row_num'))
            cols = int(_read('get_col_num'))
            if rows <= 0 or cols <= 0:
                raise LocalCliRuntimeError(f'table_column_width_exact read invalid native table dimensions: rows={rows}, cols={cols}')

            left_navigation: list[dict[str, Any]] = []
            for _ in range(min(cols + 1, 101)):
                before = list(_read('get_cell_addr', as_='tuple'))
                if int(before[0]) <= 0:
                    break
                nav = self._bundle_call_cell_navigation(hwp, 'TableLeftCell')
                after = list(_read('get_cell_addr', as_='tuple'))
                left_navigation.append({'before': before, 'after': after, 'navigation': nav})
                if after == before:
                    raise LocalCliRuntimeError('table_column_width_exact could not navigate to the first native table column')
            origin_addr = list(_read('get_cell_addr', as_='tuple'))
            if len(origin_addr) < 2 or int(origin_addr[0]) != 0:
                raise LocalCliRuntimeError(f'table_column_width_exact requires a first-column origin, got {origin_addr!r}')

            widths_mm: list[float] = []
            addresses: list[list[int]] = []
            right_navigation: list[dict[str, Any]] = []
            for index in range(cols):
                addr = list(_read('get_cell_addr', as_='tuple'))
                width = float(_read('get_col_width', as_='mm'))
                if not math.isfinite(width) or width <= 0.0:
                    raise LocalCliRuntimeError(f'table_column_width_exact read invalid column width at {addr!r}: {width!r}')
                widths_mm.append(width)
                addresses.append([int(addr[0]), int(addr[1])])
                if index < cols - 1:
                    nav = self._bundle_call_cell_navigation(hwp, 'TableRightCell')
                    after = list(_read('get_cell_addr', as_='tuple'))
                    right_navigation.append({'before': addr, 'after': after, 'navigation': nav})
                    if after == addr:
                        raise LocalCliRuntimeError(f'table_column_width_exact could not navigate from column {index} to the next column')

            return {
                'available': True,
                'entry_cell_addr': entry_addr,
                'origin_cell_addr': origin_addr,
                'rows': rows,
                'cols': cols,
                'widths_mm': widths_mm,
                'width_sum_mm': sum(widths_mm),
                'table_width_mm': float(_read('get_table_width', as_='mm')),
                'table_height_mm': float(_read('get_table_height', as_='mm')),
                'row_height_mm': float(_read('get_row_height', as_='mm')),
                'addresses_seen_zero_based_col_row': addresses,
                'left_navigation': left_navigation,
                'right_navigation': right_navigation,
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_set_table_column_widths(self, hwp: Any, widths_mm: list[float]) -> dict[str, Any]:
        method = getattr(hwp, 'set_col_width', None)
        if not callable(method):
            raise LocalCliRuntimeError('table_column_width_exact requires the admitted pyhwpx set_col_width primitive')
        if not widths_mm or any(not math.isfinite(float(value)) or float(value) <= 0.0 for value in widths_mm):
            raise LocalCliRuntimeError('table_column_width_exact received invalid finite positive widths')
        try:
            raw = method([float(value) for value in widths_mm], as_='mm')
        except Exception as exc:
            raise LocalCliRuntimeError(
                f'table_column_width_exact set_col_width(widths_mm, as_=mm) failed: {type(exc).__name__}: {exc}'
            ) from exc
        if raw is False:
            raise LocalCliRuntimeError('table_column_width_exact set_col_width returned False')
        return {
            'method': 'pyhwpx.set_col_width(widths_mm, as_=mm)',
            'result': bool(raw) if raw is not None else None,
            'requested_widths_mm': [float(value) for value in widths_mm],
        }

    def _bundle_table_column_width_exact(self, handle: LocalCliRuntimeHandle, step: dict[str, Any]) -> dict[str, Any]:
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('table_column_width_exact requires confirm_layout=true')
        resolved = self._bundle_resolve_control_target(handle.hwp, step, op_name='table_column_width_exact', require_table=True)
        target_ctrl = resolved['target_ctrl']
        expected_preimage = str(step.get('expected_preimage_sha256') or '').strip().lower()
        expected_cell_inventory_hash = str(step.get('expected_cell_inventory_hash') or '').strip().lower()
        expected_text_hash = str(step.get('expected_document_text_hash') or '').strip().lower()
        expected_bindata_hash = str(step.get('expected_bindata_manifest_hash') or '').strip().lower()
        for field_name, value in (
            ('expected_preimage_sha256', expected_preimage),
            ('expected_cell_inventory_hash', expected_cell_inventory_hash),
            ('expected_document_text_hash', expected_text_hash),
            ('expected_bindata_manifest_hash', expected_bindata_hash),
        ):
            if value.startswith('sha256:'):
                value = value[7:]
            if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
                raise LocalCliRuntimeError(f'table_column_width_exact requires a full SHA-256 value for {field_name}')

        expected_rows = int(step.get('expected_rows') or 0)
        expected_cols = int(step.get('expected_cols') or 0)
        expected_total_width = float(step.get('expected_total_width_mm') or 0.0)
        expected_height = float(step.get('expected_table_height_mm') or 0.0)
        expected_control_count = int(step.get('expected_control_count') or 0)
        expected_char_count = int(step.get('expected_text_char_count') or 0)
        expected_line_count = int(step.get('expected_nonempty_line_count') or 0)
        expected_div0_count = int(step.get('expected_div0_count') or 0)
        requested_widths = [float(value) for value in (step.get('requested_widths_mm') or [])]
        if expected_rows <= 0 or expected_cols <= 0 or expected_control_count <= 0:
            raise LocalCliRuntimeError('table_column_width_exact received invalid expected dimensions/control count')
        if not math.isfinite(expected_total_width) or not math.isfinite(expected_height) or expected_total_width <= 0.0 or expected_height <= 0.0:
            raise LocalCliRuntimeError('table_column_width_exact received invalid expected table dimensions')
        if len(requested_widths) != expected_cols or any(not math.isfinite(value) or value <= 0.0 for value in requested_widths):
            raise LocalCliRuntimeError('table_column_width_exact requested_widths_mm does not match expected_cols or contains invalid values')
        if abs(sum(requested_widths) - expected_total_width) > 0.05:
            raise LocalCliRuntimeError('table_column_width_exact fixed-total guard failed before mutation')

        working_copy_path = Path(str(handle.working_copy_path))
        actual_preimage = self._bundle_sha256_file(working_copy_path)
        if actual_preimage != expected_preimage:
            raise LocalCliRuntimeError(
                f'table_column_width_exact preimage SHA-256 mismatch: expected {expected_preimage!r}, got {actual_preimage!r}'
            )

        before_text = self._get_live_document_text(handle, purpose='table-column-width-exact:before')
        before_text_hash = self._bundle_sha256_text(before_text)
        before_text_guard = {
            'hash': before_text_hash,
            'char_count': len(before_text),
            'nonempty_line_count': len([line.strip() for line in before_text.splitlines() if line.strip()]),
            'div0_count': before_text.count('#DIV/0!'),
        }
        if (
            before_text_hash != expected_text_hash
            or before_text_guard['char_count'] != expected_char_count
            or before_text_guard['nonempty_line_count'] != expected_line_count
            or before_text_guard['div0_count'] != expected_div0_count
        ):
            raise LocalCliRuntimeError(f'table_column_width_exact document text guard failed: expected={step!r}; actual={before_text_guard!r}')

        before_control_map = self._capture_control_map_signature(handle.hwp)
        if int(before_control_map.get('control_count') or 0) != expected_control_count:
            raise LocalCliRuntimeError(
                f'table_column_width_exact native control-count guard failed: expected {expected_control_count}, got {before_control_map.get("control_count")}'
            )

        before_snapshot_path = self._snapshot_temp_hwpx(handle, purpose='table-column-width-exact-before')
        before_bindata = self._bundle_bindata_manifest(before_snapshot_path)
        if before_bindata.get('manifest_hash') != expected_bindata_hash:
            raise LocalCliRuntimeError(
                f'table_column_width_exact BinData guard failed: expected {expected_bindata_hash!r}, got {before_bindata.get("manifest_hash")!r}'
            )

        before_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
        if before_table['rows'] != expected_rows or before_table['cols'] != expected_cols:
            raise LocalCliRuntimeError(
                f'table_column_width_exact native dimension guard failed: expected {expected_rows}x{expected_cols}, got {before_table["rows"]}x{before_table["cols"]}'
            )
        if abs(float(before_table['table_width_mm']) - expected_total_width) > 0.2:
            raise LocalCliRuntimeError(
                f'table_column_width_exact fixed table width guard failed: expected {expected_total_width} mm, got {before_table["table_width_mm"]} mm'
            )
        if abs(float(before_table['table_height_mm']) - expected_height) > 0.5:
            raise LocalCliRuntimeError(
                f'table_column_width_exact table height guard failed: expected {expected_height} mm, got {before_table["table_height_mm"]} mm'
            )
        before_inventory = self._bundle_table_inventory_signature(
            target_id=resolved['target_id'],
            rows=before_table['rows'],
            cols=before_table['cols'],
            document_text_hash=before_text_hash,
            text_char_count=before_text_guard['char_count'],
            nonempty_line_count=before_text_guard['nonempty_line_count'],
            div0_count=before_text_guard['div0_count'],
        )
        if before_inventory['inventory_hash'] != expected_cell_inventory_hash:
            raise LocalCliRuntimeError(
                f'table_column_width_exact cell/text inventory guard failed: expected {expected_cell_inventory_hash!r}, got {before_inventory["inventory_hash"]!r}'
            )

        mutation_started = False
        rollback: dict[str, Any] = {'attempted': False, 'succeeded': False, 'error': None}
        original_pos = None
        try:
            original_pos = _get_pos(handle.hwp)
        except Exception:
            original_pos = None
        try:
            entered = self._bundle_enter_table_cell_for_ctrl(handle.hwp, target_ctrl)
            if not entered.get('is_cell'):
                raise LocalCliRuntimeError('table_column_width_exact could not re-enter the target table for mutation')
            operation = self._bundle_set_table_column_widths(handle.hwp, requested_widths)
            mutation_started = True
            after_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
            after_text = self._get_live_document_text(handle, purpose='table-column-width-exact:after')
            after_text_guard = {
                'hash': self._bundle_sha256_text(after_text),
                'char_count': len(after_text),
                'nonempty_line_count': len([line.strip() for line in after_text.splitlines() if line.strip()]),
                'div0_count': after_text.count('#DIV/0!'),
            }
            after_control_map = self._capture_control_map_signature(handle.hwp)
            after_snapshot_path = self._snapshot_temp_hwpx(handle, purpose='table-column-width-exact-after')
            after_bindata = self._bundle_bindata_manifest(after_snapshot_path)
            after_inventory = self._bundle_table_inventory_signature(
                target_id=resolved['target_id'],
                rows=after_table['rows'],
                cols=after_table['cols'],
                document_text_hash=after_text_guard['hash'],
                text_char_count=after_text_guard['char_count'],
                nonempty_line_count=after_text_guard['nonempty_line_count'],
                div0_count=after_text_guard['div0_count'],
            )
            width_readback_ok = all(abs(float(actual) - float(requested)) <= 0.25 for actual, requested in zip(after_table['widths_mm'], requested_widths))
            if not width_readback_ok:
                raise LocalCliRuntimeError(
                    f'table_column_width_exact width readback mismatch: requested={requested_widths!r}, actual={after_table["widths_mm"]!r}'
                )
            if after_table['rows'] != before_table['rows'] or after_table['cols'] != before_table['cols']:
                raise LocalCliRuntimeError('table_column_width_exact changed native table row/column counts')
            if abs(float(after_table['table_width_mm']) - expected_total_width) > 0.2:
                raise LocalCliRuntimeError('table_column_width_exact changed the fixed native table total width')
            if after_text_guard != before_text_guard:
                raise LocalCliRuntimeError(f'table_column_width_exact changed document text/#DIV/0! guard: before={before_text_guard!r}; after={after_text_guard!r}')
            control_proof = self._assert_control_map_unchanged(
                before=before_control_map,
                after=after_control_map,
                operation='table_column_width_exact',
            )
            if after_bindata.get('manifest_hash') != before_bindata.get('manifest_hash'):
                raise LocalCliRuntimeError('table_column_width_exact changed the in-memory HWPX BinData manifest')
            if after_inventory['inventory_hash'] != before_inventory['inventory_hash']:
                raise LocalCliRuntimeError('table_column_width_exact changed the guarded cell/text inventory signature')
            return {
                'schema_version': 'local-cli/table-column-width-exact/v1',
                'read_only': False,
                'mutation': 'native-table-column-width',
                'succeeded': True,
                'doc_backed_api_actions': ['pyhwpx.set_col_width(widths_mm, as_=mm)'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                },
                'preimage': {
                    'working_copy_path': str(working_copy_path),
                    'expected_sha256': expected_preimage,
                    'actual_sha256': actual_preimage,
                    'matched': True,
                },
                'inventory_guard': {
                    'expected_hash': expected_cell_inventory_hash,
                    'before': before_inventory,
                    'after': after_inventory,
                    'matched': True,
                },
                'text_guard': {'before': before_text_guard, 'after': after_text_guard, 'matched': True},
                'control_map_guard': control_proof,
                'bindata_guard': {
                    'expected_hash': expected_bindata_hash,
                    'before': before_bindata,
                    'after': after_bindata,
                    'matched': True,
                },
                'table_readback': {
                    'expected_rows': expected_rows,
                    'expected_cols': expected_cols,
                    'expected_total_width_mm': expected_total_width,
                    'before': before_table,
                    'after': after_table,
                    'requested_widths_mm': requested_widths,
                    'width_readback_matched': width_readback_ok,
                },
                'operation': operation,
                'rollback': rollback,
                'snapshot_paths': {'before': str(before_snapshot_path), 'after': str(after_snapshot_path)},
                'warnings': ['Working copy remains unsaved; require close-without-save rollback evidence and fresh full-document Hancom render before promotion.'],
            }
        except Exception as exc:
            if mutation_started:
                rollback['attempted'] = True
                try:
                    entered = self._bundle_enter_table_cell_for_ctrl(handle.hwp, target_ctrl)
                    if not entered.get('is_cell'):
                        raise LocalCliRuntimeError('rollback could not re-enter target table')
                    self._bundle_set_table_column_widths(handle.hwp, [float(value) for value in before_table['widths_mm']])
                    rollback_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
                    rollback['succeeded'] = all(
                        abs(float(actual) - float(expected)) <= 0.25
                        for actual, expected in zip(rollback_table['widths_mm'], before_table['widths_mm'])
                    )
                    rollback['after'] = rollback_table
                    if not rollback['succeeded']:
                        raise LocalCliRuntimeError(f'rollback width readback mismatch: {rollback_table["widths_mm"]!r}')
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if rollback.get('attempted') and not rollback.get('succeeded'):
                raise LocalCliRuntimeError(
                    f'table_column_width_exact failed and rollback failed: primary={type(exc).__name__}: {exc}; rollback={rollback!r}'
                ) from exc
            raise
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(handle.hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass


    def _bundle_table_cell_structure_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='table_cell_structure_exact', require_table=True)
        max_controls = int(step.get('max_controls') or 2048)
        page_from = resolved.get('page_from')
        page_to = resolved.get('page_to') or page_from
        before_snapshot = self._bundle_compact_snapshot(hwp)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            entered_snapshot = self._bundle_compact_snapshot(hwp)
            metrics: dict[str, Any] = {}
            if enter.get('is_cell'):
                for key, method_name, kwargs in (
                    ('cell_addr_str', 'get_cell_addr', {'as_': 'str'}),
                    ('cell_addr_tuple', 'get_cell_addr', {'as_': 'tuple'}),
                    ('row_count', 'get_row_num', {}),
                    ('col_num', 'get_col_num', {}),
                    ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
                    ('row_height_mm', 'get_row_height', {'as_': 'mm'}),
                    ('col_width_hu', 'get_col_width', {'as_': 'hwpunit'}),
                    ('col_width_mm', 'get_col_width', {'as_': 'mm'}),
                    ('table_height_hu', 'get_table_height', {'as_': 'hwpunit'}),
                    ('table_height_mm', 'get_table_height', {'as_': 'mm'}),
                    ('table_width_hu', 'get_table_width', {'as_': 'hwpunit'}),
                    ('table_width_mm', 'get_table_width', {'as_': 'mm'}),
                    ('cell_margin_hu', 'get_cell_margin', {'as_': 'hwpunit'}),
                    ('cell_margin_mm', 'get_cell_margin', {'as_': 'mm'}),
                    ('table_inside_margin_hu', 'get_table_inside_margin', {'as_': 'hwpunit'}),
                    ('table_outside_margin_hu', 'get_table_outside_margin', {'as_': 'hwpunit'}),
                ):
                    metrics[key] = self._bundle_metric_probe(hwp, method_name, kwargs)
            navigation: list[dict[str, Any]] = []
            for label, action_name in (
                ('left', 'TableLeftCell'),
                ('right', 'TableRightCell'),
                ('up', 'TableUpperCell'),
                ('down', 'TableLowerCell'),
            ):
                navigation.append(self._bundle_navigation_probe_once(hwp, resolved['target_ctrl'], label=label, kind='action', action_name=action_name))
            for label, move_id in (
                ('move_pos_left_cell', 100),
                ('move_pos_right_cell', 101),
                ('move_pos_up_cell', 102),
                ('move_pos_down_cell', 103),
                ('move_pos_row_start', 104),
                ('move_pos_row_end', 105),
                ('move_pos_col_top', 106),
                ('move_pos_col_bottom', 107),
            ):
                navigation.append(self._bundle_navigation_probe_once(hwp, resolved['target_ctrl'], label=label, kind='move_pos', move_id=move_id))
            inferred_addresses = sorted(
                {
                    tuple(item.get(key) or [])
                    for item in navigation
                    for key in ('before_cell', 'after_cell')
                    if isinstance(item.get(key), list) and len(item.get(key)) >= 2
                }
            )
            inferred_max_row_index = max((int(cell[1]) for cell in inferred_addresses), default=None)
            inferred_max_col_index = max((int(cell[0]) for cell in inferred_addresses), default=None)
            table_frame_inventory = self._bundle_table_frame_inventory(
                hwp,
                {
                    'op': 'table_frame_inventory',
                    'page_from': page_from,
                    'page_to': page_to,
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'max_controls': max_controls,
                },
            )
            same_anchor_group = None
            target_anchor = '/'.join(str(part) for part in resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None
            for group in table_frame_inventory.get('anchor_groups') or []:
                if group.get('anchor_key') == target_anchor:
                    same_anchor_group = group
                    break
            any_navigation_moved = any(bool(item.get('moved')) for item in navigation)
            row_count_value = metrics.get('row_count', {}).get('value') if isinstance(metrics.get('row_count'), dict) else None
            clipping_owner_hypothesis = (
                'single-cell-or-non-navigable table container/frame is the active limiter; the co-anchored graphic/table flow, not the graphic size alone, likely clips content'
                if not any_navigation_moved
                else 'table appears cell-navigable; row/cell-level split or fit primitive may be targetable after rendered proof'
            )
            if row_count_value in (1, '1'):
                clipping_owner_hypothesis = 'documented row count reports one row; treat target as a single-cell container/frame unless a richer table property proves otherwise'
            return {
                'schema_version': 'local-cli/table-cell-structure-exact/v1',
                'read_only': True,
                'mutation': None,
                'succeeded': True,
                'doc_backed_api_actions': [
                    'Ctrl.GetCtrlInstID + hwp.SelectCtrl for exact control selection',
                    'ShapeObjTextBoxEdit to enter table A1 in normal edit state',
                    'get_cell_addr/get_row_num/get_col_num/get_row_height/get_col_width/get_table_height/get_table_width/get_cell_margin for read-only metrics',
                    'TableLeftCell/TableRightCell/TableUpperCell/TableLowerCell and move_pos(100..107) for read-only navigation probes',
                ],
                'enumeration_mode': resolved['enumeration_mode'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                    'shape_properties': self._shape_prop_snapshot(resolved['target_ctrl']),
                },
                'before': before_snapshot,
                'enter': enter,
                'entered_snapshot': entered_snapshot,
                'metrics': metrics,
                'navigation': navigation,
                'navigation_summary': {
                    'any_navigation_moved': any_navigation_moved,
                    'addresses_seen_zero_based_col_row': [list(cell) for cell in inferred_addresses],
                    'inferred_min_rows_from_navigation': None if inferred_max_row_index is None else inferred_max_row_index + 1,
                    'inferred_min_cols_from_navigation': None if inferred_max_col_index is None else inferred_max_col_index + 1,
                },
                'same_anchor_group': same_anchor_group,
                'table_frame_inventory': table_frame_inventory,
                'clipping_owner_hypothesis': clipping_owner_hypothesis,
                'next_safe_step_hint': 'Do not force TableSplitTable if navigation cannot leave A1; prefer a documented container/cell/frame-flow primitive or accept the blocker.',
                'warnings': [],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_set_cell_margin_values(self, hwp: Any, margins: Mapping[str, Any]) -> dict[str, Any]:
        method = getattr(hwp, 'set_cell_margin', None)
        if not callable(method):
            raise LocalCliRuntimeError('cell_format_exact requires pyhwpx set_cell_margin')
        normalized = _normalize_cell_margin_readback(margins)
        if normalized is None:
            raise LocalCliRuntimeError(f'cell_format_exact cell-margin values are invalid: {margins!r}')
        args = tuple(normalized[key] for key in _CELL_MARGIN_KEYS)
        kwargs = {'as_': 'hwpunit'}
        signature = None
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # Some COM proxies do not expose a Python signature.  The pinned
            # pyhwpx order is still invoked once; never probe alternatives.
            pass
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                raise LocalCliRuntimeError(
                    'cell_format_exact set_cell_margin does not support the documented '
                    'four-side HWPUNIT call'
                ) from exc
        label = 'set_cell_margin(left,right,top,bottom, as_=hwpunit)'
        try:
            raw = method(*args, **kwargs)
        except Exception as exc:
            raise LocalCliMutationError(
                f'cell_format_exact set_cell_margin failed after invocation: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            ) from exc
        result = bool(raw) if raw is not None else None
        attempt = {'method': label, 'result': result}
        if raw is not None and not bool(raw):
            raise LocalCliMutationError(
                'cell_format_exact set_cell_margin returned false after invocation',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            )
        return {'method': label, 'result': result, 'attempts': [attempt], 'arguments': dict(normalized)}

    def _bundle_set_uniform_cell_margin(self, hwp: Any, value_hu: int) -> dict[str, Any]:
        return self._bundle_set_cell_margin_values(
            hwp,
            {key: value_hu for key in _CELL_MARGIN_KEYS},
        )

    def _bundle_apply_cell_fill_color(self, hwp: Any, fill_color: str) -> dict[str, Any]:
        value = str(fill_color or '').strip().upper()
        if re.fullmatch(r'#[0-9A-F]{6}', value) is None:
            raise LocalCliRuntimeError('cell_format_exact fill_color must be #RRGGBB')
        rgb = tuple(int(value[offset:offset + 2], 16) for offset in (1, 3, 5))

        def action_ok(raw: Any, *, action_name: str) -> bool:
            # COM automation methods commonly return None on success, but an
            # explicit False is a native failure and must never be reported as
            # a successful cell mutation.
            if raw is not None and not bool(raw):
                raise LocalCliRuntimeError(f'cell_format_exact {action_name} returned false')
            return True

        def coerce_rgb(raw: Any) -> tuple[int, int, int] | None:
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                try:
                    channels = tuple(int(raw[index]) for index in range(3))
                except (TypeError, ValueError):
                    return None
                return channels if all(0 <= channel <= 255 for channel in channels) else None
            if isinstance(raw, Mapping):
                values = []
                for names in (('r', 'red'), ('g', 'green'), ('b', 'blue')):
                    selected = next((raw[name] for name in names if name in raw), None)
                    if selected is None:
                        return None
                    values.append(selected)
                return coerce_rgb(values)
            if isinstance(raw, bool):
                return None
            if isinstance(raw, int):
                # Win32 COLORREF stores RGB as 0x00BBGGRR.
                return (raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF)
            if isinstance(raw, str):
                text = raw.strip().upper()
                if re.fullmatch(r'#[0-9A-F]{6}', text):
                    return (
                        int(text[1:3], 16),
                        int(text[3:5], 16),
                        int(text[5:7], 16),
                    )
                if text.startswith('0X'):
                    try:
                        return coerce_rgb(int(text, 16))
                    except ValueError:
                        return None
            for names in (('red', 'r'), ('green', 'g'), ('blue', 'b')):
                if not any(hasattr(raw, name) for name in names):
                    break
            else:
                return coerce_rgb({
                    'r': next(getattr(raw, name) for name in ('red', 'r') if hasattr(raw, name)),
                    'g': next(getattr(raw, name) for name in ('green', 'g') if hasattr(raw, name)),
                    'b': next(getattr(raw, name) for name in ('blue', 'b') if hasattr(raw, name)),
                })
            return None

        def readback(*, refresh_default: bool = True) -> dict[str, Any]:
            parameter_root = getattr(hwp, 'HParameterSet', None)
            parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
            action = getattr(hwp, 'HAction', None)
            get_default = getattr(action, 'GetDefault', None)
            if parameter_set is not None:
                hset = getattr(parameter_set, 'HSet', parameter_set)
                if refresh_default and callable(get_default):
                    raw_default = get_default('CellFill', hset)
                    action_ok(raw_default, action_name='CellFill GetDefault')
                fill_attr = getattr(parameter_set, 'FillAttr', None)
                if fill_attr is None:
                    fill_attr = getattr(hset, 'FillAttr', None)
                if fill_attr is not None:
                    for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                        try:
                            observed = coerce_rgb(getattr(fill_attr, attribute))
                        except Exception:
                            observed = None
                        if observed is not None:
                            if observed != rgb:
                                raise LocalCliRuntimeError(
                                    f'cell_format_exact fill-color native readback mismatch: expected={rgb!r}; observed={observed!r}'
                                )
                            return {
                                'source': (
                                    'HAction.GetDefault(CellFill)'
                                    if refresh_default and callable(get_default)
                                    else 'HCellBorderFill.FillAttr'
                                ),
                                'observed_rgb': list(observed),
                                'expected_rgb': list(rgb),
                            }
            for getter_name in ('get_cell_fill_color', 'get_cell_fill', 'cell_fill_color'):
                getter = getattr(hwp, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    observed = coerce_rgb(getter())
                except Exception:
                    observed = None
                if observed is None:
                    continue
                if observed != rgb:
                    raise LocalCliRuntimeError(
                        f'cell_format_exact fill-color native readback mismatch: expected={rgb!r}; observed={observed!r}'
                    )
                return {
                    'source': getter_name,
                    'observed_rgb': list(observed),
                    'expected_rgb': list(rgb),
                }
            raise LocalCliRuntimeError(
                'cell_format_exact fill-color native readback was unavailable; refusing to report success'
            )

        def read_current_fill() -> dict[str, Any] | None:
            parameter_root = getattr(hwp, 'HParameterSet', None)
            parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
            action = getattr(hwp, 'HAction', None)
            get_default = getattr(action, 'GetDefault', None)
            if parameter_set is not None:
                hset = getattr(parameter_set, 'HSet', parameter_set)
                if callable(get_default):
                    try:
                        raw_default = get_default('CellFill', hset)
                        if raw_default is not None and not bool(raw_default):
                            return None
                    except Exception:
                        return None
                fill_attr = getattr(parameter_set, 'FillAttr', None) or getattr(hset, 'FillAttr', None)
                if fill_attr is not None:
                    for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                        try:
                            observed = coerce_rgb(getattr(fill_attr, attribute))
                        except Exception:
                            observed = None
                        if observed is not None:
                            return {'source': 'HAction.GetDefault(CellFill)', 'rgb': list(observed)}
            for getter_name in ('get_cell_fill_color', 'get_cell_fill', 'cell_fill_color'):
                getter = getattr(hwp, getter_name, None)
                if callable(getter):
                    try:
                        observed = coerce_rgb(getter())
                    except Exception:
                        observed = None
                    if observed is not None:
                        return {'source': getter_name, 'rgb': list(observed)}
            return None

        method = getattr(hwp, 'cell_fill', None)
        if callable(method):
            preimage = read_current_fill()
            mutation_attempted = False
            try:
                mutation_attempted = True
                raw = method(rgb)
                action_ok(raw, action_name='cell_fill')
                readback_proof = readback()
            except Exception as exc:
                rollback: dict[str, Any] = {
                    'attempted': False,
                    'succeeded': False,
                    'preimage': preimage,
                }
                if preimage is not None:
                    rollback['attempted'] = True
                    try:
                        rollback_raw = method(tuple(preimage['rgb']))
                        action_ok(rollback_raw, action_name='cell_fill rollback')
                        observed_after_rollback = read_current_fill()
                        rollback['observed'] = observed_after_rollback
                        rollback['succeeded'] = bool(
                            observed_after_rollback
                            and observed_after_rollback.get('rgb') == preimage.get('rgb')
                        )
                    except Exception as rollback_exc:
                        rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
                raise LocalCliMutationError(
                    f'cell_format_exact fill-color mutation/readback failed: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=mutation_attempted and not bool(rollback.get('succeeded')),
                    rollback=rollback,
                ) from exc
            return {
                'method': 'cell_fill((r,g,b))',
                'rgb': list(rgb),
                'result': bool(raw) if raw is not None else None,
                'preimage': preimage,
                'rollback': {'attempted': False, 'succeeded': False},
                'readback_proof': readback_proof,
            }
        parameter_root = getattr(hwp, 'HParameterSet', None)
        parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
        action = getattr(hwp, 'HAction', None)
        get_default = getattr(action, 'GetDefault', None)
        execute = getattr(action, 'Execute', None)
        if parameter_set is None or not callable(get_default) or not callable(execute):
            raise LocalCliRuntimeError(
                'cell_format_exact requires pyhwpx cell_fill or the documented HCellBorderFill CellFill parameter set'
            )

        hset = getattr(parameter_set, 'HSet', parameter_set)
        fill_attr = getattr(parameter_set, 'FillAttr', None)
        if fill_attr is None:
            fill_attr = getattr(hset, 'FillAttr', None)
        if fill_attr is None:
            raise LocalCliRuntimeError('cell_format_exact CellFill parameter set has no FillAttr member')
        rgb_color = getattr(hwp, 'RGBColor', None)
        color_value = rgb_color(*rgb) if callable(rgb_color) else rgb
        initial_default_rgb: tuple[int, int, int] | None = None
        fallback_mutation_attempted = False
        try:
            action_ok(get_default('CellFill', hset), action_name='CellFill GetDefault')
            for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                try:
                    initial_default_rgb = coerce_rgb(getattr(fill_attr, attribute))
                except Exception:
                    initial_default_rgb = None
                if initial_default_rgb is not None:
                    break
            fill_attr.Type = 1
            fill_attr.WinBrushFaceColor = color_value
            fallback_mutation_attempted = True
            raw = execute('CellFill', hset)
            action_ok(raw, action_name='CellFill Execute')
            readback_proof = readback(refresh_default=initial_default_rgb is not None)
        except Exception as exc:
            rollback: dict[str, Any] = {
                'attempted': False,
                'succeeded': False,
                'preimage': {'rgb': list(initial_default_rgb)} if initial_default_rgb is not None else None,
            }
            if fallback_mutation_attempted and initial_default_rgb is not None:
                rollback['attempted'] = True
                try:
                    fill_attr.Type = 1
                    fill_attr.WinBrushFaceColor = (
                        rgb_color(*initial_default_rgb) if callable(rgb_color) else initial_default_rgb
                    )
                    rollback_raw = execute('CellFill', hset)
                    action_ok(rollback_raw, action_name='CellFill rollback')
                    observed_after_rollback = read_current_fill()
                    rollback['observed'] = observed_after_rollback
                    rollback['succeeded'] = bool(
                        observed_after_rollback
                        and observed_after_rollback.get('rgb') == list(initial_default_rgb)
                    )
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if isinstance(exc, LocalCliRuntimeError) and not fallback_mutation_attempted:
                raise
            raise LocalCliMutationError(
                f'cell_format_exact HAction.Execute(CellFill) failed: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=fallback_mutation_attempted and not bool(rollback.get('succeeded')),
                rollback=rollback,
            ) from exc
        return {
            'method': 'HAction.Execute(CellFill)',
            'rgb': list(rgb),
            'color_value': color_value,
            'result': bool(raw) if raw is not None else None,
            # A few older pyhwpx-compatible wrappers expose the parameter set
            # but do not populate it from GetDefault.  In that case the
            # post-execute FillAttr value is the only available proof and a
            # second GetDefault call would add no information.  Real native
            # bindings that provide an initial value are refreshed after the
            # mutation so a stale requested value cannot masquerade as proof.
            'readback_proof': readback_proof,
            'preimage': {'rgb': list(initial_default_rgb)} if initial_default_rgb is not None else None,
            'rollback': {'attempted': False, 'succeeded': False},
        }

    def _bundle_apply_cell_border_none(self, hwp: Any) -> dict[str, Any]:
        action = getattr(hwp, 'HAction', None)
        hset_root = getattr(hwp, 'HParameterSet', None)
        parameter_set = getattr(hset_root, 'HCellBorderFill', None) if hset_root is not None else None
        get_default = getattr(action, 'GetDefault', None)
        execute = getattr(action, 'Execute', None)
        hset_value = getattr(parameter_set, 'HSet', parameter_set) if parameter_set is not None else None
        set_item = getattr(hset_value, 'SetItem', None) if hset_value is not None else None
        if parameter_set is None or not callable(get_default) or not callable(execute) or not callable(set_item):
            raise LocalCliRuntimeError(
                'cell_format_exact border-none requires the explicit HCellBorderFill parameter set and diagonal readback; '
                'TableCellBorderNo fallback cannot prove diagonal NONE'
            )

        line_type_factory = getattr(hwp, 'HwpLineType', None)
        line_width_factory = getattr(hwp, 'HwpLineWidth', None)
        line_type = line_type_factory('None') if callable(line_type_factory) else 'None'
        line_width = line_width_factory('0.1mm') if callable(line_width_factory) else '0.1mm'
        flags = (
            'SlashFlag',
            'BackSlashFlag',
            'CounterSlashFlag',
            'CounterBackSlashFlag',
            'CenterLineFlag',
            'CrookedSlashFlag',
            'CrookedSlashFlag1',
            'CrookedSlashFlag2',
        )
        side_names = ('Left', 'Right', 'Top', 'Bottom')
        expected_type_items = tuple(f'BorderType{side}' for side in side_names) + ('DiagonalType',)
        expected_line_items = tuple(
            list(expected_type_items)
            + [f'BorderWidth{side}' for side in side_names]
            + ['DiagonalWidth']
        )

        def action_ok(raw: Any) -> bool:
            return raw is None or bool(raw)

        def read_parameter_item(name: str) -> Any:
            errors: list[str] = []
            for owner, owner_name in ((parameter_set, 'parameter set'), (hset_value, 'HSet')):
                try:
                    value = getattr(owner, name)
                except Exception as exc:
                    errors.append(f'{owner_name}: {type(exc).__name__}: {exc}')
                    continue
                if value is not None:
                    return value
                errors.append(f'{owner_name}: returned null')
            for accessor_name in ('GetItem', 'Item'):
                try:
                    accessor = getattr(hset_value, accessor_name)
                except Exception as exc:
                    errors.append(f'{accessor_name}: {type(exc).__name__}: {exc}')
                    continue
                if not callable(accessor):
                    errors.append(f'{accessor_name}: not callable')
                    continue
                try:
                    value = accessor(name)
                except Exception as exc:
                    errors.append(f'{accessor_name}: {type(exc).__name__}: {exc}')
                    continue
                if value is not None:
                    return value
                errors.append(f'{accessor_name}: returned null')
            detail = '; '.join(errors[-4:])
            raise LocalCliRuntimeError(f'cell_format_exact border-none readback unavailable for {name}: {detail}')

        def is_none_line(value: Any) -> bool:
            if isinstance(value, bool):
                return not value
            if isinstance(value, (int, float)):
                return value == 0
            normalized = str(value).strip().lower()
            return normalized in {'0', 'none', 'line:none'} or normalized.endswith(':none')

        def is_clear_flag(value: Any) -> bool:
            if isinstance(value, bool):
                return not value
            if isinstance(value, (int, float)):
                return value == 0
            return str(value).strip().lower() in {'', '0', 'false', 'none'}

        preimage_values: dict[str, Any] = {}
        preimage_missing: list[str] = []
        mutation_attempted = False
        try:
            if not action_ok(get_default('CellBorderFill', hset_value)):
                raise LocalCliRuntimeError('cell_format_exact border-none GetDefault returned false')
            for name in expected_line_items + flags + ('ApplyTo',):
                try:
                    preimage_values[name] = read_parameter_item(name)
                except LocalCliRuntimeError:
                    preimage_missing.append(name)
            for side in side_names:
                mutation_attempted = True
                set_item(f'BorderType{side}', line_type)
                set_item(f'BorderWidth{side}', line_width)
            # Hancom's documented BorderFill schema uses numeric PIT_UI*
            # values for DiagonalType/DiagonalWidth. BorderTypeDiagonal is
            # not a native item and can leave a persisted diagonal line
            # untouched. ApplyTo=0 is the selected-cell value; ApplyTo=1
            # leaves a cell's persisted diagonal unchanged on Hancom.
            set_item('DiagonalType', 0)
            set_item('DiagonalWidth', 0)
            for flag in flags:
                set_item(flag, 0)
            set_item('ApplyTo', 0)
            raw = execute('CellBorderFill', hset_value)
            if not action_ok(raw):
                raise LocalCliRuntimeError('cell_format_exact border-none Execute returned false')
            if not action_ok(get_default('CellBorderFill', hset_value)):
                raise LocalCliRuntimeError('cell_format_exact border-none readback GetDefault returned false')

            readback_values = {name: read_parameter_item(name) for name in expected_line_items}
            readback_flags = {name: read_parameter_item(name) for name in flags}
            non_none_sides = [name for name in expected_type_items if not is_none_line(readback_values[name])]
            non_clear_flags = [name for name, value in readback_flags.items() if not is_clear_flag(value)]
            if non_none_sides or non_clear_flags:
                details = ', '.join(non_none_sides + non_clear_flags)
                raise LocalCliRuntimeError(
                    f'cell_format_exact border-none diagonal/all-side readback did not prove NONE: {details}'
                )
            preimage = {
                'available': not preimage_missing,
                'values': preimage_values,
                'missing': preimage_missing,
            }
            return {
                'method': 'HAction.Execute(CellBorderFill border none)',
                'result': bool(raw) if raw is not None else None,
                'border_sides': ['Left', 'Right', 'Top', 'Bottom', 'Diagonal'],
                'diagonal_none_requested': True,
                'preimage': preimage,
                'rollback': {'attempted': False, 'succeeded': False},
                'readback_proof': {
                    'action': 'HAction.GetDefault(CellBorderFill)',
                    'all_sides_none': True,
                    'diagonal_none': True,
                    'diagonal_flags_clear': True,
                    'verified_items': list(expected_line_items) + list(flags),
                },
            }
        except LocalCliRuntimeError as exc:
            rollback: dict[str, Any] = {
                'attempted': False,
                'succeeded': False,
                'preimage': {
                    'available': not preimage_missing,
                    'values': preimage_values,
                    'missing': preimage_missing,
                },
            }
            if mutation_attempted and not preimage_missing:
                rollback['attempted'] = True
                try:
                    for name, value in preimage_values.items():
                        set_item(name, value)
                    rollback_raw = execute('CellBorderFill', hset_value)
                    if not action_ok(rollback_raw):
                        raise LocalCliRuntimeError('cell_format_exact border-none rollback Execute returned false')
                    if not action_ok(get_default('CellBorderFill', hset_value)):
                        raise LocalCliRuntimeError('cell_format_exact border-none rollback GetDefault returned false')
                    rollback_values = {name: read_parameter_item(name) for name in preimage_values}
                    rollback['observed'] = rollback_values
                    rollback['succeeded'] = rollback_values == preimage_values
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if not mutation_attempted:
                raise
            raise LocalCliMutationError(
                f'cell_format_exact border-none mutation/readback failed: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=not bool(rollback.get('succeeded')),
                rollback=rollback,
            ) from exc
        except Exception as parameter_exc:
            if not mutation_attempted:
                raise LocalCliRuntimeError(
                    f'cell_format_exact HAction.Execute(CellBorderFill border none) failed: '
                    f'{type(parameter_exc).__name__}: {parameter_exc}'
                ) from parameter_exc
            raise LocalCliMutationError(
                f'cell_format_exact border-none mutation failed: {type(parameter_exc).__name__}: {parameter_exc}',
                mutation_may_have_persisted=True,
                rollback={
                    'attempted': False,
                    'succeeded': False,
                    'preimage': {
                        'available': not preimage_missing,
                        'values': preimage_values,
                        'missing': preimage_missing,
                    },
                },
            ) from parameter_exc

    def _bundle_cell_format_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('cell_format_exact requires confirm_layout=true')
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='cell_format_exact', require_table=True)
        target_ctrl = resolved['target_ctrl']
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        if not before_metrics.get('available'):
            raise LocalCliRuntimeError(f'cell_format_exact cannot read target cell metrics: {before_metrics.get("error")}')
        requested_margin_hu: int | None = None
        if step.get('cell_margin_hu') is not None or step.get('cell_margin_mm') is not None:
            requested_margin_hu = (
                int(round(float(step.get('cell_margin_hu'))))
                if step.get('cell_margin_hu') is not None
                else self._mm_to_hwp_unit(hwp, float(step.get('cell_margin_mm')))
            )
            before_margin = _normalize_cell_margin_readback(before_metrics.get('cell_margin_hu'))
            expected_margin = {key: requested_margin_hu for key in _CELL_MARGIN_KEYS}
            if before_margin == expected_margin:
                raise LocalCliRuntimeError(
                    f'cell_format_exact requested cell margins are already present: {expected_margin!r}'
                )
        elif step.get('fill_color') is None and step.get('border') is None:
            requested_vertical_align = str(step.get('vertical_align') or '').strip()
            before_vertical = _normalize_vertical_align_readback(before_metrics.get('vertical_align'))
            if before_vertical is None:
                raise LocalCliRuntimeError(
                    'cell_format_exact cannot mutate vertical alignment without a fresh native before readback'
                )
            if requested_vertical_align == before_vertical['name']:
                raise LocalCliRuntimeError(
                    f'cell_format_exact requested vertical alignment is already present: {requested_vertical_align!r}'
                )

        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        mutation_attempted = False
        operation_kind: str | None = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, target_ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('cell_format_exact cannot enter the target table cell for mutation')
            raw_results: list[dict[str, Any]] = []
            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if callable(run):
                try:
                    raw = run('TableCellBlock')
                    raw_results.append({'method': 'TableCellBlock', 'result': bool(raw) if raw is not None else None})
                    if raw is not None and not bool(raw):
                        raise LocalCliRuntimeError('cell_format_exact TableCellBlock returned false')
                except Exception as exc:
                    if isinstance(exc, LocalCliRuntimeError):
                        raise
                    raw_results.append({'method': 'TableCellBlock', 'error': f'{type(exc).__name__}: {exc}'})
            if step.get('cell_margin_hu') is not None or step.get('cell_margin_mm') is not None:
                assert requested_margin_hu is not None
                value_hu = requested_margin_hu
                operation_kind = 'set-cell-margin'
                mutation_attempted = True
                margin_result = self._bundle_set_uniform_cell_margin(hwp, value_hu)
                raw_results.append({'method': margin_result.get('method'), 'result': margin_result.get('result'), 'value_hu': value_hu, 'attempts': margin_result.get('attempts')})
                operation = {
                    'op': 'set-cell-margin',
                    'cell_margin_hu': value_hu,
                    'requested_margins_hu': {key: value_hu for key in _CELL_MARGIN_KEYS},
                    'cell_margin_mm': self._hwp_unit_to_mm(hwp, value_hu),
                    'raw_results': raw_results,
                    'enter': enter,
                }
            elif step.get('fill_color') is not None:
                operation_kind = 'fill-color'
                fill_result = self._bundle_apply_cell_fill_color(hwp, str(step.get('fill_color')))
                mutation_attempted = True
                raw_results.append(fill_result)
                operation = {
                    'op': 'fill-color',
                    'fill_color': str(step.get('fill_color')).upper(),
                    'raw_results': raw_results,
                    'enter': enter,
                }
            elif step.get('border') is not None:
                operation_kind = 'border-none'
                border_result = self._bundle_apply_cell_border_none(hwp)
                mutation_attempted = True
                raw_results.append(border_result)
                operation = {
                    'op': 'border-none',
                    'border': 'none',
                    'raw_results': raw_results,
                    'enter': enter,
                }
            else:
                vertical_align = str(step.get('vertical_align') or '').strip()
                action_map = {
                    'top': 'TableVAlignTop',
                    'center': 'TableVAlignCenter',
                    'bottom': 'TableVAlignBottom',
                }
                action_name = action_map.get(vertical_align)
                if not action_name:
                    raise LocalCliRuntimeError('cell_format_exact requires vertical_align top, center, or bottom')
                if not callable(run):
                    raise LocalCliRuntimeError(f'cell_format_exact requires HAction.Run for {action_name}')
                operation_kind = 'vertical-align'
                mutation_attempted = True
                raw = run(action_name)
                raw_results.append({'method': action_name, 'result': bool(raw) if raw is not None else None})
                if raw is not None and not bool(raw):
                    raise LocalCliMutationError(
                        f'cell_format_exact {action_name} returned false after invocation',
                        mutation_may_have_persisted=True,
                        rollback={'attempted': False, 'succeeded': False},
                    )
                operation = {
                    'op': 'vertical-align',
                    'vertical_align': vertical_align,
                    'action_name': action_name,
                    'raw_results': raw_results,
                    'enter': enter,
                }
        except LocalCliMutationError:
            raise
        except LocalCliRuntimeError as exc:
            if mutation_attempted:
                raise LocalCliMutationError(
                    f'cell_format_exact {operation_kind or "mutation"} failed after invocation: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=True,
                    rollback={'attempted': False, 'succeeded': False},
                ) from exc
            raise
        except Exception as exc:
            if mutation_attempted:
                raise LocalCliMutationError(
                    f'cell_format_exact {operation_kind or "mutation"} failed after invocation: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=True,
                    rollback={'attempted': False, 'succeeded': False},
                ) from exc
            raise LocalCliRuntimeError(f'cell_format_exact mutation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

        try:
            after_snapshot = self._bundle_compact_snapshot(hwp)
            after_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
            changed_metrics = {
                key: {'before': before_metrics.get(key), 'after': after_metrics.get(key)}
                for key in sorted(set(before_metrics) | set(after_metrics))
                if before_metrics.get(key) != after_metrics.get(key)
            }
            _require_observed_cell_format_mutation(
                str(operation.get('op') or ''),
                before_metrics,
                after_metrics,
                changed_metrics,
                expected_vertical_align=(
                    str(operation.get('vertical_align') or '')
                    if operation.get('op') == 'vertical-align'
                    else None
                ),
                expected_cell_margin_hu=(
                    operation.get('requested_margins_hu')
                    if operation.get('op') == 'set-cell-margin'
                    else None
                ),
                expected_cell_addr=before_metrics.get('cell_addr'),
            )

            post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=int(resolved['max_controls']))
            if len(post_controls) != len(resolved['controls']):
                raise LocalCliRuntimeError(f'cell_format_exact control count changed unexpectedly: before={len(resolved["controls"])}, after={len(post_controls)}')
            post_target_items: list[dict[str, Any]] = []
            post_original_pos = None
            try:
                post_original_pos = _get_pos(hwp)
            except Exception:
                post_original_pos = None
            try:
                for index, ctrl in enumerate(post_controls):
                    item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                    if item.get('target_id') == resolved['target_id']:
                        post_target_items.append(item)
            finally:
                if post_original_pos is not None and len(post_original_pos) >= 3:
                    try:
                        _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                    except Exception:
                        pass
            if len(post_target_items) != 1:
                raise LocalCliRuntimeError(f'cell_format_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {resolved["target_id"]!r}')
        except LocalCliMutationError:
            raise
        except Exception as exc:
            raise LocalCliMutationError(
                f'cell_format_exact {operation_kind or "mutation"} post-mutation readback/proof failed: '
                f'{type(exc).__name__}: {exc}',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            ) from exc

        warnings = ['This primitive mutates one target table cell format only; rendered before/after proof is required before accepting the working copy.']
        if operation.get('op') == 'vertical-align':
            warnings.append('Vertical alignment includes native ShapeTableCell.VertAlign before/after readback; rendered review remains required for final acceptance.')
        return {
            'schema_version': 'local-cli/cell-format-exact/v1',
            'read_only': False,
            'mutation': 'cell-format',
            'succeeded': True,
            'enumeration_mode': resolved['enumeration_mode'],
            'scope': {
                'section_anchor': resolved['section_anchor'],
                'page_from': resolved['page_from'],
                'page_to': resolved['page_to'],
                'around': resolved['around'],
            },
            'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
            'target_proof': {
                'target_id': resolved['target_id'],
                'expected_hash': resolved['expected_hash'],
                'expected_page': resolved['expected_page'],
                'matched_before': resolved['before_item'],
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
            },
            'operation': operation,
            'before': before_snapshot,
            'after': after_snapshot,
            'metrics_before': before_metrics,
            'metrics_after': after_metrics,
            'changed_metrics': changed_metrics,
            'post_mutation': {'control_count_before': len(resolved['controls']), 'control_count_after': len(post_controls), 'post_target_count': len(post_target_items)},
            'warnings': warnings,
        }


    def _bundle_page_table_items(self, hwp: Any, *, page_from: int | None, page_to: int | None, max_controls: int) -> list[dict[str, Any]]:
        controls, _mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        items: list[dict[str, Any]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('type') != 'tbl':
                    continue
                page = item.get('page')
                if page is not None and page_from is not None:
                    if not (int(page_from) <= int(page) <= int(page_to or page_from)):
                        continue
                items.append({
                    'target_id': item.get('target_id'),
                    'page': page,
                    'proof_hash': item.get('proof_hash'),
                    'shape_properties': item.get('shape_properties'),
                    'text_preview': item.get('text_preview'),
                })
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        return items

    def _run_table_split_action(self, hwp: Any) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        action = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        if callable(action):
            try:
                raw = action('TableSplitTable')
                attempts.append({'method': 'HAction.Run(TableSplitTable)', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'HAction.Run', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'HAction.Run(TableSplitTable)', 'error': f'{type(exc).__name__}: {exc}'})
        run = getattr(hwp, 'Run', None)
        if callable(run):
            try:
                raw = run('TableSplitTable')
                attempts.append({'method': 'Run(TableSplitTable)', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'Run', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'Run(TableSplitTable)', 'error': f'{type(exc).__name__}: {exc}'})
        py_method = getattr(hwp, 'TableSplitTable', None)
        if callable(py_method):
            try:
                raw = py_method()
                attempts.append({'method': 'TableSplitTable()', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'TableSplitTable', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'TableSplitTable()', 'error': f'{type(exc).__name__}: {exc}'})
        return {'succeeded': False, 'method': None, 'attempts': attempts}

    def _bundle_table_split_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='table_split_exact', require_table=True)
        down_rows = int(step.get('down_rows') or 0)
        max_controls = int(step.get('max_controls') or 2048)
        page_from = resolved.get('page_from')
        page_to = resolved.get('page_to') or page_from
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, resolved['target_ctrl'])
        before_tables = self._bundle_page_table_items(hwp, page_from=page_from, page_to=page_to, max_controls=max_controls)
        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            if not enter.get('is_cell') or not enter.get('normal_edit_state'):
                raise LocalCliRuntimeError('table_split_exact cannot enter the target table cell in normal edit state for mutation')
            edit_mode_normalization: list[dict[str, Any]] = []
            pre_navigation_state: dict[str, Any] = {'snapshot': self._bundle_compact_snapshot(hwp)}
            for key, method_name, kwargs in (
                ('cell_addr', 'get_cell_addr', {'as_': 'tuple'}),
                ('row_count', 'get_row_num', {}),
                ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
            ):
                method = getattr(hwp, method_name, None)
                if callable(method):
                    try:
                        pre_navigation_state[key] = method(**kwargs)
                    except Exception as exc:
                        pre_navigation_state[key] = {'error': f'{type(exc).__name__}: {exc}'}
                else:
                    pre_navigation_state[key] = {'unavailable': method_name}
            navigation: list[dict[str, Any]] = []
            lower = getattr(hwp, 'TableLowerCell', None)
            haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            for move_index in range(down_rows):
                before_cell = self._bundle_current_cell_addr_tuple(hwp)
                try:
                    moved_down = False
                    for attempt_index in range(3):
                        if callable(lower):
                            raw = lower()
                            method = 'TableLowerCell()'
                        elif callable(haction_run):
                            raw = haction_run('TableLowerCell')
                            method = 'HAction.Run(TableLowerCell)'
                        else:
                            raise LocalCliRuntimeError('TableLowerCell navigation is unavailable')
                        after_cell = self._bundle_current_cell_addr_tuple(hwp)
                        snapshot = self._bundle_compact_snapshot(hwp)
                        moved_down = (
                            before_cell is not None
                            and after_cell is not None
                            and int(after_cell[1]) > int(before_cell[1])
                        )
                        navigation.append({
                            'step': move_index + 1,
                            'attempt': attempt_index + 1,
                            'method': method,
                            'result': bool(raw) if raw is not None else None,
                            'before_cell': list(before_cell) if before_cell is not None else None,
                            'after_cell': list(after_cell) if after_cell is not None else None,
                            'moved_down': moved_down,
                            'snapshot': snapshot,
                        })
                        if moved_down:
                            break
                    if not moved_down:
                        raise LocalCliRuntimeError(f'table_split_exact failed to move down to split row at step {move_index + 1}; pre_navigation_state={pre_navigation_state!r}; navigation={navigation!r}')
                except Exception as exc:
                    navigation.append({'step': move_index + 1, 'error': f'{type(exc).__name__}: {exc}'})
                    raise
            split_cell_snapshot = self._bundle_compact_snapshot(hwp)
            if not split_cell_snapshot.get('is_cell'):
                raise LocalCliRuntimeError('table_split_exact split point is not inside a table cell after navigation')
            if split_cell_snapshot.get('has_selection') or int(split_cell_snapshot.get('selection_mode') or 0) != 0:
                text_box_edit = getattr(hwp, 'ShapeObjTextBoxEdit', None)
                if callable(text_box_edit):
                    try:
                        raw = text_box_edit()
                        normalized_snapshot = self._bundle_compact_snapshot(hwp)
                        edit_mode_normalization.append({'method': 'ShapeObjTextBoxEdit before TableSplitTable', 'result': bool(raw) if raw is not None else None, 'before': split_cell_snapshot, 'after': normalized_snapshot})
                        split_cell_snapshot = normalized_snapshot
                    except Exception as exc:
                        edit_mode_normalization.append({'method': 'ShapeObjTextBoxEdit before TableSplitTable', 'error': f'{type(exc).__name__}: {exc}', 'before': split_cell_snapshot})
                if split_cell_snapshot.get('has_selection') or int(split_cell_snapshot.get('selection_mode') or 0) != 0:
                    raise LocalCliRuntimeError('table_split_exact split point is not in normal edit state before TableSplitTable')
            split_result = self._run_table_split_action(hwp)
            if not split_result.get('succeeded'):
                raise LocalCliRuntimeError(f'table_split_exact TableSplitTable did not succeed: {split_result.get("attempts")!r}')
            after_snapshot = self._bundle_compact_snapshot(hwp)
            after_tables = self._bundle_page_table_items(hwp, page_from=page_from, page_to=page_to, max_controls=max_controls)
            changed = before_tables != after_tables
            if not changed:
                raise LocalCliRuntimeError('table_split_exact refused to mark success: post-split table inventory did not change')
            return {
                'schema_version': 'local-cli/table-split-exact/v1',
                'succeeded': True,
                'doc_backed_action': 'TableSplitTable',
                'doc_behavior': 'Hancom Split Table splits beneath the row at the cursor position; first-row split is invalid.',
                'enumeration_mode': resolved['enumeration_mode'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                },
                'before': before_snapshot,
                'before_metrics': before_metrics,
                'before_tables': before_tables,
                'enter': enter,
                'edit_mode_normalization': edit_mode_normalization,
                'down_rows': down_rows,
                'pre_navigation_state': pre_navigation_state,
                'navigation': navigation,
                'split_cell_snapshot': split_cell_snapshot,
                'split_result': split_result,
                'after': after_snapshot,
                'after_tables': after_tables,
                'changed': changed,
                'warnings': [],
            }
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

    def _bundle_cell_row_fit_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('cell_row_fit_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('cell_row_fit_exact requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'cell_row_fit_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'cell_row_fit_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'cell_row_fit_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('type') != 'tbl':
            raise LocalCliRuntimeError(f"cell_row_fit_exact target must be a table control, got {before_item.get('type')!r}")
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"cell_row_fit_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'cell_row_fit_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'cell_row_fit_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'cell_row_fit_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')

        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        if not before_metrics.get('available'):
            raise LocalCliRuntimeError(f'cell_row_fit_exact cannot read row metrics: {before_metrics.get("error")}')
        before_height_hu = float(before_metrics.get('row_height_hu'))
        new_height_hu = None
        if step.get('row_height_percent') is not None:
            new_height_hu = max(1, int(round(before_height_hu * float(step.get('row_height_percent')) / 100.0)))
        elif step.get('row_height_hu') is not None:
            new_height_hu = int(round(float(step.get('row_height_hu'))))
        elif step.get('row_height_mm') is not None:
            new_height_hu = self._mm_to_hwp_unit(hwp, float(step.get('row_height_mm')))
        elif step.get('resize_up_steps') is None and step.get('resize_down_steps') is None and step.get('line_spacing') is None and step.get('char_height_percent') is None:
            raise LocalCliRuntimeError('cell_row_fit_exact requires row_height_percent, row_height_hu, row_height_mm, resize steps, or contained style fit')
        if new_height_hu is not None and new_height_hu <= 0:
            raise LocalCliRuntimeError('cell_row_fit_exact computed non-positive row height')

        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, target_ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('cell_row_fit_exact cannot enter the target table cell for mutation')
            raw_results = []
            if new_height_hu is not None:
                set_row_height = getattr(hwp, 'set_row_height', None)
                if not callable(set_row_height):
                    raise LocalCliRuntimeError('cell_row_fit_exact requires pyhwpx set_row_height')
                raw_result = set_row_height(new_height_hu, as_='hwpunit')
                raw_results.append({'method': 'set_row_height', 'result': bool(raw_result) if raw_result is not None else None})
                op_name = 'set-row-height'
            elif step.get('line_spacing') is not None or step.get('char_height_percent') is not None:
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                if callable(run):
                    for action in ('TableCellBlock', 'TableCellBlockExtend'):
                        try:
                            raw = run(action)
                            raw_results.append({'method': action, 'result': bool(raw) if raw is not None else None})
                        except Exception as exc:
                            raw_results.append({'method': action, 'error': f'{type(exc).__name__}: {exc}'})
                if step.get('line_spacing') is not None:
                    set_para = getattr(hwp, 'set_para', None)
                    if not callable(set_para):
                        raise LocalCliRuntimeError('cell_row_fit_exact requires pyhwpx set_para for line_spacing')
                    raw = set_para(LineSpacing=int(step.get('line_spacing')))
                    raw_results.append({'method': 'set_para(LineSpacing)', 'value': int(step.get('line_spacing')), 'result': bool(raw) if raw is not None else None})
                if step.get('char_height_percent') is not None:
                    h_parameter_set = getattr(hwp, 'HParameterSet', None)
                    h_action = getattr(hwp, 'HAction', None)
                    if h_parameter_set is None or h_action is None:
                        raise LocalCliRuntimeError('cell_row_fit_exact requires HParameterSet/HAction for char height')
                    pset = h_parameter_set.HCharShape
                    h_action.GetDefault('CharShape', pset.HSet)
                    old_height = getattr(pset, 'Height')
                    new_height = max(100, int(round(float(old_height) * float(step.get('char_height_percent')) / 100.0)))
                    setattr(pset, 'Height', new_height)
                    raw = h_action.Execute('CharShape', pset.HSet)
                    raw_results.append({'method': 'CharShape.Height', 'old_height': old_height, 'new_height': new_height, 'percent': float(step.get('char_height_percent')), 'result': bool(raw) if raw is not None else None})
                op_name = 'contained-style-fit'
            else:
                action_name = 'TableResizeUp' if step.get('resize_up_steps') is not None else 'TableResizeDown'
                count = int(step.get('resize_up_steps') or step.get('resize_down_steps') or 0)
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                method = getattr(hwp, action_name, None)
                for _ in range(count):
                    if callable(method):
                        raw = method()
                    elif callable(run):
                        raw = run(action_name)
                    else:
                        raise LocalCliRuntimeError(f'cell_row_fit_exact requires {action_name}')
                    raw_results.append({'method': action_name, 'result': bool(raw) if raw is not None else None})
                op_name = action_name
            operation = {
                'op': op_name,
                'old_row_height_hu': before_height_hu,
                'old_row_height_mm': self._hwp_unit_to_mm(hwp, before_height_hu),
                'new_row_height_hu': new_height_hu,
                'new_row_height_mm': self._hwp_unit_to_mm(hwp, new_height_hu) if new_height_hu is not None else None,
                'row_height_percent': step.get('row_height_percent'),
                'row_height_hu': step.get('row_height_hu'),
                'row_height_mm': step.get('row_height_mm'),
                'resize_up_steps': step.get('resize_up_steps'),
                'resize_down_steps': step.get('resize_down_steps'),
                'line_spacing': step.get('line_spacing'),
                'char_height_percent': step.get('char_height_percent'),
                'raw_results': raw_results[:12],
                'raw_result_count': len(raw_results),
                'enter': enter,
            }
        except LocalCliRuntimeError:
            raise
        except Exception as exc:
            raise LocalCliRuntimeError(f'cell_row_fit_exact mutation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        before_after_delta = {
            key: {'before': before_metrics.get(key), 'after': after_metrics.get(key)}
            for key in sorted(set(before_metrics) | set(after_metrics))
            if before_metrics.get(key) != after_metrics.get(key)
        }
        style_selector_used = step.get('line_spacing') is not None or step.get('char_height_percent') is not None
        if style_selector_used:
            style_changed = any(
                before_metrics.get(key) != after_metrics.get(key)
                for key in ('char_height_raw', 'para_line_spacing', 'para_line_spacing_type')
            )
            if not style_changed:
                raise LocalCliRuntimeError(
                    'cell_row_fit_exact did not observe changed contained style metrics after mutation; '
                    f'before={before_metrics!r}; after={after_metrics!r}; operation={operation!r}'
                )
        elif before_metrics.get('row_height_hu') == after_metrics.get('row_height_hu'):
            raise LocalCliRuntimeError(
                'cell_row_fit_exact did not observe changed row_height_hu after mutation; '
                f'before={before_metrics!r}; after={after_metrics!r}; operation={operation!r}'
            )

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(f'cell_row_fit_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}')
        post_target_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_target_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_target_items) != 1:
            raise LocalCliRuntimeError(f'cell_row_fit_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {target_id!r}')
        return {
            'schema_version': 'local-cli/cell-row-fit-exact/v1',
            'read_only': False,
            'mutation': 'cell-row-fit',
            'succeeded': True,
            'enumeration_mode': enumeration_mode,
            'scope': {'section_anchor': section_anchor, 'page_from': page_from, 'page_to': page_to, 'around': around},
            'anchors': {'section_anchor': anchor_evidence, 'around': around_evidence},
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'operation': operation,
            'before': before_snapshot,
            'after': after_snapshot,
            'metrics_before': before_metrics,
            'metrics_after': after_metrics,
            'changed_metrics': before_after_delta,
            'post_mutation': {'control_count_before': len(controls), 'control_count_after': len(post_controls), 'post_target_count': len(post_target_items)},
            'warnings': ['This primitive mutates one target table row/cell height only; rendered before/after proof is required before accepting the working copy.'],
        }

    def _bundle_control_move_resize_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('control_move_resize_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_move_resize_exact requires positive expected_page')

        scale_percent = step.get('scale_percent')
        scale_factor = float(scale_percent) / 100.0 if scale_percent is not None else None
        move_dx_mm = float(step.get('move_dx_mm') or 0.0)
        move_dy_mm = float(step.get('move_dy_mm') or 0.0)
        if scale_factor is None and move_dx_mm == 0.0 and move_dy_mm == 0.0:
            raise LocalCliRuntimeError('control_move_resize_exact requires scale_percent or non-zero move delta')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_move_resize_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_move_resize_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        if len(matching) != 1:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}'
            )
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"control_move_resize_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_move_resize_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}'
            )
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(
                f'control_move_resize_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}'
            )

        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_shape = self._shape_prop_snapshot(target_ctrl)
        if not before_shape.get('available'):
            raise LocalCliRuntimeError(f'control_move_resize_exact cannot read shape properties: {before_shape.get("error")}')
        before_values = dict(before_shape.get('values') or {})
        if 'Width' not in before_values or 'Height' not in before_values:
            raise LocalCliRuntimeError('control_move_resize_exact requires readable Width and Height shape properties')

        selection_proof = self._bundle_select_control_exact(hwp, target_ctrl, before_item)
        selection_warnings: list[str] = []
        if not selection_proof.get('selection_succeeded'):
            selection_warnings.append('Native exact control selection was unavailable; fell back to pre-existing inventory target proof and direct Ctrl.Properties mutation.')
        elif selection_proof.get('proof_strength') != 'exact-ctrl-inst-id':
            selection_warnings.append('CtrlInstID exact selection was unavailable or unverified; fallback selected-control proof was used before direct Ctrl.Properties mutation.')

        try:
            prop = getattr(target_ctrl, 'Properties')
            operations: list[dict[str, Any]] = []
            if scale_factor is not None:
                old_width = float(before_values['Width'])
                old_height = float(before_values['Height'])
                new_width = max(1, int(round(old_width * scale_factor)))
                new_height = max(1, int(round(old_height * scale_factor)))
                self._shape_prop_set_item(prop, 'Width', new_width)
                self._shape_prop_set_item(prop, 'Height', new_height)
                operations.append({'op': 'scale', 'scale_percent': float(scale_percent), 'old_width': old_width, 'old_height': old_height, 'new_width': new_width, 'new_height': new_height})
            if move_dx_mm != 0.0:
                old = self._shape_prop_item(prop, 'HorzOffset')
                if old is None:
                    raise LocalCliRuntimeError('control_move_resize_exact cannot move dx: HorzOffset is unavailable')
                delta = self._mm_to_hwp_unit(hwp, move_dx_mm)
                self._shape_prop_set_item(prop, 'HorzOffset', int(round(float(old))) + delta)
                operations.append({'op': 'move-dx', 'move_dx_mm': move_dx_mm, 'delta_hwpunit': delta, 'old': old, 'new': int(round(float(old))) + delta})
            if move_dy_mm != 0.0:
                old = self._shape_prop_item(prop, 'VertOffset')
                if old is None:
                    raise LocalCliRuntimeError('control_move_resize_exact cannot move dy: VertOffset is unavailable')
                delta = self._mm_to_hwp_unit(hwp, move_dy_mm)
                self._shape_prop_set_item(prop, 'VertOffset', int(round(float(old))) + delta)
                operations.append({'op': 'move-dy', 'move_dy_mm': move_dy_mm, 'delta_hwpunit': delta, 'old': old, 'new': int(round(float(old))) + delta})
            target_ctrl.Properties = prop
        except LocalCliRuntimeError:
            raise
        except Exception as exc:
            raise LocalCliRuntimeError(f'control_move_resize_exact property mutation failed: {type(exc).__name__}: {exc}') from exc

        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_shape = self._shape_prop_snapshot(target_ctrl)
        changed_values = {
            key: {'before': before_values.get(key), 'after': (after_shape.get('values') or {}).get(key)}
            for key in sorted(set(before_values) | set((after_shape.get('values') or {}).keys()))
            if before_values.get(key) != (after_shape.get('values') or {}).get(key)
        }

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(
                f'control_move_resize_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}'
            )
        post_target_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_target_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_target_items) != 1:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {target_id!r}'
            )
        if not changed_values:
            raise LocalCliRuntimeError('control_move_resize_exact did not observe any changed shape property after mutation')

        return {
            'schema_version': 'local-cli/control-move-resize-exact/v1',
            'read_only': False,
            'mutation': 'move-resize-control',
            'succeeded': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': section_anchor,
                'page_from': page_from,
                'page_to': page_to,
                'around': around,
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'operation_request': {
                'scale_percent': scale_percent,
                'move_dx_mm': move_dx_mm,
                'move_dy_mm': move_dy_mm,
            },
            'selection_proof': selection_proof,
            'selection_method_used': selection_proof.get('method_used'),
            'operations': operations,
            'before': before_snapshot,
            'after': after_snapshot,
            'shape_before': before_shape,
            'shape_after': after_shape,
            'changed_values': changed_values,
            'post_mutation': {
                'control_count_before': len(controls),
                'control_count_after': len(post_controls),
                'post_target_count': len(post_target_items),
            },
            'warnings': [
                *selection_warnings,
                'This primitive mutates one control layout only; rendered before/after proof is required before accepting the working copy.',
            ],
        }

    def _bundle_paragraph_rehome_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        delete_match = self._bundle_require_text(step, 'delete_match', max_chars=1000)
        insert_before_match = self._bundle_require_text(step, 'insert_before_match', max_chars=1000)
        insert_text = self._bundle_require_text(step, 'insert_text', max_chars=1000)
        delete_expected_page = int(step.get('delete_expected_page') or 0)
        insert_expected_page = int(step.get('insert_expected_page') or 0)
        if delete_expected_page <= 0 or insert_expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_rehome_exact requires positive expected pages')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_rehome_exact requires confirm_layout=true')
        if _remove_visible_spaces(delete_match) != _remove_visible_spaces(insert_text):
            raise LocalCliRuntimeError('paragraph_rehome_exact requires delete_match and insert_text to preserve visible text')

        before_text = ''
        try:
            if hasattr(hwp, 'get_text_file'):
                before_text = str(hwp.get_text_file('UNICODE', '') or '')
            elif hasattr(hwp, 'GetTextFile'):
                before_text = str(hwp.GetTextFile('UNICODE', '') or '')
        except Exception:
            before_text = ''
        before_visible_no_space = _remove_visible_spaces(before_text)
        before_delete_count = before_text.count(delete_match)
        before_insert_anchor_count = before_text.count(insert_before_match)

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            insert_target = self._find_text_on_expected_page(hwp, match=insert_before_match, expected_page=insert_expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_rehome_exact expected active selection for insert target')
            _, slist, spara, spos, _elist, _epara, _epos = selected
            _set_pos(hwp, int(slist), int(spara), int(spos))
            insert_context_before = _capture_nearby_text_context(hwp)
            insert_text_at_caret(hwp, insert_text)
            insert_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            raw_line_break = None
            if callable(insert_run):
                raw_line_break = insert_run('BreakLine')
            else:
                raise LocalCliRuntimeError('paragraph_rehome_exact requires HAction.Run BreakLine after insertion')

            delete_target = self._find_text_on_expected_page(hwp, match=delete_match, expected_page=delete_expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_rehome_exact expected active selection for delete target')
            selected_text = _get_selected_text(hwp, keep_select=True)
            if _remove_visible_spaces(selected_text) != _remove_visible_spaces(delete_match):
                raise LocalCliRuntimeError(f'paragraph_rehome_exact selected delete text mismatch: {selected_text!r}')
            _, _dslist, _dspara, _dspos, del_elist, del_epara, del_epos = selected
            _set_pos(hwp, int(del_elist), int(del_epara), int(del_epos))
            delete_context_before = _capture_nearby_text_context(hwp)
            delete_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(delete_run):
                raise LocalCliRuntimeError('paragraph_rehome_exact requires HAction.Run DeleteBack for exact text deletion')
            raw_delete_results = []
            for _i in range(len(delete_match)):
                raw_delete_results.append(delete_run('DeleteBack'))
            raw_delete = all(bool(item) if item is not None else True for item in raw_delete_results)

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
                    undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                    if callable(undo):
                        undo('Undo')
                        undo('Undo')
                except Exception:
                    pass
                diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
                before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
                after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
                raise LocalCliRuntimeError(
                    'paragraph_rehome_exact changed visible text; undo attempted and mutation refused; '
                    f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                    f'before_frag={before_frag!r} after_frag={after_frag!r}'
                )
            after_delete_count = after_text.count(delete_match)
            after_insert_anchor_count = after_text.count(insert_before_match)
            return {
                'schema_version': 'local-cli/paragraph-rehome-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-rehome',
                'delete_match_preview': _preview_text(delete_match, limit=80),
                'insert_before_match_preview': _preview_text(insert_before_match, limit=80),
                'delete_expected_page': delete_expected_page,
                'insert_expected_page': insert_expected_page,
                'insert_target': insert_target,
                'delete_target': delete_target,
                'insert_context_before': insert_context_before,
                'delete_context_before': delete_context_before,
                'raw_line_break_result': bool(raw_line_break) if raw_line_break is not None else None,
                'raw_delete_result': bool(raw_delete) if raw_delete is not None else None,
                'raw_delete_count': len(delete_match),
                'text_counts': {
                    'delete_match_before': before_delete_count,
                    'delete_match_after': after_delete_count,
                    'insert_before_match_before': before_insert_anchor_count,
                    'insert_before_match_after': after_insert_anchor_count,
                },
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'warnings': ['Rendered proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_control_join_previous_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        delete_back_count = int(step.get('delete_back_count') or 1)
        max_page_after = int(step.get('max_page_after') or max(1, expected_page - 1))
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('control_join_previous_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_join_previous_exact requires positive expected_page')
        if not (1 <= delete_back_count <= 5):
            raise LocalCliRuntimeError('control_join_previous_exact delete_back_count must be 1..5')

        before_text = ''
        try:
            if hasattr(hwp, 'get_text_file'):
                before_text = str(hwp.get_text_file('UNICODE', '') or '')
            elif hasattr(hwp, 'GetTextFile'):
                before_text = str(hwp.GetTextFile('UNICODE', '') or '')
        except Exception:
            before_text = ''
        before_visible_no_space = _remove_visible_spaces(before_text)

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'control_join_previous_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        _target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(f"control_join_previous_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}")
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_join_previous_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'control_join_previous_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'control_join_previous_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')
        if target_anchor_pos is None:
            raise LocalCliRuntimeError(f'control_join_previous_exact cannot prove anchor position for {target_id!r}')

        before_snapshot = self._bundle_compact_snapshot(hwp)
        _set_pos(hwp, int(target_anchor_pos[0]), int(target_anchor_pos[1]), int(target_anchor_pos[2]))
        context_before = _capture_nearby_text_context(hwp)
        run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        if not callable(run):
            raise LocalCliRuntimeError('control_join_previous_exact requires HAction.Run')
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
            try:
                for _i in range(delete_back_count):
                    run('Undo')
            except Exception:
                pass
            diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
            before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
            after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
            raise LocalCliRuntimeError(
                'control_join_previous_exact changed visible text; undo attempted and mutation refused; '
                f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                f'before_frag={before_frag!r} after_frag={after_frag!r}'
            )

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(f'control_join_previous_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}')
        post_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_items) != 1:
            raise LocalCliRuntimeError(f'control_join_previous_exact post-mutation target count must be exactly 1, got {len(post_items)} for {target_id!r}')
        after_item = post_items[0]
        after_page = after_item.get('page')
        if after_page is None or int(after_page) > max_page_after:
            raise LocalCliRuntimeError(f'control_join_previous_exact target page after mutation exceeds max_page_after={max_page_after}: {after_page}')
        after_snapshot = self._bundle_compact_snapshot(hwp)
        return {
            'schema_version': 'local-cli/control-join-previous-exact/v1',
            'read_only': False,
            'mutation': 'control-join-previous',
            'enumeration_mode': enumeration_mode,
            'target_id': target_id,
            'expected_hash': expected_hash,
            'expected_page': expected_page,
            'delete_back_count': delete_back_count,
            'max_page_after': max_page_after,
            'target_anchor_pos': list(target_anchor_pos),
            'target_before': before_item,
            'target_after': after_item,
            'context_before': context_before,
            'before': before_snapshot,
            'after': after_snapshot,
            'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
            'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
            'raw_results': [bool(item) if item is not None else None for item in raw_results],
            'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
        }

    def _bundle_control_delete_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_remove')):
            raise LocalCliRuntimeError('control_delete_exact requires confirm_remove=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_delete_exact requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_delete_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_delete_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        proof_items: list[dict[str, Any]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                proof_items.append(item)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        target_id_matches = [item for _ctrl, item, _anchor_pos in matching]
        if len(matching) != 1:
            raise LocalCliRuntimeError(
                f'control_delete_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}'
            )
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"control_delete_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_delete_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(
                f'control_delete_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}'
            )
        if page_from is not None:
            if not (int(page_from) <= int(actual_page) <= int(page_to)):
                raise LocalCliRuntimeError(
                    f'control_delete_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}'
                )

        before_snapshot = self._bundle_compact_snapshot(hwp)
        if target_anchor_pos is not None:
            _set_pos(hwp, target_anchor_pos[0], target_anchor_pos[1], target_anchor_pos[2])
        delete_succeeded = _delete_ctrl(hwp, target_ctrl)
        after_snapshot = self._bundle_compact_snapshot(hwp)

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        remaining_same_hash: list[dict[str, Any]] = []
        remaining_same_id: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('proof_hash') == expected_hash:
                    remaining_same_hash.append(item)
                if item.get('target_id') == target_id:
                    remaining_same_id.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if remaining_same_hash:
            raise LocalCliRuntimeError(
                f'control_delete_exact proof_hash still present after delete; remaining={remaining_same_hash}'
            )
        if len(post_controls) != len(controls) - 1:
            raise LocalCliRuntimeError(
                f'control_delete_exact control count did not decrease by one: before={len(controls)}, after={len(post_controls)}'
            )

        return {
            'schema_version': 'local-cli/control-delete-exact/v1',
            'read_only': False,
            'mutation': 'delete-control',
            'succeeded': bool(delete_succeeded),
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': section_anchor,
                'page_from': page_from,
                'page_to': page_to,
                'around': around,
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'target_id_match_count': len(target_id_matches),
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'before': before_snapshot,
            'after': after_snapshot,
            'post_delete': {
                'control_count_before': len(controls),
                'control_count_after': len(post_controls),
                'remaining_same_hash_count': len(remaining_same_hash),
                'remaining_same_id_count': len(remaining_same_id),
                'remaining_same_id': remaining_same_id[:5],
            },
            'warnings': [
                'This primitive is destructive; safety relies on disposable/working-copy use and rendered before/after proof.',
            ],
        }
