from __future__ import annotations

"""Runtime-state helpers for the Windows HWPX worker.

Purpose: keep status/artifact plumbing separate from COM-heavy editing logic.
Prevented risk: mixed concerns in ``worker.py`` make it easy to change metadata
behavior while touching Hancom automation.
Next-step intent: centralize other worker-safe artifact helpers here once this
first extraction proves stable.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for worker artifacts and status updates."""
    return datetime.now(timezone.utc).isoformat()


def write_json_artifact(path: Path, payload: object) -> None:
    """Write structured artifacts with parent creation handled in one place.

    Purpose: every worker artifact writer should serialize identically.
    Prevented risk: ad-hoc writes can forget directory creation or drift in JSON
    formatting, which makes evidence diffs harder to review.
    Next-step intent: keep all pure JSON artifact writes flowing through this
    helper so future schema changes stay mechanical.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')



def _load_json_artifact(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return data if isinstance(data, dict) else None



def _instruction_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    metadata = payload.get('metadata') if isinstance(payload, dict) else None
    return metadata if isinstance(metadata, dict) else {}


WORKFLOW_MODE_TO_RUNTIME_LANE = {
    'batch': '.50',
    'interactive': '.51',
}

RUNTIME_LANE_TO_WORKFLOW_MODE = {
    value: key
    for key, value in WORKFLOW_MODE_TO_RUNTIME_LANE.items()
}



def _normalize_workflow_mode(value: Any, *, default: str = 'batch') -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    if normalized in WORKFLOW_MODE_TO_RUNTIME_LANE:
        return normalized
    return default



def _normalize_runtime_lane(value: Any, *, default: str = '.50') -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip()
    if not normalized:
        return default
    workflow_mode = normalized.lower()
    if workflow_mode in WORKFLOW_MODE_TO_RUNTIME_LANE:
        return WORKFLOW_MODE_TO_RUNTIME_LANE[workflow_mode]
    return normalized



def _workflow_mode_from_runtime_lane(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_runtime_lane(value, default='')
    if not normalized:
        return None
    return RUNTIME_LANE_TO_WORKFLOW_MODE.get(normalized)



def _resolve_workflow_mode(instruction_payload: dict[str, Any], *, default: str = 'batch') -> str:
    metadata = _instruction_metadata(instruction_payload)
    value = metadata.get('workflow_mode')
    if isinstance(value, str) and value.strip():
        return _normalize_workflow_mode(value, default=default)
    for key in ('runtime_lane', 'execution_lane', 'lane'):
        resolved = _workflow_mode_from_runtime_lane(metadata.get(key))
        if resolved is not None:
            return resolved
    return default



def _workflow_mode_to_runtime_lane(value: Any, *, default: str = '.50') -> str:
    workflow_mode = _normalize_workflow_mode(value, default=RUNTIME_LANE_TO_WORKFLOW_MODE.get(default, 'batch'))
    return WORKFLOW_MODE_TO_RUNTIME_LANE.get(workflow_mode, default)



def _resolve_runtime_lane(instruction_payload: dict[str, Any], *, default: str = '.50') -> str:
    metadata = _instruction_metadata(instruction_payload)
    workflow_mode = metadata.get('workflow_mode')
    if isinstance(workflow_mode, str) and workflow_mode.strip():
        return _workflow_mode_to_runtime_lane(workflow_mode, default=default)
    for key in ('runtime_lane', 'execution_lane', 'lane'):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_runtime_lane(value, default=default)
    return default



def classify_runtime_stage(phase: str | None) -> str:
    """Collapse fine-grained phases into coarse failure-report stages.

    Purpose: failure summaries stay readable even when runtime phases become more
    detailed.
    Prevented risk: external tooling should not need to understand every worker
    phase name just to classify a failure artifact.
    Next-step intent: keep stage mapping here if new runtime phases are added.
    """
    if not phase:
        return 'resolution'
    if phase in {'load_instruction_payload'}:
        return 'resolution'
    if phase in {'apply_edit_operations', 'save_edited_hwpx', 'save_intermediate_edited_hwpx_for_pdf_export', 'post_serialization_proof'}:
        return 'apply'
    if phase in {'save_source_baseline_pdf', 'save_result_pdf', 'save_pdf'}:
        return 'render'
    if phase in {'validate_outputs'}:
        return 'qa'
    return 'resolution'



def read_runtime_status(job_dir: Path) -> dict[str, object] | None:
    path = job_dir / 'metadata' / 'runtime_status.json'
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None



def write_failure_artifacts(job_dir: Path, message: str) -> None:
    """Persist the human-readable failure bundle for a job directory.

    Purpose: every failure path should leave the same baseline breadcrumbs.
    Prevented risk: exception handling branches can diverge and omit the concise
    summary humans reach for first.
    Next-step intent: richer failure bundle fields can be added here without
    touching the COM execution path.
    """
    metadata_dir = job_dir / 'metadata'
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / 'failure.txt').write_text(message, encoding='utf-8')
    runtime_status = read_runtime_status(job_dir)
    phase = runtime_status.get('phase') if isinstance(runtime_status, dict) else None
    write_json_artifact(
        metadata_dir / 'failure_summary.json',
        {
            'stage': classify_runtime_stage(str(phase) if phase else None),
            'runtime_phase': phase,
            'message': message,
        },
    )



def write_failure_snapshot_artifacts(job_dir: Path) -> None:
    """Capture the latest runtime status and a short history tail on failure.

    Purpose: preserve nearby runtime evidence before later retries overwrite it.
    Prevented risk: operators lose the exact terminal state when subsequent work
    reuses the same job directory.
    Next-step intent: keep adding cheap forensic snapshots here, not inline in
    exception handlers.
    """
    metadata_dir = job_dir / 'metadata'
    metadata_dir.mkdir(parents=True, exist_ok=True)
    runtime_status = read_runtime_status(job_dir)
    if runtime_status is not None:
        write_json_artifact(metadata_dir / 'failure_runtime_status.json', runtime_status)
    history_path = metadata_dir / 'runtime_status.history.jsonl'
    if history_path.exists():
        lines = history_path.read_text(encoding='utf-8').splitlines()
        tail = lines[-30:]
        (metadata_dir / 'failure_runtime_status.history.jsonl').write_text(
            '\n'.join(tail) + ('\n' if tail else ''),
            encoding='utf-8',
        )



def get_runtime_status_path(log_path: Path) -> Path:
    return log_path.parent.parent / 'metadata' / 'runtime_status.json'



def get_runtime_status_history_path(log_path: Path) -> Path:
    return log_path.parent.parent / 'metadata' / 'runtime_status.history.jsonl'



def update_runtime_status(
    log_path: Path,
    *,
    phase: str,
    state: str = 'running',
    detail: str | None = None,
    extra: dict | None = None,
    append_history: bool = True,
) -> None:
    """Refresh the job runtime_status artifact and optional JSONL history.

    Purpose: status writes stay uniform across watchdog, apply, render, and
    failure paths.
    Prevented risk: partial status updates can accidentally drop the latest
    action counters/evidence pointer that operators still need.
    Next-step intent: if runtime status gains new sticky fields, extend the
    carry-forward list here rather than in each caller.
    """
    status_path = get_runtime_status_path(log_path)
    existing_status = _load_json_artifact(status_path) or {}
    payload: dict[str, object] = {
        'state': state,
        'phase': phase,
        'updated_at': utc_now_iso(),
    }
    for key in ('last_action', 'action_counts', 'action_evidence_index_path'):
        if key in existing_status:
            payload[key] = existing_status[key]
    if detail:
        payload['detail'] = detail
    if extra:
        payload.update(extra)

    history_path = get_runtime_status_history_path(log_path)
    write_json_artifact(status_path, payload)
    if append_history:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')



def format_last_phase(job_dir: Path) -> str | None:
    status = read_runtime_status(job_dir)
    if not status:
        return None
    phase = status.get('phase')
    updated_at = status.get('updated_at')
    detail = status.get('detail')
    parts = []
    if phase:
        parts.append(f'last phase={phase}')
    if updated_at:
        parts.append(f'updated_at={updated_at}')
    if detail:
        parts.append(f'detail={detail}')
    return ', '.join(parts) if parts else None
