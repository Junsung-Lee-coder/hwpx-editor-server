from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from app.config import get_settings
from app.edit_ops import EditOperationError, SUPPORTED_OPS, SUPPORTED_VALIDATION_KEYS, normalize_instruction_payload, normalize_validation
from app.interactive_session_manager import (
    VERIFY_EVIDENCE_HARD_STALE_AGE_MS,
    VERIFY_EVIDENCE_SOFT_WARN_AGE_MS,
    InteractiveSessionError,
    InteractiveSessionManager,
)
from app.logging_utils import configure_logger
from app.local_cli_router import build_local_cli_router
from app.local_cli_service import LocalCliService
from app.command_packages.runtime import get_command_package_registry

from app.models import (
    AuthorAndConvertFromPathRequest,
    CompileAuthoringFromPathRequest,
    ConfirmActionRequest,
    ConfirmStateResponse,
    ConvertResponse,
    EditAndConvertFromPathRequest,
    ErrorResponse,
    HealthResponse,
    InteractiveApplyRequest,
    InteractiveChooseRequest,
    InteractiveCloseRequest,
    InteractiveEnterRequest,
    InteractiveFindRequest,
    InteractiveLockRequest,
    InteractiveSessionOpenRequest,
    InteractiveSessionResponse,
    InteractiveUndoRequest,
    InteractiveVerificationRequest,
    JobStatus,
    JobStatusResponse,
)
from app.observation import ensure_viewer_session, latest_frame_metadata_path, latest_frame_path, load_viewer_session
from app.queue_db import QueueDB
from app.readiness import (
    build_plain_readiness_failure,
    load_runtime_readiness_snapshot,
    readiness_matches_current_worker,
    resolve_candidate_generation,
)
from app.services.job_artifacts import (
    load_job_metadata_json as load_job_artifact_metadata_json,
    read_json_if_exists as _read_json_if_exists,
    write_json as _write_json,
)
from app.template_engine import (
    _merge_instruction_proof_metadata,
    TemplateEngineError,
    build_clear_placeholders_payload,
    compile_authoring_payload,
    compile_placeholder_fill_payload,
    normalize_policy_override,
    parse_cleanup_placeholders,
    parse_validation_json,
)

settings = get_settings()
db = QueueDB(settings.db_path)
logger = configure_logger('hwp.api', settings.log_level, settings.logs_root / 'api.log')
interactive_sessions = InteractiveSessionManager(settings)
app = FastAPI(title='Windows HWPX Converter Pilot', version='0.2.0')
local_cli_service = LocalCliService(settings=settings, interactive_sessions=interactive_sessions)
app.include_router(
    build_local_cli_router(
        settings=settings,
        interactive_sessions=interactive_sessions,
        service=local_cli_service,
    )
)

ensure_viewer_session()


def require_runtime_readiness_or_503(task_label: str) -> dict[str, Any]:
    snapshot = load_runtime_readiness_snapshot()
    if not readiness_matches_current_worker(
        snapshot,
        candidate_generation=resolve_candidate_generation(),
    ):
        raise HTTPException(status_code=503, detail=build_plain_readiness_failure(task_label))
    return snapshot


def build_server_revision() -> str:
    paths = [
        Path(__file__),
        Path(__file__).with_name('edit_ops.py'),
        Path(__file__).with_name('native_actions.py'),
        Path(__file__).with_name('worker.py'),
        Path(__file__).with_name('template_engine.py'),
    ]
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(f'{path.name}:{path.stat().st_mtime_ns}')
        except FileNotFoundError:
            parts.append(f'{path.name}:missing')
    try:
        package_revision = get_command_package_registry().revision()
        if package_revision:
            parts.append(f'command_packages:{package_revision}')
    except Exception as exc:
        parts.append(f'command_packages:error:{type(exc).__name__}')
    return '|'.join(parts)


def get_job_or_404(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f'Job not found: {job_id}')
    return job


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

SUPPORTED_STEP_VERIFICATION_MODES = (
    'text-local',
    'text-broader',
    'image',
)


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


# Artifact/evidence reconstruction lives in app.services.job_artifacts so the
# route layer can stay focused on HTTP contracts while preserving the same
# lazy synthesis-on-read behavior for derived metadata files.


def load_job_metadata_json(job_id: str, filename: str, not_found_detail: str) -> JSONResponse:
    """Fetch a job metadata JSON document, synthesizing derived files on first read."""
    job = get_job_or_404(job_id)
    return load_job_artifact_metadata_json(job, filename, not_found_detail)


def load_job_runtime_status(job_id: str) -> JSONResponse:
    job = get_job_or_404(job_id)
    path = Path(job['job_dir']) / 'metadata' / 'runtime_status.json'
    if not path.exists():
        raise HTTPException(status_code=404, detail='Runtime status is not available for this job.')

    payload = json.loads(path.read_text(encoding='utf-8'))
    if (
        isinstance(payload, dict)
        and job.get('status') == JobStatus.succeeded.value
        and payload.get('state') != 'succeeded'
    ):
        payload = {
            **payload,
            'state': 'succeeded',
            'phase': 'finished',
            'detail': payload.get('detail') or 'normalized from succeeded job state',
        }
    return JSONResponse(content=payload)


async def save_upload_to_job(file: UploadFile, source_path: Path) -> int:
    size_bytes = 0
    with source_path.open('wb') as target:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > settings.max_upload_mb * 1024 * 1024:
                raise HTTPException(status_code=413, detail='Upload exceeds configured size limit.')
            target.write(chunk)
    if size_bytes == 0:
        raise HTTPException(status_code=400, detail='Empty upload is not allowed.')
    return size_bytes



def _parse_json_request_field(field_name: str, value: Any, *, allow_none: bool = False) -> Any:
    if value is None:
        if allow_none:
            return None
        raise HTTPException(status_code=400, detail=f'{field_name} is required')
    if isinstance(value, str):
        if not value.strip():
            if allow_none:
                return None
            raise HTTPException(status_code=400, detail=f'{field_name} must not be empty')
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f'invalid {field_name}: {exc}') from exc
    return value


def _is_absolute_source_path(value: str) -> bool:
    return Path(value).is_absolute() or ntpath.isabs(value)


def _resolve_source_path_input(source_path_value: str) -> tuple[Path, str, int]:
    source_path_text = str(source_path_value or '').strip()
    if not source_path_text:
        raise HTTPException(status_code=400, detail='source_path is required')
    if not _is_absolute_source_path(source_path_text):
        raise HTTPException(status_code=400, detail='source_path must be an absolute path on the worker machine')

    source_path = Path(source_path_text)
    suffix = source_path.suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )
    if not source_path.exists():
        raise HTTPException(status_code=400, detail=f'source_path does not exist: {source_path_text}')
    if not source_path.is_file():
        raise HTTPException(status_code=400, detail=f'source_path must point to a file: {source_path_text}')

    size_bytes = source_path.stat().st_size
    if size_bytes <= 0:
        raise HTTPException(status_code=400, detail=f'source_path is empty: {source_path_text}')
    return source_path, source_path.name, size_bytes


def _default_source_content_type(source_path: Path) -> str | None:
    if source_path.suffix.lower() == '.hwpx':
        return 'application/haansoft-hwpx'
    return None


@app.get('/health', response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status='ok',
        api_host=settings.api_host,
        api_port=settings.api_port,
        spool_root=str(settings.spool_root),
        db_path=str(settings.db_path),
        queue_depth=db.count_by_status(JobStatus.queued),
        running_jobs=db.count_by_status(JobStatus.running),
    )


@app.get('/runtime-readiness')
def runtime_readiness() -> JSONResponse:
    snapshot = load_runtime_readiness_snapshot()
    if snapshot is None:
        return JSONResponse({'ok': False, 'ready': False, 'detail': build_plain_readiness_failure('runtime')}, status_code=503)
    current = readiness_matches_current_worker(
        snapshot,
        candidate_generation=resolve_candidate_generation(),
    )
    status_code = 200 if current else 503
    if not current:
        snapshot = dict(snapshot)
        snapshot['ready'] = False
        snapshot['status'] = 'not_ready'
        snapshot['freshness_error'] = 'worker, candidate generation, or heartbeat is stale.'
    return JSONResponse(snapshot, status_code=status_code)


@app.get('/observation-viewer/session')
def observation_viewer_session() -> JSONResponse:
    return JSONResponse(ensure_viewer_session())


@app.get('/observation-viewer/frame/latest.png')
def observation_viewer_latest_frame() -> FileResponse:
    path = latest_frame_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail='Observation frame not available yet.')
    return FileResponse(path, media_type='image/png')


@app.get('/observation-viewer/frame/latest.json')
def observation_viewer_latest_frame_metadata() -> JSONResponse:
    path = latest_frame_metadata_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail='Observation frame metadata not available yet.')
    payload = json.loads(path.read_text(encoding='utf-8'))
    return JSONResponse(payload)


def _observation_stream_generator():
    last_bytes: bytes | None = None
    while True:
        frame_path = latest_frame_path()
        if frame_path.exists():
            payload = frame_path.read_bytes()
            if payload != last_bytes:
                last_bytes = payload
                yield b'--frame\r\nContent-Type: image/png\r\n\r\n' + payload + b'\r\n'
        time.sleep(1.0)


@app.get('/observation-viewer/stream.mjpg')
def observation_viewer_stream() -> StreamingResponse:
    return StreamingResponse(
        _observation_stream_generator(),
        media_type='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-store'},
    )


@app.get('/observation-viewer')
def observation_viewer_page() -> HTMLResponse:
    session = load_viewer_session() or ensure_viewer_session()
    html = f"""<!doctype html>
<html lang='en'>
  <head>
    <meta charset='utf-8' />
    <title>writer v1 observation viewer</title>
    <style>
      body {{ font-family: Arial, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 16px; }}
      .card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; }}
      img {{ width: 100%; max-width: 1200px; border: 1px solid #475569; border-radius: 8px; background: black; }}
      code {{ color: #93c5fd; }}
      .muted {{ color: #94a3b8; }}
      .ok {{ color: #86efac; }}
      .warn {{ color: #fbbf24; }}
    </style>
  </head>
  <body>
    <div class='card'>
      <h2>writer v1 observation viewer</h2>
      <p><strong>mode:</strong> strict view-only</p>
      <p><strong>remote control:</strong> disabled</p>
      <p><strong>public exposure:</strong> disabled by default</p>
      <p class='muted'>viewer_session_id=<code>{session.get('viewer_session_id')}</code></p>
      <p id='viewer-status' class='muted'>Connecting live stream…</p>
      <img id='viewer-image' src='/observation-viewer/stream.mjpg' alt='live Hancom observation stream' />
      <p class='muted'>If live streaming stays blank in this browser, the page will switch to an auto-refresh still frame every 5 seconds.</p>
    </div>
    <script>
      (() => {{
        const image = document.getElementById('viewer-image');
        const status = document.getElementById('viewer-status');
        const streamUrl = '/observation-viewer/stream.mjpg';
        const fallbackBaseUrl = '/observation-viewer/frame/latest.png';
        const refreshMs = 5000;
        let fallbackTimer = null;
        let usingFallback = false;
        let streamLoaded = false;

        function fallbackUrl() {{
          return `${{fallbackBaseUrl}}?ts=${{Date.now()}}`;
        }}

        function startFallback() {{
          if (usingFallback) return;
          usingFallback = true;
          image.src = fallbackUrl();
          status.textContent = 'Auto-refresh fallback active (latest still frame every 5 seconds).';
          status.className = 'warn';
          fallbackTimer = window.setInterval(() => {{
            image.src = fallbackUrl();
          }}, refreshMs);
        }}

        image.addEventListener('load', () => {{
          if (!usingFallback && !streamLoaded) {{
            streamLoaded = true;
            status.textContent = 'Live MJPG stream active.';
            status.className = 'ok';
          }}
        }}, {{ once: true }});

        image.addEventListener('error', startFallback);
        window.setTimeout(() => {{
          if (!streamLoaded) startFallback();
        }}, 4000);
      }})();
    </script>
  </body>
</html>
"""
    return HTMLResponse(html)


