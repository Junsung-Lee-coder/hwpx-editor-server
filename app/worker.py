from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import get_settings
from app.edit_ops import (
    _apply_cursor_snapshot,
    _apply_selected_text_proof,
    _capture_nearby_text_context,
    _capture_selected_text_snapshot,
    _inventory_controls_at_cell,
    _move_doc_begin,
    _move_to_field,
    _run_table_cell_action,
    _snapshot_cursor_context,
    _verify_table_snapshot,
    apply_edit_operations,
    load_instruction_payload_from_file,
)
from app.logging_utils import configure_logger
from app.native_actions import get_native_capabilities
from app.observation import ensure_viewer_session, observe_job
from app.queue_db import QueueDB
from app.readiness import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    build_current_run_not_ready_snapshot,
    build_runtime_readiness_snapshot,
    current_worker_identity,
    new_readiness_run_id,
    ReadinessOwnershipError,
    readiness_matches_current_worker,
    resolve_candidate_generation,
    touch_runtime_readiness_heartbeat,
    write_runtime_readiness_snapshot,
    load_runtime_readiness_snapshot,
)
from app.runtime_state import (
    _load_json_artifact,
    _normalize_workflow_mode,
    _resolve_runtime_lane,
    _resolve_workflow_mode,
    _workflow_mode_from_runtime_lane,
    _workflow_mode_to_runtime_lane,
    format_last_phase,
    read_runtime_status,
    update_runtime_status,
    utc_now_iso,
    write_failure_artifacts,
    write_failure_snapshot_artifacts,
    write_json_artifact,
)
from app.step_evidence import (
    _action_evidence_index_path,
    _append_step_journal_steps,
    _append_step_journal_terminal_event,
    _build_action_tracker_state,
    _build_gui_edit_scaffolding,
    _collect_verification_modes,
    _initialize_step_journal,
    _normalize_step_verification_mode,
    _read_gui_edit_scaffolding,
    _record_apply_step,
    _summarize_action_counts,
)

settings = get_settings()
db = QueueDB(settings.db_path)
logger = configure_logger('hwp.worker', settings.log_level, settings.logs_root / 'worker.log')

ensure_viewer_session()


def _parse_cell_addr(addr: str | None) -> tuple[int, int] | None:
    if not isinstance(addr, str):
        return None
    match = re.fullmatch(r'([A-Z]+)(\d+)', addr.strip().upper())
    if not match:
        return None
    col_letters, row_digits = match.groups()
    col = 0
    for char in col_letters:
        col = col * 26 + (ord(char) - 64)
    return int(row_digits), col


def _recover_expected_cell(
    hwp: Any,
    *,
    expected_cell_addr: str,
    max_steps: int = 16,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    target = _parse_cell_addr(expected_cell_addr)
    snapshot = _snapshot_cursor_context(hwp)
    if target is None:
        return snapshot, steps

    for _ in range(max_steps):
        actual_addr = snapshot.get('cell_addr')
        current = _parse_cell_addr(actual_addr)
        if snapshot.get('is_cell') is not True or current is None or current == target:
            break
        current_row, current_col = current
        target_row, target_col = target
        if current_row > target_row:
            action = 'up'
        elif current_row < target_row:
            action = 'down'
        elif current_col > target_col:
            action = 'left'
        else:
            action = 'right'
        before = dict(snapshot)
        _run_table_cell_action(hwp, action)
        snapshot = _snapshot_cursor_context(hwp)
        steps.append(
            {
                'action': action,
                'before': before,
                'after': snapshot,
            }
        )
        if snapshot.get('cell_addr') == expected_cell_addr:
            break
    return snapshot, steps


def _pick_tracking_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value not in (None, ''):
        return value
    metadata = payload.get('metadata')
    if isinstance(metadata, dict):
        nested = metadata.get(key)
        if nested not in (None, ''):
            return nested
    return None


TRACKING_KEYS = (
    'request_id',
    'intent_id',
    'idempotency_key',
    'template_version',
    'inspect_snapshot_id',
    'resolved_via',
    'resolved_target_id',
    'execution_run_id',
    'validation_run_id',
)

SECTION_XML_RE = re.compile(r'^Contents/section\d+\.xml$')
PARAGRAPH_RANGE_RE = re.compile(r'^(?P<start>Contents/section\d+\.xml#p\d+)\s*(?:\.\.|~|->)\s*(?P<end>Contents/section\d+\.xml#p\d+)$')
HWPX_META_NS = {'opf': 'http://www.idpf.org/2007/opf/'}
CONTENT_HPF_VOLATILE_META_NAMES = {'CreatedDate', 'ModifiedDate', 'date'}
SECTION_VOLATILE_ATTRS = {
    ('sz', 'height'),
    ('curSz', 'height'),
    ('curSz', 'width'),
    ('scaMatrix', 'e1'),
    ('scaMatrix', 'e5'),
    ('rotationInfo', 'centerX'),
    ('rotationInfo', 'centerY'),
}


def build_fixture_instruction_payload(instruction_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in instruction_payload.items()
        if key not in TRACKING_KEYS and value is not None
    }


def write_native_capabilities_artifact(metadata_dir: Path, hwp: Any) -> dict[str, Any]:
    payload = get_native_capabilities(hwp)
    write_json_artifact(metadata_dir / 'native_capabilities.json', payload)
    return payload


def xml_local_name(tag: str) -> str:
    return tag.split('}', 1)[1] if '}' in tag else tag


def normalize_content_hpf_bytes(data: bytes) -> bytes:
    try:
        root = ET.fromstring(data)
    except Exception:
        return data

    for meta in root.findall('.//opf:meta', HWPX_META_NS):
        if meta.attrib.get('name') in CONTENT_HPF_VOLATILE_META_NAMES:
            meta.text = '__NORMALIZED__'
    return ET.tostring(root, encoding='utf-8')


def normalize_section_xml_bytes(data: bytes) -> bytes:
    try:
        root = ET.fromstring(data)
    except Exception:
        return data

    for parent in list(root.iter()):
        for child in list(parent):
            if xml_local_name(child.tag) == 'linesegarray':
                parent.remove(child)

    for elem in root.iter():
        elem_name = xml_local_name(elem.tag)
        for attr_name in list(elem.attrib):
            if (elem_name, attr_name) in SECTION_VOLATILE_ATTRS:
                elem.attrib[attr_name] = '__NORMALIZED__'

    return ET.tostring(root, encoding='utf-8')


def build_hwpx_semantic_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None

    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            raw = archive.read(name)
            if name == 'Contents/content.hpf':
                normalized = normalize_content_hpf_bytes(raw)
            elif SECTION_XML_RE.match(name):
                normalized = normalize_section_xml_bytes(raw)
            else:
                normalized = raw

            digest.update(name.encode('utf-8'))
            digest.update(b'\0')
            digest.update(hashlib.sha256(normalized).digest())
            digest.update(b'\0')

    return digest.hexdigest()


def _parse_hwpx_paragraph_id(paragraph_id: str) -> tuple[str, int]:
    matched = re.fullmatch(r'(Contents/section\d+\.xml)#p(\d+)', paragraph_id)
    if not matched:
        raise RuntimeError(f'Unsupported HWPX paragraph id: {paragraph_id!r}')
    return matched.group(1), int(matched.group(2))


def _parse_hwpx_paragraph_range(paragraph_range: str) -> tuple[str, int, int]:
    matched = PARAGRAPH_RANGE_RE.fullmatch(str(paragraph_range or '').strip())
    if not matched:
        raise RuntimeError(f'Unsupported HWPX paragraph range: {paragraph_range!r}')
    start_section, start_index = _parse_hwpx_paragraph_id(matched.group('start'))
    end_section, end_index = _parse_hwpx_paragraph_id(matched.group('end'))
    if start_section != end_section:
        raise RuntimeError(f'HWPX paragraph range must stay within one section: {paragraph_range!r}')
    if end_index < start_index:
        raise RuntimeError(f'HWPX paragraph range end precedes start: {paragraph_range!r}')
    return start_section, start_index, end_index


def _normalize_visible_text(value: str | None) -> str:
    return ' '.join(str(value or '').split()).strip()


def _normalize_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, str) else value
    if not isinstance(raw_values, list):
        raise RuntimeError(f'{field_name} must be a string or list of strings when provided')
    normalized: list[str] = []
    for item in raw_values:
        if not isinstance(item, str):
            raise RuntimeError(f'{field_name} entries must be strings')
        token = item.strip()
        if not token:
            raise RuntimeError(f'{field_name} entries must be non-empty strings')
        normalized.append(token)
    return normalized


def _extract_serialized_paragraph_range_snapshot(hwpx_path: Path, paragraph_range: str) -> dict[str, Any]:
    section_name, start_index, end_index = _parse_hwpx_paragraph_range(paragraph_range)
    with zipfile.ZipFile(hwpx_path) as archive:
        try:
            raw = archive.read(section_name)
        except KeyError as exc:
            raise RuntimeError(f'Section not found in serialized HWPX: {section_name!r}') from exc

    root = ET.fromstring(raw)
    paragraphs = [elem for elem in root.iter() if xml_local_name(elem.tag) == 'p']
    if start_index < 1 or end_index > len(paragraphs):
        raise RuntimeError(
            f'Paragraph range {paragraph_range!r} is out of bounds for serialized HWPX section with {len(paragraphs)} paragraphs'
        )

    entries: list[dict[str, Any]] = []
    aggregate_field_begin = 0
    aggregate_field_end = 0
    aggregate_ctrl = 0
    for para_index in range(start_index, end_index + 1):
        para = paragraphs[para_index - 1]
        text = _normalize_visible_text(''.join(para.itertext()))
        field_begin_count = sum(1 for elem in para.iter() if xml_local_name(elem.tag) == 'fieldBegin')
        field_end_count = sum(1 for elem in para.iter() if xml_local_name(elem.tag) == 'fieldEnd')
        ctrl_count = sum(1 for elem in para.iter() if xml_local_name(elem.tag) == 'ctrl')
        aggregate_field_begin += field_begin_count
        aggregate_field_end += field_end_count
        aggregate_ctrl += ctrl_count
        entries.append(
            {
                'paragraph_id': f'{section_name}#p{para_index}',
                'text': text,
                'field_begin_count': field_begin_count,
                'field_end_count': field_end_count,
                'ctrl_count': ctrl_count,
            }
        )

    combined_text = _normalize_visible_text(' '.join(entry['text'] for entry in entries))
    return {
        'hwpx_path': str(hwpx_path),
        'paragraph_range': paragraph_range,
        'section_name': section_name,
        'paragraph_count': len(entries),
        'paragraphs': entries,
        'combined_text': combined_text,
        'aggregate_field_begin_count': aggregate_field_begin,
        'aggregate_field_end_count': aggregate_field_end,
        'aggregate_ctrl_count': aggregate_ctrl,
    }


