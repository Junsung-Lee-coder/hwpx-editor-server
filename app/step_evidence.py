from __future__ import annotations

"""Step-journal and action-evidence helpers for the Windows HWPX worker.

Purpose: isolate evidence-generation plumbing from the document-edit pipeline.
Prevented risk: touching audit artifacts should not require editing the COM-heavy
worker flow where behavior regressions are harder to spot.
Next-step intent: grow future evidence schemas here while keeping worker.py as a
high-level orchestration file.
"""

import json
import re
from pathlib import Path
from typing import Any

from app.observation import ensure_viewer_session, load_viewer_session
from app.runtime_state import (
    _instruction_metadata,
    _load_json_artifact,
    _normalize_workflow_mode,
    _resolve_runtime_lane,
    _resolve_workflow_mode,
    _workflow_mode_from_runtime_lane,
    _workflow_mode_to_runtime_lane,
    update_runtime_status,
    utc_now_iso,
    write_json_artifact,
)

SUPPORTED_STEP_VERIFICATION_MODES = (
    'text-local',
    'text-broader',
    'image',
)



def _normalize_step_verification_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in SUPPORTED_STEP_VERIFICATION_MODES:
        return normalized
    return None



def _extract_operation_verification_mode(operation: dict[str, Any]) -> str | None:
    metadata = operation.get('metadata') if isinstance(operation.get('metadata'), dict) else {}
    verification = operation.get('verification') if isinstance(operation.get('verification'), dict) else {}
    for candidate in (
        operation.get('verification_mode'),
        verification.get('mode'),
        metadata.get('verification_mode'),
    ):
        normalized = _normalize_step_verification_mode(candidate)
        if normalized is not None:
            return normalized
    return None



def _collect_verification_modes(
    *,
    instruction_payload: dict[str, Any] | None = None,
    operations: list[dict[str, Any]] | None = None,
    summary: list[dict[str, Any]] | None = None,
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def _push(value: Any) -> None:
        normalized = _normalize_step_verification_mode(value)
        if normalized is None or normalized in seen:
            return
        seen.add(normalized)
        ordered.append(normalized)

    metadata = _instruction_metadata(instruction_payload)
    verification = metadata.get('verification') if isinstance(metadata.get('verification'), dict) else {}
    _push(metadata.get('verification_mode'))
    _push(verification.get('mode'))
    raw_modes = metadata.get('verification_modes')
    if isinstance(raw_modes, list):
        for item in raw_modes:
            _push(item)

    for collection in (operations, summary):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if isinstance(item, dict):
                _push(_extract_operation_verification_mode(item))

    return ordered



def _step_journal_artifact_path(metadata_dir: Path) -> Path:
    return metadata_dir / 'step_journal.jsonl'



def _step_journal_enabled(instruction_payload: dict[str, Any]) -> bool:
    metadata = _instruction_metadata(instruction_payload)
    step_journal = metadata.get('step_journal') if isinstance(metadata.get('step_journal'), dict) else {}
    if isinstance(step_journal.get('enabled'), bool):
        return bool(step_journal.get('enabled'))
    return _resolve_workflow_mode(instruction_payload) == 'interactive'



def _append_jsonl_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + '\n')