@app.get('/capabilities')
def capabilities() -> JSONResponse:
    payload = {
        'ok': True,
        'version': app.version,
        'server_revision': build_server_revision(),
        'pid': os.getpid(),
        'supported_ops': sorted(SUPPORTED_OPS),
        'supported_validation_keys': sorted(SUPPORTED_VALIDATION_KEYS),
        'supported_authoring_features': [
            'markdown_headings',
            'paragraph_blocks',
            'bullet_list',
            'numbered_list',
            'target_hint_directive',
            'placeholder_directive',
            'cleanup_placeholders',
            'clear_placeholders_endpoint',
            'placeholder_name_normalization',
            'placeholder_fill_compiler',
            'table_cell_placeholder_compiler',
            'template_map_mvp',
            'native_action_runtime',
            'native_only_directive',
            'cursor_snapshot_runtime',
            'table_cell_runtime',
            'native_list_directives_v1',
            'native_v2_scaffold',
            'confirm_policy_v1',
            'confirm_state_machine_v1',
            'confirm_brief_v1',
            'evidence_store_v1',
            'render_evidence_digest_v1',
            'direct_path_edit_endpoint',
            'direct_path_authoring_endpoint',
            'direct_path_compile_endpoint',
            'interactive_session_mvp',
            'interactive_session_state_store',
            'interactive_operator_telemetry',
            'interactive_verify_evidence_http_routes',
            'interactive_verify_evidence_trust_policy_v1',
            'interactive_verify_evidence_retention_v1',
        ],
        'interactive_session': {
            'enabled': True,
            'mode': 'single_session_mvp',
            'workflow_mode': 'interactive',
            'runtime_lane': '.51',
            'verify_evidence_policy': {
                'schema_version': 'verify-evidence-trust-policy/v1',
                'advisory_only': True,
                'soft_warn_age_ms': VERIFY_EVIDENCE_SOFT_WARN_AGE_MS,
                'hard_stale_age_ms': VERIFY_EVIDENCE_HARD_STALE_AGE_MS,
            },
            'verify_evidence_retention_policy': {
                'schema_version': 'interactive-verify-evidence-retention/v1',
                'cleanup_mode': 'delete_frozen_verify_evidence_after_terminal_session_ttl',
                'retention_days': interactive_sessions.verify_evidence_retention_days,
                'retention_anchor': 'session_closed_at',
                'active_session_exempt': True,
            },
            'commands': [
                'open',
                'status',
                'find',
                'choose',
                'enter',
                'lock',
                'verify-pre',
                'apply',
                'verify-post',
                'undo',
                'close',
            ],
            'operator_visible_fields': [
                'session_state',
                'command_progress',
                'verify_pre',
                'verify_post',
                'popup_status',
                'failure_reason',
                'operator_status_text',
            ],
        },
    }
    return JSONResponse(payload)