def _run_live_reopened_cell_proof(
    hwp: Any,
    *,
    instruction_payload: dict[str, Any] | None,
    expected_cell_addr: str,
    expected_present_any: list[str],
    expected_absent_any: list[str],
    expected_absent_ctrl_ids: list[str],
    expected_selection_mode: int,
    expected_is_cell: bool,
) -> dict[str, Any]:
    """Reopen the edited document and prove we can still land on the intended cell.

    This is intentionally defensive: reopen/save cycles can shift cursor coordinates or
    retarget shared A1-style tables, so the proof prefers identity-locked snapshots and
    only falls back to field moves when the stronger evidence is unavailable.
    """
    operations = instruction_payload.get('operations') if isinstance(instruction_payload, dict) else None

    def _normalize_proof_identity_text(value: Any) -> str:
        return ' '.join(str(value or '').split()).strip()

    def _build_single_cell_post_edit_table_fingerprint(replace_text: Any) -> dict[str, Any] | None:
        normalized_header = _normalize_proof_identity_text(replace_text)
        if not normalized_header:
            return None
        payload = {
            'row_count': 1,
            'col_count': 1,
            'header_row': [normalized_header],
            'first_col_keys_sample': [],
        }
        payload['fingerprint'] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()[:16]
        return payload

    def _resolve_identity_locked_cursor_snapshot() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not isinstance(operations, list):
            return None, None

        def _preview_text(value: Any, *, limit: int = 120) -> str | None:
            text = ' '.join(str(value or '').split()).strip()
            if not text:
                return None
            return text if len(text) <= limit else text[:limit].rstrip() + '...'

        def _relation_keys(snapshot_op: dict[str, Any]) -> dict[str, str]:
            return {
                key: str(snapshot_op.get(key) or '').strip()
                for key in ('candidate_identity_key', 'probe_identity_key', 'action_identity_key', 'resolved_target_id', 'section_key')
                if str(snapshot_op.get(key) or '').strip()
            }

        def _matching_related_operations(snapshot_op: dict[str, Any]) -> list[dict[str, Any]]:
            relation_keys = _relation_keys(snapshot_op)
            related_items: list[dict[str, Any]] = []
            for item in operations:
                if not isinstance(item, dict):
                    continue
                if relation_keys and not any(str(item.get(key) or '').strip() == value for key, value in relation_keys.items()):
                    continue
                related_items.append(item)
            if not related_items:
                related_items = [snapshot_op]
            return related_items

        def _build_post_edit_identity_locked_snapshot(snapshot_op: dict[str, Any]) -> dict[str, Any]:
            patched = dict(snapshot_op)
            expected_table_fingerprint = patched.get('expected_table_fingerprint')
            if not isinstance(expected_table_fingerprint, dict):
                return patched
            if int(expected_table_fingerprint.get('row_count', 0) or 0) != 1 or int(expected_table_fingerprint.get('col_count', 0) or 0) != 1:
                return patched
            if str(patched.get('expected_cell_addr') or '').strip() != 'A1':
                return patched

            related_write = next(
                (
                    item for item in _matching_related_operations(snapshot_op)
                    if str(item.get('op') or '') == 'table_cell_replace_text'
                    and str(item.get('replace') or '').strip()
                ),
                None,
            )
            if not isinstance(related_write, dict):
                return patched

            post_edit_fingerprint = _build_single_cell_post_edit_table_fingerprint(related_write.get('replace'))
            if not isinstance(post_edit_fingerprint, dict):
                return patched

            replace_text = str(related_write.get('replace') or '')
            post_edit_find = next(
                (token for token in expected_present_any if token and token in replace_text),
                None,
            )
            if not post_edit_find:
                post_edit_lines = [line.strip() for line in replace_text.splitlines() if line.strip()]
                post_edit_find = post_edit_lines[0] if post_edit_lines else None
            if not post_edit_find:
                return patched

            # Reopened live proof must not reuse the pre-save cursor coordinates for shared A1
            # single-cell tables. Those coordinates can drift after serialization and land on the
            # wrong A1 carrier (for example the title table). Force the proof hop to use the
            # post-edit text token + fingerprint lock instead.
            patched.pop('cursor_pos', None)
            patched['find'] = post_edit_find
            patched['expected_table_fingerprint'] = post_edit_fingerprint
            patched['proof_identity_lock'] = 'post_edit_single_cell_replace_text'
            patched['proof_identity_source'] = {
                'section_key': related_write.get('section_key') or patched.get('section_key'),
                'resolved_target_id': related_write.get('resolved_target_id') or patched.get('resolved_target_id'),
                'candidate_identity_key': related_write.get('candidate_identity_key') or patched.get('candidate_identity_key'),
                'probe_identity_key': related_write.get('probe_identity_key') or patched.get('probe_identity_key'),
                'action_identity_key': related_write.get('action_identity_key') or patched.get('action_identity_key'),
            }
            return patched

        def _summarize_candidate(
            snapshot_op: dict[str, Any],
            *,
            score: int,
            candidate_index: int,
            related_items: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            patched = _build_post_edit_identity_locked_snapshot(snapshot_op)
            related_ops: list[dict[str, Any]] = []
            for item in related_items[:6]:
                if not isinstance(item, dict):
                    continue
                related_ops.append(
                    {
                        'step_id': item.get('step_id'),
                        'op': item.get('op'),
                        'find_preview': _preview_text(item.get('find')),
                        'replace_preview': _preview_text(item.get('replace')),
                    }
                )
            summary = {
                'candidate_index': candidate_index,
                'score': score,
                'section_key': patched.get('section_key'),
                'resolved_target_id': patched.get('resolved_target_id'),
                'candidate_identity_key': patched.get('candidate_identity_key'),
                'probe_identity_key': patched.get('probe_identity_key'),
                'action_identity_key': patched.get('action_identity_key'),
                'expected_cell_addr': patched.get('expected_cell_addr'),
                'find_preview': _preview_text(patched.get('find')),
                'proof_identity_lock': patched.get('proof_identity_lock'),
                'proof_identity_source': patched.get('proof_identity_source'),
                'related_operation_count': len(related_items),
                'related_ops': related_ops,
            }
            return patched, summary

        candidates: list[dict[str, Any]] = []
        for candidate in operations:
            if not isinstance(candidate, dict):
                continue
            if candidate.get('op') != 'cursor_snapshot':
                continue
            if str(candidate.get('expected_cell_addr') or '').strip() != expected_cell_addr:
                continue
            if not isinstance(candidate.get('expected_table_fingerprint'), dict):
                continue
            candidates.append(dict(candidate))

        if len(candidates) == 1:
            related_items = _matching_related_operations(candidates[0])
            patched, summary = _summarize_candidate(candidates[0], score=1, candidate_index=0, related_items=related_items)
            return patched, {
                'selection_mode': 'single_candidate',
                'candidate_count': 1,
                'selected_candidate_index': 0,
                'selection_reason': 'only_matching_cursor_snapshot_candidate',
                'candidates': [summary],
            }
        if not candidates:
            return None, None

        def _related_operations_text(snapshot_op: dict[str, Any]) -> str:
            related_items = _matching_related_operations(snapshot_op)
            return json.dumps(related_items, ensure_ascii=False)

        # Capture every plausible candidate before narrowing so the caller can review the full
        # choice set, understand why one candidate won, and request a GUI confirmation step when
        # text-only evidence is not persuasive enough.
        scored: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for index, candidate in enumerate(candidates):
            related_items = _matching_related_operations(candidate)
            haystack = json.dumps(related_items, ensure_ascii=False)
            score = 0
            for token in expected_present_any:
                needle = str(token or '').strip()
                if needle and needle in haystack:
                    score += 100
            for token in expected_absent_any:
                needle = str(token or '').strip()
                if needle and needle not in haystack:
                    score += 1
            patched, summary = _summarize_candidate(
                candidate,
                score=score,
                candidate_index=index,
                related_items=related_items,
            )
            scored.append((score, index, patched, summary))

        scored.sort(key=lambda item: (-item[0], item[1]))
        candidate_review: dict[str, Any] = {
            'selection_mode': 'multiple_candidates',
            'candidate_count': len(scored),
            'candidates': [item[3] for item in scored],
        }
        best_score, best_index, best_candidate, best_summary = scored[0]
        if best_score <= 0:
            candidate_review.update(
                {
                    'selected_candidate_index': None,
                    'selection_reason': 'no_candidate_reached_positive_text_score',
                }
            )
            return None, candidate_review
        second_score = scored[1][0] if len(scored) > 1 else None
        if second_score is not None and second_score == best_score:
            tied_indexes = [item[1] for item in scored if item[0] == best_score]
            candidate_review.update(
                {
                    'selected_candidate_index': None,
                    'selection_reason': 'top_score_tie_requires_higher_level_confirmation',
                    'best_score': best_score,
                    'tied_candidate_indexes': tied_indexes,
                }
            )
            return None, candidate_review
        candidate_review.update(
            {
                'selected_candidate_index': best_index,
                'selected_candidate_identity_key': best_summary.get('candidate_identity_key'),
                'selection_reason': 'highest_related_text_score',
                'best_score': best_score,
                'second_score': second_score,
            }
        )
        return best_candidate, candidate_review

    # Rebind to the intended cell using the strongest surviving identity evidence first,
    # then fall back to the older field-navigation path when the instruction payload does
    # not provide a richer cursor snapshot.
    fill_addr_field = getattr(hwp, 'fill_addr_field', None)
    fill_addr_field_error = None
    if callable(fill_addr_field):
        try:
            fill_addr_field()
        except Exception as exc:
            fill_addr_field_error = str(exc)
    rebound_via_cursor_snapshot = None
    identity_locked_snapshot_op, identity_locked_candidate_review = _resolve_identity_locked_cursor_snapshot()
    if identity_locked_snapshot_op is not None:
        rebound_via_cursor_snapshot = _apply_cursor_snapshot(hwp, identity_locked_snapshot_op)
        rebound_snapshots = rebound_via_cursor_snapshot.get('snapshots') if isinstance(rebound_via_cursor_snapshot, dict) else None
        rebound_snapshot = rebound_snapshots[-1] if isinstance(rebound_snapshots, list) and rebound_snapshots else None
        if not isinstance(rebound_snapshot, dict) or rebound_snapshot.get('is_cell') is not True:
            raise RuntimeError(
                'post-serialization live proof identity lock failed to re-enter a table cell via cursor_snapshot; '
                f'expected={expected_cell_addr!r}, rebound={rebound_snapshot}, fill_addr_field_error={fill_addr_field_error!r}'
            )
    elif not _move_to_field(hwp, expected_cell_addr):
        _move_doc_begin(hwp)
        if callable(fill_addr_field):
            try:
                fill_addr_field()
            except Exception as exc:
                fill_addr_field_error = str(exc)
        if not _move_to_field(hwp, expected_cell_addr):
            snapshot_op = None
            if isinstance(operations, list):
                for candidate in operations:
                    if not isinstance(candidate, dict):
                        continue
                    if candidate.get('op') != 'cursor_snapshot':
                        continue
                    if str(candidate.get('expected_cell_addr') or '').strip() != expected_cell_addr:
                        continue
                    snapshot_op = dict(candidate)
                    break
            if snapshot_op is None:
                raise RuntimeError(
                    f'post-serialization live proof could not move_to_field({expected_cell_addr!r}); '
                    f'fill_addr_field_error={fill_addr_field_error!r}'
                )
            rebound_via_cursor_snapshot = _apply_cursor_snapshot(hwp, snapshot_op)
            rebound_snapshots = rebound_via_cursor_snapshot.get('snapshots') if isinstance(rebound_via_cursor_snapshot, dict) else None
            rebound_snapshot = rebound_snapshots[-1] if isinstance(rebound_snapshots, list) and rebound_snapshots else None
            if not isinstance(rebound_snapshot, dict) or rebound_snapshot.get('cell_addr') != expected_cell_addr:
                raise RuntimeError(
                    'post-serialization live proof could not recover the expected cell via cursor_snapshot fallback; '
                    f'expected={expected_cell_addr!r}, rebound={rebound_snapshot}, fill_addr_field_error={fill_addr_field_error!r}'
                )

    # Once we think we are back in the target cell, prove that the selection mechanics still
    # behave correctly before asserting on text/control absence.
    before_block = _snapshot_cursor_context(hwp)
    cell_recovery_steps: list[dict[str, Any]] = []
    if before_block.get('cell_addr') != expected_cell_addr and before_block.get('is_cell') is True:
        before_block, cell_recovery_steps = _recover_expected_cell(
            hwp,
            expected_cell_addr=expected_cell_addr,
        )
    if before_block.get('cell_addr') != expected_cell_addr:
        raise RuntimeError(
            'post-serialization live proof entered the wrong cell before block: '
            f"expected={expected_cell_addr!r}, actual={before_block.get('cell_addr')!r}, "
            f"snapshot={before_block}, cell_recovery_steps={cell_recovery_steps}"
        )

    _run_table_cell_action(hwp, 'block')
    selected = _snapshot_cursor_context(hwp)
    selected.update(_capture_selected_text_snapshot(hwp))
    if selected.get('cell_addr') != expected_cell_addr:
        raise RuntimeError(
            'post-serialization live proof selected the wrong cell after block: '
            f"expected={expected_cell_addr!r}, actual={selected.get('cell_addr')!r}, snapshot={selected}"
        )
    effective_has_selection = bool(selected.get('has_selection')) or bool(selected.get('selected_text'))
    if not effective_has_selection:
        raise RuntimeError(
            'post-serialization live proof produced an empty selection after reopen; '
            f'snapshot={selected}'
        )

    verification = _verify_table_snapshot(
        op_name='post_serialization_proof',
        action='reopened_live_cell_block',
        after=selected,
        expected_selection_mode=expected_selection_mode,
        expected_is_cell=expected_is_cell,
    )
    try:
        _apply_selected_text_proof(
            selected,
            expected_absent_any=expected_absent_any,
            expected_present_any=expected_present_any,
        )
    except Exception as exc:
        raise RuntimeError(f'post-serialization live proof text assertion failed: {exc}') from exc

    control_inventory = None
    if expected_absent_ctrl_ids:
        results: list[dict[str, Any]] = []
        for ctrl_id in expected_absent_ctrl_ids:
            inventory, _matching_ctrls = _inventory_controls_at_cell(
                hwp,
                expected_cell_addr=expected_cell_addr,
                target_ctrl_id=ctrl_id,
                anchor_option=1,
            )
            results.append(
                {
                    'target_ctrl_id': ctrl_id,
                    'enumeration_mode': inventory.get('enumeration_mode'),
                    'inventory_exists': inventory.get('inventory_exists'),
                    'controls_in_expected_cell_count': inventory.get('controls_in_expected_cell_count'),
                    'matching_target_count': inventory.get('matching_target_count'),
                    'matching_target_controls': inventory.get('matching_target_controls'),
                    'passed': inventory.get('matching_target_count', 0) == 0,
                }
            )
        control_inventory = {
            'expected_absent_ctrl_ids': list(expected_absent_ctrl_ids),
            'results': results,
            'passed': all(item.get('passed') for item in results),
        }
        if not control_inventory['passed']:
            raise RuntimeError(
                'post-serialization live proof control inventory assertion failed: '
                + json.dumps(control_inventory, ensure_ascii=False)
            )

    return {
        'fill_addr_field_error': fill_addr_field_error,
        'identity_locked_cursor_snapshot': None if identity_locked_snapshot_op is None else {
            'section_key': identity_locked_snapshot_op.get('section_key'),
            'resolved_target_id': identity_locked_snapshot_op.get('resolved_target_id'),
            'expected_cell_addr': identity_locked_snapshot_op.get('expected_cell_addr'),
            'find': identity_locked_snapshot_op.get('find'),
            'expected_table_fingerprint': identity_locked_snapshot_op.get('expected_table_fingerprint'),
            'proof_identity_lock': identity_locked_snapshot_op.get('proof_identity_lock'),
            'proof_identity_source': identity_locked_snapshot_op.get('proof_identity_source'),
        },
        'identity_locked_candidate_review': identity_locked_candidate_review,
        'rebound_via_cursor_snapshot': rebound_via_cursor_snapshot,
        'cell_recovery_steps': cell_recovery_steps,
        'effective_has_selection': effective_has_selection,
        'before_block': before_block,
        'selected': selected,
        'verification': verification,
        'control_inventory': control_inventory,
    }


def _collect_post_serialization_proof_specs(metadata: Any) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []

    proof_specs: list[dict[str, Any]] = []
    single_proof = metadata.get('post_serialization_proof')
    if isinstance(single_proof, dict):
        proof_specs.append(single_proof)

    multiple_proofs = metadata.get('post_serialization_proofs')
    if isinstance(multiple_proofs, list):
        proof_specs.extend(item for item in multiple_proofs if isinstance(item, dict))
    return proof_specs


# Keep worker-only deployments compatible with older live edit_ops.py builds that
# do not yet accept the optional step_recorder hook.
def _apply_edit_operations_with_optional_step_recorder(
    hwp: Any,
    operations: list[dict[str, Any]],
    *,
    step_recorder: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    try:
        parameters = inspect.signature(apply_edit_operations).parameters
    except (TypeError, ValueError):
        parameters = {}

    if 'step_recorder' in parameters:
        return apply_edit_operations(
            hwp,
            operations,
            step_recorder=step_recorder,
        )

    return apply_edit_operations(hwp, operations)



def _build_post_serialization_proof_artifact(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    hwpx_path: Path,
    proof_spec: dict[str, Any],
    hwp: Any | None = None,
) -> dict[str, Any]:
    paragraph_range = str(proof_spec.get('paragraph_range') or '').strip()
    if not paragraph_range:
        raise RuntimeError('metadata.post_serialization_proof.paragraph_range is required')

    expected_absent_any = _normalize_string_list(
        proof_spec.get('expected_absent_any'),
        field_name='metadata.post_serialization_proof.expected_absent_any',
    )
    expected_present_any = _normalize_string_list(
        proof_spec.get('expected_present_any'),
        field_name='metadata.post_serialization_proof.expected_present_any',
    )
    expected_cell_addr = str(proof_spec.get('expected_cell_addr') or '').strip() or None
    expected_absent_ctrl_ids = _normalize_string_list(
        proof_spec.get('expected_absent_ctrl_ids'),
        field_name='metadata.post_serialization_proof.expected_absent_ctrl_ids',
    )
    expected_selection_mode = proof_spec.get('expected_selection_mode', 3)
    if isinstance(expected_selection_mode, bool) or not isinstance(expected_selection_mode, int):
        raise RuntimeError('metadata.post_serialization_proof.expected_selection_mode must be an integer when provided')
    expected_is_cell = proof_spec.get('expected_is_cell', True)
    if not isinstance(expected_is_cell, bool):
        raise RuntimeError('metadata.post_serialization_proof.expected_is_cell must be a boolean when provided')
    max_field_begin_count = proof_spec.get('max_field_begin_count')
    if max_field_begin_count is not None and (isinstance(max_field_begin_count, bool) or not isinstance(max_field_begin_count, int)):
        raise RuntimeError('metadata.post_serialization_proof.max_field_begin_count must be an integer when provided')
    max_ctrl_count = proof_spec.get('max_ctrl_count')
    if max_ctrl_count is not None and (isinstance(max_ctrl_count, bool) or not isinstance(max_ctrl_count, int)):
        raise RuntimeError('metadata.post_serialization_proof.max_ctrl_count must be an integer when provided')

    snapshot = _extract_serialized_paragraph_range_snapshot(hwpx_path, paragraph_range)
    combined_text = str(snapshot.get('combined_text') or '')
    matched_absent_any = [term for term in expected_absent_any if term in combined_text]
    matched_present_any = [term for term in expected_present_any if term in combined_text]
    serialized_proof = {
        'matched_absent_any': matched_absent_any,
        'matched_present_any': matched_present_any,
        'absent_any_ok': not matched_absent_any,
        'present_any_ok': True if not expected_present_any else bool(matched_present_any),
        'field_begin_count_ok': True if max_field_begin_count is None else snapshot['aggregate_field_begin_count'] <= max_field_begin_count,
        'ctrl_count_ok': True if max_ctrl_count is None else snapshot['aggregate_ctrl_count'] <= max_ctrl_count,
    }
    serialized_proof['passed'] = bool(
        serialized_proof['absent_any_ok']
        and serialized_proof['present_any_ok']
        and serialized_proof['field_begin_count_ok']
        and serialized_proof['ctrl_count_ok']
    )

    live_reopened_proof = None
    if expected_cell_addr is not None:
        if hwp is None:
            raise RuntimeError('metadata.post_serialization_proof.expected_cell_addr requires a reopened live HWP handle')
        live_reopened_proof = _run_live_reopened_cell_proof(
            hwp,
            instruction_payload=instruction_payload,
            expected_cell_addr=expected_cell_addr,
            expected_present_any=expected_present_any,
            expected_absent_any=expected_absent_any,
            expected_absent_ctrl_ids=expected_absent_ctrl_ids,
            expected_selection_mode=expected_selection_mode,
            expected_is_cell=expected_is_cell,
        )

    artifact = {
        'schema_version': 'post-serialization-proof/v2',
        **build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir),
        'hwpx_path': str(hwpx_path),
        'proof_spec': {
            'paragraph_range': paragraph_range,
            'expected_cell_addr': expected_cell_addr,
            'expected_absent_any': expected_absent_any,
            'expected_present_any': expected_present_any,
            'expected_absent_ctrl_ids': expected_absent_ctrl_ids,
            'expected_selection_mode': expected_selection_mode,
            'expected_is_cell': expected_is_cell,
            'max_field_begin_count': max_field_begin_count,
            'max_ctrl_count': max_ctrl_count,
        },
        'serialized_snapshot': snapshot,
        'serialized_proof': serialized_proof,
        'reopened_live_proof': live_reopened_proof,
    }
    artifact['proof'] = {
        'serialized_passed': serialized_proof['passed'],
        'reopened_live_passed': None if live_reopened_proof is None else True,
        'passed': bool(serialized_proof['passed'] and (live_reopened_proof is not None if expected_cell_addr is not None else True)),
    }
    return artifact



def run_post_serialization_proof(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    hwpx_path: Path,
    hwp: Any | None = None,
) -> dict[str, Any] | None:
    metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else None
    proof_specs = _collect_post_serialization_proof_specs(metadata)
    if not proof_specs:
        return None

    proof_artifacts = [
        _build_post_serialization_proof_artifact(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            hwpx_path=hwpx_path,
            proof_spec=proof_spec,
            hwp=hwp,
        )
        for proof_spec in proof_specs
    ]
    artifact: dict[str, Any] | list[dict[str, Any]]
    if len(proof_artifacts) == 1:
        artifact = proof_artifacts[0]
    else:
        artifact = {
            'schema_version': 'post-serialization-proof-bundle/v1',
            **build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir),
            'proof_count': len(proof_artifacts),
            'proofs': proof_artifacts,
        }

    artifact_path = metadata_dir / 'post_serialization_proof.json'
    write_json_artifact(artifact_path, artifact)
    failed_proof = next((item for item in proof_artifacts if not bool(item.get('proof', {}).get('passed'))), None)
    if failed_proof is not None:
        proof_spec = failed_proof.get('proof_spec') if isinstance(failed_proof.get('proof_spec'), dict) else {}
        snapshot = failed_proof.get('serialized_snapshot') if isinstance(failed_proof.get('serialized_snapshot'), dict) else {}
        serialized = failed_proof.get('serialized_proof') if isinstance(failed_proof.get('serialized_proof'), dict) else {}
        raise RuntimeError(
            'Post-serialization proof failed: '
            + json.dumps(
                {
                    'paragraph_range': proof_spec.get('paragraph_range'),
                    'expected_cell_addr': proof_spec.get('expected_cell_addr'),
                    'matched_absent_any': serialized.get('matched_absent_any'),
                    'matched_present_any': serialized.get('matched_present_any'),
                    'aggregate_field_begin_count': snapshot.get('aggregate_field_begin_count'),
                    'aggregate_ctrl_count': snapshot.get('aggregate_ctrl_count'),
                    'reopened_live_present': failed_proof.get('reopened_live_proof') is not None,
                },
                ensure_ascii=False,
            )
        )
    return artifact if isinstance(artifact, dict) else None


