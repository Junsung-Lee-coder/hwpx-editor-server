from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobStatus(StrEnum):
    queued = 'queued'
    running = 'running'
    succeeded = 'succeeded'
    failed = 'failed'


class JobRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: JobStatus
    task_type: str = 'convert'
    source_filename: str
    source_path: str
    output_path: Optional[str] = None
    instructions_path: Optional[str] = None
    edited_output_path: Optional[str] = None
    job_dir: str
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 1
    worker_name: Optional[str] = None
    error: Optional[str] = None
    file_size_bytes: int = 0
    content_type: Optional[str] = None


class ConvertResponse(BaseModel):
    ok: bool = True
    job: JobRecord


class JobStatusResponse(BaseModel):
    ok: bool = True
    job: JobRecord


class HealthResponse(BaseModel):
    ok: bool = True
    status: str
    api_host: str
    api_port: int
    spool_root: str
    db_path: str
    queue_depth: int
    running_jobs: int


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    details: Optional[Any] = None


class ConfirmActionRequest(BaseModel):
    action: str
    authoring_markdown: Optional[str] = None
    validation_json: Optional[Any] = None
    cleanup_placeholders: Optional[str] = None
    policy_override: Optional[Any] = None
    enqueue_convert: bool = False


class EditAndConvertFromPathRequest(BaseModel):
    source_path: str
    instructions_json: Any


class CompileAuthoringFromPathRequest(BaseModel):
    source_path: str
    authoring_markdown: str
    validation_json: Optional[Any] = None
    cleanup_placeholders: Optional[str] = None
    policy_override_json: Optional[Any] = None


class AuthorAndConvertFromPathRequest(BaseModel):
    source_path: str
    authoring_markdown: str
    validation_json: Optional[Any] = None
    cleanup_placeholders: Optional[str] = None
    policy_override_json: Optional[Any] = None


class ConfirmStateResponse(BaseModel):
    ok: bool = True
    job_id: str
    state: dict[str, Any]


class InteractiveSessionOpenRequest(BaseModel):
    source_path: str
    session_label: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractiveCommandRequest(BaseModel):
    session_id: Optional[str] = None
    result_state: str = 'succeeded'
    summary: Optional[str] = None
    popup_status: Optional[dict[str, Any]] = None
    failure_reason: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractiveFindRequest(InteractiveCommandRequest):
    query: Optional[str] = None
    resolved_target_id: Optional[str] = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class InteractiveChooseRequest(InteractiveCommandRequest):
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_candidate_index: Optional[int] = None
    selected_candidate: Optional[dict[str, Any]] = None
    selection_reason: Optional[str] = None


class InteractiveEnterRequest(InteractiveCommandRequest):
    resolved_target_id: Optional[str] = None
    cursor_anchor: Optional[dict[str, Any]] = None
    target_summary: dict[str, Any] = Field(default_factory=dict)


class InteractiveLockRequest(InteractiveCommandRequest):
    inspect_snapshot_id: Optional[str] = None
    rule: Optional[str] = None
    on_mismatch: Optional[str] = None
    mismatch_reason_code: Optional[str] = None
    lock_details: dict[str, Any] = Field(default_factory=dict)


class InteractiveVerificationRequest(InteractiveCommandRequest):
    verification_mode: Optional[str] = None
    expected_present: list[str] = Field(default_factory=list)
    expected_absent: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    gui: dict[str, Any] = Field(default_factory=dict)


class InteractiveApplyRequest(InteractiveCommandRequest):
    operation: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class InteractiveUndoRequest(InteractiveCommandRequest):
    reason: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)


class InteractiveCloseRequest(InteractiveCommandRequest):
    outcome: str = 'closed'


class InteractiveVerificationStatus(BaseModel):
    state: str = 'pending'
    verification_mode: Optional[str] = None
    summary: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    gui: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[str] = None


class InteractivePopupStatus(BaseModel):
    state: str = 'not_reported'
    security_module_name: Optional[str] = None
    security_module_dll: Optional[str] = None
    popup_detected: Optional[bool] = None
    summary: Optional[str] = None
    detail: Optional[str] = None
    updated_at: Optional[str] = None


class InteractiveFailureReason(BaseModel):
    code: Optional[str] = None
    command: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[str] = None
    updated_at: Optional[str] = None


class InteractiveCommandProgress(BaseModel):
    command: Optional[str] = None
    state: str = 'idle'
    completed_count: int = 0
    total_count: int = 0
    next_expected_command: Optional[str] = None
    last_updated_at: Optional[str] = None


class InteractiveSessionRecord(BaseModel):
    session_id: str
    state: str
    workflow_mode: str = 'interactive'
    runtime_lane: str = '.51'
    source_path: str
    source_filename: str
    file_size_bytes: int = 0
    content_type: Optional[str] = None
    session_label: Optional[str] = None
    created_at: str
    updated_at: str
    closed_at: Optional[str] = None
    current_command: Optional[str] = None
    command_progress: InteractiveCommandProgress = Field(default_factory=InteractiveCommandProgress)
    command_history: list[dict[str, Any]] = Field(default_factory=list)
    find_result: dict[str, Any] = Field(default_factory=dict)
    choose_result: dict[str, Any] = Field(default_factory=dict)
    selected_candidate: Optional[dict[str, Any]] = None
    active_target: dict[str, Any] = Field(default_factory=dict)
    runtime_preparation: dict[str, Any] = Field(default_factory=dict)
    lock_status: dict[str, Any] = Field(default_factory=dict)
    verify_pre: InteractiveVerificationStatus = Field(default_factory=InteractiveVerificationStatus)
    apply_result: dict[str, Any] = Field(default_factory=dict)
    verify_post: InteractiveVerificationStatus = Field(default_factory=InteractiveVerificationStatus)
    undo_result: dict[str, Any] = Field(default_factory=dict)
    popup_status: InteractivePopupStatus = Field(default_factory=InteractivePopupStatus)
    failure_reason: Optional[InteractiveFailureReason] = None
    readiness: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    live_runtime: dict[str, Any] = Field(default_factory=dict)
    operator_status_lines: list[str] = Field(default_factory=list)
    operator_status_text: str = ''
    artifacts: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractiveSessionResponse(BaseModel):
    ok: bool = True
    session: InteractiveSessionRecord