def _interactive_http_error(exc: InteractiveSessionError, *, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


_PRIVATE_INTERACTIVE_PATH_KEYS = {
    'path', 'paths', 'source_path', 'document_path', 'working_copy_path',
    'artifact_path', 'artifact_paths', 'manifest_path', 'output_path',
    'session_state_path', 'session_events_path', 'operator_status_path',
    'verify_evidence_retention_path', 'evidence_dir', 'directory', 'root',
    'working_directory',
}
_ABSOLUTE_PATH_IN_STATUS = re.compile(r'(?i)(?:[a-z]:[\\/]|\\\\|/(?:home|srv|tmp|var)/)')


def _redact_interactive_status_value(value: Any, *, key: str | None = None) -> Any:
    """Remove server paths from public interactive status without mutating state."""

    folded_key = str(key or '').casefold()
    if folded_key in _PRIVATE_INTERACTIVE_PATH_KEYS or (
        folded_key.endswith('_path') and not folded_key.endswith('_download_path')
    ):
        return None
    if isinstance(value, dict):
        return {
            str(child_key): _redact_interactive_status_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
            if str(child_key).casefold() not in _PRIVATE_INTERACTIVE_PATH_KEYS
            and not (
                str(child_key).casefold().endswith('_path')
                and not str(child_key).casefold().endswith('_download_path')
            )
        }
    if isinstance(value, list):
        return [_redact_interactive_status_value(item) for item in value]
    if isinstance(value, str):
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', value):
            return value
        if _ABSOLUTE_PATH_IN_STATUS.search(value):
            return '<redacted>'
    return value


def _interactive_session_has_managed_origin(
    session: dict[str, Any],
    *,
    artifact_service: Any | None = None,
) -> bool:
    """Return whether the managed service proves a working-copy route."""

    session_id = str(session.get('session_id') or '').strip()
    if not session_id:
        return False
    service = artifact_service if artifact_service is not None else local_cli_service
    try:
        projection = service.public_artifact_projection(session_id=session_id, session=session)
    except Exception:
        return False
    return isinstance(projection, dict) and isinstance(
        projection.get('latest_working_copy_download_path'),
        str,
    )


def _strip_untrusted_artifact_routes(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            folded_key = str(key).casefold()
            if (
                folded_key == 'download_path'
                or folded_key.endswith('_download_path')
                or folded_key in {'latest_recovery_sha256', 'latest_recovery_size_bytes'}
            ):
                continue
            result[str(key)] = _strip_untrusted_artifact_routes(child)
        return result
    if isinstance(value, list):
        return [_strip_untrusted_artifact_routes(item) for item in value]
    return value


def _public_interactive_session(
    session: dict[str, Any],
    *,
    artifact_service: Any | None = None,
) -> dict[str, Any]:
    """Project an interactive record for external HTTP/MCP consumers."""

    public = _redact_interactive_status_value(session)
    if not isinstance(public, dict):
        public = {}
    session_id = str(session.get('session_id') or '').strip()
    if session.get('source_path') and 'source_path' not in public:
        public['source_path'] = '<redacted>'
    artifacts = _strip_untrusted_artifact_routes(public.get('artifacts'))
    if not isinstance(artifacts, dict):
        artifacts = {}
    service = artifact_service if artifact_service is not None else local_cli_service
    authoritative: dict[str, Any] = {}
    if session_id:
        try:
            projection = service.public_artifact_projection(session_id=session_id, session=session)
            if isinstance(projection, dict):
                authoritative = {
                    str(key): value
                    for key, value in projection.items()
                    if str(key).casefold().endswith('_download_path')
                    and isinstance(value, str)
                }
        except Exception:
            authoritative = {}
    artifacts.update(authoritative)
    public['artifacts'] = artifacts
    download_path = authoritative.get('latest_working_copy_download_path')
    if session_id and session.get('source_path') and isinstance(download_path, str):
        public['source_path'] = download_path
    return public


def _interactive_response(
    session: dict[str, Any],
    *,
    artifact_service: Any | None = None,
) -> InteractiveSessionResponse:
    return InteractiveSessionResponse(
        session=_public_interactive_session(session, artifact_service=artifact_service),
    )


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    return payload if isinstance(payload, dict) else None


def _build_interactive_verification_gui(
    step_name: str,
    *,
    gui_payload: dict[str, Any] | None,
    result_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    # Keep the public verification API step-centric for multimodal clients:
    # the client should not need a separate screenshot-first flow just to fetch
    # the latest Hancom GUI evidence for verify-pre / verify-post.
    viewer = load_viewer_session() or ensure_viewer_session()
    latest_frame_file = latest_frame_path()
    latest_frame_meta_file = latest_frame_metadata_path()
    latest_frame_meta = _load_json_dict(latest_frame_meta_file) or {}
    supplied_gui = dict(gui_payload) if isinstance(gui_payload, dict) else {}
    result_payload = dict(result_payload) if isinstance(result_payload, dict) else {}

    frame = dict(latest_frame_meta)
    if isinstance(result_payload.get('observation_frame'), dict):
        frame.update(result_payload['observation_frame'])
    if isinstance(supplied_gui.get('frame'), dict):
        frame.update(supplied_gui['frame'])

    observation_status: dict[str, Any] = {}
    if isinstance(result_payload.get('observation_status'), dict):
        observation_status.update(result_payload['observation_status'])
    if isinstance(supplied_gui.get('observation_status'), dict):
        observation_status.update(supplied_gui['observation_status'])

    primary_image = dict(supplied_gui.get('primary_image')) if isinstance(supplied_gui.get('primary_image'), dict) else {}
    default_image_path = str(latest_frame_file) if latest_frame_file.exists() else None
    default_primary_image = {
        'kind': 'hancom_gui_latest_frame',
        'label': 'Hancom GUI latest frame',
        'url': frame.get('frame_url') or (viewer.get('latest_frame_url') if latest_frame_file.exists() else None),
        'path': frame.get('frame_path') or default_image_path,
        'mime_type': 'image/png',
        'captured_at': frame.get('captured_at'),
        'ok': frame.get('ok') if frame else (True if latest_frame_file.exists() else None),
        'reason_code': frame.get('reason_code'),
        'window_handle': frame.get('window_handle'),
        'window_pid': frame.get('window_pid'),
        'window_title': frame.get('window_title'),
        'window_class': frame.get('window_class'),
    }
    for key, value in default_primary_image.items():
        if primary_image.get(key) in (None, '', []):
            primary_image[key] = value

    images: list[dict[str, Any]] = []
    raw_images = supplied_gui.get('images')
    if isinstance(raw_images, list):
        for item in raw_images:
            if isinstance(item, dict):
                images.append(dict(item))
    if not images and (primary_image.get('url') or primary_image.get('path')):
        images.append(dict(primary_image))
    if not primary_image and images:
        primary_image = dict(images[0])

    artifacts = dict(supplied_gui.get('artifacts')) if isinstance(supplied_gui.get('artifacts'), dict) else {}
    artifacts.setdefault('viewer_url', viewer.get('viewer_url'))
    artifacts.setdefault('stream_url', viewer.get('stream_url'))
    artifacts.setdefault('latest_frame_url', viewer.get('latest_frame_url'))
    artifacts.setdefault('latest_frame_metadata_url', viewer.get('latest_frame_metadata_url'))
    if latest_frame_file.exists():
        artifacts.setdefault('latest_frame_path', str(latest_frame_file))
    if latest_frame_meta_file.exists():
        artifacts.setdefault('latest_frame_metadata_path', str(latest_frame_meta_file))

    viewer_payload = dict(supplied_gui.get('viewer')) if isinstance(supplied_gui.get('viewer'), dict) else {}
    viewer_payload.setdefault('viewer_session_id', viewer.get('viewer_session_id'))
    viewer_payload.setdefault('viewer_url', viewer.get('viewer_url'))
    viewer_payload.setdefault('stream_url', viewer.get('stream_url'))
    viewer_payload.setdefault('latest_frame_url', viewer.get('latest_frame_url'))
    viewer_payload.setdefault('latest_frame_metadata_url', viewer.get('latest_frame_metadata_url'))

    capture_state = str(supplied_gui.get('capture_state') or '').strip()
    if not capture_state:
        if primary_image and (primary_image.get('url') or primary_image.get('path')) and primary_image.get('ok') is not False:
            capture_state = 'available'
        else:
            capture_state = 'unavailable'

    return {
        'capture_state': capture_state,
        'source': supplied_gui.get('source') or 'verify-step.latest-observation-frame',
        'step': step_name,
        'primary_image': primary_image or None,
        'images': images,
        'frame': frame,
        'observation_status': observation_status,
        'viewer': viewer_payload,
        'artifacts': artifacts,
        'updated_at': supplied_gui.get('updated_at') or frame.get('captured_at') or viewer.get('updated_at'),
    }


def _build_interactive_verification_result(step_name: str, request: InteractiveVerificationRequest) -> dict[str, Any]:
    return {
        'verification_mode': request.verification_mode,
        'summary': request.summary,
        'result': {
            'expected_present': request.expected_present,
            'expected_absent': request.expected_absent,
            **request.result,
        },
        'gui': _build_interactive_verification_gui(
            step_name,
            gui_payload=request.gui,
            result_payload=request.result,
        ),
    }


def _format_interactive_paragraph_range(start_paragraph_id: Any, end_paragraph_id: Any) -> str | None:
    start = str(start_paragraph_id or '').strip()
    end = str(end_paragraph_id or '').strip() or start
    if not start:
        return None
    if start == end:
        return start
    return f'{start}..{end}'


def _append_target_variant(
    variants: list[dict[str, Any]],
    seen: set[str],
    *,
    variant_kind: str,
    resolved_target_id: Any,
    target_kind: Any,
    label: str,
    editable_preferred: bool,
    candidate: dict[str, Any],
) -> None:
    normalized_target_id = str(resolved_target_id or '').strip()
    if not normalized_target_id or normalized_target_id in seen:
        return
    seen.add(normalized_target_id)
    variants.append(
        {
            'variant_kind': variant_kind,
            'resolved_target_id': normalized_target_id,
            'target_kind': str(target_kind or '').strip() or None,
            'label': label,
            'editable_preferred': editable_preferred,
            'heading_paragraph_id': candidate.get('heading_paragraph_id'),
            'body_start_paragraph_id': candidate.get('body_start_paragraph_id'),
            'body_end_paragraph_id': candidate.get('body_end_paragraph_id'),
            'paragraph_range': _format_interactive_paragraph_range(
                candidate.get('body_start_paragraph_id'),
                candidate.get('body_end_paragraph_id'),
            ),
        }
    )


def _build_target_variants(candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    target_kind = str(candidate.get('target_kind') or '').strip() or None
    body_range = _format_interactive_paragraph_range(
        candidate.get('body_start_paragraph_id'),
        candidate.get('body_end_paragraph_id'),
    )
    body_target_kind = 'paragraph_range' if body_range and '..' in body_range else 'body_paragraph'

    _append_target_variant(
        variants,
        seen,
        variant_kind='candidate_target',
        resolved_target_id=candidate.get('resolved_target_id'),
        target_kind=target_kind,
        label='candidate target',
        editable_preferred=target_kind not in {'heading', 'number'},
        candidate=candidate,
    )
    _append_target_variant(
        variants,
        seen,
        variant_kind='recommended_target',
        resolved_target_id=candidate.get('recommended_resolved_target_id'),
        target_kind=body_target_kind,
        label='recommended writable scope',
        editable_preferred=True,
        candidate=candidate,
    )
    _append_target_variant(
        variants,
        seen,
        variant_kind='body_scope',
        resolved_target_id=body_range,
        target_kind=body_target_kind,
        label='body scope variant',
        editable_preferred=True,
        candidate=candidate,
    )
    _append_target_variant(
        variants,
        seen,
        variant_kind='heading_anchor',
        resolved_target_id=candidate.get('heading_paragraph_id'),
        target_kind='heading',
        label='heading anchor',
        editable_preferred=False,
        candidate=candidate,
    )

    preferred_variant = next((item for item in variants if item.get('editable_preferred')), None)
    if preferred_variant is None and variants:
        preferred_variant = variants[0]
    return variants, preferred_variant


def _normalize_interactive_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    variants, preferred_variant = _build_target_variants(normalized)
    if variants:
        normalized['target_variants'] = variants
    if preferred_variant is not None:
        normalized['preferred_target_variant'] = preferred_variant
        normalized.setdefault('interactive_resolved_target_id', preferred_variant.get('resolved_target_id'))
    normalized.setdefault(
        'paragraph_range',
        _format_interactive_paragraph_range(
            normalized.get('body_start_paragraph_id'),
            normalized.get('body_end_paragraph_id'),
        ),
    )
    return normalized


def _normalize_interactive_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized.append(_normalize_interactive_candidate(candidate))
    return normalized


def _build_find_result(request: InteractiveFindRequest) -> dict[str, Any]:
    candidates = _normalize_interactive_candidates(request.candidates)
    preferred_variant = None
    if candidates:
        preferred_variant = candidates[0].get('preferred_target_variant')
    return {
        'query': request.query,
        'resolved_target_id': request.resolved_target_id,
        'candidate_count': len(candidates),
        'candidates': candidates,
        'candidate_bundle_state': (
            'candidates_available'
            if candidates
            else ('resolved_target_only' if request.resolved_target_id else 'empty')
        ),
        'preferred_target_variant': preferred_variant,
        'next_step': 'choose' if candidates else 'retry_find',
        'telemetry': request.telemetry,
    }


def _resolve_selected_candidate(
    request: InteractiveChooseRequest,
    *,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    candidate_pool = candidates if candidates is not None else request.candidates
    if isinstance(request.selected_candidate, dict) and request.selected_candidate:
        candidate = _normalize_interactive_candidate(request.selected_candidate)
    elif request.selected_candidate_index is not None and 0 <= request.selected_candidate_index < len(candidate_pool):
        candidate = _normalize_interactive_candidate(candidate_pool[request.selected_candidate_index])
    else:
        return None
    if request.selection_reason and candidate.get('selection_reason') in (None, ''):
        candidate['selection_reason'] = request.selection_reason
    if request.selected_candidate_index is not None and candidate.get('selected_candidate_index') in (None, ''):
        candidate['selected_candidate_index'] = request.selected_candidate_index
    return candidate


def _build_runtime_preparation(
    selected_candidate: dict[str, Any] | None,
    *,
    find_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(selected_candidate, dict) or not selected_candidate:
        return {
            'state': 'needs_choice',
            'summary': 'Choose a candidate before runtime entry can be prepared.',
            'next_step': 'choose',
        }

    candidate = _normalize_interactive_candidate(selected_candidate)
    preferred_variant = candidate.get('preferred_target_variant') if isinstance(candidate.get('preferred_target_variant'), dict) else None
    variants = candidate.get('target_variants') if isinstance(candidate.get('target_variants'), list) else []
    if preferred_variant is None and variants:
        preferred_variant = variants[0] if isinstance(variants[0], dict) else None

    resolved_target_id = str(
        (preferred_variant or {}).get('resolved_target_id')
        or candidate.get('recommended_resolved_target_id')
        or candidate.get('resolved_target_id')
        or ''
    ).strip() or None
    paragraph_range = (
        (preferred_variant or {}).get('paragraph_range')
        or candidate.get('paragraph_range')
        or resolved_target_id
    )
    entry_mode = 'table_cell_runtime_candidate' if candidate.get('expected_cell_addr') else 'paragraph_scope_entry'
    inspect_snapshot_id = None
    telemetry = find_result.get('telemetry') if isinstance(find_result, dict) else {}
    if isinstance(telemetry, dict):
        inspect_snapshot_id = telemetry.get('inspect_snapshot_id')
    if inspect_snapshot_id in (None, ''):
        inspect_snapshot_id = candidate.get('inspect_snapshot_id')

    enter_target = {
        'query': find_result.get('query') if isinstance(find_result, dict) else None,
        'resolved_target_id': resolved_target_id,
        'target_kind': (preferred_variant or {}).get('target_kind') or candidate.get('target_kind'),
        'variant_kind': (preferred_variant or {}).get('variant_kind'),
        'matched_anchor_id': candidate.get('matched_anchor_id'),
        'heading_text': candidate.get('heading_text') or candidate.get('matched_heading'),
        'heading_paragraph_id': candidate.get('heading_paragraph_id'),
        'body_start_paragraph_id': candidate.get('body_start_paragraph_id'),
        'body_end_paragraph_id': candidate.get('body_end_paragraph_id'),
        'paragraph_range': paragraph_range,
        'preview_text': candidate.get('preview_text'),
        'selection_preview': candidate.get('selection_preview'),
    }
    if candidate.get('expected_cell_addr'):
        enter_target['expected_cell_addr'] = candidate.get('expected_cell_addr')

    return {
        'state': 'prepared',
        'summary': 'Chosen candidate normalized into direct runtime entry/lock/apply preparation.',
        'next_step': 'enter',
        'resolved_target_id': resolved_target_id,
        'selected_candidate': candidate,
        'selected_target_variant': preferred_variant,
        'enter': {
            'state': 'prepared',
            'mode': entry_mode,
            'target_summary': enter_target,
            'cursor_anchor': {
                'resolved_target_id': resolved_target_id,
                'paragraph_range': paragraph_range,
                'heading_paragraph_id': candidate.get('heading_paragraph_id'),
                'body_start_paragraph_id': candidate.get('body_start_paragraph_id'),
                'body_end_paragraph_id': candidate.get('body_end_paragraph_id'),
            },
        },
        'lock': {
            'state': 'pending',
            'inspect_snapshot_id': inspect_snapshot_id,
            'rule': 'selected_target_variant_identity',
            'on_mismatch': 'stop_and_rechoose',
            'lock_details': {
                'resolved_target_id': resolved_target_id,
                'variant_kind': (preferred_variant or {}).get('variant_kind'),
                'heading_paragraph_id': candidate.get('heading_paragraph_id'),
                'body_start_paragraph_id': candidate.get('body_start_paragraph_id'),
                'body_end_paragraph_id': candidate.get('body_end_paragraph_id'),
                'preview_text': candidate.get('preview_text'),
            },
        },
        'apply': {
            'state': 'pending_runtime_execution',
            'operation': {
                'resolved_target_id': resolved_target_id,
                'variant_kind': (preferred_variant or {}).get('variant_kind'),
                'target_kind': (preferred_variant or {}).get('target_kind') or candidate.get('target_kind'),
                'selection_preview': candidate.get('selection_preview'),
            },
            'requires_prior_steps': ['enter', 'lock', 'verify-pre'],
        },
        'verify_pre': {
            'state': 'pending',
            'suggested_mode': 'text-or-gui',
        },
        'verify_post': {
            'state': 'pending',
            'suggested_mode': 'text-or-gui',
        },
    }


def _build_choose_result(
    *,
    candidates: list[dict[str, Any]],
    selected_candidate: dict[str, Any] | None,
    selected_candidate_index: int | None,
    selection_reason: str | None,
) -> dict[str, Any]:
    return {
        'candidate_count': len(candidates),
        'candidates': candidates,
        'selected_candidate_index': selected_candidate_index,
        'selected_candidate': selected_candidate,
        'selected_target_variant': (
            selected_candidate.get('preferred_target_variant')
            if isinstance(selected_candidate, dict)
            else None
        ),
        'selection_reason': selection_reason,
        'next_step': 'enter' if isinstance(selected_candidate, dict) and selected_candidate else 'choose',
    }


def _prepared_target_summary(session: dict[str, Any]) -> dict[str, Any]:
    runtime_preparation = session.get('runtime_preparation') if isinstance(session.get('runtime_preparation'), dict) else {}
    enter = runtime_preparation.get('enter') if isinstance(runtime_preparation.get('enter'), dict) else {}
    target_summary = enter.get('target_summary') if isinstance(enter.get('target_summary'), dict) else {}
    return dict(target_summary)


def _prepared_lock_details(session: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None, str | None]:
    runtime_preparation = session.get('runtime_preparation') if isinstance(session.get('runtime_preparation'), dict) else {}
    lock = runtime_preparation.get('lock') if isinstance(runtime_preparation.get('lock'), dict) else {}
    lock_details = lock.get('lock_details') if isinstance(lock.get('lock_details'), dict) else {}
    return dict(lock_details), lock.get('inspect_snapshot_id'), lock.get('rule'), lock.get('on_mismatch')


def _prepared_apply_operation(session: dict[str, Any]) -> dict[str, Any]:
    runtime_preparation = session.get('runtime_preparation') if isinstance(session.get('runtime_preparation'), dict) else {}
    apply_state = runtime_preparation.get('apply') if isinstance(runtime_preparation.get('apply'), dict) else {}
    operation = apply_state.get('operation') if isinstance(apply_state.get('operation'), dict) else {}
    return dict(operation)


# Interactive `.51` session routes intentionally map 1:1 to the state-machine
# boundaries (find -> choose -> enter -> lock -> verify -> apply -> verify ->
# undo/close). Request shaping stays here, while session persistence plus
# operator/verify rendering helpers live under the interactive session modules.
@app.post('/interactive/session/open', response_model=InteractiveSessionResponse)
def interactive_session_open(request: InteractiveSessionOpenRequest) -> InteractiveSessionResponse:
    source_path, source_filename, size_bytes = _resolve_source_path_input(request.source_path)
    try:
        session = interactive_sessions.open_session(
            source_path=source_path,
            source_filename=source_filename,
            file_size_bytes=size_bytes,
            content_type=_default_source_content_type(source_path),
            session_label=request.session_label,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc) from exc
    return _interactive_response(session)


@app.get('/interactive/session/status', response_model=InteractiveSessionResponse)
def interactive_session_status(session_id: str | None = None) -> InteractiveSessionResponse:
    try:
        session = interactive_sessions.get_status(session_id=session_id)
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


# The manifest/frame/frame.json trio deliberately share one artifact loader so the
# HTTP contract stays aligned with the session manager's retention/prune rules.
@app.get('/interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}')
def interactive_session_verify_evidence_manifest(session_id: str, step_name: str, recorded_at: str) -> JSONResponse:
    try:
        artifact = interactive_sessions.load_verify_evidence_artifact(
            session_id=session_id,
            step_name=step_name,
            recorded_at=recorded_at,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc

    payload = dict(artifact.get('payload') or {})
    payload['artifact'] = {
        'ok': True,
        'session_id': artifact['session_id'],
        'step': artifact['step_name'],
        'recorded_at': artifact['recorded_at'],
        'evidence_dir': str(artifact['evidence_dir']),
        'frame_path': str(artifact['frame_path']) if artifact.get('frame_path') else None,
        'metadata_path': str(artifact['metadata_path']),
        'urls': artifact['urls'],
    }
    return JSONResponse(payload)


@app.get('/interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}/frame')
def interactive_session_verify_evidence_frame(session_id: str, step_name: str, recorded_at: str) -> FileResponse:
    try:
        artifact = interactive_sessions.load_verify_evidence_artifact(
            session_id=session_id,
            step_name=step_name,
            recorded_at=recorded_at,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc

    frame_path = artifact.get('frame_path')
    if not isinstance(frame_path, Path) or not frame_path.exists():
        raise HTTPException(status_code=404, detail='Frozen verify frame image is not available for this artifact.')
    return FileResponse(frame_path)


@app.get('/interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}/frame.json')
def interactive_session_verify_evidence_metadata(session_id: str, step_name: str, recorded_at: str) -> JSONResponse:
    try:
        artifact = interactive_sessions.load_verify_evidence_artifact(
            session_id=session_id,
            step_name=step_name,
            recorded_at=recorded_at,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return JSONResponse(artifact.get('payload') or {})


@app.post('/interactive/session/find', response_model=InteractiveSessionResponse)
def interactive_session_find(request: InteractiveFindRequest) -> InteractiveSessionResponse:
    # Candidate search stays as its own role boundary so duplicated phrases can be
    # reviewed before cursor entry/apply logic starts mutating the live document.
    find_result = _build_find_result(request)
    payload = find_result
    summary = request.summary or f"Find recorded ({find_result.get('candidate_count', 0)} candidates)."
    active_target = {
        'query': request.query,
        'resolved_target_id': request.resolved_target_id,
    }
    preferred_variant = find_result.get('preferred_target_variant') if isinstance(find_result.get('preferred_target_variant'), dict) else None
    if preferred_variant and active_target.get('resolved_target_id') in (None, ''):
        active_target['resolved_target_id'] = preferred_variant.get('resolved_target_id')
    try:
        session = interactive_sessions.record_command(
            'find',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=payload,
            find_result=find_result,
            active_target=active_target,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/choose', response_model=InteractiveSessionResponse)
def interactive_session_choose(request: InteractiveChooseRequest) -> InteractiveSessionResponse:
    # Candidate choice is stored separately because the safety rule is to preserve
    # the full candidate set plus the selected one for later review/rollback.
    try:
        current_session = interactive_sessions.get_status(session_id=request.session_id)
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    fallback_find_result = current_session.get('find_result') if isinstance(current_session.get('find_result'), dict) else {}
    candidate_pool = _normalize_interactive_candidates(
        request.candidates
        if request.candidates
        else (fallback_find_result.get('candidates') if isinstance(fallback_find_result.get('candidates'), list) else [])
    )
    selected_candidate = _resolve_selected_candidate(request, candidates=candidate_pool)
    choose_result = _build_choose_result(
        candidates=candidate_pool,
        selected_candidate=selected_candidate,
        selected_candidate_index=request.selected_candidate_index,
        selection_reason=request.selection_reason,
    )
    runtime_preparation = _build_runtime_preparation(selected_candidate, find_result=fallback_find_result)
    payload = choose_result
    summary = request.summary or 'Candidate choice recorded.'
    try:
        session = interactive_sessions.record_command(
            'choose',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=payload,
            choose_result=choose_result,
            selected_candidate=selected_candidate,
            active_target=(runtime_preparation.get('enter') or {}).get('target_summary'),
            runtime_preparation=runtime_preparation,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/enter', response_model=InteractiveSessionResponse)
def interactive_session_enter(request: InteractiveEnterRequest) -> InteractiveSessionResponse:
    # Cursor-entry intent is distinct from lock/verify so later runtime hooks can
    # prove where the caret re-entered before an irreversible edit is attempted.
    try:
        current_session = interactive_sessions.get_status(session_id=request.session_id)
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    active_target = _prepared_target_summary(current_session)
    active_target.update(request.target_summary)
    if request.resolved_target_id and active_target.get('resolved_target_id') in (None, ''):
        active_target['resolved_target_id'] = request.resolved_target_id
    if request.cursor_anchor is not None:
        active_target['cursor_anchor'] = request.cursor_anchor
    summary = request.summary or 'Cursor-entry intent recorded.'
    runtime_preparation = {
        'state': 'enter_recorded',
        'next_step': 'lock',
        'enter': {
            'state': request.result_state,
            'mode': ((current_session.get('runtime_preparation') or {}).get('enter') or {}).get('mode'),
            'target_summary': active_target,
            'cursor_anchor': request.cursor_anchor,
        },
    }
    try:
        session = interactive_sessions.record_command(
            'enter',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=active_target,
            active_target=active_target,
            runtime_preparation=runtime_preparation,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/lock', response_model=InteractiveSessionResponse)
def interactive_session_lock(request: InteractiveLockRequest) -> InteractiveSessionResponse:
    # Lock telemetry exists to surface pre-apply identity drift early instead of
    # hiding snapshot mismatches inside a later apply failure blob.
    try:
        current_session = interactive_sessions.get_status(session_id=request.session_id)
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    prepared_lock_details, prepared_snapshot_id, prepared_rule, prepared_on_mismatch = _prepared_lock_details(current_session)
    lock_status = prepared_lock_details
    lock_status.update(request.lock_details)
    inspect_snapshot_id = request.inspect_snapshot_id or prepared_snapshot_id
    rule = request.rule or prepared_rule
    on_mismatch = request.on_mismatch or prepared_on_mismatch
    lock_status.update(
        {
            'state': request.result_state,
            'inspect_snapshot_id': inspect_snapshot_id,
            'rule': rule,
            'on_mismatch': on_mismatch,
            'mismatch_reason_code': request.mismatch_reason_code,
        }
    )
    summary = request.summary or 'Pre-apply lock state recorded.'
    runtime_preparation = {
        'state': 'lock_recorded',
        'next_step': 'verify-pre',
        'lock': {
            'state': request.result_state,
            'inspect_snapshot_id': inspect_snapshot_id,
            'rule': rule,
            'on_mismatch': on_mismatch,
            'lock_details': lock_status,
        },
    }
    try:
        session = interactive_sessions.record_command(
            'lock',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=lock_status,
            lock_status=lock_status,
            runtime_preparation=runtime_preparation,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/verify-pre', response_model=InteractiveSessionResponse)
def interactive_session_verify_pre(request: InteractiveVerificationRequest) -> InteractiveSessionResponse:
    # Verify-pre remains first-class so the pass/fail verdict stays visible
    # in logs/TUI output immediately, not only as nested internal evidence.
    verify_result = _build_interactive_verification_result('verify-pre', request)
    summary = request.summary or 'Pre-apply verification recorded.'
    try:
        session = interactive_sessions.record_command(
            'verify-pre',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=verify_result,
            verify_stage='pre',
            verify_result=verify_result,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/apply', response_model=InteractiveSessionResponse)
def interactive_session_apply(request: InteractiveApplyRequest) -> InteractiveSessionResponse:
    try:
        current_session = interactive_sessions.get_status(session_id=request.session_id)
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    operation = _prepared_apply_operation(current_session)
    operation.update(request.operation)
    apply_result = {
        'state': request.result_state,
        'summary': request.summary,
        'operation': operation,
        'result': request.result,
        'updated_at': int(time.time()),
    }
    summary = request.summary or 'Apply step recorded.'
    runtime_preparation = {
        'state': 'apply_recorded',
        'next_step': 'verify-post',
        'apply': {
            'state': request.result_state,
            'operation': operation,
            'result': request.result,
        },
    }
    try:
        session = interactive_sessions.record_command(
            'apply',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=apply_result,
            apply_result=apply_result,
            runtime_preparation=runtime_preparation,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/verify-post', response_model=InteractiveSessionResponse)
def interactive_session_verify_post(request: InteractiveVerificationRequest) -> InteractiveSessionResponse:
    # Verify-post mirrors verify-pre so the operator can compare intended delta vs.
    # observed delta before deciding whether to keep or undo the step.
    verify_result = _build_interactive_verification_result('verify-post', request)
    summary = request.summary or 'Post-apply verification recorded.'
    try:
        session = interactive_sessions.record_command(
            'verify-post',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=verify_result,
            verify_stage='post',
            verify_result=verify_result,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/undo', response_model=InteractiveSessionResponse)
def interactive_session_undo(request: InteractiveUndoRequest) -> InteractiveSessionResponse:
    undo_result = {
        'state': request.result_state,
        'summary': request.summary,
        'reason': request.reason,
        'result': request.result,
    }
    summary = request.summary or 'Undo step recorded.'
    try:
        session = interactive_sessions.record_command(
            'undo',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload=undo_result,
            undo_result=undo_result,
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


@app.post('/interactive/session/close', response_model=InteractiveSessionResponse)
def interactive_session_close(request: InteractiveCloseRequest) -> InteractiveSessionResponse:
    summary = request.summary or 'Interactive session closed.'
    try:
        session = interactive_sessions.record_command(
            'close',
            session_id=request.session_id,
            state=request.result_state,
            summary=summary,
            payload={'outcome': request.outcome},
            popup_status=request.popup_status,
            failure_reason=request.failure_reason,
            metadata=request.metadata,
            session_state=request.outcome,
        )
    except InteractiveSessionError as exc:
        raise _interactive_http_error(exc, status_code=404) from exc
    return _interactive_response(session)


def create_job_layout(job_id: str) -> tuple[Path, Path, Path, Path, Path]:
    # Queue records are consumed by an interactive scheduled-task worker and a
    # child process.  Store absolute artifact paths so Hancom COM open/save calls
    # never depend on the scheduled task's current working directory.
    job_dir = (settings.jobs_root / job_id).resolve()
    input_dir = job_dir / 'input'
    output_dir = job_dir / 'output'
    metadata_dir = job_dir / 'metadata'
    logs_dir = job_dir / 'logs'
    for path in (input_dir, output_dir, metadata_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)
    return job_dir, input_dir, output_dir, metadata_dir, logs_dir


def _metadata_dir_for_job(job_id: str) -> Path:
    return settings.jobs_root / job_id / 'metadata'


def _confirm_state_path(job_id: str) -> Path:
    return _metadata_dir_for_job(job_id) / 'confirm_state.json'


def _compiled_plan_path(job_id: str) -> Path:
    return _metadata_dir_for_job(job_id) / 'compiled_plan.json'


def _job_input_source_path(job_id: str) -> Path:
    input_dir = settings.jobs_root / job_id / 'input'
    metadata_dir = _metadata_dir_for_job(job_id)
    context = _load_compile_context(metadata_dir)
    context_source_path = context.get('source_path') if isinstance(context, dict) else None
    if context_source_path:
        candidate = Path(str(context_source_path))
        if candidate.exists() and candidate.is_file():
            return candidate
    matches = sorted(path for path in input_dir.glob('source*') if path.is_file())
    if not matches:
        matches = sorted(path for path in input_dir.iterdir() if path.is_file())
    if not matches:
        raise HTTPException(status_code=404, detail=f'No source file found for compile job: {job_id}')
    return matches[0]


def _load_compiled_plan_or_404(job_id: str) -> dict[str, Any]:
    path = _compiled_plan_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'Compiled plan not found for job: {job_id}')
    return json.loads(path.read_text(encoding='utf-8'))


def _default_confirm_state(job_id: str, compiled: dict[str, Any]) -> dict[str, Any]:
    confirm_policy = compiled.get('confirm_policy', {}) if isinstance(compiled, dict) else {}
    approval_packet = compiled.get('approval_packet', {}) if isinstance(compiled, dict) else {}
    compile_readiness = compiled.get('compile_readiness', {}) if isinstance(compiled, dict) else {}
    shared_table_blocker = _shared_table_conflict_blocker(compiled)
    state = {
        'schema_version': 'confirm-session/v1',
        'job_id': job_id,
        'status': 'pending',
        'decision': confirm_policy.get('decision', 'confirm'),
        'decision_reason': confirm_policy.get('decision_reason'),
        'force_confirm': bool(confirm_policy.get('force_confirm')),
        'compile_status': compile_readiness.get('status'),
        'available_actions': ['inspect_more', 'regenerate', 'abort'],
        'confirm_policy': confirm_policy,
        'approval_packet': approval_packet,
        'last_action': None,
        'action_history': [],
        'approved_instruction_path': None,
        'queued_job': None,
    }
    if compile_readiness.get('status') == 'ready' and not shared_table_blocker:
        state['available_actions'].insert(0, 'approve')
    return state


def _load_confirm_state(job_id: str, compiled: dict[str, Any]) -> dict[str, Any]:
    path = _confirm_state_path(job_id)
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    state = _default_confirm_state(job_id, compiled)
    _write_json(path, state)
    return state


def _save_confirm_state(job_id: str, state: dict[str, Any]) -> dict[str, Any]:
    _write_json(_confirm_state_path(job_id), state)
    return state


def _append_confirm_action(state: dict[str, Any], action: str, *, note: str | None = None) -> None:
    history = state.setdefault('action_history', [])
    history.append({'action': action, 'at': uuid.uuid4().hex, 'note': note})
    state['last_action'] = action


def _build_confirm_inspection_payload(compiled: dict[str, Any]) -> dict[str, Any]:
    compile_readiness = compiled.get('compile_readiness', {}) if isinstance(compiled, dict) else {}
    return {
        'warning_badges': compiled.get('warning_badges', []),
        'apply_scope_previews': compiled.get('apply_scope_previews', []),
        'target_recommendations': compiled.get('target_recommendations', []),
        'search_first_candidate_reviews': compile_readiness.get('search_first_candidate_reviews', []),
        'search_first_runtime_flow': compile_readiness.get('search_first_runtime_flow', {}),
        'target_conflict_groups': compile_readiness.get('target_conflict_groups', []),
        'native_runtime_candidates': compile_readiness.get('native_runtime_candidates', []),
        'unsafe_structural_ranges': compile_readiness.get('unsafe_structural_ranges', []),
        'next_action': compile_readiness.get('next_action'),
        'interactive_choices': compile_readiness.get('interactive_choices', []),
    }


def _preview_excerpt(value: Any, *, limit: int = 120) -> str:
    text = str(value or '').replace('\n', ' ').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


RISK_FLAG_LABELS = {
    'execution_mode_not_native': 'native-only 경로가 아니라 렌더 검토가 필요합니다.',
    'list_structure_present': 'list 구조가 있어서 style drift 가능성이 있습니다.',
    'table_edit_present': 'table 편집이 포함되어 구조 영향 검토가 필요합니다.',
    'multiple_touched_ranges': '여러 구간을 한 번에 수정하므로 범위 확인이 중요합니다.',
    'prior_evidence_missing_for_auto': '자동 진행 근거가 아직 부족합니다.',
    'render_qa_evidence_missing_for_auto': 'render QA 근거가 아직 없습니다.',
    'blocking_warning_badges_present': 'blocking warning이 있어 확인이 필요합니다.',
}


ACTION_HINTS = {
    'choose_or_confirm_resolved_target_id_and_resend': 'resolved_target_id를 확정한 뒤 다시 compile 하십시오.',
    'use_placeholders_or_table_cell_mode': 'placeholder 또는 table-cell 방식으로 다시 좁혀야 합니다.',
    'apply_safe': '바로 approve 할 수 있지만 warning과 render diff를 먼저 보는 편이 안전합니다.',
}


def _humanize_risk_flag(flag: Any) -> str:
    text = str(flag or '').strip()
    if not text:
        return ''
    if text in RISK_FLAG_LABELS:
        return RISK_FLAG_LABELS[text]
    if text.startswith('fallback_reason:'):
        detail = text.split(':', 1)[1]
        if detail == 'text_anchor_replace_path_still_present':
            return 'text-anchor replace 경로가 남아 있어서 정확한 범위 검토가 필요합니다.'
        if detail == 'exemplar_or_legacy_style_ops_still_present':
            return 'legacy/exemplar style 경로가 남아 있어서 스타일 상속이 완전히 안정적이지 않을 수 있습니다.'
        return f'fallback reason: {detail}'
    return text.replace('_', ' ')


def _render_confirm_brief(state: dict[str, Any]) -> str:
    approval_packet = state.get('approval_packet') if isinstance(state.get('approval_packet'), dict) else {}
    confirm_policy = state.get('confirm_policy') if isinstance(state.get('confirm_policy'), dict) else {}
    inspection_payload = state.get('inspection_payload', {}) if isinstance(state.get('inspection_payload'), dict) else {}
    target_summary = approval_packet.get('target_summary') if isinstance(approval_packet.get('target_summary'), dict) else {}
    guards = approval_packet.get('guards') if isinstance(approval_packet.get('guards'), dict) else {}
    change_preview = approval_packet.get('change_preview') if isinstance(approval_packet.get('change_preview'), dict) else {}
    previews = change_preview.get('previews') if isinstance(change_preview.get('previews'), list) else []
    warning_badges = inspection_payload.get('warning_badges', []) if isinstance(inspection_payload.get('warning_badges'), list) else []
    apply_scope_previews = inspection_payload.get('apply_scope_previews', []) if isinstance(inspection_payload.get('apply_scope_previews'), list) else []
    target_recommendations = inspection_payload.get('target_recommendations', []) if isinstance(inspection_payload.get('target_recommendations'), list) else []
    target_conflict_groups = inspection_payload.get('target_conflict_groups', []) if isinstance(inspection_payload.get('target_conflict_groups'), list) else []
    interactive_choices = approval_packet.get('interactive_choices', []) if isinstance(approval_packet.get('interactive_choices'), list) else []
    risk_flags = approval_packet.get('risk_flags', []) if isinstance(approval_packet.get('risk_flags'), list) else []
    next_action = inspection_payload.get('next_action')

    lines = [
        f"Status: {state.get('status', 'unknown')}",
        f"Decision: {confirm_policy.get('decision', 'confirm')} ({confirm_policy.get('decision_reason', 'unspecified')})",
        f"Intent: {approval_packet.get('intent_summary', 'n/a')}",
        f"Execution mode: {approval_packet.get('execution_mode', 'n/a')}",
        f"Targets: sections={','.join(target_summary.get('section_keys', [])) or 'n/a'}, resolved={target_summary.get('resolved_target_count', 0)}, unresolved={target_summary.get('unresolved_target_count', 0)}",
        f"Guards: snapshot={guards.get('inspect_snapshot_id') or 'n/a'}, tables={len(guards.get('table_fingerprints', [])) if isinstance(guards.get('table_fingerprints'), list) else 0}",
        f"Risk flags: {', '.join(risk_flags[:6]) or 'none'}",
        f"Override: {confirm_policy.get('policy_override', {}).get('decision_override') or 'none'}",
        f"Available actions: {', '.join(state.get('available_actions', [])) or 'none'}",
    ]

    if next_action:
        lines.append(f"Next step: {ACTION_HINTS.get(str(next_action), str(next_action))}")
    if interactive_choices:
        top_choice = interactive_choices[0] if isinstance(interactive_choices[0], dict) else {}
        if top_choice:
            lines.append(
                f"Recommended path: {top_choice.get('label') or top_choice.get('action')}"
                f" ({top_choice.get('reason') or 'reason unavailable'})"
            )

    humanized_risks = [item for item in (_humanize_risk_flag(flag) for flag in risk_flags[:4]) if item]
    if humanized_risks:
        lines.append('Why confirm:')
        for item in humanized_risks[:4]:
            lines.append(f"- {item}")

    if apply_scope_previews:
        lines.append('Resolver scope:')
        for preview in apply_scope_previews[:2]:
            if not isinstance(preview, dict):
                continue
            section_key = preview.get('section_key') or 'unknown section'
            resolved_target_type = preview.get('resolved_target_type') or 'unknown'
            resolved_target_id = preview.get('resolved_target_id') or 'n/a'
            heading_paragraph_id = preview.get('heading_paragraph_id') or 'none'
            body_start = preview.get('body_start_paragraph_id') or 'none'
            body_end = preview.get('body_end_paragraph_id') or body_start
            warning_state = preview.get('warning_state') or 'clear'
            blocking_codes = preview.get('blocking_warning_codes') or []
            lines.append(f"- {section_key}")
            lines.append(f"  resolved: {resolved_target_type} | {resolved_target_id}")
            lines.append(f"  heading anchor: {heading_paragraph_id} | {_preview_excerpt(preview.get('heading_before_preview_text'))}")
            lines.append(f"  editable body: {body_start}..{body_end}")
            lines.append(f"  body before: {_preview_excerpt(preview.get('body_before_preview_text') or preview.get('before_preview_text'))}")
            lines.append(f"  apply preview: {_preview_excerpt(preview.get('apply_preview_text') or preview.get('after_preview_text'))}")
            if warning_state != 'clear' or blocking_codes:
                lines.append(
                    f"  warning: {warning_state}"
                    + (f" | {', '.join(blocking_codes)}" if blocking_codes else '')
                )
            if preview.get('why_not_body_safe'):
                lines.append(f"  why: {preview.get('why_not_body_safe')}")
    elif previews:
        lines.append('Preview:')
        for preview in previews[:2]:
            if not isinstance(preview, dict):
                continue
            section_key = preview.get('section_key') or 'unknown section'
            lines.append(f"- {section_key}")
            lines.append(f"  before: {_preview_excerpt(preview.get('before_preview_text'))}")
            lines.append(f"  after: {_preview_excerpt(preview.get('after_preview_text'))}")

    if warning_badges:
        lines.append('Warnings:')
        for badge in warning_badges[:4]:
            if isinstance(badge, dict):
                lines.append(f"- {badge.get('code')}: {badge.get('summary')}")
    if target_recommendations:
        lines.append('Target candidates:')
        for recommendation in target_recommendations[:3]:
            if not isinstance(recommendation, dict):
                continue
            lines.append(
                f"- section {recommendation.get('section_key')} | {recommendation.get('candidate_state_label')} ({recommendation.get('candidate_state_reason')})"
            )
            if recommendation.get('section_domain_note'):
                lines.append(f"  domain: {recommendation.get('section_domain_note')}")
            if recommendation.get('conflict_priority_summary'):
                lines.append(f"  conflict: {recommendation.get('conflict_priority_summary')}")
            if recommendation.get('candidate_comparison_summary'):
                lines.append(f"  compare: {recommendation.get('candidate_comparison_summary')}")
            if recommendation.get('top_choice_summary'):
                lines.append(f"  why_top1: {recommendation.get('top_choice_summary')}")
            for candidate in (recommendation.get('candidates') or [])[:3]:
                if not isinstance(candidate, dict):
                    continue
                suffixes = []
                if candidate.get('ownership_note'):
                    suffixes.append(candidate.get('ownership_note'))
                if candidate.get('domain_note'):
                    suffixes.append(candidate.get('domain_note'))
                lines.append(
                    f"  * {candidate.get('resolved_target_id')} | {candidate.get('confidence_label')} | {candidate.get('selection_preview') or candidate.get('preview_text')}"
                )
                if suffixes:
                    lines.append(f"    note: {' / '.join(suffixes)}")
            actions = recommendation.get('section_actions') or []
            if actions:
                lines.append('  actions:')
                for action in actions[:4]:
                    if isinstance(action, dict):
                        lines.append(f"    - {action.get('action')}: {action.get('label')} ({action.get('reason')})")
            if recommendation.get('recommended_action'):
                lines.append(f"  recommended_action: {recommendation.get('recommended_action')}")
            advanced_actions = recommendation.get('advanced_section_actions') or []
            if advanced_actions:
                lines.append('  advanced_actions:')
                for action in advanced_actions[:3]:
                    if isinstance(action, dict):
                        lines.append(f"    - {action.get('action')}: {action.get('label')} ({action.get('reason')})")
    if target_conflict_groups:
        lines.append('Conflict groups:')
        for group in target_conflict_groups[:4]:
            if isinstance(group, dict):
                lines.append(f"- {group.get('resolved_target_id')}: {', '.join(group.get('section_keys', []))}")
    if interactive_choices:
        lines.append('Suggested choices:')
        for choice in interactive_choices[:4]:
            if isinstance(choice, dict):
                lines.append(f"- {choice.get('action')}: {choice.get('label')} ({choice.get('reason')})")
    return '\n'.join(lines)


def _persist_compile_artifacts(metadata_dir: Path, compiled: dict[str, Any]) -> None:
    if compiled.get('template_map') is not None:
        metadata_dir.joinpath('template_map.json').write_text(json.dumps(compiled['template_map'], ensure_ascii=False, indent=2), encoding='utf-8')
    if compiled.get('style_roles') is not None:
        metadata_dir.joinpath('style_roles.json').write_text(json.dumps(compiled['style_roles'], ensure_ascii=False, indent=2), encoding='utf-8')
    if compiled.get('content_spec') is not None:
        metadata_dir.joinpath('content_spec.json').write_text(json.dumps(compiled['content_spec'], ensure_ascii=False, indent=2), encoding='utf-8')
    metadata_dir.joinpath('compiled_plan.json').write_text(json.dumps(compiled, ensure_ascii=False, indent=2), encoding='utf-8')


def _persist_compile_context(metadata_dir: Path, *, source_filename: str, source_path: Path, content_type: str | None) -> None:
    _write_json(
        metadata_dir / 'compile_context.json',
        {
            'source_filename': source_filename,
            'source_path': str(source_path),
            'content_type': content_type,
        },
    )


def parse_policy_override_json(text: str | None) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditOperationError(f'invalid policy_override_json: {exc}') from exc
    return normalize_policy_override(raw)


def _load_compile_context(metadata_dir: Path) -> dict[str, Any]:
    context = _read_json_if_exists(metadata_dir / 'compile_context.json')
    return context if isinstance(context, dict) else {}


@app.post('/convert', response_model=ConvertResponse)
async def convert(file: UploadFile = File(...)) -> ConvertResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, output_dir, metadata_dir, logs_dir = create_job_layout(job_id)

    source_path = input_dir / f'source{suffix}'
    output_path = output_dir / 'result.pdf'

    try:
        require_runtime_readiness_or_503('convert')
        size_bytes = await save_upload_to_job(file, source_path)

        job = db.enqueue_job(
            task_type='convert',
            job_id=job_id,
            source_filename=filename,
            source_path=source_path,
            output_path=output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=file.content_type,
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted job %s for %s (%s bytes)', job_id, filename, size_bytes)
        return ConvertResponse(job=job)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register upload for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to register conversion job.')
    finally:
        await file.close()


@app.post('/edit-and-convert', response_model=ConvertResponse)
async def edit_and_convert(
    file: UploadFile = File(...),
    instructions_json: str = Form(...),
) -> ConvertResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    try:
        payload = normalize_instruction_payload(json.loads(instructions_json))
    except (json.JSONDecodeError, EditOperationError) as exc:
        raise HTTPException(status_code=400, detail=f'Invalid edit instructions: {exc}') from exc

    job_id = uuid.uuid4().hex
    job_dir, input_dir, output_dir, metadata_dir, logs_dir = create_job_layout(job_id)

    source_path = input_dir / f'source{suffix}'
    output_path = output_dir / 'result.pdf'
    edited_output_path = output_dir / f'edited{suffix}'
    instructions_path = metadata_dir / 'edit_instructions.json'

    try:
        require_runtime_readiness_or_503('edit_and_convert')
        size_bytes = await save_upload_to_job(file, source_path)
        instructions_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=file.content_type,
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted edit job %s for %s (%s bytes, %s ops)', job_id, filename, size_bytes, len(payload['operations']))
        return ConvertResponse(job=job)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register edit job for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to register edit job.')

    finally:
        await file.close()


@app.post('/edit-and-convert-from-path', response_model=ConvertResponse)
def edit_and_convert_from_path(request: EditAndConvertFromPathRequest) -> ConvertResponse:
    try:
        payload = normalize_instruction_payload(_parse_json_request_field('instructions_json', request.instructions_json))
    except EditOperationError as exc:
        raise HTTPException(status_code=400, detail=f'Invalid edit instructions: {exc}') from exc

    job_id = uuid.uuid4().hex
    job_dir, _input_dir, output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)
    output_path = output_dir / 'result.pdf'

    try:
        require_runtime_readiness_or_503('edit_and_convert_from_path')
        source_path, source_filename, size_bytes = _resolve_source_path_input(request.source_path)
        edited_output_path = output_dir / f'edited{source_path.suffix.lower() or ".hwpx"}'
        instructions_path = metadata_dir / 'edit_instructions.json'
        instructions_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=source_filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=_default_source_content_type(source_path),
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted direct-path edit job %s for %s (%s bytes, %s ops)', job_id, source_path, size_bytes, len(payload['operations']))
        return ConvertResponse(job=job)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register direct-path edit job for %s', request.source_path)
        raise HTTPException(status_code=500, detail='Failed to register direct-path edit job.')


@app.post('/compile-authoring')
async def compile_authoring(
    file: UploadFile = File(...),
    authoring_markdown: str = Form(...),
    validation_json: str | None = Form(default=None),
    cleanup_placeholders: str | None = Form(default=None),
    policy_override_json: str | None = Form(default=None),
) -> JSONResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, _output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'

    try:
        await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        policy_override = parse_policy_override_json(policy_override_json)
        compiled = compile_authoring_payload(
            source_path,
            authoring_markdown,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(cleanup_placeholders),
            policy_override=policy_override,
        )
        _persist_compile_artifacts(metadata_dir, compiled)
        _persist_compile_context(metadata_dir, source_filename=filename, source_path=source_path, content_type=file.content_type)
        confirm_state = _save_confirm_state(job_id, _default_confirm_state(job_id, compiled))
        return JSONResponse({'ok': True, 'job_id': job_id, 'confirm_state': confirm_state, 'confirm_brief': _render_confirm_brief(confirm_state), **compiled})
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to compile authoring input: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to compile authoring input for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to compile authoring input.')

    finally:
        await file.close()


@app.post('/compile-authoring-from-path')
def compile_authoring_from_path(request: CompileAuthoringFromPathRequest) -> JSONResponse:
    job_id = uuid.uuid4().hex
    job_dir, _input_dir, _output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)

    try:
        source_path, source_filename, _size_bytes = _resolve_source_path_input(request.source_path)
        validation_raw = _parse_json_request_field('validation_json', request.validation_json, allow_none=True)
        policy_override_raw = _parse_json_request_field('policy_override_json', request.policy_override_json, allow_none=True)
        validation = normalize_validation(validation_raw or {})
        policy_override = normalize_policy_override(policy_override_raw or {})
        compiled = compile_authoring_payload(
            source_path,
            request.authoring_markdown,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(request.cleanup_placeholders),
            policy_override=policy_override,
        )
        _persist_compile_artifacts(metadata_dir, compiled)
        _persist_compile_context(
            metadata_dir,
            source_filename=source_filename,
            source_path=source_path,
            content_type=_default_source_content_type(source_path),
        )
        confirm_state = _save_confirm_state(job_id, _default_confirm_state(job_id, compiled))
        return JSONResponse({'ok': True, 'job_id': job_id, 'confirm_state': confirm_state, 'confirm_brief': _render_confirm_brief(confirm_state), **compiled})
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to compile authoring input: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to compile direct-path authoring input for %s', request.source_path)
        raise HTTPException(status_code=500, detail='Failed to compile direct-path authoring input.')


@app.get('/jobs/{job_id}/confirm-state', response_model=ConfirmStateResponse)
def get_confirm_state(job_id: str) -> ConfirmStateResponse:
    compiled = _load_compiled_plan_or_404(job_id)
    state = _load_confirm_state(job_id, compiled)
    return ConfirmStateResponse(job_id=job_id, state=state)


@app.get('/jobs/{job_id}/confirm-brief')
def get_confirm_brief(job_id: str) -> JSONResponse:
    compiled = _load_compiled_plan_or_404(job_id)
    state = _load_confirm_state(job_id, compiled)
    return JSONResponse({'ok': True, 'job_id': job_id, 'brief': _render_confirm_brief(state), 'state': state})


@app.post('/jobs/{job_id}/confirm-action', response_model=ConfirmStateResponse)
def confirm_action(job_id: str, request: ConfirmActionRequest) -> ConfirmStateResponse:
    compiled = _load_compiled_plan_or_404(job_id)
    metadata_dir = _metadata_dir_for_job(job_id)
    state = _load_confirm_state(job_id, compiled)
    action = str(request.action or '').strip().lower()
    if not action:
        raise HTTPException(status_code=400, detail='confirm action is required')

    if action == 'inspect_more':
        state['status'] = 'inspect_requested'
        state['inspection_payload'] = _build_confirm_inspection_payload(compiled)
        shared_table_blocker = _shared_table_conflict_blocker(compiled)
        state['available_actions'] = ['approve', 'regenerate', 'abort'] if compiled.get('compile_readiness', {}).get('status') == 'ready' and not shared_table_blocker else ['regenerate', 'abort']
        _append_confirm_action(state, action)
        _save_confirm_state(job_id, state)
        return ConfirmStateResponse(job_id=job_id, state=state)

    if action == 'abort':
        state['status'] = 'aborted'
        state['available_actions'] = []
        _append_confirm_action(state, action)
        _save_confirm_state(job_id, state)
        return ConfirmStateResponse(job_id=job_id, state=state)

    if action == 'regenerate':
        if request.authoring_markdown is None or not str(request.authoring_markdown).strip():
            raise HTTPException(status_code=400, detail='authoring_markdown is required for regenerate')
        source_path = _job_input_source_path(job_id)
        validation = normalize_validation(request.validation_json) if request.validation_json is not None else (compiled.get('edit_plan', {}).get('validation') or {})
        policy_override = normalize_policy_override(request.policy_override) if request.policy_override is not None else (compiled.get('policy_override') or {})
        regenerated = compile_authoring_payload(
            source_path,
            request.authoring_markdown,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(request.cleanup_placeholders),
            policy_override=policy_override,
        )
        _persist_compile_artifacts(metadata_dir, regenerated)
        context = _load_compile_context(metadata_dir)
        _persist_compile_context(
            metadata_dir,
            source_filename=str(context.get('source_filename') or source_path.name),
            source_path=source_path,
            content_type=context.get('content_type'),
        )
        state = _default_confirm_state(job_id, regenerated)
        _append_confirm_action(state, action)
        _save_confirm_state(job_id, state)
        return ConfirmStateResponse(job_id=job_id, state=state)

    if action == 'approve':
        compile_status = compiled.get('compile_readiness', {}).get('status')
        if compile_status != 'ready':
            raise HTTPException(status_code=409, detail=f'Cannot approve when compile status is {compile_status!r}')
        shared_table_blocker = _shared_table_conflict_blocker(compiled)
        if shared_table_blocker:
            raise HTTPException(status_code=409, detail=shared_table_blocker)
        instructions_payload = _build_authoring_instruction_payload(compiled)
        instructions_path = metadata_dir / 'edit_instructions.json'
        instructions_path.write_text(json.dumps(instructions_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        state['status'] = 'approved'
        state['approved_instruction_path'] = str(instructions_path)
        state['available_actions'] = ['inspect_more', 'regenerate', 'abort']
        _append_confirm_action(state, action)

        if request.enqueue_convert:
            require_runtime_readiness_or_503('edit_and_convert')
            if db.get_job(job_id):
                raise HTTPException(status_code=409, detail=f'Execution job already exists for {job_id}')
            source_path = _job_input_source_path(job_id)
            output_path = settings.jobs_root / job_id / 'output' / 'result.pdf'
            edited_output_path = settings.jobs_root / job_id / 'output' / f'edited{source_path.suffix.lower() or ".hwpx"}'
            context = _load_compile_context(metadata_dir)
            queued_job = db.enqueue_job(
                task_type='edit_and_convert',
                job_id=job_id,
                source_filename=str(context.get('source_filename') or source_path.name),
                source_path=source_path,
                output_path=output_path,
                instructions_path=instructions_path,
                edited_output_path=edited_output_path,
                job_dir=settings.jobs_root / job_id,
                file_size_bytes=source_path.stat().st_size,
                content_type=context.get('content_type'),
                max_attempts=settings.max_attempts,
            )
            state['status'] = 'queued_for_execution'
            state['queued_job'] = queued_job
        _save_confirm_state(job_id, state)
        return ConfirmStateResponse(job_id=job_id, state=state)

    raise HTTPException(status_code=400, detail=f'Unsupported confirm action: {action}')


@app.post('/clear-placeholders')
async def clear_placeholders(
    file: UploadFile = File(...),
    validation_json: str | None = Form(default=None),
) -> JSONResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, _output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'

    try:
        await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        compiled = build_clear_placeholders_payload(source_path, validation=validation)
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return JSONResponse({'ok': True, 'job_id': job_id, **compiled})
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to build placeholder cleanup plan: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to compile placeholder cleanup for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to compile placeholder cleanup.')
    finally:
        await file.close()


@app.post('/clear-placeholders-and-convert', response_model=ConvertResponse)
async def clear_placeholders_and_convert(
    file: UploadFile = File(...),
    validation_json: str | None = Form(default=None),
) -> ConvertResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, output_dir, metadata_dir, logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'
    output_path = output_dir / 'result.pdf'
    edited_output_path = output_dir / f'edited{suffix}'
    instructions_path = metadata_dir / 'edit_instructions.json'

    try:
        require_runtime_readiness_or_503('clear_placeholders_and_convert')
        size_bytes = await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        compiled = build_clear_placeholders_payload(source_path, validation=validation)
        instructions_payload = normalize_instruction_payload(compiled['edit_plan'])
        instructions_path.write_text(json.dumps(instructions_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=file.content_type,
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted clear-placeholders job %s for %s (%s bytes, %s ops)', job_id, filename, size_bytes, len(instructions_payload['operations']))
        return ConvertResponse(job=job)
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to register placeholder cleanup job: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register clear-placeholders job for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to register clear-placeholders job.')
    finally:
        await file.close()


@app.post('/compile-placeholder-fills')
async def compile_placeholder_fills(
    file: UploadFile = File(...),
    placeholder_fills_json: str = Form(...),
    validation_json: str | None = Form(default=None),
    cleanup_placeholders: str | None = Form(default=None),
) -> JSONResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, _output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'

    try:
        await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        compiled = compile_placeholder_fill_payload(
            source_path,
            placeholder_fills_json,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(cleanup_placeholders),
        )
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('placeholder_fill_spec.json').write_text(
            json.dumps(compiled['placeholder_fill_spec'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return JSONResponse({'ok': True, 'job_id': job_id, **compiled})
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to compile placeholder fills: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to compile placeholder fills for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to compile placeholder fills.')
    finally:
        await file.close()


@app.post('/fill-placeholders-and-convert', response_model=ConvertResponse)
async def fill_placeholders_and_convert(
    file: UploadFile = File(...),
    placeholder_fills_json: str = Form(...),
    validation_json: str | None = Form(default=None),
    cleanup_placeholders: str | None = Form(default=None),
) -> ConvertResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, output_dir, metadata_dir, logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'
    output_path = output_dir / 'result.pdf'
    edited_output_path = output_dir / f'edited{suffix}'
    instructions_path = metadata_dir / 'edit_instructions.json'

    try:
        size_bytes = await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        compiled = compile_placeholder_fill_payload(
            source_path,
            placeholder_fills_json,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(cleanup_placeholders),
        )
        instructions_payload = normalize_instruction_payload(compiled['edit_plan'])
        instructions_path.write_text(json.dumps(instructions_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('placeholder_fill_spec.json').write_text(
            json.dumps(compiled['placeholder_fill_spec'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=file.content_type,
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted placeholder-fill job %s for %s (%s bytes, %s ops)', job_id, filename, size_bytes, len(instructions_payload['operations']))
        return ConvertResponse(job=job)
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to register placeholder-fill job: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register placeholder-fill job for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to register placeholder-fill job.')
    finally:
        await file.close()


@app.post('/author-and-convert', response_model=ConvertResponse)
async def author_and_convert(
    file: UploadFile = File(...),
    authoring_markdown: str = Form(...),
    validation_json: str | None = Form(default=None),
    cleanup_placeholders: str | None = Form(default=None),
    policy_override_json: str | None = Form(default=None),
) -> ConvertResponse:
    filename = Path(file.filename or 'upload.hwpx').name
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type: {suffix or "<none>"}. Allowed: {settings.allowed_extensions_list}',
        )

    job_id = uuid.uuid4().hex
    job_dir, input_dir, output_dir, metadata_dir, logs_dir = create_job_layout(job_id)
    source_path = input_dir / f'source{suffix}'
    output_path = output_dir / 'result.pdf'
    edited_output_path = output_dir / f'edited{suffix}'
    instructions_path = metadata_dir / 'edit_instructions.json'

    try:
        size_bytes = await save_upload_to_job(file, source_path)
        validation = parse_validation_json(validation_json)
        policy_override = parse_policy_override_json(policy_override_json)
        compiled = compile_authoring_payload(
            source_path,
            authoring_markdown,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(cleanup_placeholders),
            policy_override=policy_override,
        )
        instructions_payload = _build_authoring_instruction_payload(compiled)
        instructions_path.write_text(json.dumps(instructions_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('style_roles.json').write_text(
            json.dumps(compiled['style_roles'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('content_spec.json').write_text(
            json.dumps(compiled['content_spec'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=file.content_type,
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted authoring job %s for %s (%s bytes, %s ops)', job_id, filename, size_bytes, len(instructions_payload['operations']))
        return ConvertResponse(job=job)
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to compile authoring input: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register authoring job for %s', filename)
        raise HTTPException(status_code=500, detail='Failed to register authoring job.')

    finally:
        await file.close()


@app.post('/author-and-convert-from-path', response_model=ConvertResponse)
def author_and_convert_from_path(request: AuthorAndConvertFromPathRequest) -> ConvertResponse:
    job_id = uuid.uuid4().hex
    job_dir, _input_dir, output_dir, metadata_dir, _logs_dir = create_job_layout(job_id)
    output_path = output_dir / 'result.pdf'

    try:
        require_runtime_readiness_or_503('author_and_convert_from_path')
        source_path, source_filename, size_bytes = _resolve_source_path_input(request.source_path)
        validation_raw = _parse_json_request_field('validation_json', request.validation_json, allow_none=True)
        policy_override_raw = _parse_json_request_field('policy_override_json', request.policy_override_json, allow_none=True)
        validation = normalize_validation(validation_raw or {})
        policy_override = normalize_policy_override(policy_override_raw or {})
        compiled = compile_authoring_payload(
            source_path,
            request.authoring_markdown,
            validation=validation,
            cleanup_policy=parse_cleanup_placeholders(request.cleanup_placeholders),
            policy_override=policy_override,
        )
        instructions_payload = _build_authoring_instruction_payload(compiled)
        instructions_path = metadata_dir / 'edit_instructions.json'
        edited_output_path = output_dir / f'edited{source_path.suffix.lower() or ".hwpx"}'
        instructions_path.write_text(json.dumps(instructions_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        metadata_dir.joinpath('template_map.json').write_text(
            json.dumps(compiled['template_map'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('style_roles.json').write_text(
            json.dumps(compiled['style_roles'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('content_spec.json').write_text(
            json.dumps(compiled['content_spec'], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        metadata_dir.joinpath('compiled_plan.json').write_text(
            json.dumps(compiled, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        job = db.enqueue_job(
            task_type='edit_and_convert',
            job_id=job_id,
            source_filename=source_filename,
            source_path=source_path,
            output_path=output_path,
            instructions_path=instructions_path,
            edited_output_path=edited_output_path,
            job_dir=job_dir,
            file_size_bytes=size_bytes,
            content_type=_default_source_content_type(source_path),
            max_attempts=settings.max_attempts,
        )
        logger.info('Accepted direct-path authoring job %s for %s (%s bytes, %s ops)', job_id, source_path, size_bytes, len(instructions_payload['operations']))
        return ConvertResponse(job=job)
    except (TemplateEngineError, EditOperationError) as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f'Failed to compile authoring input: {exc}') from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('Failed to register direct-path authoring job for %s', request.source_path)
        raise HTTPException(status_code=500, detail='Failed to register direct-path authoring job.')


@app.get('/jobs/{job_id}', response_model=JobStatusResponse)
def job_status(job_id: str) -> JobStatusResponse:
    return JobStatusResponse(job=get_job_or_404(job_id))


@app.get('/jobs/{job_id}/observation-status')
def get_job_observation_status(job_id: str) -> JSONResponse:
    job = get_job_or_404(job_id)
    metadata_dir = Path(str(job['job_dir'])) / 'metadata'
    payload = _read_json_if_exists(metadata_dir / 'observation_status.json')
    if payload is None:
        raise HTTPException(status_code=404, detail=f'Observation status not found for {job_id}')
    return JSONResponse(payload)


@app.get('/jobs/{job_id}/result')
def job_result(job_id: str):
    job = get_job_or_404(job_id)
    status = job['status']
    output_path = Path(job['output_path']) if job.get('output_path') else None

    if status != JobStatus.succeeded.value:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(error=f'Job is not ready. Current status: {status}', details=job).model_dump(),
        )

    if output_path is None or not output_path.exists():
        logger.error('Job %s marked succeeded but output is missing', job_id)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error='Job is marked succeeded but result file is missing.', details=job).model_dump(),
        )

    return FileResponse(
        path=output_path,
        media_type='application/pdf',
        filename=f'{Path(job["source_filename"]).stem}.pdf',
    )


@app.get('/jobs/{job_id}/edited')
def job_edited(job_id: str):
    job = get_job_or_404(job_id)
    status = job['status']
    edited_output_path = Path(job['edited_output_path']) if job.get('edited_output_path') else None

    if status != JobStatus.succeeded.value:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(error=f'Job is not ready. Current status: {status}', details=job).model_dump(),
        )

    if edited_output_path is None or not edited_output_path.exists():
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(error='Edited output is not available for this job.', details=job).model_dump(),
        )

    suffix = edited_output_path.suffix.lower() or '.hwpx'
    media_type = 'application/octet-stream'
    if suffix == '.hwpx':
        media_type = 'application/zip'

    return FileResponse(
        path=edited_output_path,
        media_type=media_type,
        filename=f'{Path(job["source_filename"]).stem}-edited{suffix}',
    )


@app.get('/jobs/{job_id}/edit-summary')
def job_edit_summary(job_id: str):
    return load_job_metadata_json(job_id, 'edit_summary.json', 'Edit summary is not available for this job.')


@app.get('/jobs/{job_id}/execution-result')
def job_execution_result(job_id: str):
    return load_job_metadata_json(job_id, 'execution_result.json', 'Execution result is not available for this job.')


@app.get('/jobs/{job_id}/validation-artifact')
def job_validation_artifact(job_id: str):
    return load_job_metadata_json(job_id, 'validation_artifact.json', 'Validation artifact is not available for this job.')


@app.get('/jobs/{job_id}/validation-report')
def job_validation_report(job_id: str):
    return load_job_metadata_json(job_id, 'validation_report.json', 'Validation report is not available for this job.')


@app.get('/jobs/{job_id}/bundle-manifest')
def job_bundle_manifest(job_id: str):
    return load_job_metadata_json(job_id, 'bundle_manifest.json', 'Bundle manifest is not available for this job.')


@app.get('/jobs/{job_id}/evidence-event')
def job_evidence_event(job_id: str):
    return load_job_metadata_json(job_id, 'evidence_event.json', 'Evidence event is not available for this job.')


@app.get('/jobs/{job_id}/evidence-index')
def job_evidence_index(job_id: str):
    return load_job_metadata_json(job_id, 'evidence_index.json', 'Evidence index is not available for this job.')


@app.get('/jobs/{job_id}/failure-summary')
def job_failure_summary(job_id: str):
    return load_job_metadata_json(job_id, 'failure_summary.json', 'Failure summary is not available for this job.')


@app.get('/jobs/{job_id}/runtime-status')
def job_runtime_status(job_id: str):
    return load_job_runtime_status(job_id)


if __name__ == '__main__':
    uvicorn.run('app.api_server:app', host=settings.api_host, port=settings.api_port, reload=False)