def extract_pdf_text_bytes(path: Path) -> tuple[bytes | None, str | None]:
    try:
        from pypdf import PdfReader  # type: ignore

        text = ''.join(page.extract_text() or '' for page in PdfReader(str(path)).pages)
        return text.encode('utf-8'), 'pypdf'
    except Exception:
        pass

    try:
        from PyPDF2 import PdfReader  # type: ignore

        text = ''.join(page.extract_text() or '' for page in PdfReader(str(path)).pages)
        return text.encode('utf-8'), 'PyPDF2'
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ['pdftotext', str(path), '-'],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0:
            return proc.stdout, 'pdftotext'
    except Exception:
        pass

    return None, None


def build_pdf_text_sha256(path: Path) -> tuple[str | None, str | None]:
    if not path.exists() or not path.is_file():
        return None, None
    text_bytes, method = extract_pdf_text_bytes(path)
    if text_bytes is None or method is None:
        return None, None
    return hashlib.sha256(text_bytes).hexdigest(), method


def build_pdf_page_text_hashes(path: Path) -> tuple[list[str] | None, str | None]:
    if not path.exists() or not path.is_file():
        return None, None

    try:
        from pypdf import PdfReader  # type: ignore

        hashes = [
            hashlib.sha256((page.extract_text() or '').encode('utf-8')).hexdigest()
            for page in PdfReader(str(path)).pages
        ]
        return hashes, 'pypdf'
    except Exception:
        pass

    try:
        from PyPDF2 import PdfReader  # type: ignore

        hashes = [
            hashlib.sha256((page.extract_text() or '').encode('utf-8')).hexdigest()
            for page in PdfReader(str(path)).pages
        ]
        return hashes, 'PyPDF2'
    except Exception:
        pass

    return None, None


def build_render_diff_summary(
    *,
    source_pdf_path: Path,
    result_pdf_path: Path,
    source_page_count: int,
    result_page_count: int,
    regions_artifact_path: Path | None = None,
) -> dict[str, Any]:
    source_hashes, method = build_pdf_page_text_hashes(source_pdf_path)
    result_hashes, result_method = build_pdf_page_text_hashes(result_pdf_path)
    hash_method = method or result_method

    changed_pages: list[int] = []
    unchanged_pages: list[int] = []
    if source_hashes is not None and result_hashes is not None:
        overlap = min(len(source_hashes), len(result_hashes))
        for index in range(overlap):
            page_no = index + 1
            if source_hashes[index] == result_hashes[index]:
                unchanged_pages.append(page_no)
            else:
                changed_pages.append(page_no)
        if result_page_count > source_page_count:
            changed_pages.extend(range(source_page_count + 1, result_page_count + 1))
        elif source_page_count > result_page_count:
            changed_pages.extend(range(result_page_count + 1, source_page_count + 1))
    else:
        overlap = min(source_page_count, result_page_count)
        changed_pages = list(range(1, overlap + 1))
        if result_page_count != source_page_count:
            changed_pages.extend(range(overlap + 1, max(source_page_count, result_page_count) + 1))

    viewer_pages: list[dict[str, Any]] = []
    max_pages = max(source_page_count, result_page_count)
    changed_page_set = set(changed_pages)
    for page_no in range(1, max_pages + 1):
        changed = page_no in changed_page_set
        viewer_pages.append(
            {
                'page_number': page_no,
                'source_page_number': page_no if page_no <= source_page_count else None,
                'result_page_number': page_no if page_no <= result_page_count else None,
                'changed': changed,
                'regions': [
                    {
                        'kind': 'page_box',
                        'label': 'changed_page',
                        'x': 0.0,
                        'y': 0.0,
                        'width': 1.0,
                        'height': 1.0,
                    }
                ] if changed else [],
            }
        )

    return {
        'source_pdf_path': str(source_pdf_path),
        'result_pdf_path': str(result_pdf_path),
        'source_page_count': source_page_count,
        'result_page_count': result_page_count,
        'compared_page_count': min(source_page_count, result_page_count),
        'changed_pages': sorted(dict.fromkeys(changed_pages)),
        'unchanged_pages': unchanged_pages,
        'text_hash_method': hash_method,
        'changed_region_strategy': 'full_page_box_for_changed_pages',
        'changed_region_artifact_status': 'page_box_ready' if regions_artifact_path is not None else 'inline_only',
        'changed_regions_artifact_path': str(regions_artifact_path) if regions_artifact_path is not None else None,
        'viewer_pages': viewer_pages,
    }


def build_render_diff_regions_artifact(*, render_diff: dict[str, Any], artifact_path: Path) -> dict[str, Any]:
    regions = []
    for page in render_diff.get('viewer_pages', []):
        if not isinstance(page, dict):
            continue
        if not page.get('changed'):
            continue
        regions.append(
            {
                'page_number': page.get('page_number'),
                'regions': page.get('regions', []),
            }
        )
    artifact = {
        'schema_version': 'render-diff-regions/v1',
        'source_pdf_path': render_diff.get('source_pdf_path'),
        'result_pdf_path': render_diff.get('result_pdf_path'),
        'changed_pages': render_diff.get('changed_pages', []),
        'regions': regions,
    }
    write_json_artifact(artifact_path, artifact)
    return artifact


def build_tracking_fields(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
) -> dict[str, Any]:
    execution_run_id = str(
        _pick_tracking_value(instruction_payload, 'execution_run_id')
        or metadata_dir.parent.name
    )
    validation_run_id = str(
        _pick_tracking_value(instruction_payload, 'validation_run_id')
        or f'{execution_run_id}:validation'
    )

    tracking: dict[str, Any] = {
        'execution_run_id': execution_run_id,
        'validation_run_id': validation_run_id,
    }
    for key in TRACKING_KEYS:
        if key in tracking:
            continue
        tracking[key] = _pick_tracking_value(instruction_payload, key)
    return tracking


def build_validation_artifact(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    edited_output_path: Path,
    source_pdf_path: Path | None,
    pdf_output_path: Path,
    validation_report_path: Path,
) -> dict[str, Any]:
    tracking = build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir)
    gui_edit_scaffolding = _read_gui_edit_scaffolding(metadata_dir, instruction_payload)

    qa_summary: dict[str, Any] = {}
    qa_status = 'partial'
    post_serialization_proof_path = metadata_dir / 'post_serialization_proof.json'
    qa_evidence: dict[str, Any] = {
        'pdf_path': str(pdf_output_path),
        'page_count': None,
        'issues': [],
        'warning_badges': [],
        'render_diff': None,
        'render_diff_regions': None,
        'compile_warning_badges': [],
        'post_serialization_proof': None,
    }
    if validation_report_path.exists():
        report = json.loads(validation_report_path.read_text(encoding='utf-8'))
        checks = report.get('checks') if isinstance(report, dict) else []
        if isinstance(checks, list):
            qa_summary['checks'] = checks
            if checks:
                passed_flags = [bool(check.get('passed')) for check in checks if isinstance(check, dict)]
                qa_status = 'pass' if passed_flags and all(passed_flags) else 'fail'
                result_page_counts = [check.get('result_page_count') for check in checks if isinstance(check, dict) and check.get('result_page_count') is not None]
                if result_page_counts:
                    qa_evidence['page_count'] = result_page_counts[-1]
                qa_evidence['issues'] = [
                    {
                        'check': check.get('name'),
                        'detail': check,
                    }
                    for check in checks
                    if isinstance(check, dict) and not bool(check.get('passed'))
                ]
            else:
                qa_status = 'partial'
        if isinstance(report, dict) and isinstance(report.get('validation'), dict):
            qa_summary['validation'] = report['validation']
        if isinstance(report, dict) and isinstance(report.get('warning_badges'), list):
            qa_evidence['warning_badges'] = report['warning_badges']
        if isinstance(report, dict) and isinstance(report.get('render_diff'), dict):
            qa_evidence['render_diff'] = report['render_diff']
        if isinstance(report, dict) and isinstance(report.get('render_diff_regions'), dict):
            qa_evidence['render_diff_regions'] = report['render_diff_regions']
        compile_badges = []
        metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else None
        if isinstance(metadata, dict) and isinstance(metadata.get('compile_warning_badges'), list):
            compile_badges = metadata['compile_warning_badges']
        elif isinstance(report, dict) and isinstance(report.get('compile_warning_badges'), list):
            compile_badges = report['compile_warning_badges']
        qa_evidence['compile_warning_badges'] = compile_badges

    if post_serialization_proof_path.exists():
        try:
            qa_evidence['post_serialization_proof'] = json.loads(post_serialization_proof_path.read_text(encoding='utf-8'))
        except Exception:
            qa_evidence['post_serialization_proof'] = {
                'path': str(post_serialization_proof_path),
                'read_error': True,
            }

    artifact = {
        'schema_version': 'validation-artifact/v1',
        'stage': 'qa',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'input': {
            'hwpx_path': str(edited_output_path),
            'source_pdf_path': str(source_pdf_path) if source_pdf_path is not None else None,
        },
        'output': {
            'pdf_path': str(pdf_output_path),
        },
        'qa_status': qa_status,
        'qa_summary': qa_summary,
        'qa_evidence': qa_evidence,
        'artifacts': {
            'render_qa_report_path': str(validation_report_path),
            'post_serialization_proof_path': str(post_serialization_proof_path) if post_serialization_proof_path.exists() else None,
            'step_journal_path': gui_edit_scaffolding.get('step_journal_path'),
        },
    }
    write_json_artifact(metadata_dir / 'validation_artifact.json', artifact)
    return artifact