# ---------------------------------------------------------------------------
# Targeted four-side cell-margin getter: shared strict request models and the
# canonical request hash (R48-A01 sections 3/5). These models define the
# closed public request surface shared by the REST route and the MCP adapter.
# They depend only on the standard library and Pydantic; native COM, FastAPI
# and the optional MCP SDK are intentionally not imported.
# ---------------------------------------------------------------------------
_SESSION_ID_PATTERN = r'^[a-f0-9]{32}$'
_TARGET_ID_PATTERN = r'^ctrl/[0-9]+/tbl/[^/]+$'
_GENERATION_SID_PATTERN = r'^local-cli/live-document/v1:(?P<sid>[a-f0-9]{32}):sha256:(?P<digest>[a-f0-9]{64})$'
# The native control-inventory proof is a truncated SHA-256 (24 lowercase hex
# characters) with an optional ``sha256:`` prefix.  The setter's wider legacy
# forms are deliberately not accepted by this getter.
_EXPECTED_HASH_PATTERN = r'^(?:sha256:)?[a-f0-9]{24}$'

_HWPUNIT_INT_MAX = 2147483647


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CellMarginsGetTarget(_ClosedModel):
    """Exact live target binding for one four-side cell-margin observation."""

    document_id: Annotated[str, Field(pattern=_SESSION_ID_PATTERN)]
    expected_document_generation: Annotated[str, Field(pattern=_GENERATION_SID_PATTERN)]
    target_id: Annotated[str, Field(min_length=1, max_length=500, pattern=_TARGET_ID_PATTERN)]
    expected_hash: Annotated[str, Field(pattern=_EXPECTED_HASH_PATTERN)]
    expected_page: int = Field(ge=1, le=10000)
    expected_cell_page: int = Field(ge=1, le=10000)
    page_from: int = Field(ge=1, le=10000)
    page_to: int = Field(ge=1, le=10000)
    cell_pos: Annotated[list[int], Field(min_length=3, max_length=3)]
    cell_addr: Annotated[list[int], Field(min_length=2, max_length=2)]
    section_anchor: Annotated[str, Field(min_length=1, max_length=1024)]
    max_controls: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def bind_exact_live_target(self) -> 'CellMarginsGetTarget':
        for name in ('cell_pos', 'cell_addr'):
            values = getattr(self, name)
            if any(isinstance(item, bool) or item < 0 or item > _HWPUNIT_INT_MAX for item in values):
                raise ValueError(f'{name} entries must be nonnegative integers <= {_HWPUNIT_INT_MAX}')
        if self.page_from > self.page_to:
            raise ValueError('page_from must not exceed page_to')
        if not (self.page_from <= self.expected_page <= self.page_to):
            raise ValueError('expected_page must lie within the closed page range')
        if not (self.page_from <= self.expected_cell_page <= self.page_to):
            raise ValueError('expected_cell_page must lie within the closed page range')
        generation_parts = self.expected_document_generation.split(':')
        if len(generation_parts) != 4 or generation_parts[1] != self.document_id:
            raise ValueError('expected_document_generation must carry the same session id as document_id')
        if self.target_id != self.target_id.strip():
            raise ValueError('target_id must not have surrounding whitespace')
        if self.section_anchor != self.section_anchor.strip():
            raise ValueError('section_anchor must not have leading or trailing whitespace')
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.section_anchor):
            raise ValueError('section_anchor must not contain control characters')
        return self

    def normalized_expected_hash(self) -> str:
        raw = self.expected_hash
        return raw if raw.startswith('sha256:') else 'sha256:' + raw


class CellMarginsGetRequest(_ClosedModel):
    """Outer REST/MCP request: one explicit session plus one exact target."""

    session_id: Annotated[str, Field(pattern=_SESSION_ID_PATTERN)]
    request: CellMarginsGetTarget

    @model_validator(mode='after')
    def bind_session_identity(self) -> 'CellMarginsGetRequest':
        if self.request.document_id != self.session_id:
            raise ValueError('request.document_id must equal the outer session_id')
        return self


def canonical_cell_margins_request_sha256(request: CellMarginsGetRequest) -> str:
    """Compute the canonical full-request digest shared by REST and MCP.

    The model is re-validated first so callers always hash the normalized
    shape (defaulted ``max_controls``, prefixed ``expected_hash``).
    """

    validated = CellMarginsGetRequest.model_validate(request.model_dump(mode='json'))
    payload = {
        'operation': 'cell_margins_get',
        'session_id': validated.session_id,
        'request': validated.request.model_dump(mode='json'),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()
