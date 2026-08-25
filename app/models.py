from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


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