def file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def build_execution_result_artifact(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    edited_output_path: Path,
    pdf_output_path: Path,
    edit_summary_path: Path,
    validation_report_path: Path,
) -> dict[str, Any]:
    tracking = build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir)
    gui_edit_scaffolding = _read_gui_edit_scaffolding(metadata_dir, instruction_payload)
    validation_artifact_path = metadata_dir / 'validation_artifact.json'
    edit_summary = _load_json_artifact(edit_summary_path) or {}
    summary = edit_summary.get('operations') if isinstance(edit_summary.get('operations'), list) else []
    action_evidence_index_path = _action_evidence_index_path(metadata_dir)
    action_evidence_index = _load_json_artifact(action_evidence_index_path)
    action_counts = action_evidence_index.get('action_counts') if isinstance(action_evidence_index, dict) else None
    if not isinstance(action_counts, dict):
        action_counts = _summarize_action_counts(summary)

    post_serialization_proof_path = metadata_dir / 'post_serialization_proof.json'
    artifact_paths = {
        'edited_hwpx_path': str(edited_output_path),
        'pdf_path': str(pdf_output_path),
        'edit_summary_path': str(edit_summary_path),
        'validation_report_path': str(validation_report_path),
        'validation_artifact_path': str(validation_artifact_path),
        'render_diff_regions_path': str(validation_report_path.with_name('render_diff_regions.json')),
        'runtime_status_path': str(metadata_dir / 'runtime_status.json'),
    }
    if post_serialization_proof_path.exists():
        artifact_paths['post_serialization_proof_path'] = str(post_serialization_proof_path)
    if gui_edit_scaffolding.get('step_journal_path'):
        artifact_paths['step_journal_path'] = str(gui_edit_scaffolding['step_journal_path'])
    if action_evidence_index_path.exists():
        artifact_paths['action_evidence_index_path'] = str(action_evidence_index_path)

    artifact = {
        'schema_version': 'execution-result/v1',
        'stage': 'execution_completed',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'action_counts': action_counts,
        'action_evidence_index_path': str(action_evidence_index_path) if action_evidence_index_path.exists() else None,
        'artifacts': artifact_paths,
    }
    write_json_artifact(metadata_dir / 'execution_result.json', artifact)
    return artifact


def build_bundle_manifest(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    instructions_path: Path,
    edited_output_path: Path,
    pdf_output_path: Path,
) -> dict[str, Any]:
    tracking = build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir)
    gui_edit_scaffolding = _read_gui_edit_scaffolding(metadata_dir, instruction_payload)
    fixture_payload = build_fixture_instruction_payload(instruction_payload)
    fixture_payload_path = metadata_dir / 'fixture_instruction_payload.json'
    write_json_artifact(fixture_payload_path, fixture_payload)
    edited_hwpx_semantic_sha256 = build_hwpx_semantic_sha256(edited_output_path)
    pdf_text_sha256, pdf_text_hash_method = build_pdf_text_sha256(pdf_output_path)
    manifest = {
        'schema_version': 'bundle-manifest/v1',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'fixture_identity': {
            'instructions_path': str(fixture_payload_path),
            'instructions_sha256': file_sha256(fixture_payload_path),
        },
        'outputs': {
            'edited_hwpx_path': str(edited_output_path),
            'edited_hwpx_sha256': file_sha256(edited_output_path),
            'pdf_path': str(pdf_output_path),
            'pdf_sha256': file_sha256(pdf_output_path),
        },
        'semantic_fingerprints': {
            'edited_hwpx_semantic_sha256': edited_hwpx_semantic_sha256,
            'pdf_text_sha256': pdf_text_sha256,
            'pdf_text_hash_method': pdf_text_hash_method,
        },
        'artifacts': {
            'runtime_status_path': str(metadata_dir / 'runtime_status.json'),
            'step_journal_path': gui_edit_scaffolding.get('step_journal_path'),
        },
    }
    write_json_artifact(metadata_dir / 'bundle_manifest.json', manifest)
    return manifest


def _safe_get_attr(obj: object, attr: str, default: object = None) -> object:
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def classify_popup_candidates(windows: list[dict[str, object]]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    keywords = (
        '확인', '경고', '알림', '오류', '질문', '저장', '열기', '변환', '호환',
        '보안', '매크로', '모듈', '파일', '닫기', 'continue', 'warning', 'error',
        'save', 'open', 'security', 'compat', 'confirm', 'dialog',
    )
    dialog_classes = {'#32770', 'HncMessageBox', 'HncDialog', 'NUIDialog'}

    for window in windows:
        title = str(window.get('title') or '').strip()
        class_name = str(window.get('class') or '').strip()
        haystack = f'{title} {class_name}'.lower()
        if class_name in dialog_classes or any(keyword.lower() in haystack for keyword in keywords):
            candidates.append(window)

    return candidates[:5]


def _window_is_maximized(window_handle: int, *, win32gui: Any, win32con: Any | None = None) -> bool | None:
    try:
        placement = win32gui.GetWindowPlacement(window_handle)
        if isinstance(placement, tuple) and len(placement) >= 2:
            show_cmd = int(placement[1])
            if win32con is not None:
                return show_cmd == int(win32con.SW_SHOWMAXIMIZED)
            return show_cmd == 3
    except Exception:
        pass

    if win32con is not None:
        try:
            style = int(win32gui.GetWindowLong(window_handle, win32con.GWL_STYLE))
            return bool(style & int(win32con.WS_MAXIMIZE))
        except Exception:
            pass
    return None


def _window_rect_snapshot(window_handle: int, *, win32gui: Any) -> dict[str, int] | None:
    dwm_rect = _dwm_extended_frame_rect_snapshot(window_handle)
    if dwm_rect:
        return dwm_rect
    try:
        left, top, right, bottom = win32gui.GetWindowRect(window_handle)
    except Exception:
        return None
    return {
        'left': int(left),
        'top': int(top),
        'right': int(right),
        'bottom': int(bottom),
        'width': int(right - left),
        'height': int(bottom - top),
        'source': 'get_window_rect',
    }


def _set_process_dpi_awareness() -> dict[str, object]:
    if sys.platform != 'win32':
        return {'platform': sys.platform, 'attempted': False}
    attempts: list[dict[str, object]] = []
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
            attempts.append({'method': 'SetProcessDpiAwarenessContext', 'context': 'PER_MONITOR_AWARE_V2', 'ok': ok})
            if ok:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDpiAwarenessContext', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDpiAwarenessContext', 'ok': False, 'error': repr(exc)})

        try:
            result = int(ctypes.windll.shcore.SetProcessDpiAwareness(2))
            attempts.append({'method': 'SetProcessDpiAwareness', 'awareness': 'PROCESS_PER_MONITOR_DPI_AWARE', 'ok': result == 0, 'result': result})
            if result == 0:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDpiAwareness', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDpiAwareness', 'ok': False, 'error': repr(exc)})

        try:
            ok = bool(user32.SetProcessDPIAware())
            attempts.append({'method': 'SetProcessDPIAware', 'ok': ok})
            if ok:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDPIAware', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDPIAware', 'ok': False, 'error': repr(exc)})
    except Exception as exc:
        attempts.append({'method': 'dpi_awareness_setup', 'ok': False, 'error': repr(exc)})
    return {'attempted': True, 'ok': False, 'attempts': attempts}


def _dwm_extended_frame_rect_snapshot(window_handle: int) -> dict[str, int] | None:
    if sys.platform != 'win32':
        return None
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ('left', ctypes.c_long),
                ('top', ctypes.c_long),
                ('right', ctypes.c_long),
                ('bottom', ctypes.c_long),
            ]

        rect = RECT()
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(int(window_handle)),
            ctypes.c_uint(9),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if int(result) != 0:
            return None
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        if right <= left or bottom <= top:
            return None
        return {
            'left': left,
            'top': top,
            'right': right,
            'bottom': bottom,
            'width': int(right - left),
            'height': int(bottom - top),
            'source': 'dwm_extended_frame_bounds',
        }
    except Exception:
        return None


def _window_rect_area(rect: dict[str, object] | None) -> int:
    if not isinstance(rect, dict):
        return 0
    try:
        return max(0, int(rect.get('width') or 0)) * max(0, int(rect.get('height') or 0))
    except Exception:
        return 0


def _resolve_capture_window_handle(
    window_handle: int,
    *,
    win32gui: Any,
    win32process: Any,
    win32con: Any | None = None,
) -> tuple[int, int | None, dict[str, object]]:
    requested_handle = int(window_handle)
    requested_rect = _window_rect_snapshot(requested_handle, win32gui=win32gui)
    requested_area = _window_rect_area(requested_rect)
    try:
        _, requested_pid = win32process.GetWindowThreadProcessId(requested_handle)
    except Exception:
        requested_pid = None

    resolution: dict[str, object] = {
        'requested_window_handle': requested_handle,
        'strategy': 'requested_window',
    }
    if requested_pid:
        resolution['requested_window_pid'] = int(requested_pid)
    if requested_rect:
        resolution['requested_window_rect'] = requested_rect

    best_handle = requested_handle
    best_area = requested_area
    best_strategy = 'requested_window'
    seen: set[int] = {requested_handle}

    def _consider(candidate_handle: int | None, strategy: str) -> None:
        nonlocal best_handle, best_area, best_strategy
        if not candidate_handle:
            return
        hwnd = int(candidate_handle)
        if hwnd in seen:
            return
        seen.add(hwnd)
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return
        except Exception:
            return

        rect = _window_rect_snapshot(hwnd, win32gui=win32gui)
        area = _window_rect_area(rect)
        if area <= 0:
            return

        # Hancom sometimes exposes the active editing pane/child window handle.
        # Prefer a visible ancestor or same-process top-level window when it is
        # materially larger so screenshots capture the full frame instead of the
        # child surface pasted into the top-left of a larger bitmap.
        if hwnd == requested_handle:
            return
        if strategy in {'ga_rootowner', 'ga_root'}:
            if area >= best_area:
                best_handle = hwnd
                best_area = area
                best_strategy = strategy
            return
        if area > max(best_area, int(best_area * 1.1)):
            best_handle = hwnd
            best_area = area
            best_strategy = strategy

    try:
        root_owner_flag = int(getattr(win32con, 'GA_ROOTOWNER', 3)) if win32con is not None else 3
        _consider(win32gui.GetAncestor(requested_handle, root_owner_flag), 'ga_rootowner')
    except Exception:
        pass
    try:
        root_flag = int(getattr(win32con, 'GA_ROOT', 2)) if win32con is not None else 2
        _consider(win32gui.GetAncestor(requested_handle, root_flag), 'ga_root')
    except Exception:
        pass

    if requested_pid:
        def _enum_cb(hwnd: int, _: object) -> None:
            try:
                _, candidate_pid = win32process.GetWindowThreadProcessId(hwnd)
                if candidate_pid != requested_pid:
                    return
            except Exception:
                return
            _consider(hwnd, 'same_pid_visible_top_level')

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            pass

    resolution['resolved_window_handle'] = int(best_handle)
    resolution['strategy'] = best_strategy
    if best_handle != requested_handle:
        resolved_rect = _window_rect_snapshot(best_handle, win32gui=win32gui)
        if resolved_rect:
            resolution['resolved_window_rect'] = resolved_rect
    resolution['resolved_from_requested_window_handle'] = bool(best_handle != requested_handle)
    return best_handle, requested_pid, resolution


def collect_window_snapshot(window_handle: int | None) -> dict[str, object]:
    if not window_handle or sys.platform != 'win32':
        return {}

    dpi_awareness = _set_process_dpi_awareness()
    try:
        import win32con  # type: ignore
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except Exception:
        return {'window_handle': window_handle}

    requested_handle = int(window_handle)
    snapshot: dict[str, object] = {'window_handle': requested_handle, 'dpi_awareness': dpi_awareness}
    try:
        snapshot['requested_window_title'] = win32gui.GetWindowText(requested_handle)
    except Exception:
        pass
    try:
        snapshot['requested_window_class'] = win32gui.GetClassName(requested_handle)
    except Exception:
        pass
    requested_rect = _window_rect_snapshot(requested_handle, win32gui=win32gui)
    if requested_rect:
        snapshot['requested_window_rect'] = requested_rect

    resolved_handle, requested_pid, resolution = _resolve_capture_window_handle(
        requested_handle,
        win32gui=win32gui,
        win32process=win32process,
        win32con=win32con,
    )
    snapshot['window_handle_resolution'] = resolution
    if resolved_handle != requested_handle:
        snapshot['requested_window_handle'] = requested_handle
        snapshot['window_handle'] = int(resolved_handle)

    try:
        snapshot['window_title'] = win32gui.GetWindowText(resolved_handle)
    except Exception:
        pass
    try:
        snapshot['window_class'] = win32gui.GetClassName(resolved_handle)
    except Exception:
        pass
    try:
        window_is_maximized = _window_is_maximized(int(resolved_handle), win32gui=win32gui, win32con=win32con)
        if window_is_maximized is not None:
            snapshot['window_is_maximized'] = window_is_maximized
    except Exception:
        pass
    try:
        snapshot['window_is_minimized'] = bool(win32gui.IsIconic(resolved_handle))
    except Exception:
        pass
    rect = _window_rect_snapshot(resolved_handle, win32gui=win32gui)
    if rect:
        snapshot['window_rect'] = rect
        snapshot['full_frame_rect'] = rect
    dwm_rect = _dwm_extended_frame_rect_snapshot(resolved_handle)
    if dwm_rect:
        snapshot['dwm_extended_frame_rect'] = dwm_rect
        snapshot['full_frame_rect'] = dwm_rect
    try:
        placement = win32gui.GetWindowPlacement(resolved_handle)
        if isinstance(placement, tuple) and len(placement) >= 2:
            snapshot['window_show_cmd'] = int(placement[1])
    except Exception:
        pass
    try:
        _, pid = win32process.GetWindowThreadProcessId(resolved_handle)
        snapshot['window_pid'] = pid
    except Exception:
        pid = requested_pid
        if pid:
            snapshot['window_pid'] = pid

    if pid:
        windows: list[dict[str, object]] = []

        def _enum_cb(hwnd: int, _: object) -> None:
            try:
                _, candidate_pid = win32process.GetWindowThreadProcessId(hwnd)
                if candidate_pid != pid:
                    return
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                class_name = win32gui.GetClassName(hwnd)
                if not title and not class_name:
                    return
                windows.append(
                    {
                        'hwnd': int(hwnd),
                        'title': title,
                        'class': class_name,
                        'rect': _window_rect_snapshot(hwnd, win32gui=win32gui),
                        'enabled': bool(win32gui.IsWindowEnabled(hwnd)),
                        'is_requested_window': int(hwnd) == requested_handle,
                        'is_resolved_window': int(hwnd) == resolved_handle,
                    }
                )
            except Exception:
                return

        try:
            win32gui.EnumWindows(_enum_cb, None)
            snapshot['visible_windows'] = windows[:10]
            popup_candidates = classify_popup_candidates(windows)
            if popup_candidates:
                snapshot['popup_candidates'] = popup_candidates
        except Exception:
            pass

    return snapshot


