from __future__ import annotations

"""Artifact and evidence synthesis helpers for completed converter jobs.

This module keeps the lazy "synthesize on first read" behavior out of the API
route file so the HTTP surface stays easier to scan. The route layer still owns
job lookup and endpoint wiring; this module owns metadata reconstruction.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.edit_ops import normalize_instruction_payload

settings = get_settings()

SYNTHESIZED_JOB_METADATA_FILENAMES = {
    'validation_artifact.json',
    'execution_result.json',
    'bundle_manifest.json',
    'evidence_event.json',
    'evidence_index.json',
}


def read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _append_jsonl(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + '\n')


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

SUPPORTED_STEP_VERIFICATION_MODES = (
    'text-local',
    'text-broader',
    'image',
)

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


def _workflow_mode_to_runtime_lane(value: Any, *, default: str = '.50') -> str:
    workflow_mode = _normalize_workflow_mode(value, default=RUNTIME_LANE_TO_WORKFLOW_MODE.get(default, 'batch'))
    return WORKFLOW_MODE_TO_RUNTIME_LANE.get(workflow_mode, default)


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
    if isinstance(operations, list):
        for item in operations:
            if isinstance(item, dict):
                _push(_extract_operation_verification_mode(item))
    return ordered


def _build_gui_edit_scaffolding_metadata(instruction_payload: dict[str, Any], metadata_dir: Path) -> dict[str, Any]:
    """Recover workflow metadata needed by synthesized execution artifacts."""
    edit_summary = read_json_if_exists(metadata_dir / 'edit_summary.json')
    summary_ops = edit_summary.get('operations') if isinstance(edit_summary, dict) and isinstance(edit_summary.get('operations'), list) else None
    verification_modes = _collect_verification_modes(
        instruction_payload=instruction_payload,
        operations=summary_ops,
    )
    if isinstance(edit_summary, dict) and isinstance(edit_summary.get('verification_modes'), list):
        explicit_modes = [
            mode
            for mode in (_normalize_step_verification_mode(item) for item in edit_summary.get('verification_modes', []))
            if mode is not None
        ]
        if explicit_modes:
            verification_modes = explicit_modes

    workflow_mode = _resolve_workflow_mode(instruction_payload)
    runtime_lane = _resolve_runtime_lane(instruction_payload)
    has_explicit_workflow_mode = bool(
        isinstance(edit_summary, dict)
        and isinstance(edit_summary.get('workflow_mode'), str)
        and edit_summary.get('workflow_mode').strip()
    )
    if has_explicit_workflow_mode:
        workflow_mode = _normalize_workflow_mode(edit_summary['workflow_mode'])
        runtime_lane = _workflow_mode_to_runtime_lane(workflow_mode, default=runtime_lane)
    if isinstance(edit_summary, dict) and isinstance(edit_summary.get('runtime_lane'), str) and edit_summary.get('runtime_lane').strip():
        if has_explicit_workflow_mode:
            runtime_lane = _workflow_mode_to_runtime_lane(workflow_mode, default=runtime_lane)
        else:
            runtime_lane = edit_summary['runtime_lane'].strip()
            workflow_mode = _workflow_mode_from_runtime_lane(runtime_lane) or workflow_mode

    payload: dict[str, Any] = {
        'workflow_mode': workflow_mode,
        'runtime_lane': runtime_lane,
        'verification_modes': verification_modes,
    }
    step_journal_path = metadata_dir / 'step_journal.jsonl'
    if step_journal_path.exists() or workflow_mode == 'interactive':
        payload['step_journal_path'] = str(step_journal_path)
    return payload


def _build_fixture_instruction_payload(instruction_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in instruction_payload.items()
        if key not in TRACKING_KEYS and value is not None
    }


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


def _job_path(job: dict[str, Any], key: str) -> Path | None:
    value = job.get(key)
    if not value:
        return None
    return Path(str(value))


def _file_sha256(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _build_tracking_fields(instruction_payload: dict[str, Any], metadata_dir: Path) -> dict[str, Any]:
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


def _evidence_root() -> Path:
    return settings.spool_root / 'evidence'


def _slugify_shape_key(value: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-')
    return cleaned[:120] or 'unknown-shape'


def _derive_shape_key(instruction_payload: dict[str, Any]) -> str:
    metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else {}
    confirm_policy = metadata.get('confirm_policy') if isinstance(metadata.get('confirm_policy'), dict) else {}
    policy_override = confirm_policy.get('policy_override') if isinstance(confirm_policy.get('policy_override'), dict) else {}
    explicit = policy_override.get('shape_key') if isinstance(policy_override.get('shape_key'), str) else None
    if explicit:
        return explicit
    operations = instruction_payload.get('operations') if isinstance(instruction_payload.get('operations'), list) else []
    op_names = [str(item.get('op', 'unknown')) for item in operations if isinstance(item, dict)]
    resolved_target_id = _pick_tracking_value(instruction_payload, 'resolved_target_id') or 'no-target'
    fingerprint = hashlib.sha256(json.dumps({'ops': op_names, 'target': resolved_target_id}, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:12]
    op_prefix = '+'.join(op_names[:3]) if op_names else 'no-ops'
    return f'{op_prefix}:{fingerprint}'


def _build_render_evidence_digest(
    *,
    qa_status: str,
    report: dict[str, Any] | None,
    instruction_payload: dict[str, Any],
    tracking: dict[str, Any],
    pdf_path: str,
) -> dict[str, Any]:
    report = report if isinstance(report, dict) else {}
    metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else {}
    touched_ranges = metadata.get('touched_ranges') if isinstance(metadata.get('touched_ranges'), list) else []
    execution_mode = metadata.get('execution_mode')
    render_diff = report.get('render_diff') if isinstance(report.get('render_diff'), dict) else {}
    render_diff_regions = report.get('render_diff_regions') if isinstance(report.get('render_diff_regions'), dict) else {}
    changed_pages = render_diff_regions.get('changed_pages') if isinstance(render_diff_regions.get('changed_pages'), list) else []
    checks = report.get('checks') if isinstance(report.get('checks'), list) else []
    failed_checks = [check.get('name') for check in checks if isinstance(check, dict) and not bool(check.get('passed'))]
    page_count_ok = not any(name in {'page_count', 'page_budget', 'page_increase'} for name in failed_checks)
    table_integrity_ok = not any(name in {'table_integrity', 'table_structure', 'table_count'} for name in failed_checks)
    wording_lock_ok = not any(name in {'wording_lock', 'text_lock', 'content_regression'} for name in failed_checks)
    hierarchy_residual = 'none' if not any(name in {'list_hierarchy', 'hierarchy'} for name in failed_checks) else 'present'
    table_residual = 'none' if table_integrity_ok else 'present'
    wrap_residual = 'small' if any(name in {'render_diff', 'wrap', 'orphan_wrap'} for name in failed_checks) else 'none'
    style_bleed_residual = 'none' if not any(name in {'style_bleed', 'style_regression'} for name in failed_checks) else 'present'
    changed_pixel_ratio = float(render_diff.get('changed_pixel_ratio') or 0.0) if isinstance(render_diff.get('changed_pixel_ratio'), (int, float)) else 0.0
    if qa_status == 'fail' or not page_count_ok or not table_integrity_ok or not wording_lock_ok:
        writer_gate_hint = 'fail_review'
    elif failed_checks or changed_pixel_ratio > 0.01:
        writer_gate_hint = 'warn_review'
    else:
        writer_gate_hint = 'pass_candidate'

    if qa_status == 'fail' or changed_pixel_ratio > 0.05:
        collateral_drift = 'large'
    elif failed_checks or changed_pixel_ratio > 0.01:
        collateral_drift = 'small'
    else:
        collateral_drift = 'none'

    residual_digest = {
        'failed_check_count': len(failed_checks),
        'failed_checks': failed_checks[:6],
        'hierarchy_residual': hierarchy_residual,
        'table_residual': table_residual,
        'wrap_residual': wrap_residual,
        'style_bleed_residual': style_bleed_residual,
        'collateral_drift': collateral_drift,
        'render_diff_summary': render_diff,
    }
    changed_page_numbers: list[int] = []
    changed_page_preview = []
    for item in changed_pages[:5]:
        if isinstance(item, dict):
            if isinstance(item.get('page'), int):
                changed_page_numbers.append(int(item.get('page')))
            changed_page_preview.append({
                'page': item.get('page'),
                'change_ratio': item.get('change_ratio'),
                'severity': item.get('severity'),
            })
        else:
            if isinstance(item, int):
                changed_page_numbers.append(item)
            changed_page_preview.append({'page': item})
    touched_ranges_digest = [
        {
            'section_key': item.get('section_key'),
            'resolved_target_id': item.get('resolved_target_id'),
            'mode': execution_mode,
            'edit_shape': item.get('edit_shape'),
        }
        for item in touched_ranges[:8]
        if isinstance(item, dict)
    ]
    return {
        'schema_version': 'render-evidence-digest/v1',
        'request_id': tracking.get('request_id'),
        'intent_id': tracking.get('intent_id'),
        'execution_run_id': tracking.get('execution_run_id'),
        'validation_run_id': tracking.get('validation_run_id'),
        'inspect_snapshot_id': tracking.get('inspect_snapshot_id'),
        'execution_mode': execution_mode,
        'writer_gate_hint': writer_gate_hint,
        'summary': {
            'changed_page_count': len(changed_page_numbers) or len(changed_pages),
            'changed_pages': changed_page_numbers[:8],
            'page_count_ok': page_count_ok,
            'table_integrity_ok': table_integrity_ok,
            'wording_lock_ok': wording_lock_ok,
            'hierarchy_residual': hierarchy_residual,
            'table_residual': table_residual,
            'wrap_residual': wrap_residual,
            'style_bleed_residual': style_bleed_residual,
            'collateral_drift': collateral_drift,
        },
        'touched_ranges_digest': touched_ranges_digest,
        'residual_digest': residual_digest,
        'artifacts': {
            'pdf_path': pdf_path,
            'changed_page_preview_paths': [],
        },
        'changed_page_preview': changed_page_preview,
    }


def _update_shape_summary(shape_key: str, event: dict[str, Any]) -> dict[str, Any]:
    root = _evidence_root()
    path = root / 'shapes' / f'{_slugify_shape_key(shape_key)}.json'
    summary = read_json_if_exists(path)
    if not isinstance(summary, dict):
        summary = {
            'schema_version': 'shape-evidence-summary/v1',
            'shape_key': shape_key,
            'total_runs': 0,
            'qa_pass_runs': 0,
            'qa_fail_runs': 0,
            'writer_gate_hint_counts': {'pass_candidate': 0, 'warn_review': 0, 'fail_review': 0},
            'last_event': None,
        }
    summary['total_runs'] = int(summary.get('total_runs', 0)) + 1
    qa_status = event.get('qa_status')
    if qa_status == 'pass':
        summary['qa_pass_runs'] = int(summary.get('qa_pass_runs', 0)) + 1
    elif qa_status == 'fail':
        summary['qa_fail_runs'] = int(summary.get('qa_fail_runs', 0)) + 1
    writer_gate_hint = event.get('render_evidence_digest', {}).get('writer_gate_hint')
    counts = summary.get('writer_gate_hint_counts') if isinstance(summary.get('writer_gate_hint_counts'), dict) else {}
    if writer_gate_hint in {'pass_candidate', 'warn_review', 'fail_review'}:
        counts[writer_gate_hint] = int(counts.get(writer_gate_hint, 0)) + 1
    summary['writer_gate_hint_counts'] = counts
    summary['last_event'] = {
        'job_id': event.get('job_id'),
        'execution_run_id': event.get('execution_run_id'),
        'qa_status': qa_status,
        'decision': event.get('decision'),
        'writer_gate_hint': writer_gate_hint,
    }
    write_json(path, summary)
    return summary


def _write_evidence_event(
    job: dict[str, Any],
    metadata_dir: Path,
    *,
    instruction_payload: dict[str, Any],
    validation_artifact: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist event-level evidence once execution-facing artifacts exist."""
    metadata = instruction_payload.get('metadata') if isinstance(instruction_payload.get('metadata'), dict) else {}
    confirm_policy = metadata.get('confirm_policy') if isinstance(metadata.get('confirm_policy'), dict) else {}
    approval_packet = metadata.get('approval_packet') if isinstance(metadata.get('approval_packet'), dict) else {}
    qa_status = validation_artifact.get('qa_status') if isinstance(validation_artifact, dict) else 'unknown'
    tracking = _build_tracking_fields(instruction_payload, metadata_dir)
    gui_edit_scaffolding = _build_gui_edit_scaffolding_metadata(instruction_payload, metadata_dir)
    render_evidence_digest = None
    if isinstance(validation_artifact, dict):
        render_evidence_digest = validation_artifact.get('render_evidence_digest')
    if not isinstance(render_evidence_digest, dict):
        render_evidence_digest = _build_render_evidence_digest(
            qa_status=qa_status,
            report=read_json_if_exists(metadata_dir / 'validation_report.json'),
            instruction_payload=instruction_payload,
            tracking=tracking,
            pdf_path=str(_job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'),
        )
    shape_key = _derive_shape_key(instruction_payload)
    event = {
        'schema_version': 'evidence-event/v1',
        'job_id': job.get('job_id'),
        'task_type': job.get('task_type'),
        'shape_key': shape_key,
        'execution_run_id': _pick_tracking_value(instruction_payload, 'execution_run_id'),
        'validation_run_id': _pick_tracking_value(instruction_payload, 'validation_run_id'),
        'resolved_target_id': _pick_tracking_value(instruction_payload, 'resolved_target_id'),
        'decision': confirm_policy.get('decision'),
        'decision_reason': confirm_policy.get('decision_reason'),
        'policy_override': confirm_policy.get('policy_override', {}),
        'risk_flags': approval_packet.get('risk_flags', []),
        'execution_mode': metadata.get('execution_mode'),
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'qa_status': qa_status,
        'render_evidence_digest': render_evidence_digest,
        'artifacts': execution_result.get('artifacts') if isinstance(execution_result, dict) else {},
    }
    write_json(metadata_dir / 'evidence_event.json', event)
    _append_jsonl(_evidence_root() / 'events.jsonl', event)
    summary = _update_shape_summary(shape_key, event)
    evidence_index = {
        'schema_version': 'evidence-index/v1',
        'shape_key': shape_key,
        'event_path': str(metadata_dir / 'evidence_event.json'),
        'summary_path': str(_evidence_root() / 'shapes' / f'{_slugify_shape_key(shape_key)}.json'),
    }
    write_json(metadata_dir / 'evidence_index.json', evidence_index)
    return {'event': event, 'summary': summary, 'index': evidence_index}


def _synthesize_validation_artifact(job: dict[str, Any], metadata_dir: Path) -> dict[str, Any] | None:
    """Rebuild validation_artifact.json from durable metadata when the file is absent."""
    report_path = metadata_dir / 'validation_report.json'
    report = read_json_if_exists(report_path)
    if not isinstance(report, dict):
        return None

    instructions_path = _job_path(job, 'instructions_path')
    instruction_payload = {'operations': [], 'validation': {}}
    if instructions_path and instructions_path.exists():
        raw_payload = read_json_if_exists(instructions_path)
        if raw_payload is not None:
            instruction_payload = normalize_instruction_payload(raw_payload)
    tracking = _build_tracking_fields(instruction_payload, metadata_dir)
    gui_edit_scaffolding = _build_gui_edit_scaffolding_metadata(instruction_payload, metadata_dir)

    checks = report.get('checks') if isinstance(report.get('checks'), list) else []
    passed_flags = [bool(check.get('passed')) for check in checks if isinstance(check, dict)]
    qa_status = 'partial'
    if checks:
        qa_status = 'pass' if passed_flags and all(passed_flags) else 'fail'

    page_count = None
    for check in checks:
        if isinstance(check, dict) and check.get('result_page_count') is not None:
            page_count = check.get('result_page_count')

    qa_evidence = {
        'pdf_path': str(_job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'),
        'page_count': page_count,
        'issues': [
            {
                'check': check.get('name'),
                'detail': check,
            }
            for check in checks
            if isinstance(check, dict) and not bool(check.get('passed'))
        ],
        'warning_badges': report.get('warning_badges', []) if isinstance(report.get('warning_badges'), list) else [],
        'render_diff': report.get('render_diff') if isinstance(report.get('render_diff'), dict) else None,
        'render_diff_regions': report.get('render_diff_regions') if isinstance(report.get('render_diff_regions'), dict) else None,
        'compile_warning_badges': report.get('compile_warning_badges', []) if isinstance(report.get('compile_warning_badges'), list) else [],
    }
    render_evidence_digest = _build_render_evidence_digest(
        qa_status=qa_status,
        report=report,
        instruction_payload=instruction_payload,
        tracking=tracking,
        pdf_path=str(_job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'),
    )

    artifact = {
        'schema_version': 'validation-artifact/v1',
        'stage': 'qa',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'input': {
            'hwpx_path': str(_job_path(job, 'edited_output_path') or metadata_dir.parent / 'output' / 'edited.hwpx'),
            'source_pdf_path': str(metadata_dir / 'source_baseline.pdf') if (metadata_dir / 'source_baseline.pdf').exists() else None,
        },
        'output': {
            'pdf_path': qa_evidence['pdf_path'],
        },
        'qa_status': qa_status,
        'qa_summary': {
            'checks': checks,
            'validation': report.get('validation', {}),
        },
        'qa_evidence': qa_evidence,
        'render_evidence_digest': render_evidence_digest,
        'artifacts': {
            'render_qa_report_path': str(report_path),
            'step_journal_path': gui_edit_scaffolding.get('step_journal_path'),
        },
    }
    write_json(metadata_dir / 'validation_artifact.json', artifact)
    return artifact


def _synthesize_execution_result(job: dict[str, Any], metadata_dir: Path) -> dict[str, Any] | None:
    """Rebuild execution_result.json and downstream evidence from job metadata."""
    instructions_path = _job_path(job, 'instructions_path')
    if instructions_path is None or not instructions_path.exists():
        return None
    instruction_payload = normalize_instruction_payload(json.loads(instructions_path.read_text(encoding='utf-8')))
    tracking = _build_tracking_fields(instruction_payload, metadata_dir)
    gui_edit_scaffolding = _build_gui_edit_scaffolding_metadata(instruction_payload, metadata_dir)
    validation_artifact_path = metadata_dir / 'validation_artifact.json'
    validation_artifact = read_json_if_exists(validation_artifact_path)
    render_evidence_digest = (
        validation_artifact.get('render_evidence_digest')
        if isinstance(validation_artifact, dict) and isinstance(validation_artifact.get('render_evidence_digest'), dict)
        else _build_render_evidence_digest(
            qa_status=validation_artifact.get('qa_status', 'unknown') if isinstance(validation_artifact, dict) else 'unknown',
            report=read_json_if_exists(metadata_dir / 'validation_report.json'),
            instruction_payload=instruction_payload,
            tracking=tracking,
            pdf_path=str(_job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'),
        )
    )

    artifact = {
        'schema_version': 'execution-result/v1',
        'stage': 'execution_completed',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'render_evidence_digest': render_evidence_digest,
        'artifacts': {
            'edited_hwpx_path': str(_job_path(job, 'edited_output_path') or metadata_dir.parent / 'output' / 'edited.hwpx'),
            'pdf_path': str(_job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'),
            'edit_summary_path': str(metadata_dir / 'edit_summary.json'),
            'validation_report_path': str(metadata_dir / 'validation_report.json'),
            'validation_artifact_path': str(validation_artifact_path),
            'render_diff_regions_path': str(metadata_dir / 'render_diff_regions.json'),
            'runtime_status_path': str(metadata_dir / 'runtime_status.json'),
            'step_journal_path': gui_edit_scaffolding.get('step_journal_path'),
        },
    }
    write_json(metadata_dir / 'execution_result.json', artifact)
    _write_evidence_event(
        job,
        metadata_dir,
        instruction_payload=instruction_payload,
        validation_artifact=validation_artifact if isinstance(validation_artifact, dict) else None,
        execution_result=artifact,
    )
    return artifact


def _synthesize_bundle_manifest(job: dict[str, Any], metadata_dir: Path) -> dict[str, Any] | None:
    instructions_path = _job_path(job, 'instructions_path')
    if instructions_path is None or not instructions_path.exists():
        return None
    instruction_payload = normalize_instruction_payload(json.loads(instructions_path.read_text(encoding='utf-8')))
    tracking = _build_tracking_fields(instruction_payload, metadata_dir)
    gui_edit_scaffolding = _build_gui_edit_scaffolding_metadata(instruction_payload, metadata_dir)
    fixture_payload = _build_fixture_instruction_payload(instruction_payload)
    fixture_payload_path = metadata_dir / 'fixture_instruction_payload.json'
    write_json(fixture_payload_path, fixture_payload)
    edited_output_path = _job_path(job, 'edited_output_path') or metadata_dir.parent / 'output' / 'edited.hwpx'
    pdf_output_path = _job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'

    manifest = {
        'schema_version': 'bundle-manifest/v1',
        **tracking,
        'workflow_mode': gui_edit_scaffolding['workflow_mode'],
        'runtime_lane': gui_edit_scaffolding['runtime_lane'],
        'verification_modes': gui_edit_scaffolding.get('verification_modes', []),
        'fixture_identity': {
            'instructions_path': str(fixture_payload_path),
            'instructions_sha256': _file_sha256(fixture_payload_path),
        },
        'outputs': {
            'edited_hwpx_path': str(edited_output_path),
            'edited_hwpx_sha256': _file_sha256(edited_output_path),
            'pdf_path': str(pdf_output_path),
            'pdf_sha256': _file_sha256(pdf_output_path),
        },
        'evidence': {
            'evidence_event_path': str(metadata_dir / 'evidence_event.json'),
            'evidence_index_path': str(metadata_dir / 'evidence_index.json'),
        },
        'artifacts': {
            'runtime_status_path': str(metadata_dir / 'runtime_status.json'),
            'step_journal_path': gui_edit_scaffolding.get('step_journal_path'),
        },
    }
    write_json(metadata_dir / 'bundle_manifest.json', manifest)
    return manifest


def _can_synthesize_execution_artifacts(job: dict[str, Any], metadata_dir: Path) -> bool:
    runtime_status = read_json_if_exists(metadata_dir / 'runtime_status.json')
    if isinstance(runtime_status, dict):
        runtime_state = str(runtime_status.get('state') or '').strip().lower()
        runtime_phase = str(runtime_status.get('phase') or '').strip().lower()
        if runtime_state == 'failed' or runtime_phase == 'failed':
            return False

    edited_output_path = _job_path(job, 'edited_output_path') or metadata_dir.parent / 'output' / 'edited.hwpx'
    pdf_output_path = _job_path(job, 'output_path') or metadata_dir.parent / 'output' / 'result.pdf'
    validation_report_path = metadata_dir / 'validation_report.json'
    validation_artifact_path = metadata_dir / 'validation_artifact.json'

    return any(
        path.exists()
        for path in (edited_output_path, pdf_output_path, validation_report_path, validation_artifact_path)
    )


def _synthesize_artifact_if_possible(job: dict[str, Any], metadata_dir: Path, filename: str) -> dict[str, Any] | None:
    if filename in SYNTHESIZED_JOB_METADATA_FILENAMES:
        if not _can_synthesize_execution_artifacts(job, metadata_dir):
            return None
    if filename == 'validation_artifact.json':
        return _synthesize_validation_artifact(job, metadata_dir)
    if filename == 'execution_result.json':
        if not (metadata_dir / 'validation_artifact.json').exists():
            _synthesize_validation_artifact(job, metadata_dir)
        return _synthesize_execution_result(job, metadata_dir)
    if filename == 'bundle_manifest.json':
        return _synthesize_bundle_manifest(job, metadata_dir)
    if filename == 'evidence_event.json':
        if not (metadata_dir / 'execution_result.json').exists():
            _synthesize_execution_result(job, metadata_dir)
        return read_json_if_exists(metadata_dir / 'evidence_event.json')
    if filename == 'evidence_index.json':
        if not (metadata_dir / 'execution_result.json').exists():
            _synthesize_execution_result(job, metadata_dir)
        return read_json_if_exists(metadata_dir / 'evidence_index.json')
    return None


def load_job_metadata_json(job: dict[str, Any], filename: str, not_found_detail: str) -> JSONResponse:
    """Load a job metadata file and lazily synthesize derived artifacts on demand."""
    metadata_dir = Path(job['job_dir']) / 'metadata'
    path = metadata_dir / filename
    if filename in SYNTHESIZED_JOB_METADATA_FILENAMES:
        if not _can_synthesize_execution_artifacts(job, metadata_dir):
            raise HTTPException(status_code=404, detail=not_found_detail)
    if not path.exists():
        synthesized = _synthesize_artifact_if_possible(job, path.parent, filename)
        if synthesized is None:
            raise HTTPException(status_code=404, detail=not_found_detail)
        return JSONResponse(content=synthesized)
    return JSONResponse(content=json.loads(path.read_text(encoding='utf-8')))