def _build_gui_edit_scaffolding(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    operations: list[dict[str, Any]] | None = None,
    summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the metadata a reviewer needs to understand an interactive run.

    Purpose: expose viewer links, verification modes, and evidence paths in one
    payload that downstream artifacts can reuse.
    Prevented risk: scattered scaffolding fields drift across worker phases and
    make evidence bundles harder to consume.
    Next-step intent: attach future reviewer-facing surfaces here without
    widening the main worker function.
    """
    verification_modes = _collect_verification_modes(
        instruction_payload=instruction_payload,
        operations=operations,
        summary=summary,
    )
    viewer_session = load_viewer_session() or ensure_viewer_session()
    payload: dict[str, Any] = {
        'workflow_mode': _resolve_workflow_mode(instruction_payload),
        'runtime_lane': _resolve_runtime_lane(instruction_payload),
        'verification_modes': verification_modes,
        'observation_surface': {
            'viewer_url': viewer_session.get('viewer_url'),
            'stream_url': viewer_session.get('stream_url'),
            'latest_frame_url': viewer_session.get('latest_frame_url'),
            'latest_frame_metadata_url': viewer_session.get('latest_frame_metadata_url'),
            'session_metadata_url': viewer_session.get('session_metadata_url'),
            'mode': viewer_session.get('mode'),
            'remote_control_permitted': viewer_session.get('remote_control_permitted'),
        },
    }
    step_journal_path = _step_journal_artifact_path(metadata_dir)
    if step_journal_path.exists() or _step_journal_enabled(instruction_payload):
        payload['step_journal_path'] = str(step_journal_path)
    action_evidence_index_path = _action_evidence_index_path(metadata_dir)
    if action_evidence_index_path.exists() or _action_evidence_enabled(instruction_payload):
        payload['action_evidence_index_path'] = str(action_evidence_index_path)
    return payload



def _initialize_step_journal(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    tracking: dict[str, Any],
    operations: list[dict[str, Any]],
) -> Path | None:
    """Start the per-step JSONL journal before edit execution begins.

    Purpose: create a durable audit stream that mirrors the planned operations.
    Prevented risk: a mid-run crash should not erase the fact that step-level
    evidence was expected and which verification modes were in play.
    Next-step intent: keep the journal header authoritative for future replay or
    review tooling.
    """
    if not _step_journal_enabled(instruction_payload):
        return None
    path = _step_journal_artifact_path(metadata_dir)
    entry = {
        'event': 'journal_started',
        'logged_at': utc_now_iso(),
        'workflow_mode': _resolve_workflow_mode(instruction_payload),
        'runtime_lane': _resolve_runtime_lane(instruction_payload),
        'verification_modes': _collect_verification_modes(instruction_payload=instruction_payload, operations=operations),
        'operation_count': len(operations),
        'execution_run_id': tracking.get('execution_run_id'),
        'request_id': tracking.get('request_id'),
    }
    _append_jsonl_artifact(path, entry)
    return path



def _append_step_journal_step_item(*, step_journal_path: Path | None, item: dict[str, Any]) -> None:
    if step_journal_path is None or not isinstance(item, dict):
        return
    entry: dict[str, Any] = {
        'event': 'step',
        'logged_at': utc_now_iso(),
        'index': item.get('index'),
        'step_id': item.get('step_id') or f"step-{item.get('index')}",
        'op': item.get('op'),
        'step_role': item.get('step_role'),
        'step_purpose': item.get('step_purpose'),
        'risk_prevented': item.get('risk_prevented'),
        'next_step_intent': item.get('next_step_intent'),
        'changed': item.get('changed'),
        'matches': item.get('matches'),
        'allow_zero_match': item.get('allow_zero_match'),
        'verification_mode': _extract_operation_verification_mode(item),
        'needs_visual_verification': item.get('needs_visual_verification') if isinstance(item.get('needs_visual_verification'), bool) else None,
    }
    if isinstance(item.get('state'), str) and item.get('state').strip():
        entry['state'] = item.get('state').strip()
    if item.get('detail'):
        entry['detail'] = str(item.get('detail'))
    _append_jsonl_artifact(
        step_journal_path,
        {key: value for key, value in entry.items() if value is not None},
    )



def _append_step_journal_steps(
    *,
    step_journal_path: Path | None,
    summary: list[dict[str, Any]],
) -> list[str]:
    observed_modes = _collect_verification_modes(summary=summary)
    if step_journal_path is None:
        return observed_modes
    for item in summary:
        _append_step_journal_step_item(step_journal_path=step_journal_path, item=item)
    return observed_modes



def _append_step_journal_terminal_event(
    *,
    step_journal_path: Path | None,
    state: str,
    detail: str | None = None,
    verification_modes: list[str] | None = None,
) -> None:
    if step_journal_path is None:
        return
    payload: dict[str, Any] = {
        'event': 'journal_finished',
        'logged_at': utc_now_iso(),
        'state': state,
    }
    if detail:
        payload['detail'] = detail
    if verification_modes:
        payload['verification_modes'] = verification_modes
    _append_jsonl_artifact(step_journal_path, payload)



def _action_evidence_dir(metadata_dir: Path) -> Path:
    return metadata_dir / 'action-evidence'



def _action_evidence_index_path(metadata_dir: Path) -> Path:
    return _action_evidence_dir(metadata_dir) / 'index.json'



def _action_evidence_enabled(instruction_payload: dict[str, Any]) -> bool:
    metadata = _instruction_metadata(instruction_payload)
    action_evidence = metadata.get('action_evidence') if isinstance(metadata.get('action_evidence'), dict) else {}
    if isinstance(action_evidence.get('enabled'), bool):
        return bool(action_evidence.get('enabled'))
    return _resolve_workflow_mode(instruction_payload) == 'interactive'



def _slugify_action_evidence_token(value: Any, *, default: str) -> str:
    text = re.sub(r'[^0-9A-Za-z._-]+', '-', str(value or '').strip())
    text = text.strip('-._')
    return text or default



def _action_evidence_artifact_path(metadata_dir: Path, item: dict[str, Any]) -> Path:
    index = item.get('index')
    try:
        normalized_index = int(index)
    except Exception:
        normalized_index = 0
    step_id = _slugify_action_evidence_token(item.get('step_id'), default=f'step-{normalized_index or "unknown"}')
    op_name = _slugify_action_evidence_token(item.get('op'), default='action')
    return _action_evidence_dir(metadata_dir) / f'{normalized_index:04d}-{step_id}-{op_name}.json'



def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None



def _summarize_action_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        'total': 0,
        'succeeded': 0,
        'failed': 0,
        'with_matches': 0,
        'changed': 0,
        'zero_match': 0,
        'allow_zero_match': 0,
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        counts['total'] += 1
        state = str(item.get('state') or 'succeeded').strip().lower()
        if state == 'failed':
            counts['failed'] += 1
        else:
            counts['succeeded'] += 1

        matches = _coerce_int(item.get('matches'))
        if matches is not None and matches > 0:
            counts['with_matches'] += 1
        if bool(item.get('changed')) or (matches is not None and matches > 0):
            counts['changed'] += 1
        if bool(item.get('allow_zero_match')):
            counts['allow_zero_match'] += 1
        elif state != 'failed' and matches is not None and matches <= 0:
            counts['zero_match'] += 1
    return counts



def _build_action_tracker_state(
    *,
    instruction_payload: dict[str, Any],
    metadata_dir: Path,
    tracking: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    enabled = _action_evidence_enabled(instruction_payload)
    state: dict[str, Any] = {
        'enabled': enabled,
        'metadata_dir': metadata_dir,
        'tracking': dict(tracking),
        'workflow_mode': _resolve_workflow_mode(instruction_payload),
        'runtime_lane': _resolve_runtime_lane(instruction_payload),
        'expected_count': len(operations),
        'entries': [],
        'index_path': _action_evidence_index_path(metadata_dir),
    }
    if enabled:
        index_path = Path(state['index_path'])
        write_json_artifact(
            index_path,
            {
                'schema_version': 'action-evidence-index/v1',
                **tracking,
                'workflow_mode': state['workflow_mode'],
                'runtime_lane': state['runtime_lane'],
                'updated_at': utc_now_iso(),
                'expected_action_count': len(operations),
                'action_counts': _summarize_action_counts([]),
                'actions': [],
                'last_action': None,
            },
        )
    return state



def _record_action_step(
    *,
    action_tracker_state: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    metadata_dir = Path(action_tracker_state['metadata_dir'])
    tracking = action_tracker_state.get('tracking') if isinstance(action_tracker_state.get('tracking'), dict) else {}
    normalized_item = dict(item)
    normalized_item.setdefault('state', 'succeeded')
    normalized_item['step_id'] = normalized_item.get('step_id') or f"step-{normalized_item.get('index')}"

    artifact_path: Path | None = None
    if bool(action_tracker_state.get('enabled')):
        artifact_path = _action_evidence_artifact_path(metadata_dir, normalized_item)
        write_json_artifact(
            artifact_path,
            {
                'schema_version': 'action-evidence/v1',
                **tracking,
                'workflow_mode': action_tracker_state.get('workflow_mode'),
                'runtime_lane': action_tracker_state.get('runtime_lane'),
                'logged_at': utc_now_iso(),
                'index': normalized_item.get('index'),
                'step_id': normalized_item.get('step_id'),
                'op': normalized_item.get('op'),
                'state': normalized_item.get('state'),
                'matches': normalized_item.get('matches'),
                'changed': normalized_item.get('changed'),
                'allow_zero_match': normalized_item.get('allow_zero_match'),
                'summary': {
                    key: value
                    for key, value in normalized_item.items()
                    if key != 'operation'
                },
                'operation': normalized_item.get('operation') if isinstance(normalized_item.get('operation'), dict) else None,
            },
        )

    entry = {
        'index': normalized_item.get('index'),
        'step_id': normalized_item.get('step_id'),
        'op': normalized_item.get('op'),
        'state': normalized_item.get('state'),
        'matches': normalized_item.get('matches'),
        'changed': normalized_item.get('changed'),
        'allow_zero_match': normalized_item.get('allow_zero_match'),
        'artifact_path': str(artifact_path) if artifact_path is not None else None,
        'logged_at': utc_now_iso(),
    }
    action_tracker_state.setdefault('entries', []).append(entry)

    if bool(action_tracker_state.get('enabled')):
        index_path = Path(action_tracker_state['index_path'])
        write_json_artifact(
            index_path,
            {
                'schema_version': 'action-evidence-index/v1',
                **tracking,
                'workflow_mode': action_tracker_state.get('workflow_mode'),
                'runtime_lane': action_tracker_state.get('runtime_lane'),
                'updated_at': utc_now_iso(),
                'expected_action_count': action_tracker_state.get('expected_count'),
                'action_counts': _summarize_action_counts(action_tracker_state.get('entries') or []),
                'actions': list(action_tracker_state.get('entries') or []),
                'last_action': entry,
            },
        )

    return entry



def _record_apply_step(
    *,
    log_path: Path,
    step_journal_path: Path | None,
    action_tracker_state: dict[str, Any],
    item: dict[str, Any],
    runtime_scaffolding: dict[str, Any] | None,
    native_capabilities: dict[str, Any] | None,
) -> None:
    """Mirror one applied edit into evidence artifacts and runtime status.

    Purpose: keep step journal, action evidence, and runtime status advancing in
    lock-step for each applied edit.
    Prevented risk: review artifacts can disagree about the last executed step if
    callers update them independently.
    Next-step intent: route any per-step evidence expansion through this single
    checkpoint.
    """
    _append_step_journal_step_item(step_journal_path=step_journal_path, item=item)
    last_action = _record_action_step(action_tracker_state=action_tracker_state, item=item)
    action_counts = _summarize_action_counts(action_tracker_state.get('entries') or [])
    extra = {
        **(runtime_scaffolding or {}),
        'operation_count': action_tracker_state.get('expected_count'),
        'action_counts': action_counts,
        'last_action': last_action,
    }
    if bool(action_tracker_state.get('enabled')):
        extra['action_evidence_index_path'] = str(action_tracker_state.get('index_path'))
    if native_capabilities is not None:
        extra['native_capabilities'] = native_capabilities
    update_runtime_status(
        log_path,
        phase='apply_edit_operations',
        detail=f"step {last_action.get('index')}/{action_tracker_state.get('expected_count')}: {last_action.get('op')}",
        extra=extra,
        append_history=False,
    )



def _read_gui_edit_scaffolding(metadata_dir: Path, instruction_payload: dict[str, Any]) -> dict[str, Any]:
    edit_summary = _load_json_artifact(metadata_dir / 'edit_summary.json') or {}
    summary = edit_summary.get('operations') if isinstance(edit_summary.get('operations'), list) else None
    payload = _build_gui_edit_scaffolding(
        instruction_payload=instruction_payload,
        metadata_dir=metadata_dir,
        summary=summary,
    )
    if isinstance(edit_summary.get('verification_modes'), list):
        verification_modes = [
            mode
            for mode in (_normalize_step_verification_mode(item) for item in edit_summary.get('verification_modes', []))
            if mode is not None
        ]
        if verification_modes:
            payload['verification_modes'] = verification_modes
    has_explicit_workflow_mode = bool(
        isinstance(edit_summary.get('workflow_mode'), str)
        and edit_summary.get('workflow_mode').strip()
    )
    if has_explicit_workflow_mode:
        payload['workflow_mode'] = _normalize_workflow_mode(edit_summary['workflow_mode'])
        payload['runtime_lane'] = _workflow_mode_to_runtime_lane(payload['workflow_mode'], default=payload['runtime_lane'])
    if isinstance(edit_summary.get('runtime_lane'), str) and edit_summary.get('runtime_lane').strip():
        if has_explicit_workflow_mode:
            payload['runtime_lane'] = _workflow_mode_to_runtime_lane(payload['workflow_mode'], default=payload['runtime_lane'])
        else:
            payload['runtime_lane'] = edit_summary['runtime_lane'].strip()
            payload['workflow_mode'] = _workflow_mode_from_runtime_lane(payload['runtime_lane']) or payload['workflow_mode']
    return payload