def collect_hwp_snapshot(hwp: object | None) -> dict[str, object]:
    if hwp is None:
        return {}

    snapshot: dict[str, object] = {}
    try:
        docs = _safe_get_attr(hwp, 'XHwpDocuments')
        if docs:
            active_doc = _safe_get_attr(docs, 'Active_XHwpDocument')
            if active_doc:
                doc_info: dict[str, object] = {}
                for attr in ('DocumentID', 'Title', 'FullName', 'Format', 'EditMode', 'ReadOnly'):
                    value = _safe_get_attr(active_doc, attr)
                    if value is not None:
                        doc_info[attr] = value
                if doc_info:
                    snapshot['document'] = doc_info
    except Exception:
        pass

    try:
        windows = _safe_get_attr(hwp, 'XHwpWindows')
        active_window = _safe_get_attr(windows, 'Active_XHwpWindow') if windows else None
        if active_window:
            window_handle = _safe_get_attr(active_window, 'WindowHandle')
            visible = _safe_get_attr(active_window, 'Visible')
            if visible is not None:
                snapshot['window_visible'] = bool(visible)
            snapshot.update(collect_window_snapshot(int(window_handle)) if window_handle else {})
    except Exception:
        pass

    return snapshot


def emit_runtime_observation(
    log_path: Path,
    *,
    hwp: object | None,
    marker: dict[str, Any] | None = None,
    phase_override: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    status = read_runtime_status(log_path.parent.parent)
    phase = phase_override or (str(status.get('phase')) if status else 'unknown')
    state = str(status.get('state')) if status else 'running'
    window_snapshot = collect_hwp_snapshot(hwp)
    if hwp is not None:
        try:
            window_snapshot['cursor_context'] = _snapshot_cursor_context(hwp)
        except Exception as exc:
            window_snapshot['cursor_context_error'] = repr(exc)
        try:
            nearby_context = _capture_nearby_text_context(hwp)
            window_snapshot['nearby_context'] = nearby_context
            if isinstance(nearby_context, dict) and nearby_context.get('current_paragraph_preview'):
                window_snapshot['current_paragraph_preview'] = nearby_context.get('current_paragraph_preview')
        except Exception as exc:
            window_snapshot['nearby_context_error'] = repr(exc)
    observation_status, frame_meta = observe_job(
        log_path.parent.parent,
        window_snapshot=window_snapshot,
        marker=marker or {},
    )
    extra = {
        'watchdog_at': utc_now_iso(),
        'watchdog': window_snapshot,
        'observation_status': observation_status,
    }
    if frame_meta:
        extra['observation_frame'] = frame_meta
    update_runtime_status(
        log_path,
        phase=phase,
        state=state,
        extra=extra,
        append_history=False,
    )
    return observation_status, frame_meta


def start_runtime_watchdog(
    log_path: Path,
    hwp_getter: Callable[[], object | None],
    marker_getter: Callable[[], dict[str, Any]] | None = None,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _emit() -> None:
        emit_runtime_observation(
            log_path,
            hwp=hwp_getter(),
            marker=marker_getter() if marker_getter is not None else {},
        )

    def _run() -> None:
        _emit()
        while not stop_event.wait(2.0):
            _emit()

    thread = threading.Thread(target=_run, name=f'hwp-watchdog-{log_path.parent.parent.name}', daemon=True)
    thread.start()
    return stop_event, thread


# Hancom automation bootstrap helpers.
# Keep each behavior isolated so startup diagnostics show exactly what succeeded or failed.
def _resolve_security_module_registration() -> dict[str, Any]:
    dll_name = str(settings.security_module_dll or '').strip()
    module_name = str(settings.security_module_name or '').strip()
    return {
        'dll_name': dll_name,
        'module_name': module_name,
        'configured': bool(dll_name and module_name),
    }


def _set_hwp_message_box_mode(hwp: object) -> dict[str, Any]:
    state: dict[str, Any] = {'attempted': False, 'mode': 0xFFFFFF}
    set_message_box_mode = getattr(hwp, 'SetMessageBoxMode', None)
    if callable(set_message_box_mode):
        try:
            state['attempted'] = True
            set_message_box_mode(0xFFFFFF)
            state['succeeded'] = True
        except Exception as exc:
            state['succeeded'] = False
            state['exception'] = repr(exc)
    else:
        state['succeeded'] = False
        state['reason'] = 'missing_SetMessageBoxMode'
    return state


def _register_hwp_security_module(hwp: object) -> dict[str, Any]:
    state = _resolve_security_module_registration()
    register_module = getattr(hwp, 'RegisterModule', None)
    state['method_available'] = callable(register_module)
    state['attempted'] = False
    state['call_completed'] = False
    if not state['configured']:
        state['succeeded'] = False
        state['reason'] = 'security_module_config_incomplete'
        return state
    if callable(register_module):
        try:
            state['attempted'] = True
            result = register_module(state['dll_name'], state['module_name'])
            state['call_completed'] = True
            if result is not None:
                state['return_value'] = result
            if isinstance(result, bool):
                state['succeeded'] = result
            else:
                state['succeeded'] = True
        except Exception as exc:
            state['succeeded'] = False
            state['exception'] = repr(exc)
    else:
        state['succeeded'] = False
        state['reason'] = 'missing_RegisterModule'
    return state


def _set_hwp_window_visibility(hwp: object) -> dict[str, Any]:
    state: dict[str, Any] = {'attempted': False}

    try:
        windows = _safe_get_attr(hwp, 'XHwpWindows')
        active_window = _safe_get_attr(windows, 'Active_XHwpWindow') if windows else None
        if active_window is not None:
            try:
                state['attempted'] = True
                active_window.Visible = True
                state['succeeded'] = True
            except Exception as exc:
                state['succeeded'] = False
                state['exception'] = repr(exc)
        else:
            state['succeeded'] = False
            state['reason'] = 'missing_active_window'
    except Exception as exc:
        state['succeeded'] = False
        state['exception'] = repr(exc)
    return state


def _set_hwp_window_maximized(hwp: object) -> dict[str, Any]:
    state: dict[str, Any] = {'attempted': False, 'target': 'active_hancom_window'}
    if sys.platform != 'win32':
        state['succeeded'] = False
        state['reason'] = 'windows_only'
        return state

    try:
        windows = _safe_get_attr(hwp, 'XHwpWindows')
        active_window = _safe_get_attr(windows, 'Active_XHwpWindow') if windows else None
        if active_window is None:
            state['succeeded'] = False
            state['reason'] = 'missing_active_window'
            return state

        window_handle = _safe_get_attr(active_window, 'WindowHandle')
        if not window_handle:
            state['succeeded'] = False
            state['reason'] = 'missing_window_handle'
            return state

        import win32con  # type: ignore
        import win32gui  # type: ignore

        hwnd = int(window_handle)
        state['attempted'] = True
        state['window_handle'] = hwnd
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        state['method'] = 'win32gui.ShowWindow(SW_MAXIMIZE)'
        window_is_maximized = _window_is_maximized(hwnd, win32gui=win32gui, win32con=win32con)
        state['window_is_maximized'] = window_is_maximized
        state['verification_status'] = (
            'verified_maximized'
            if window_is_maximized is True
            else 'verified_not_maximized'
            if window_is_maximized is False
            else 'verification_unknown'
        )
        state['succeeded'] = window_is_maximized
        return state
    except Exception as exc:
        state['succeeded'] = False
        state['exception'] = repr(exc)
        return state


def configure_hwp_automation(hwp: object) -> dict[str, Any]:
    return {
        'message_box_mode': _set_hwp_message_box_mode(hwp),
        'security_module_registration': _register_hwp_security_module(hwp),
        'window_visibility': _set_hwp_window_visibility(hwp),
        'window_maximize_on_open': _set_hwp_window_maximized(hwp),
    }


def _instantiate_hwp_without_builtin_register_module(Hwp: Callable[..., object]) -> tuple[object, str]:
    constructor_attempts = (
        ({'visible': True, 'register_module': False}, 'Hwp(visible=True, register_module=False)'),
        ({'register_module': False}, 'Hwp(register_module=False)'),
        ({'visible': True}, 'Hwp(visible=True)'),
        ({}, 'Hwp()'),
    )
    last_type_error: TypeError | None = None
    for kwargs, label in constructor_attempts:
        try:
            return Hwp(**kwargs), label
        except TypeError as exc:
            last_type_error = exc
            continue
    if last_type_error is not None:
        raise last_type_error
    raise RuntimeError('Failed to construct pyhwpx Hwp instance.')


def create_visible_hwp_instance(Hwp: Callable[..., object]) -> tuple[object, dict[str, Any]]:
    hwp, constructor = _instantiate_hwp_without_builtin_register_module(Hwp)
    automation = configure_hwp_automation(hwp)
    automation['constructor'] = constructor
    automation['security_module_registration_mode'] = 'manual_post_init'
    return hwp, automation


def close_hwp_instance(hwp: object | None) -> None:
    if hwp is None:
        return
    if hasattr(hwp, 'quit'):
        hwp.quit()
        return
    if hasattr(hwp, 'Quit'):
        hwp.Quit()


def kill_hwp_runtime() -> dict[str, Any]:
    result: dict[str, Any] = {'platform': sys.platform}
    if sys.platform != 'win32':
        return result
    try:
        proc = subprocess.run(
            ['taskkill', '/IM', 'Hwp.exe', '/F'],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        result.update(
            {
                'returncode': proc.returncode,
                'stdout': (proc.stdout or '').strip().splitlines()[:5],
                'stderr': (proc.stderr or '').strip().splitlines()[:5],
            }
        )
    except Exception as exc:
        result['exception'] = repr(exc)
    time.sleep(2.0)
    return result


def perform_native_export_preflight(
    Hwp: Callable[..., object],
    *,
    log_path: Path,
    detail: str,
) -> None:
    if sys.platform != 'win32':
        return
    preflight: dict[str, Any] = {
        'detail': detail,
        'stale_hwp_cleanup': kill_hwp_runtime(),
    }
    update_runtime_status(
        log_path,
        phase='native_export_preflight',
        detail=detail,
        extra={'native_export_preflight': preflight},
        append_history=True,
    )
    probe_hwp = None
    try:
        probe_hwp, automation = create_visible_hwp_instance(Hwp)
        preflight['cold_start'] = 'ok'
        preflight['hwp_automation'] = automation
        preflight['hwp_snapshot'] = collect_hwp_snapshot(probe_hwp)
        update_runtime_status(
            log_path,
            phase='native_export_preflight',
            detail=detail,
            extra={'native_export_preflight': preflight},
            append_history=True,
        )
    except Exception as exc:
        preflight['cold_start'] = 'failed'
        preflight['exception'] = repr(exc)
        update_runtime_status(
            log_path,
            phase='native_export_preflight_failed',
            detail=str(exc),
            extra={'native_export_preflight': preflight},
            append_history=True,
        )
        raise RuntimeError(f'native_preflight_failed: {exc}') from exc
    finally:
        close_hwp_instance(probe_hwp)
        time.sleep(1.0)


def create_hwp_instance_with_recovery(
    Hwp: Callable[..., object],
    *,
    log_path: Path,
    phase: str,
    detail: str,
    max_attempts: int = 2,
) -> object:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        update_runtime_status(
            log_path,
            phase=phase,
            detail=detail,
            extra={'hwp_start_attempt': attempt, 'hwp_start_max_attempts': max_attempts},
        )
        try:
            hwp, automation = create_visible_hwp_instance(Hwp)
            update_runtime_status(
                log_path,
                phase=phase,
                detail=detail,
                extra={
                    'hwp_start_attempt': attempt,
                    'hwp_start_max_attempts': max_attempts,
                    'hwp_automation': automation,
                },
                append_history=True,
            )
            registration = automation.get('security_module_registration') if isinstance(automation, dict) else None
            if isinstance(registration, dict) and not registration.get('succeeded'):
                logger.warning('Hancom security-module registration did not fully succeed: %s', registration)
            else:
                logger.info('Hancom automation bootstrap configured: %s', automation)
            return hwp
        except Exception as exc:
            last_exc = exc
            recovery = {'attempt': attempt, 'exception': repr(exc)}
            if attempt < max_attempts:
                recovery['kill_hwp_runtime'] = kill_hwp_runtime()
                update_runtime_status(
                    log_path,
                    phase='recover_hwp_runtime',
                    detail=f'{detail} retry after Hancom COM startup failure',
                    extra={'hwp_runtime_recovery': recovery},
                    append_history=True,
                )
                continue
            update_runtime_status(
                log_path,
                phase='recover_hwp_runtime_failed',
                detail=str(exc),
                extra={'hwp_runtime_recovery': recovery},
                append_history=True,
            )
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError('Failed to create Hancom automation instance.')


def reopen_hwp_for_pdf_export(
    *,
    Hwp: Callable[..., object],
    log_path: Path,
    document_path: Path,
) -> object:
    hwp = create_hwp_instance_with_recovery(
        Hwp,
        log_path=log_path,
        phase='reopen_hwp_for_pdf_export',
        detail=f'reopen {document_path.name} for native PDF export',
    )
    update_runtime_status(log_path, phase='reopen_document_for_pdf_export', detail=document_path.name)
    if hasattr(hwp, 'open'):
        hwp.open(str(document_path))
    elif hasattr(hwp, 'Open'):
        hwp.Open(str(document_path))
    else:
        close_hwp_instance(hwp)
        raise RuntimeError('pyhwpx Hwp object does not expose an open/Open method as expected.')
    _set_hwp_window_maximized(hwp)
    return hwp


def normalize_hwp_for_export(hwp: object) -> None:
    cancel = getattr(hwp, 'Cancel', None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            pass
    haction = getattr(hwp, 'HAction', None)
    run = getattr(haction, 'Run', None)
    if callable(run):
        for action in ('Cancel', 'CloseEx', 'MoveDocBegin'):
            try:
                run(action)
            except Exception:
                pass
    move_doc_begin = getattr(hwp, 'MoveDocBegin', None)
    if callable(move_doc_begin):
        try:
            move_doc_begin()
        except Exception:
            pass


def get_pdf_export_probe_history_path(log_path: Path) -> Path:
    return log_path.parent.parent / 'metadata' / 'pdf_export_probes.jsonl'


def append_pdf_export_probe(log_path: Path, payload: dict[str, Any]) -> None:
    history_path = get_pdf_export_probe_history_path(log_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + '\n')


def collect_process_snapshot(pid: int | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {'pid': pid}
    if sys.platform != 'win32':
        return snapshot
    if not pid:
        snapshot['alive'] = None
        return snapshot

    try:
        proc = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        stdout = (proc.stdout or '').strip()
        snapshot['tasklist_returncode'] = proc.returncode
        snapshot['tasklist_stdout'] = stdout.splitlines()[:3]
        snapshot['tasklist_stderr'] = (proc.stderr or '').strip().splitlines()[:3]
        if not stdout or 'No tasks are running' in stdout:
            snapshot['alive'] = False
            return snapshot

        row = next((line for line in stdout.splitlines() if line.strip()), '')
        snapshot['alive'] = True
        if row:
            try:
                import csv

                parsed = next(csv.reader([row]))
                if len(parsed) >= 5:
                    snapshot['image_name'] = parsed[0]
                    snapshot['session_name'] = parsed[2]
                    snapshot['mem_usage'] = parsed[4]
            except Exception:
                snapshot['tasklist_row'] = row
        return snapshot
    except Exception as exc:
        snapshot['alive'] = None
        snapshot['snapshot_error'] = repr(exc)
        return snapshot


def collect_pdf_export_probe(
    *,
    hwp: object,
    primitive: str,
    output_path: Path,
    exc: Exception | None = None,
) -> dict[str, Any]:
    hwp_snapshot = collect_hwp_snapshot(hwp)
    window_pid = hwp_snapshot.get('window_pid')
    try:
        pid = int(window_pid) if window_pid is not None else None
    except Exception:
        pid = None

    probe: dict[str, Any] = {
        'captured_at': utc_now_iso(),
        'primitive': primitive,
        'output_path': str(output_path),
        'output_exists': output_path.exists(),
        'hwp_snapshot': hwp_snapshot,
        'process_snapshot': collect_process_snapshot(pid),
    }
    if exc is not None:
        probe['exception'] = {
            'type': type(exc).__name__,
            'repr': repr(exc),
            'message': str(exc),
        }
    return probe


def list_available_pdf_export_primitives(hwp: object) -> list[str]:
    available: list[str] = []
    haction = getattr(hwp, 'HAction', None)
    hparameter_set = getattr(hwp, 'HParameterSet', None)
    run_get_default = getattr(haction, 'GetDefault', None)
    run_execute = getattr(haction, 'Execute', None)
    file_open_save = getattr(hparameter_set, 'HFileOpenSave', None)
    if callable(run_get_default) and callable(run_execute) and file_open_save is not None and hasattr(file_open_save, 'HSet'):
        available.append('file_save_as_pdf')
    if hasattr(hwp, 'save_as_pdf'):
        available.append('save_as_pdf')
    if hasattr(hwp, 'save_as'):
        available.append('save_as')
    if hasattr(hwp, 'SaveAs'):
        available.append('SaveAs')
    return available


def resolve_pdf_export_primitive(hwp: object) -> str:
    requested = str(os.environ.get('HWPX_PDF_EXPORT_PRIMITIVE', '') or '').strip().lower()
    available = list_available_pdf_export_primitives(hwp)
    if requested in {'', 'auto', 'default'}:
        return available[0] if available else 'unavailable'
    if requested in available:
        return requested
    return available[0] if available else requested


def save_hwp_as(
    hwp: object,
    output_path: Path,
    fmt: str,
    log_path: Path | None = None,
    *,
    pdf_primitive: str | None = None,
    pdf_probe_context: dict[str, Any] | None = None,
) -> None:
    """Persist the current document through the narrowest export surface available.

    PDF saves get extra probe/status logging because native export failures are one of the
    flakiest parts of the Windows worker and we need enough artifacts to diagnose them.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalize_hwp_for_export(hwp)
    normalized_fmt = str(fmt).upper()
    if normalized_fmt == 'PDF':
        # Record the exact primitive/runtime state around PDF export so retries and failure
        # artifacts explain which Hancom surface was attempted.
        primitive = pdf_primitive or resolve_pdf_export_primitive(hwp)
        phase_name = 'save_pdf'
        if log_path is not None:
            status = read_runtime_status(log_path.parent.parent)
            current_phase = status.get('phase') if isinstance(status, dict) else None
            if isinstance(current_phase, str) and current_phase:
                phase_name = current_phase
            probe = collect_pdf_export_probe(hwp=hwp, primitive=primitive, output_path=output_path)
            if pdf_probe_context:
                probe.update(pdf_probe_context)
            probe['event'] = 'before_pdf_export'
            append_pdf_export_probe(log_path, probe)
            update_runtime_status(log_path, phase=phase_name, extra={'pdf_export_probe': probe}, append_history=True)
        try:
            haction = getattr(hwp, 'HAction', None)
            hparameter_set = getattr(hwp, 'HParameterSet', None)
            run_get_default = getattr(haction, 'GetDefault', None)
            run_execute = getattr(haction, 'Execute', None)
            file_open_save = getattr(hparameter_set, 'HFileOpenSave', None)
            if primitive == 'file_save_as_pdf':
                if not (callable(run_get_default) and callable(run_execute) and file_open_save is not None and hasattr(file_open_save, 'HSet')):
                    raise RuntimeError('Requested PDF primitive file_save_as_pdf is unavailable.')
                run_get_default('FileSaveAsPdf', file_open_save.HSet)
                file_open_save.filename = str(output_path)
                file_open_save.Format = 'PDF'
                file_open_save.Attributes = 16384
                if not run_execute('FileSaveAsPdf', file_open_save.HSet):
                    raise RuntimeError(f'FileSaveAsPdf action failed for {output_path}')
            elif primitive == 'save_as_pdf':
                if not hasattr(hwp, 'save_as_pdf'):
                    raise RuntimeError('Requested PDF primitive save_as_pdf is unavailable.')
                hwp.save_as_pdf(str(output_path))
            elif primitive == 'save_as':
                if not hasattr(hwp, 'save_as'):
                    raise RuntimeError('Requested PDF primitive save_as is unavailable.')
                hwp.save_as(str(output_path), format='PDF')
            elif primitive == 'SaveAs':
                if not hasattr(hwp, 'SaveAs'):
                    raise RuntimeError('Requested PDF primitive SaveAs is unavailable.')
                try:
                    hwp.SaveAs(str(output_path), 'PDF')
                except TypeError:
                    hwp.SaveAs(str(output_path), format='PDF')
            else:
                raise RuntimeError('No available PDF export primitive was found on the Hwp object.')
        except Exception as exc:
            if log_path is not None:
                probe = collect_pdf_export_probe(hwp=hwp, primitive=primitive, output_path=output_path, exc=exc)
                if pdf_probe_context:
                    probe.update(pdf_probe_context)
                probe['event'] = 'pdf_export_failure'
                append_pdf_export_probe(log_path, probe)
                update_runtime_status(log_path, phase=phase_name, extra={'pdf_export_probe': probe}, append_history=True)
            raise
        if log_path is not None:
            probe = collect_pdf_export_probe(hwp=hwp, primitive=primitive, output_path=output_path)
            if pdf_probe_context:
                probe.update(pdf_probe_context)
            probe['event'] = 'pdf_export_success'
            append_pdf_export_probe(log_path, probe)
            update_runtime_status(log_path, phase=phase_name, extra={'pdf_export_probe': probe}, append_history=True)
        return
    # Non-PDF exports stay simple: prefer the modern wrapper, then fall back to the legacy
    # COM-facing method name without changing the caller contract.
    if hasattr(hwp, 'save_as'):
        hwp.save_as(str(output_path), format=fmt)
        return
    if hasattr(hwp, 'SaveAs'):
        try:
            hwp.SaveAs(str(output_path), fmt)
        except TypeError:
            hwp.SaveAs(str(output_path), format=fmt)
        return
    raise RuntimeError('pyhwpx Hwp object does not expose a recognizable SaveAs/save_as method.')


def count_pdf_pages(path: Path) -> int:
    try:
        from pypdf import PdfReader  # type: ignore

        return len(PdfReader(str(path)).pages)
    except Exception:
        pass

    try:
        from PyPDF2 import PdfReader  # type: ignore

        return len(PdfReader(str(path)).pages)
    except Exception:
        pass

    data = path.read_bytes()
    count = len(re.findall(rb'/Type\s*/Page\b', data))
    if count <= 0:
        raise RuntimeError(f'Failed to count PDF pages for {path}')
    return count


def validate_pdf_outputs(
    *,
    source_pdf_path: Path | None,
    result_pdf_path: Path,
    validation: dict,
    instruction_metadata: dict[str, Any] | None,
    report_path: Path,
) -> None:
    """Write QA evidence for the produced PDF and raise on configured validation failures."""
    report: dict[str, object] = {
        'validation': validation,
        'checks': [],
        'warning_badges': [],
        'render_diff': None,
        'render_diff_regions': None,
        'compile_warning_badges': [],
    }

    # Render diff and page-count checks only make sense when we have a preserved baseline PDF.
    needs_source_pdf = bool(source_pdf_path is not None and source_pdf_path.exists())

    if needs_source_pdf:
        if source_pdf_path is None or not source_pdf_path.exists():
            raise RuntimeError(
                'page-count validation requires a baseline source PDF artifact '
                '(preserve_page_count/max_page_count/max_page_increase)'
            )
        source_pages = count_pdf_pages(source_pdf_path)
        result_pages = count_pdf_pages(result_pdf_path)
        regions_artifact_path = report_path.with_name('render_diff_regions.json')
        render_diff = build_render_diff_summary(
            source_pdf_path=source_pdf_path,
            result_pdf_path=result_pdf_path,
            source_page_count=source_pages,
            result_page_count=result_pages,
            regions_artifact_path=regions_artifact_path,
        )
        report['render_diff'] = render_diff
        report['render_diff_regions'] = build_render_diff_regions_artifact(
            render_diff=render_diff,
            artifact_path=regions_artifact_path,
        )
        report['checks'].append(
            {
                'name': 'page_count_observed',
                'passed': True,
                'source_page_count': source_pages,
                'result_page_count': result_pages,
            }
        )
        report['checks'].append(
            {
                'name': 'render_diff_available',
                'passed': True,
                'source_page_count': source_pages,
                'result_page_count': result_pages,
                'changed_page_count': len(render_diff.get('changed_pages') or []),
            }
        )
        if source_pages != result_pages:
            report['warning_badges'].append(
                {
                    'code': 'page_count_change',
                    'severity': 'warning',
                    'stage': 'qa',
                    'blocking': bool(validation.get('preserve_page_count')),
                    'summary': f'Page count changed from {source_pages} to {result_pages}.',
                    'detail': f'{source_pdf_path.name} -> {result_pdf_path.name}',
                }
            )
        if result_pages > source_pages:
            report['warning_badges'].append(
                {
                    'code': 'render_overflow',
                    'severity': 'warning',
                    'stage': 'qa',
                    'blocking': False,
                    'summary': 'Result PDF has more pages than the source baseline.',
                    'detail': f'page increase {result_pages - source_pages}',
                }
            )

    # Carry compile-time warnings into QA so downstream reviewers can line them up with the
    # visual evidence instead of treating compile and render review as separate stories.
    compile_warning_badges = []
    if isinstance(instruction_metadata, dict) and isinstance(instruction_metadata.get('compile_warning_badges'), list):
        compile_warning_badges = instruction_metadata.get('compile_warning_badges', [])
    if compile_warning_badges:
        report['compile_warning_badges'] = compile_warning_badges
        for badge in compile_warning_badges:
            if isinstance(badge, dict) and badge.get('code') == 'style_drift':
                linked_badge = {
                    **badge,
                    'stage': 'qa',
                    'summary': 'Compile-time style drift warning should be reviewed with render evidence.',
                    'detail': 'Linked to render_diff/viewer_pages for visual review.',
                }
                report['warning_badges'].append(linked_badge)

    if validation.get('preserve_page_count'):
        passed = source_pages == result_pages
        report['checks'].append(
            {
                'name': 'preserve_page_count',
                'passed': passed,
                'source_page_count': source_pages,
                'result_page_count': result_pages,
            }
        )
        write_json_artifact(report_path, report)
        if not passed:
            raise RuntimeError(
                f'Validation failed: page count changed from {source_pages} to {result_pages} '
                f'({source_pdf_path.name} -> {result_pdf_path.name})'
            )

    if validation.get('max_page_count') is not None:
        max_page_count = int(validation['max_page_count'])
        passed = result_pages <= max_page_count
        report['checks'].append(
            {
                'name': 'max_page_count',
                'passed': passed,
                'source_page_count': source_pages,
                'result_page_count': result_pages,
                'max_page_count': max_page_count,
            }
        )
        if not passed:
            raise RuntimeError(
                f'Validation failed: page count {result_pages} exceeds max_page_count {max_page_count} '
                f'({source_pdf_path.name} -> {result_pdf_path.name})'
            )

    if validation.get('max_page_increase') is not None:
        max_page_increase = int(validation['max_page_increase'])
        page_increase = result_pages - source_pages
        passed = page_increase <= max_page_increase
        report['checks'].append(
            {
                'name': 'max_page_increase',
                'passed': passed,
                'source_page_count': source_pages,
                'result_page_count': result_pages,
                'page_increase': page_increase,
                'max_page_increase': max_page_increase,
            }
        )
        if not passed:
            raise RuntimeError(
                f'Validation failed: page count increase {page_increase} exceeds max_page_increase {max_page_increase} '
                f'({source_pdf_path.name} -> {result_pdf_path.name})'
            )

    write_json_artifact(report_path, report)


def needs_source_pdf_validation(validation: dict) -> bool:
    if not isinstance(validation, dict) or not validation:
        return False
    return any(
        validation.get(key) is not None
        for key in ('preserve_page_count', 'max_page_count', 'max_page_increase')
    )


MATCH_REQUIRED_OPS = {
    'replace_text_safe',
    'replace_paragraph_safe',
    'replace_paragraph_range_safe',
    'paragraph_replace_native',
    'paragraph_range_replace_native',
    'replace_between_anchors_safe',
    'clone_text_style',
    'clone_paragraph_shape',
    'style_text',
    'align_paragraph',
    'paragraph_shape',
    'list_paragraph',
    'replace_empty_native_list_scaffold',
    'native_action',
    'table_cell_action',
    'table_cell_replace_text',
    'table_patch_cells',
}


def _collect_zero_match_blockers(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        op_name = str(item.get('op') or '')
        if op_name not in MATCH_REQUIRED_OPS:
            continue
        if bool(item.get('allow_zero_match')):
            continue
        matches = item.get('matches')
        if matches is None:
            continue
        try:
            match_count = int(matches)
        except Exception:
            continue
        if match_count <= 0:
            blockers.append({'index': item.get('index'), 'op': op_name, 'matches': match_count})
    return blockers


def convert_with_pyhwpx(source_path: Path, output_path: Path, log_path: Path) -> None:
    job_logger = configure_logger(f'hwp.job.{source_path.stem}', settings.log_level, log_path)
    job_logger.info('Starting conversion: %s -> %s', source_path, output_path)
    update_runtime_status(log_path, phase='starting', detail=f'convert {source_path.name}')

    if sys.platform != 'win32':
        raise RuntimeError('HWPX conversion worker must run on Windows in an interactive user session.')

    pythoncom = None
    try:
        import pythoncom  # type: ignore
    except ImportError:
        pythoncom = None

    if pythoncom is not None:
        update_runtime_status(log_path, phase='pythoncom_initialize')
        pythoncom.CoInitialize()

    hwp = None
    watchdog_stop: threading.Event | None = None
    watchdog_thread: threading.Thread | None = None
    observation_marker = {
        'job_id': log_path.parent.parent.name,
        'execution_run_id': log_path.parent.parent.name,
        'run_label': log_path.parent.parent.name,
    }
    success = False
    native_capabilities: dict[str, Any] | None = None
    step_journal_path: Path | None = None
    last_gui_edit_scaffolding: dict[str, Any] | None = None
    try:
        update_runtime_status(log_path, phase='import_pyhwpx')
        from pyhwpx import Hwp  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            'pyhwpx import failed. Install pyhwpx on Windows and verify Hancom Office automation is available.'
        ) from exc

    try:
        # NOTE: pyhwpx and Hancom method names can differ by version.
        # This routine intentionally tries a few plausible call patterns, then fails loudly.
        # TODO on Jun's Windows machine: confirm the exact open/save/close API names for the installed Hancom build.
        # TODO on Jun's Windows machine: add popup/security-dialog handling after observing real behavior.
        hwp = create_hwp_instance_with_recovery(
            Hwp,
            log_path=log_path,
            phase='create_hwp_instance',
            detail=f'convert {source_path.name}',
        )
        watchdog_stop, watchdog_thread = start_runtime_watchdog(log_path, lambda: hwp, lambda: observation_marker)

        update_runtime_status(log_path, phase='open_document')
        if hasattr(hwp, 'open'):
            hwp.open(str(source_path))
        elif hasattr(hwp, 'Open'):
            hwp.Open(str(source_path))
        else:
            raise RuntimeError('pyhwpx Hwp object does not expose an open/Open method as expected.')
        window_open_state = _set_hwp_window_maximized(hwp)
        native_capabilities = write_native_capabilities_artifact(log_path.parent.parent / 'metadata', hwp)
        update_runtime_status(
            log_path,
            phase='document_opened',
            extra={
                'native_capabilities': native_capabilities,
                'window_open_state': window_open_state,
            },
        )
        emit_runtime_observation(
            log_path,
            hwp=hwp,
            marker=observation_marker,
            phase_override='document_opened',
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        update_runtime_status(log_path, phase='save_pdf')
        if hasattr(hwp, 'save_as_pdf'):
            hwp.save_as_pdf(str(output_path))
        elif hasattr(hwp, 'SaveAs'):
            try:
                hwp.SaveAs(str(output_path), 'PDF')
            except TypeError:
                hwp.SaveAs(str(output_path), format='PDF')
        else:
            raise RuntimeError('pyhwpx Hwp object does not expose a recognizable PDF save method.')

        if not output_path.exists():
            raise RuntimeError('Conversion routine completed without creating the expected PDF file.')

        update_runtime_status(log_path, phase='finished', state='succeeded')
        success = True
        job_logger.info('Conversion succeeded: %s', output_path)
    finally:
        if watchdog_stop is not None:
            watchdog_stop.set()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=1.0)
        if hwp is not None:
            try:
                update_runtime_status(log_path, phase='close_hwp', state='succeeded' if success else 'running')
                close_hwp_instance(hwp)
            except Exception:
                job_logger.warning('Failed to close Hancom automation cleanly.', exc_info=True)

        if pythoncom is not None:
            pythoncom.CoUninitialize()

        if success:
            update_runtime_status(
                log_path,
                phase='finished',
                state='succeeded',
                extra={'native_capabilities': native_capabilities} if native_capabilities else None,
                append_history=False,
            )


def edit_and_convert_with_pyhwpx(
    source_path: Path,
    edited_output_path: Path,
    pdf_output_path: Path,
    instructions_path: Path,
    log_path: Path,
) -> None:
    """Apply queued edits, export artifacts, and emit the evidence bundle for one job."""
    job_logger = configure_logger(f'hwp.job.{source_path.stem}', settings.log_level, log_path)
    job_logger.info('Starting edit+convert: %s -> %s / %s', source_path, edited_output_path, pdf_output_path)
    update_runtime_status(log_path, phase='starting', detail=f'edit_and_convert {source_path.name}')

    if sys.platform != 'win32':
        raise RuntimeError('HWPX edit worker must run on Windows in an interactive user session.')

    pythoncom = None
    try:
        import pythoncom  # type: ignore
    except ImportError:
        pythoncom = None

    if pythoncom is not None:
        update_runtime_status(log_path, phase='pythoncom_initialize')
        pythoncom.CoInitialize()

    hwp = None
    watchdog_stop: threading.Event | None = None
    watchdog_thread: threading.Thread | None = None
    observation_marker = {
        'job_id': log_path.parent.parent.name,
        'execution_run_id': log_path.parent.parent.name,
        'run_label': log_path.parent.parent.name,
    }
    success = False
    native_capabilities: dict[str, Any] | None = None
    try:
        update_runtime_status(log_path, phase='import_pyhwpx')
        from pyhwpx import Hwp  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            'pyhwpx import failed. Install pyhwpx on Windows and verify Hancom Office automation is available.'
        ) from exc

    try:
        # Phase 1: load instructions/tracking first so every later branch can leave coherent
        # evidence, even when Hancom automation fails mid-run.
        update_runtime_status(log_path, phase='load_instruction_payload')
        instruction_payload = load_instruction_payload_from_file(instructions_path)
        operations = instruction_payload['operations']
        validation = instruction_payload.get('validation') or {}
        metadata_dir = instructions_path.parent
        tracking = build_tracking_fields(instruction_payload=instruction_payload, metadata_dir=metadata_dir)
        step_journal_path = _initialize_step_journal(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            tracking=tracking,
            operations=operations,
        )
        action_tracker_state = _build_action_tracker_state(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            tracking=tracking,
            operations=operations,
        )
        last_gui_edit_scaffolding = _build_gui_edit_scaffolding(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            operations=operations,
        )
        observation_marker['execution_run_id'] = str(tracking.get('execution_run_id') or observation_marker['execution_run_id'])
        observation_marker['run_label'] = str(tracking.get('execution_run_id') or observation_marker['run_label'])
        hwp = create_hwp_instance_with_recovery(
            Hwp,
            log_path=log_path,
            phase='create_hwp_instance',
            detail=f'edit_and_convert {source_path.name}',
        )
        watchdog_stop, watchdog_thread = start_runtime_watchdog(log_path, lambda: hwp, lambda: observation_marker)

        # Phase 2: open the document and capture baseline/runtime capabilities before any edit.
        update_runtime_status(log_path, phase='open_document')
        if hasattr(hwp, 'open'):
            hwp.open(str(source_path))
        elif hasattr(hwp, 'Open'):
            hwp.Open(str(source_path))
        else:
            raise RuntimeError('pyhwpx Hwp object does not expose an open/Open method as expected.')
        window_open_state = _set_hwp_window_maximized(hwp)
        native_capabilities = write_native_capabilities_artifact(metadata_dir, hwp)
        update_runtime_status(
            log_path,
            phase='document_opened',
            extra={
                'native_capabilities': native_capabilities,
                'window_open_state': window_open_state,
                **(last_gui_edit_scaffolding or {}),
            },
        )
        emit_runtime_observation(
            log_path,
            hwp=hwp,
            marker=observation_marker,
            phase_override='document_opened',
        )

        source_pdf_path = metadata_dir / 'source_baseline.pdf'
        edit_summary_path = metadata_dir / 'edit_summary.json'
        validation_report_path = metadata_dir / 'validation_report.json'

        if needs_source_pdf_validation(validation):
            update_runtime_status(log_path, phase='save_source_baseline_pdf')
            save_hwp_as(hwp, source_pdf_path, 'PDF', log_path)

        # Phase 3: apply edits and freeze the action/evidence summary before export.
        update_runtime_status(
            log_path,
            phase='apply_edit_operations',
            extra={
                'operation_count': len(operations),
                **(last_gui_edit_scaffolding or {}),
            },
        )
        summary = _apply_edit_operations_with_optional_step_recorder(
            hwp,
            operations,
            step_recorder=lambda item: _record_apply_step(
                log_path=log_path,
                step_journal_path=step_journal_path,
                action_tracker_state=action_tracker_state,
                item=item,
                runtime_scaffolding=last_gui_edit_scaffolding,
                native_capabilities=native_capabilities,
            ),
        )
        observed_verification_modes = _collect_verification_modes(summary=summary)
        last_gui_edit_scaffolding = _build_gui_edit_scaffolding(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            summary=summary,
        )
        if observed_verification_modes:
            last_gui_edit_scaffolding['verification_modes'] = observed_verification_modes
        if step_journal_path is not None:
            last_gui_edit_scaffolding['step_journal_path'] = str(step_journal_path)
        zero_match_blockers = _collect_zero_match_blockers(summary)
        write_json_artifact(edit_summary_path, {**tracking, **(last_gui_edit_scaffolding or {}), 'operations': summary})
        if zero_match_blockers:
            raise RuntimeError(
                'Edit apply produced zero-match blocker operations: '
                + json.dumps(zero_match_blockers, ensure_ascii=False)
            )
        job_logger.info('Applied edit operations: %s', summary)

        edited_output_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_output_path.parent.mkdir(parents=True, exist_ok=True)

        instruction_metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else None
        post_serialization_proof_specs = _collect_post_serialization_proof_specs(instruction_metadata)
        requires_post_serialization_proof = bool(post_serialization_proof_specs)
        if requires_post_serialization_proof:
            # Save once, reopen, then prove the serialized file still targets the same cell.
            # This catches cases where in-memory edits looked correct but the written HWPX/PDF
            # would drift to a different table carrier after reload.
            update_runtime_status(log_path, phase='save_intermediate_edited_hwpx_for_pdf_export')
            save_hwp_as(hwp, edited_output_path, 'HWPX')
            if not edited_output_path.exists():
                raise RuntimeError('Edit routine completed without creating the expected edited HWPX file before proof.')

            reopened_proof_hwp = hwp
            expected_cell_addr = next(
                (
                    str(proof_spec.get('expected_cell_addr') or '').strip()
                    for proof_spec in post_serialization_proof_specs
                    if isinstance(proof_spec, dict) and str(proof_spec.get('expected_cell_addr') or '').strip()
                ),
                '',
            )
            if expected_cell_addr:
                close_hwp_instance(hwp)
                reopened_proof_hwp = reopen_hwp_for_pdf_export(
                    Hwp=Hwp,
                    log_path=log_path,
                    document_path=edited_output_path,
                )
                hwp = reopened_proof_hwp
            update_runtime_status(log_path, phase='post_serialization_proof')
            run_post_serialization_proof(
                instruction_payload=instruction_payload,
                metadata_dir=metadata_dir,
                hwpx_path=edited_output_path,
                hwp=reopened_proof_hwp,
            )

        requested_pdf_primitive = str(os.environ.get('HWPX_PDF_EXPORT_PRIMITIVE', '') or '').strip().lower()
        available_pdf_primitives = list_available_pdf_export_primitives(hwp)
        if requested_pdf_primitive in {'', 'auto', 'default'}:
            pdf_export_attempts = available_pdf_primitives or ['unavailable']
        else:
            pdf_export_attempts = [requested_pdf_primitive]
            if requested_pdf_primitive not in available_pdf_primitives and available_pdf_primitives:
                pdf_export_attempts.extend([item for item in available_pdf_primitives if item != requested_pdf_primitive])

        # Phase 4: PDF export is intentionally retried across compatible primitives because the
        # Hancom automation surface varies by build and can fail differently across machines.
        pdf_export_errors: list[dict[str, Any]] = []
        for attempt_index, attempt_primitive in enumerate(pdf_export_attempts, start=1):
            update_runtime_status(
                log_path,
                phase='save_result_pdf',
                extra={
                    'pdf_export_attempt': {
                        'attempt_index': attempt_index,
                        'primitive': attempt_primitive,
                        'requested_primitive': requested_pdf_primitive or 'default',
                        'available_primitives': available_pdf_primitives,
                        'output_parent_exists': pdf_output_path.parent.exists(),
                    }
                },
                append_history=True,
            )
            try:
                save_hwp_as(
                    hwp,
                    pdf_output_path,
                    'PDF',
                    log_path,
                    pdf_primitive=attempt_primitive,
                    pdf_probe_context={
                        'attempt_index': attempt_index,
                        'requested_primitive': requested_pdf_primitive or 'default',
                        'available_primitives': available_pdf_primitives,
                    },
                )
                break
            except Exception as exc:
                pdf_export_errors.append({'attempt_index': attempt_index, 'primitive': attempt_primitive, 'error': repr(exc)})
                if attempt_index >= len(pdf_export_attempts):
                    raise RuntimeError(
                        'Native PDF export failed across primitives: '
                        + json.dumps(pdf_export_errors, ensure_ascii=False)
                    ) from exc

        if not pdf_output_path.exists():
            raise RuntimeError('Edit routine completed without creating the expected PDF file.')

        if not requires_post_serialization_proof:
            update_runtime_status(log_path, phase='save_edited_hwpx_final')
            save_hwp_as(hwp, edited_output_path, 'HWPX')
            if not edited_output_path.exists():
                raise RuntimeError('Edit routine completed without creating the expected edited HWPX file.')

        # Phase 5: validate and bundle artifacts only after both output files exist.
        update_runtime_status(log_path, phase='validate_outputs')
        validate_pdf_outputs(
            source_pdf_path=source_pdf_path if needs_source_pdf_validation(validation) else None,
            result_pdf_path=pdf_output_path,
            validation=validation,
            instruction_metadata=instruction_metadata,
            report_path=validation_report_path,
        )
        build_validation_artifact(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            edited_output_path=edited_output_path,
            source_pdf_path=source_pdf_path if needs_source_pdf_validation(validation) else None,
            pdf_output_path=pdf_output_path,
            validation_report_path=validation_report_path,
        )
        build_execution_result_artifact(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            edited_output_path=edited_output_path,
            pdf_output_path=pdf_output_path,
            edit_summary_path=edit_summary_path,
            validation_report_path=validation_report_path,
        )
        build_bundle_manifest(
            instruction_payload=instruction_payload,
            metadata_dir=metadata_dir,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            pdf_output_path=pdf_output_path,
        )

        if os.environ.get('HWPX_SAVE_EDITED_HWPX', '0') == '1' and not edited_output_path.exists():
            update_runtime_status(log_path, phase='save_edited_hwpx_optional')
            save_hwp_as(hwp, edited_output_path, 'HWPX')
            if not edited_output_path.exists():
                raise RuntimeError('Optional edited HWPX save was enabled but no edited HWPX file was created.')

        update_runtime_status(
            log_path,
            phase='finished',
            state='succeeded',
            extra=last_gui_edit_scaffolding,
        )
        _append_step_journal_terminal_event(
            step_journal_path=step_journal_path,
            state='succeeded',
            verification_modes=(last_gui_edit_scaffolding or {}).get('verification_modes'),
        )
        success = True
        job_logger.info('Edit+convert succeeded: %s / %s', edited_output_path, pdf_output_path)
    except Exception as exc:
        _append_step_journal_terminal_event(
            step_journal_path=step_journal_path,
            state='failed',
            detail=str(exc) or repr(exc),
            verification_modes=(last_gui_edit_scaffolding or {}).get('verification_modes'),
        )
        raise
    finally:
        if watchdog_stop is not None:
            watchdog_stop.set()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=1.0)
        if hwp is not None:
            try:
                update_runtime_status(log_path, phase='close_hwp', state='succeeded' if success else 'running')
                close_hwp_instance(hwp)
            except Exception:
                job_logger.warning('Failed to close Hancom automation cleanly.', exc_info=True)

        if pythoncom is not None:
            pythoncom.CoUninitialize()

        if success:
            update_runtime_status(
                log_path,
                phase='finished',
                state='succeeded',
                extra={
                    **({'native_capabilities': native_capabilities} if native_capabilities else {}),
                    **(last_gui_edit_scaffolding or {}),
                } or None,
                append_history=False,
            )


def run_conversion_subprocess(job_id: str, source_path: Path, output_path: Path, log_path: Path) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        '-m',
        'app.worker',
        'process-one',
        '--task-type',
        'convert',
        '--job-id',
        job_id,
        '--source',
        str(source_path),
        '--output',
        str(output_path),
        '--log-path',
        str(log_path),
    ]
    logger.info('Launching conversion subprocess for job %s', job_id)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.job_timeout_seconds,
    )


def run_edit_subprocess(
    job_id: str,
    source_path: Path,
    edited_output_path: Path,
    pdf_output_path: Path,
    instructions_path: Path,
    log_path: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        '-m',
        'app.worker',
        'process-one',
        '--task-type',
        'edit_and_convert',
        '--job-id',
        job_id,
        '--source',
        str(source_path),
        '--output',
        str(pdf_output_path),
        '--edited-output',
        str(edited_output_path),
        '--instructions',
        str(instructions_path),
        '--log-path',
        str(log_path),
    ]
    logger.info('Launching edit subprocess for job %s', job_id)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.job_timeout_seconds,
    )


def _worker_runtime_path(value: Any, *, field_name: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    # Jobs created before the absolute-path fix, or by older API workers, may
    # still carry paths relative to the runtime root.  Resolve them at the
    # worker boundary before launching Hancom/pyhwpx child processes.
    return (Path.cwd() / path).resolve()


def claim_job_for_execution(
    worker_name: str,
    run_id: str,
    candidate_generation: str,
) -> dict | None:
    """Claim and immediately fence a job before starting native work."""
    job = db.claim_next_job(
        worker_name,
        run_id=run_id,
        candidate_generation=candidate_generation,
    )
    if job is None:
        return None
    if db.worker_lease_matches(worker_name, run_id, candidate_generation):
        return job
    db.requeue_claimed_job_if_owned(
        job['job_id'],
        worker_name=worker_name,
        run_id=run_id,
        candidate_generation=candidate_generation,
        error='Worker lease was replaced before native execution started.',
    )
    logger.warning('Worker lease changed after claiming job %s; native execution was not started.', job['job_id'])
    return None


def handle_job(
    job: dict,
    *,
    worker_name: str,
    run_id: str,
    candidate_generation: str,
) -> None:
    """Run one queued job in a child process and translate failures into queue state."""
    job_id = job['job_id']
    task_type = job.get('task_type') or 'convert'
    job_dir = _worker_runtime_path(job['job_dir'], field_name='job_dir')
    source_path = _worker_runtime_path(job['source_path'], field_name='source_path')
    output_path = _worker_runtime_path(job['output_path'], field_name='output_path')
    edited_output_path = _worker_runtime_path(job['edited_output_path'], field_name='edited_output_path') if job.get('edited_output_path') else None
    instructions_path = _worker_runtime_path(job['instructions_path'], field_name='instructions_path') if job.get('instructions_path') else None
    log_path = job_dir / 'logs' / 'job.log'

    def _run_child() -> subprocess.CompletedProcess[str]:
        if task_type == 'convert':
            return run_conversion_subprocess(job_id, source_path, output_path, log_path)
        if task_type == 'edit_and_convert':
            if edited_output_path is None or instructions_path is None:
                raise RuntimeError('Edit job is missing edited_output_path or instructions_path.')
            return run_edit_subprocess(
                job_id,
                source_path,
                edited_output_path,
                output_path,
                instructions_path,
                log_path,
            )
        raise RuntimeError(f'Unsupported task_type: {task_type}')

    if not db.worker_lease_matches(worker_name, run_id, candidate_generation):
        db.requeue_claimed_job_if_owned(
            job_id,
            worker_name=worker_name,
            run_id=run_id,
            candidate_generation=candidate_generation,
            error='Worker lease was replaced before native execution started.',
        )
        logger.warning('Job %s lease was replaced before native execution started.', job_id)
        return
    if not db.touch_heartbeat_if_owned(
        job_id,
        worker_name=worker_name,
        run_id=run_id,
        candidate_generation=candidate_generation,
    ):
        logger.warning('Job %s is no longer owned by the claimed worker; native execution was not started.', job_id)
        return
    logger.info('Processing job %s (%s)', job_id, source_path.name)

    try:
        # Keep the owner worker process small: the child process does the risky Hancom work,
        # while this wrapper owns queue bookkeeping, logs, and bounded recovery decisions.
        retry_used = False
        result = _run_child()
        if not db.touch_heartbeat_if_owned(
            job_id,
            worker_name=worker_name,
            run_id=run_id,
            candidate_generation=candidate_generation,
        ):
            logger.warning('Job %s claim changed while native execution was running; result was discarded.', job_id)
            return

        if result.stdout:
            logger.info('Job %s stdout: %s', job_id, result.stdout.strip())
        if result.stderr:
            logger.warning('Job %s stderr: %s', job_id, result.stderr.strip())

        error_text = (result.stderr or result.stdout or 'Unknown conversion failure').strip()
        # Only cold-restart/retry the narrow class of native startup/export failures where the
        # child died before producing an output artifact. Everything else should surface directly.
        retryable_runtime_failure = (
            result.returncode != 0
            and not output_path.exists()
            and (
                'native_preflight_failed:' in error_text
                or 'com_error(' in error_text
                or '-2147417851' in error_text
            )
        )
        if retryable_runtime_failure:
            retry_used = True
            cold_restart = {'kill_hwp_runtime': kill_hwp_runtime(), 'retry_reason': error_text[:1000]}
            update_runtime_status(
                log_path,
                phase='cold_restart_before_retry',
                detail='bounded runtime retry after native export/preflight failure',
                extra={'cold_restart_retry': cold_restart},
                append_history=True,
            )
            result = _run_child()
            if not db.touch_heartbeat_if_owned(
                job_id,
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
            ):
                logger.warning('Job %s claim changed during retry; result was discarded.', job_id)
                return
            if result.stdout:
                logger.info('Job %s retry stdout: %s', job_id, result.stdout.strip())
            if result.stderr:
                logger.warning('Job %s retry stderr: %s', job_id, result.stderr.strip())
            error_text = (result.stderr or result.stdout or error_text).strip()

        if result.returncode == 0 and output_path.exists():
            if db.mark_succeeded_if_owned(
                job_id,
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
            ):
                logger.info('Job %s succeeded%s', job_id, ' after bounded retry' if retry_used else '')
            else:
                logger.warning('Job %s claim changed before success was recorded.', job_id)
            return

        raise RuntimeError(error_text)

    except subprocess.TimeoutExpired:
        # Timeout handling stays separate so operators can distinguish worker starvation from
        # content/automation errors and requeue under the normal attempt budget.
        last_phase = format_last_phase(job_dir)
        message = (
            f'Conversion subprocess timed out after {settings.job_timeout_seconds} seconds. '
            'TODO: add Hancom-specific popup dismissal and lingering process cleanup if needed.'
        )
        if last_phase:
            message = f'{message} {last_phase}'
        write_failure_artifacts(job_dir, message)
        write_failure_snapshot_artifacts(job_dir)
        logger.error('Job %s timed out', job_id, exc_info=True)
        current = db.get_job(job_id)
        attempts = int(current['attempts']) if current else 1
        max_attempts = int(current['max_attempts']) if current else settings.max_attempts
        if attempts < max_attempts:
            if not db.requeue_claimed_job_if_owned(
                job_id,
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
                error=message,
            ):
                logger.warning('Job %s claim changed before timeout recovery was recorded.', job_id)
        else:
            if not db.mark_failed_if_owned(
                job_id,
                message,
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
            ):
                logger.warning('Job %s claim changed before timeout failure was recorded.', job_id)

    except Exception as exc:
        # Generic failures still preserve last-phase evidence before the queue state changes.
        last_phase = format_last_phase(job_dir)
        message = f'{exc}\n\n{traceback.format_exc()}'
        if last_phase:
            message = f'{message}\n\n{last_phase}'
        write_failure_artifacts(job_dir, message)
        write_failure_snapshot_artifacts(job_dir)
        logger.error('Job %s failed', job_id, exc_info=True)
        current = db.get_job(job_id)
        attempts = int(current['attempts']) if current else 1
        max_attempts = int(current['max_attempts']) if current else settings.max_attempts
        if attempts < max_attempts:
            if not db.requeue_claimed_job_if_owned(
                job_id,
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
                error=str(exc),
            ):
                logger.warning('Job %s claim changed before failure recovery was recorded.', job_id)
        else:
            if not db.mark_failed_if_owned(
                job_id,
                str(exc),
                worker_name=worker_name,
                run_id=run_id,
                candidate_generation=candidate_generation,
            ):
                logger.warning('Job %s claim changed before failure was recorded.', job_id)


def readiness_heartbeat(
    stop_event: threading.Event,
    *,
    run_id: str,
    candidate_generation: str | None,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Keep the current worker's readiness lease alive during long jobs."""

    while not stop_event.wait(interval_seconds):
        current = load_runtime_readiness_snapshot()
        if not isinstance(current, dict):
            continue
        if current.get('run_id') != run_id or current.get('candidate_generation') != candidate_generation:
            continue
        try:
            write_runtime_readiness_snapshot(
                touch_runtime_readiness_heartbeat(current),
                expected_run_id=run_id,
            )
        except ReadinessOwnershipError:
            logger.warning('Runtime readiness ownership moved to a newer worker run.')
            return
        except Exception:
            logger.exception('Unable to refresh runtime readiness heartbeat.')


def worker_loop() -> int:
    run_id = new_readiness_run_id()
    worker_identity = current_worker_identity()
    candidate_generation = resolve_candidate_generation()
    # Replace any predecessor PASS before the slow Hancom/COM probe starts.
    # This prevents an API/verifier restart from accepting a stale worker.
    probing_snapshot = build_current_run_not_ready_snapshot(
        run_id=run_id,
        candidate_generation=candidate_generation,
        worker_identity=worker_identity,
    )
    write_runtime_readiness_snapshot(probing_snapshot)
    readiness_snapshot = build_runtime_readiness_snapshot(
        probe_hwp=True,
        run_id=run_id,
        candidate_generation=candidate_generation,
        worker_identity=worker_identity,
    )
    try:
        write_runtime_readiness_snapshot(readiness_snapshot, expected_run_id=run_id)
    except ReadinessOwnershipError:
        logger.error('Worker readiness ownership was replaced during the Hancom probe.')
        return 2
    final_readiness = load_runtime_readiness_snapshot()
    if not bool(readiness_snapshot.get('ready')) or not readiness_matches_current_worker(
        final_readiness,
        candidate_generation=candidate_generation,
        run_id=run_id,
    ):
        logger.error('Worker readiness failed before polling: %s', readiness_snapshot.get('summary'))
        return 2
    if not isinstance(candidate_generation, str) or not candidate_generation:
        logger.error('Worker candidate generation is not bound; refusing to acquire a queue lease.')
        return 2
    db.acquire_worker_lease(
        settings.worker_name,
        run_id,
        candidate_generation,
        int(worker_identity['pid']),
        str(worker_identity['start_identity']),
    )

    readiness_stop_event = threading.Event()
    readiness_heartbeat_thread = threading.Thread(
        target=readiness_heartbeat,
        kwargs={
            'stop_event': readiness_stop_event,
            'run_id': run_id,
            'candidate_generation': candidate_generation,
        },
        name='runtime-readiness-heartbeat',
        daemon=True,
    )
    readiness_heartbeat_thread.start()

    recovered = db.recover_stale_running_jobs(settings.job_stale_seconds)
    if recovered:
        logger.warning('Recovered %s stale running job(s) on startup.', recovered)

    logger.info('Worker started. Polling every %s seconds.', settings.poll_interval_seconds)
    while True:
        current_readiness = load_runtime_readiness_snapshot()
        if not readiness_matches_current_worker(
            current_readiness,
            candidate_generation=candidate_generation,
            run_id=run_id,
        ):
            logger.error('Worker readiness ownership is no longer current; stopping worker.')
            return 2
        job = claim_job_for_execution(settings.worker_name, run_id, candidate_generation)
        if job is None:
            time.sleep(settings.poll_interval_seconds)
            continue
        handle_job(
            job,
            worker_name=settings.worker_name,
            run_id=run_id,
            candidate_generation=candidate_generation,
        )


def process_one_command(
    task_type: str,
    job_id: str,
    source_path: Path,
    output_path: Path,
    log_path: Path,
    edited_output_path: Optional[Path] = None,
    instructions_path: Optional[Path] = None,
) -> int:
    try:
        update_runtime_status(log_path, phase='child_process_started', detail=f'job_id={job_id}', extra={'task_type': task_type})
        from pyhwpx import Hwp  # type: ignore
        perform_native_export_preflight(Hwp, log_path=log_path, detail=f'{task_type} {source_path.name}')
        if task_type == 'convert':
            convert_with_pyhwpx(source_path, output_path, log_path)
        elif task_type == 'edit_and_convert':
            if edited_output_path is None or instructions_path is None:
                raise RuntimeError('Edit task requires edited_output_path and instructions_path.')
            edit_and_convert_with_pyhwpx(source_path, edited_output_path, output_path, instructions_path, log_path)
        else:
            raise RuntimeError(f'Unsupported task_type: {task_type}')
        return 0
    except Exception as exc:
        update_runtime_status(log_path, phase='failed', state='failed', detail=str(exc))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(f'ERROR: {exc}\n')
            handle.write(traceback.format_exc())
            handle.write('\n')
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Windows HWPX conversion worker')
    subparsers = parser.add_subparsers(dest='command')

    process_parser = subparsers.add_parser('process-one', help='Run one queued task in a child process')
    process_parser.add_argument('--task-type', default='convert')
    process_parser.add_argument('--job-id', required=True)
    process_parser.add_argument('--source', required=True)
    process_parser.add_argument('--output', required=True)
    process_parser.add_argument('--edited-output')
    process_parser.add_argument('--instructions')
    process_parser.add_argument('--log-path', required=True)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'process-one':
        return process_one_command(
            task_type=args.task_type,
            job_id=args.job_id,
            source_path=Path(args.source),
            output_path=Path(args.output),
            log_path=Path(args.log_path),
            edited_output_path=Path(args.edited_output) if args.edited_output else None,
            instructions_path=Path(args.instructions) if args.instructions else None,
        )

    return worker_loop()


if __name__ == '__main__':
    raise SystemExit(main())
