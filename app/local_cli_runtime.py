from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from queue import Empty, Queue
import hashlib
import math
import csv
import io
import subprocess
import os
import threading
import time
import uuid
from typing import Any, Callable

from app.atomic_json import read_json_object, update_json_object
from app.edit_ops import _capture_nearby_text_context, _snapshot_cursor_context
from app.native_actions import NativeActionRunner
from app.runtime_state import update_runtime_status, utc_now_iso
from app.worker import (
    close_hwp_instance,
    collect_hwp_snapshot,
    collect_process_snapshot,
    create_hwp_instance_with_recovery,
    emit_runtime_observation,
    kill_hwp_runtime,
    list_available_pdf_export_primitives,
    save_hwp_as,
    start_runtime_watchdog,
)


class LocalCliRuntimeError(RuntimeError):
    pass


class LocalCliRuntimeTimeoutError(LocalCliRuntimeError):
    """A command timed out and the live session was terminalized.

    Native COM calls cannot be safely interrupted from the request thread.  A
    timeout therefore closes admission, records a command identity, and lets
    the worker finish/clean up the in-flight call so the result can be
    reconciled instead of allowing a retry to race it.
    """

    def __init__(self, message: str, *, command_id: str, command_state: str):
        super().__init__(message)
        self.command_id = command_id
        self.command_state = command_state


class LocalCliNativeCustodyHeldError(LocalCliRuntimeError):
    """A command reached a native mutator and its outcome is unknown; the owning STA is retained, not released.

    The original cause (an exception or a control-flow signal such as ``KeyboardInterrupt``) is ``__cause__``.
    Errors raised while reporting the held state (journal/status/pruning) are kept in ``reporting_errors``.
    """

    mutation_state = 'unknown'
    mutation_may_have_persisted = True

    def __init__(self, message: str):
        super().__init__(message)
        self.reporting_errors: list[BaseException] = []


_HELD_CLOSE_MESSAGE = (
    'The live local CLI session holds a native command whose outcome is unknown; '
    'it is retained and is not closed, discarded or released by this runtime.'
)
MAX_RUNTIME_COMMAND_HISTORY = 100
MAX_RUNTIME_JOURNAL_VALUE_CHARS = 2048
MAX_RUNTIME_JOURNAL_LIST_ITEMS = 100
MAX_RUNTIME_JOURNAL_DEPTH = 6
_SENSITIVE_JOURNAL_KEYS = {
    'text', 'selected_text', 'typed_text', 'replaced_text', 'content', 'preview',
    'current_paragraph_preview', 'document_text', 'document', 'nearby', 'query', 'find_query',
    'source_path', 'document_path', 'working_copy_path', 'raw_text_path',
    'path', 'paths', 'artifact_path', 'artifact_paths', 'manifest_path', 'output_path',
    'filename', 'source_filename', 'directory', 'root', 'working_directory',
    'token', 'secret', 'password', 'cookie', 'credential', 'error', 'exception', 'warning',
}
_SENSITIVE_JOURNAL_KEY_MARKERS = tuple(sorted(_SENSITIVE_JOURNAL_KEYS, key=len, reverse=True))
_RECONCILIATION_SCHEMA_VERSION = 1
_RECOVERY_STATES = {
    'none',
    'pending_execution',
    'quarantined',
    'saving',
    'failed',
    'preserved',
}
_RECOVERY_ARTIFACT_KINDS = {'export', 'screenshot', 'working-copy', 'working_copy', 'recovery'}
_KNOWN_NON_ENVELOPE_COMMANDS = {
    'health_probe', 'find', 'info', 'move', 'select', 'replace', 'cell', 'cellmove',
    'cursormove', 'cell-replace', 'type', 'anchor-insert', 'figure-section',
    'image', 'image-at-anchor', 'fontsize', 'bold', 'font', 'bullet', 'table',
    'list', 'action', 'undo', 'redo', 'where', 'save', 'export', 'screenshot',
    'insert-text-file', 'open', 'close',
}
_MUTATING_COMMANDS = {
    'replace', 'cell-replace', 'type', 'anchor-insert', 'figure-section',
    'image', 'image-at-anchor', 'fontsize', 'bold', 'font', 'bullet', 'table',
    'list', 'action', 'undo', 'redo', 'insert-text-file', 'command-bundle', 'save',
}


def _native_diagnostic(site: object, error: BaseException) -> dict[str, str]:
    """Fixed labels from already available Python state; no exception callbacks/values.

    This is a last-entered caller boundary, never a native root-cause verdict.
    Unknown classes and sites remain UNKNOWN; no messages, repr, traceback or SDK reads.
    """
    sites = ('command_handler', 'handler_returned', 'save_precondition', 'save_native', 'save_file_readback',
             'save_custody_readback', 'save_location_readback', 'action_pre_read',
             'action_native', 'action_post_read', 'close_wrapper', 'bundle_step')
    diagnostic = {'site': site if type(site) is str and site in sites else 'UNKNOWN',
                  'causal_class': _exception_label(error)}
    if type(site) is str and site == 'bundle_step':
        # The bundle catches this exception rather than letting it reach the
        # worker's escaped-error diagnostic. Keep only exact trusted types and
        # two explicit cause edges, never implicit context or arbitrary fields.
        from app.edit_ops import EditOperationError
        from app.local_cli_service_support import LocalCliMutationError
        from app.native_numbering import (
            NativeNumberingInputError, NativeNumberingRefusal, NativeNumberingMutationError,
        )
        diagnostic.update(cause_class='UNKNOWN', cause2_class='UNKNOWN', refusal_code='UNKNOWN')
        # Optional fixed source-bound phase, never arbitrary error text or
        # release authority. Exact containers/keys/values avoid callbacks even
        # when a trusted exception's replaceable dictionary is hostile.
        if type(error) is LocalCliRuntimeError or type(error) is LocalCliMutationError:
            data = BaseException.__dict__['__dict__'].__get__(error)
            phase = reason = None
            if type(data) is dict and len(data) <= 16:
                for key, value in dict.items(data):
                    if type(key) is str and type(value) is str:
                        if key == 'numbering_phase':
                            phase = value
                        elif key == 'numbering_reason':
                            reason = value
            if type(phase) is str and type(reason) is str and (phase, reason) in (
                ('preflight', 'preservation_unproven'), ('pre_target', 'finder_failed'),
                ('native', 'native_failed'), ('restore', 'restore_unproven'),
                ('preservation', 'preservation_unproven'),
            ):
                diagnostic.update(numbering_phase=phase, numbering_reason=reason)
        current = error
        for field in ('causal_class', 'cause_class', 'cause2_class'):
            label = _exception_label(current)
            for cls, name in ((EditOperationError, 'EditOperationError'),
                              (LocalCliMutationError, 'LocalCliMutationError'),
                              (NativeNumberingInputError, 'NativeNumberingInputError'),
                              (NativeNumberingRefusal, 'NativeNumberingRefusal'),
                              (NativeNumberingMutationError, 'NativeNumberingMutationError')):
                if type(current) is cls:
                    label = name
                    break
            diagnostic[field] = label
            if label == 'UNKNOWN':
                break
            if type(current) is NativeNumberingRefusal:
                data = BaseException.__dict__['__dict__'].__get__(current)
                if type(data) is dict and len(data) <= 16:
                    for key, value in dict.items(data):
                        if type(key) is str and key == 'code' and type(value) is str and value in (
                            'control_enumeration_unavailable', 'existing_number_or_bullet', 'existing_page_number',
                            'foreign_outline_definition', 'header_footer_unprovable', 'native_defaults_unavailable',
                            'native_parameters_unavailable', 'native_unavailable', 'outline_definition_unreadable',
                            'outline_usage_unprovable', 'paragraph_shape_unreadable', 'preservation_unprovable',
                            'protection_unavailable', 'section_count_unprovable', 'target_ambiguous', 'target_moved',
                            'target_not_found', 'target_not_in_body', 'target_unidentifiable', 'target_unlocatable',
                            'text_readback_unavailable',
                        ):
                            diagnostic['refusal_code'] = value
            if field == 'cause2_class':
                break
            current = BaseException.__dict__['__cause__'].__get__(current)
            if current is None:
                break
    return diagnostic


def _exception_label(error: BaseException) -> str:
    # Identity comparisons only: even type.__getattribute__ can invoke a
    # user metaclass's __name__ descriptor. No arbitrary metadata is needed.
    for cls, label in ((ValueError, 'ValueError'), (TypeError, 'TypeError'),
                       (OSError, 'OSError'), (RuntimeError, 'RuntimeError'),
                       (LocalCliRuntimeError, 'LocalCliRuntimeError'),
                       (LocalCliNativeCustodyHeldError, 'LocalCliNativeCustodyHeldError'),
                       (KeyboardInterrupt, 'KeyboardInterrupt'), (SystemExit, 'SystemExit')):
        if type(error) is cls:
            return label
    return 'UNKNOWN'


def _filesystem_identity(path: Path) -> dict[str, int] | None:
    try:
        stat_result = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return {
        'device': int(stat_result.st_dev),
        'inode': int(stat_result.st_ino),
        'mode': int(stat_result.st_mode),
    }


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def normalize_command_outcome(
    command_name: str,
    result: Any = None,
    *,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Normalize a handler outcome before any public/journal redaction.

    ``ok`` is intentionally strict.  A bundle's aggregate is calculated from
    the complete raw step list so a bounded public projection cannot hide a
    later failed step.
    """

    command = str(command_name or '')
    if error is not None:
        # Exception instances are untrusted callbacks, not no-effect evidence.
        may_have_mutated = command in _MUTATING_COMMANDS or command == 'pycall'
        return {
            'semantic_ok': False,
            'may_have_mutated': may_have_mutated,
            'delta_dirty': None,
            'step_count': 0,
            'failed_step_count': 0,
            'error_code': _exception_label(error),
        }

    if not isinstance(result, dict):
        return {
            # A mutating command without a result envelope is not evidence of
            # success.  Keep non-envelope read/status commands compatible with
            # their existing handlers, but fail closed for mutation commands.
            'semantic_ok': False if command in _MUTATING_COMMANDS else (
                True if command in _KNOWN_NON_ENVELOPE_COMMANDS else None
            ),
            'may_have_mutated': command in _MUTATING_COMMANDS,
            'delta_dirty': None,
            'step_count': 0,
            'failed_step_count': 0,
        }

    explicit_ok = result.get('ok') if 'ok' in result else None
    malformed_ok = 'ok' in result and not isinstance(explicit_ok, bool)
    semantic_ok: bool | None
    if malformed_ok:
        semantic_ok = False
    elif isinstance(explicit_ok, bool):
        semantic_ok = explicit_ok
    elif command in _KNOWN_NON_ENVELOPE_COMMANDS:
        semantic_ok = True
    else:
        semantic_ok = None

    raw_steps = result.get('steps')
    step_count = len(raw_steps) if isinstance(raw_steps, list) else 0
    failed_step_count = 0
    may_have_mutated = bool(result.get('may_have_mutated') is True)
    explicit_mutation_evidence = 'may_have_mutated' in result or 'mutation_may_have_persisted' in result
    may_have_mutated = may_have_mutated or bool(result.get('mutation_may_have_persisted') is True)
    if isinstance(raw_steps, list):
        for step in raw_steps:
            if not isinstance(step, dict) or not isinstance(step.get('ok'), bool) or step.get('ok') is False:
                failed_step_count += 1
            if isinstance(step, dict) and (
                'dirty' in step
                or 'mutation_may_have_persisted' in step
                or (
                    isinstance(step.get('mutation'), dict)
                    and 'may_have_persisted' in step['mutation']
                )
            ):
                explicit_mutation_evidence = True
            if (
                isinstance(step, dict)
                and (
                    step.get('dirty') is True
                    or step.get('mutation_may_have_persisted') is True
                    or (
                        isinstance(step.get('mutation'), dict)
                        and step['mutation'].get('may_have_persisted') is True
                    )
                )
            ):
                may_have_mutated = True
        if failed_step_count:
            semantic_ok = False

    delta_dirty = result.get('dirty') if isinstance(result.get('dirty'), bool) else None
    may_have_mutated = may_have_mutated or delta_dirty is True
    if command in _MUTATING_COMMANDS and semantic_ok is False and not explicit_mutation_evidence:
        may_have_mutated = True
    return {
        'semantic_ok': semantic_ok,
        'may_have_mutated': may_have_mutated,
        'delta_dirty': delta_dirty,
        'step_count': step_count,
        'failed_step_count': failed_step_count,
        'error_code': 'MALFORMED_OK' if malformed_ok else None,
    }


def _artifact_kind_from_key(key: str) -> str | None:
    folded = str(key or '').casefold()
    for kind in ('working-copy', 'working_copy', 'screenshot', 'export', 'recovery'):
        if kind in folded:
            return kind
    return None


def _bounded_journal_value(value: Any, *, depth: int = 0, key_hint: str | None = None) -> Any:
    """Convert command results/errors to bounded, non-sensitive JSON data."""

    if depth >= MAX_RUNTIME_JOURNAL_DEPTH:
        return '<nested value omitted>'
    if key_hint and any(marker in key_hint.casefold() for marker in _SENSITIVE_JOURNAL_KEY_MARKERS):
        return '<redacted>'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if len(value) <= MAX_RUNTIME_JOURNAL_VALUE_CHARS:
            return value
        return value[:MAX_RUNTIME_JOURNAL_VALUE_CHARS] + '…'
    if isinstance(value, BaseException):
        return _bounded_journal_value(str(value), depth=depth + 1)
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_RUNTIME_JOURNAL_LIST_ITEMS:
                bounded['__truncated__'] = True
                break
            key_text = str(key)[:MAX_RUNTIME_JOURNAL_VALUE_CHARS]
            bounded[key_text] = _bounded_journal_value(item, depth=depth + 1, key_hint=key_text)
        return bounded
    if isinstance(value, (list, tuple)):
        bounded_items = [_bounded_journal_value(item, depth=depth + 1) for item in value[:MAX_RUNTIME_JOURNAL_LIST_ITEMS]]
        if len(value) > MAX_RUNTIME_JOURNAL_LIST_ITEMS:
            bounded_items.append('<items omitted>')
        return bounded_items
    return _bounded_journal_value(repr(value), depth=depth + 1)


def _bounded_command_result(value: Any) -> Any:
    """Keep only reconciliation-relevant fields from native command output."""

    if not isinstance(value, dict):
        return _bounded_journal_value(value)
    allowed = {
        'ok', 'dirty', 'artifact_path', 'artifacts', 'location',
        'after_location', 'before_location', 'warnings', 'steps',
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if key == 'steps' and isinstance(item, list):
            compact_steps: list[Any] = []
            for step in item[:MAX_RUNTIME_JOURNAL_LIST_ITEMS]:
                if not isinstance(step, dict):
                    continue
                compact_steps.append({
                    field: _bounded_journal_value(step[field], key_hint=field)
                    for field in ('index', 'label', 'op', 'ok', 'dirty', 'error', 'mutation')
                    if field in step
                })
            result[key] = compact_steps
        else:
            result[key] = _bounded_journal_value(item, key_hint=key)
    return result


def _safe_hwp_attr(hwp: object, name: str, default: Any = None) -> Any:
    try:
        return getattr(hwp, name)
    except Exception:
        return default


def _safe_ctrl_summary(ctrl: Any) -> dict[str, Any] | None:
    if ctrl is None:
        return None
    payload: dict[str, Any] = {}
    for attr in ('CtrlID', 'UserDesc'):
        value = _safe_hwp_attr(ctrl, attr)
        if value not in (None, ''):
            payload[attr] = value
    return payload or None


def current_windows_session_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        'platform': os.sys.platform,
        'pid': os.getpid(),
        'session_name': os.environ.get('SESSIONNAME') or None,
    }
    if os.sys.platform != 'win32':
        snapshot['interactive'] = True
        return snapshot
    try:
        import ctypes

        session_id = ctypes.c_uint()
        kernel32 = ctypes.windll.kernel32
        ok = bool(kernel32.ProcessIdToSessionId(ctypes.c_uint(os.getpid()), ctypes.byref(session_id)))
        snapshot['session_query_ok'] = ok
        if ok:
            snapshot['session_id'] = int(session_id.value)
    except Exception as exc:
        snapshot['session_query_error'] = repr(exc)
    session_id_value = snapshot.get('session_id')
    snapshot['interactive'] = session_id_value not in (0, '0')
    return snapshot


def ensure_session_layout(session_root: Path) -> None:
    for relative in ('upload', 'working', 'output', 'metadata', 'logs'):
        (session_root / relative).mkdir(parents=True, exist_ok=True)


def command_journal_path(session_root: Path) -> Path:
    ensure_session_layout(session_root)
    return session_root / 'metadata' / 'command-journal.json'


def _raw_journal_command(payload: dict[str, Any] | None, command_id: str | None = None) -> dict[str, Any] | None:
    commands = payload.get('commands') if isinstance(payload, dict) else None
    if not isinstance(commands, list):
        return None
    selected = None
    if command_id:
        for item in commands:
            if isinstance(item, dict) and item.get('command_id') == command_id:
                selected = item
                break
    elif commands:
        selected = commands[-1]
    if not isinstance(selected, dict):
        return None
    return dict(selected)


def _public_command_record(record: dict[str, Any] | None, command_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {'command_id': command_id, 'state': 'unknown', 'reconcilable': False}
    public = dict(record)
    public.pop('reconciliation_data', None)
    return public


def _journal_command_status(payload: dict[str, Any] | None, command_id: str | None = None) -> dict[str, Any]:
    return _public_command_record(_raw_journal_command(payload, command_id), command_id)


def read_command_journal(session_root: Path, command_id: str | None = None) -> dict[str, Any]:
    """Read one durable command status without requiring a live COM thread."""

    payload = read_json_object(command_journal_path(session_root))
    return _journal_command_status(payload, command_id)


def read_command_custody(session_root: Path, command_id: str) -> dict[str, Any]:
    """Read the private, session-root-bound record for service reconciliation."""

    payload = read_json_object(command_journal_path(session_root))
    record = _raw_journal_command(payload, command_id)
    if record is None:
        return {
            'command_id': command_id,
            'state': 'unknown',
            'reconcilable': False,
            'reconciliation_data': None,
        }
    return record


def acknowledge_command_journal(session_root: Path, command_id: str) -> dict[str, Any]:
    """Acknowledge only a custody-preserved command after service projection."""

    journal_path = command_journal_path(session_root)

    def _ack(payload: dict[str, Any]) -> dict[str, Any]:
        commands_raw = payload.get('commands')
        commands: list[Any] = commands_raw if isinstance(commands_raw, list) else []
        found = False
        for item in commands:
            if not isinstance(item, dict) or item.get('command_id') != command_id:
                continue
            found = True
            data = item.get('reconciliation_data') if isinstance(item.get('reconciliation_data'), dict) else {}
            recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
            if (
                item.get('state') in {'completed_after_timeout', 'failed_after_timeout'}
                and recovery.get('state') == 'preserved'
            ):
                item['reconciled'] = True
                item['reconciled_at'] = utc_now_iso()
        if not found:
            raise LocalCliRuntimeError(f'Local CLI command is not present in the durable journal: {command_id}')
        payload['commands'] = commands
        return payload

    updated = update_json_object(
        journal_path,
        _ack,
        default={
            'schema_version': 'local-cli/command-journal/v1',
            'session_id': session_root.name,
            'commands': [],
        },
    )
    return _journal_command_status(updated, command_id)


def reconcile_command_journal(session_root: Path, command_id: str) -> dict[str, Any]:
    """Legacy journal-only acknowledgement retained for old test seams.

    The production runtime manager uses ``read_command_journal`` followed by
    the native custody/acknowledgement pair; it never calls this compatibility
    helper for a live reconciliation.
    """

    journal_path = command_journal_path(session_root)

    def _mark(payload: dict[str, Any]) -> dict[str, Any]:
        commands_raw = payload.get('commands')
        commands: list[Any] = commands_raw if isinstance(commands_raw, list) else []
        found = False
        for item in commands:
            if not isinstance(item, dict) or item.get('command_id') != command_id:
                continue
            found = True
            if item.get('state') in {'completed_after_timeout', 'failed_after_timeout'}:
                item['reconciled'] = True
                item['reconciled_at'] = utc_now_iso()
        if not found:
            raise LocalCliRuntimeError(f'Local CLI command is not present in the durable journal: {command_id}')
        payload['commands'] = commands
        return payload

    updated = update_json_object(
        journal_path,
        _mark,
        default={
            'schema_version': 'local-cli/command-journal/v1',
            'session_id': session_root.name,
            'commands': [],
        },
    )
    return _journal_command_status(updated, command_id)


def session_log_path(session_root: Path) -> Path:
    ensure_session_layout(session_root)
    return session_root / 'logs' / 'local_cli_runtime.log'


def snapshot_live_location(
    *,
    hwp: object,
    source_filename: str,
    working_copy_id: str,
    include_nearby_context: bool = True,
    include_document_snapshot: bool = True,
) -> dict[str, Any]:
    cursor = _snapshot_cursor_context(hwp)
    nearby = _capture_nearby_text_context(hwp) if include_nearby_context else {}
    hwp_snapshot = collect_hwp_snapshot(hwp) if include_document_snapshot else {}
    document_info = hwp_snapshot.get('document') if isinstance(hwp_snapshot.get('document'), dict) else {}
    doc_path = str(_safe_hwp_attr(hwp, 'Path') or document_info.get('FullName') or '').strip()
    doc_title = str(_safe_hwp_attr(hwp, 'Title') or document_info.get('Title') or source_filename or '').strip()
    is_modified = bool(_safe_hwp_attr(hwp, 'IsModified', False))
    cur_field_state = _safe_hwp_attr(hwp, 'CurFieldState')
    selection_mode = _safe_hwp_attr(hwp, 'SelectionMode')
    page_count = _safe_hwp_attr(hwp, 'PageCount')
    in_cell = cur_field_state == 1
    current_ctrl = _safe_ctrl_summary(_safe_hwp_attr(hwp, 'CurSelectedCtrl'))
    parent_ctrl = _safe_ctrl_summary(_safe_hwp_attr(hwp, 'ParentCtrl'))
    cell_ref = cursor.get('cell_ref') if isinstance(cursor.get('cell_ref'), dict) else None
    cursor_summary = (
        f"cell {cell_ref.get('addr')}"
        if in_cell and isinstance(cell_ref, dict) and cell_ref.get('addr')
        else f"pos {cursor.get('pos')}"
    )
    selection_summary = 'active selection' if cursor.get('has_selection') else 'none'
    if selection_mode not in (None, ''):
        selection_summary = f'{selection_summary} (mode={selection_mode})'
    current_preview = str(nearby.get('current_paragraph_preview') or '').strip()
    return {
        'document_name': doc_title or source_filename,
        'document_path': doc_path or None,
        'working_copy_id': working_copy_id,
        'cursor': cursor,
        'cursor_summary': cursor_summary,
        'selection_summary': selection_summary,
        'current_paragraph_preview': current_preview or None,
        'nearby_context': nearby,
        'document_snapshot': document_info,
        'document_is_modified': is_modified,
        'caret_in_table_cell': in_cell,
        'cur_field_state': cur_field_state,
        'selection_mode': selection_mode,
        'page_count': page_count,
        'current_selected_ctrl': current_ctrl,
        'parent_ctrl': parent_ctrl,
    }


def insert_text_at_caret(hwp: object, text: str) -> None:
    if '\n' in text or '\r' in text:
        raise LocalCliRuntimeError(
            'Multiline hwpx type is disabled because Hancom paragraph-break insertion is not verified as layout-safe. '
            'Use the bounded native multiline table-cell replacement primitive instead.'
        )
    if hasattr(hwp, 'insert_text'):
        hwp.insert_text(text)
        return

    _insert_text_with_hancom_action(hwp, text)


def insert_multiline_text_at_caret_native(hwp: object, text: str) -> dict[str, Any]:
    """Insert exact multiline cell text without importing file-level controls.

    Each visible line is inserted through Hancom's native ``InsertText`` action and
    each line boundary through ``BreakPara``.  This intentionally never calls
    ``set_text_file``/``SetTextFile`` because their ``insertfile`` path can import
    section/column-definition (``cold``) controls into a fixed-form document.
    """

    normalized = str(text).replace('\r\n', '\n').replace('\r', '\n')
    lines = normalized.split('\n')
    if not normalized:
        raise LocalCliRuntimeError('Native multiline insertion requires non-empty text.')

    action_count = 0
    break_count = 0
    for index, line in enumerate(lines):
        if index:
            _break_paragraph_with_hancom_action(hwp)
            action_count += 1
            break_count += 1
        if line:
            _insert_text_with_hancom_action(hwp, line)
            action_count += 1

    return {
        'strategy': 'hancom-native-inserttext-breakpara',
        'method': 'HAction.Execute:InsertText + HAction.Run:BreakPara',
        'attempt_mode': 'multiline-native-paragraph-actions',
        'line_count': len(lines),
        'paragraph_break_count': break_count,
        'native_action_count': action_count,
        'file_import_used': False,
    }


def replace_selection_with_text(hwp: object, text: str) -> dict[str, Any]:
    """Replace the active editor selection using Hancom's native text action.

    pyhwpx's helper insertion path can append at the selection edge on some live
    stacks. The native InsertText action is preferred for selected ranges because
    Hancom treats it like normal typing over a selection.
    """

    insert_text_at_caret(hwp, text)
    return {'strategy': 'HAction.Execute:InsertText'}


def _insert_text_with_hancom_action(hwp: object, text: str) -> None:

    haction = _safe_hwp_attr(hwp, 'HAction')
    hparameter_set = _safe_hwp_attr(hwp, 'HParameterSet')
    insert_text_set = _safe_hwp_attr(hparameter_set, 'HInsertText') if hparameter_set is not None else None
    get_default = _safe_hwp_attr(haction, 'GetDefault') if haction is not None else None
    execute = _safe_hwp_attr(haction, 'Execute') if haction is not None else None
    hset = _safe_hwp_attr(insert_text_set, 'HSet') if insert_text_set is not None else None
    if callable(get_default) and callable(execute) and insert_text_set is not None and hset is not None:
        get_default('InsertText', hset)
        setattr(insert_text_set, 'Text', text)
        result = execute('InsertText', hset)
        if result is False:
            raise LocalCliRuntimeError('Hancom InsertText action returned false.')
        return

    raise LocalCliRuntimeError('pyhwpx insert_text / InsertText is unavailable on this machine')


def _break_paragraph_with_hancom_action(hwp: object) -> None:
    haction = _safe_hwp_attr(hwp, 'HAction')
    run = _safe_hwp_attr(haction, 'Run') if haction is not None else None
    if callable(run):
        result = run('BreakPara')
        if result is not False:
            return

    method = _safe_hwp_attr(hwp, 'BreakPara')
    if callable(method):
        result = method()
        if result is not False:
            return

    raise LocalCliRuntimeError('Hancom native BreakPara action is unavailable or returned false.')


def apply_char_style(
    hwp: object,
    *,
    face_name: str | None = None,
    height_pt: float | None = None,
    bold: bool | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if face_name is not None:
        kwargs['FaceName'] = str(face_name)
    if height_pt is not None:
        kwargs['Height'] = float(height_pt)
    if bold is not None:
        kwargs['Bold'] = bool(bold)
    if not kwargs:
        raise LocalCliRuntimeError('No text style fields were provided.')

    set_font = _safe_hwp_attr(hwp, 'set_font')
    set_font_error: str | None = None
    if callable(set_font):
        try:
            raw = set_font(**kwargs)
            if raw is False:
                raise LocalCliRuntimeError('Hancom set_font returned false.')
            return {
                'strategy': 'hwp.set_font',
                'mode': 'helper',
                'kwargs': kwargs,
            }
        except Exception as exc:
            set_font_error = str(exc)

    native = NativeActionRunner(hwp).execute('CharShape', parameters=kwargs, set_name='HCharShape')
    if native.succeeded:
        return {
            'strategy': native.strategy or 'CharShape',
            'mode': native.mode,
            'kwargs': kwargs,
        }

    if set_font_error:
        raise LocalCliRuntimeError(
            f'Failed to apply text style via set_font ({set_font_error}) and CharShape ({native.error or "unknown error"}).'
        )
    raise LocalCliRuntimeError(native.error or 'pyhwpx set_font / CharShape is unavailable on this machine')


def save_document(hwp: object, *, diagnostic: dict[str, Any] | None = None) -> bool:
    result: Any
    if hasattr(hwp, 'save'):
        result = hwp.save()
    elif hasattr(hwp, 'Save'):
        result = hwp.Save()
    else:
        raise LocalCliRuntimeError('pyhwpx save/Save is unavailable on this machine')
    if diagnostic is not None:
        # Record arrival before the existing False branch; never coerce the return.
        diagnostic['save_return_arrived'] = True
        diagnostic['save_returned_false'] = result is False
    if result is False:
        raise LocalCliRuntimeError('Hancom save returned false for the active working copy.')
    return result is True


def checked_save_hwp_as(hwp: object, output_path: Path, fmt: str) -> dict[str, Any]:
    """Save a new native artifact and require an affirmative, hashed result."""

    output_path = Path(output_path)
    if output_path.exists() or output_path.is_symlink():
        raise LocalCliRuntimeError('Recovery output already exists; refusing to overwrite it.')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_method = _safe_hwp_attr(hwp, 'save_as')
    if not callable(save_method):
        save_method = _safe_hwp_attr(hwp, 'SaveAs')
    if not callable(save_method):
        raise LocalCliRuntimeError('Hancom save_as/SaveAs is unavailable for recovery.')
    try:
        native_result = save_method(str(output_path), format=str(fmt).upper())
    except Exception as exc:
        raise LocalCliRuntimeError(f'Native recovery SaveAs raised {type(exc).__name__}.') from exc
    if native_result is not True:
        raise LocalCliRuntimeError('Native recovery SaveAs did not return an affirmative result.')
    if not output_path.exists() or output_path.is_symlink() or not output_path.is_file():
        raise LocalCliRuntimeError('Native recovery SaveAs returned success without a regular output file.')
    identity_before = _filesystem_identity(output_path)
    if identity_before is None:
        raise LocalCliRuntimeError('Native recovery output identity could not be verified.')
    try:
        with output_path.open('rb') as stream:
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        size_bytes, sha256 = _sha256_file(output_path)
    except OSError as exc:
        raise LocalCliRuntimeError('Native recovery output could not be read back.') from exc
    identity_after = _filesystem_identity(output_path)
    if identity_after != identity_before:
        raise LocalCliRuntimeError('Native recovery output changed during custody readback.')
    if size_bytes <= 0:
        raise LocalCliRuntimeError('Native recovery SaveAs created an empty output file.')
    return {
        'path': str(output_path),
        'size_bytes': size_bytes,
        'sha256': sha256,
        'format': str(fmt).upper(),
    }


def _resolve_hwp_open_method(hwp: object) -> Callable[[str], Any]:
    open_method = _safe_hwp_attr(hwp, 'open')
    if not callable(open_method):
        open_method = _safe_hwp_attr(hwp, 'Open')
    if not callable(open_method):
        raise LocalCliRuntimeError('pyhwpx Hwp object does not expose an open/Open method as expected.')
    return open_method


def _probe_document_ready(*, hwp: object, document_path: Path, source_filename: str) -> dict[str, Any]:
    hwp_snapshot = collect_hwp_snapshot(hwp)
    document_info = hwp_snapshot.get('document') if isinstance(hwp_snapshot.get('document'), dict) else {}
    doc_path = str(_safe_hwp_attr(hwp, 'Path') or document_info.get('FullName') or '').strip()
    doc_title = str(_safe_hwp_attr(hwp, 'Title') or document_info.get('Title') or '').strip()
    cursor = _snapshot_cursor_context(hwp)
    if not doc_path and not doc_title:
        raise LocalCliRuntimeError('Hancom document open returned before active document state became readable.')
    if not cursor:
        raise LocalCliRuntimeError('Hancom document open returned before cursor state became readable.')
    return {
        'document_path': doc_path or None,
        'document_name': doc_title or source_filename,
        'cursor': cursor,
    }


def open_document_with_recovery(
    *,
    Hwp: Callable[..., object],
    hwp: object,
    document_path: Path,
    log_path: Path,
    detail: str,
    max_attempts: int = 3,
    probe_attempts: int = 6,
) -> object:
    active_hwp = hwp
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        update_runtime_status(
            log_path,
            phase='local_cli_prepare_document_open',
            detail=document_path.name,
            extra={
                'local_cli_task': 'open',
                'document_open_attempt': attempt,
                'document_open_max_attempts': max_attempts,
                'pre_open_snapshot': collect_hwp_snapshot(active_hwp),
            },
            append_history=True,
        )
        try:
            time.sleep(1.0 if attempt == 1 else 1.5)
            open_method = _resolve_hwp_open_method(active_hwp)
            update_runtime_status(
                log_path,
                phase='local_cli_call_document_open',
                detail=document_path.name,
                extra={
                    'local_cli_task': 'open',
                    'document_open_attempt': attempt,
                    'document_open_max_attempts': max_attempts,
                    'open_method_name': getattr(open_method, '__name__', None),
                    'pre_open_snapshot': collect_hwp_snapshot(active_hwp),
                },
                append_history=True,
            )
            open_method(str(document_path))
            update_runtime_status(
                log_path,
                phase='local_cli_document_open_returned',
                detail=document_path.name,
                extra={
                    'local_cli_task': 'open',
                    'document_open_attempt': attempt,
                    'document_open_max_attempts': max_attempts,
                    'post_open_snapshot': collect_hwp_snapshot(active_hwp),
                },
                append_history=True,
            )
            probe_error: Exception | None = None
            for probe_attempt in range(1, probe_attempts + 1):
                try:
                    _probe_document_ready(
                        hwp=active_hwp,
                        document_path=document_path,
                        source_filename=document_path.name,
                    )
                    return active_hwp
                except Exception as probe_exc:
                    probe_error = probe_exc
                    update_runtime_status(
                        log_path,
                        phase='local_cli_verify_document_open',
                        detail=f'{document_path.name} probe {probe_attempt}/{probe_attempts}',
                        extra={
                            'local_cli_task': 'open',
                            'document_open_attempt': attempt,
                            'document_probe_attempt': probe_attempt,
                            'document_probe_max_attempts': probe_attempts,
                            'document_probe_error': repr(probe_exc),
                            'document_probe_snapshot': collect_hwp_snapshot(active_hwp),
                        },
                        append_history=probe_attempt == 1,
                    )
                    time.sleep(0.6 if probe_attempt < probe_attempts else 0.0)
            if probe_error is not None:
                raise probe_error
            raise LocalCliRuntimeError('Hancom document open returned but document readiness probe did not complete.')
        except Exception as exc:
            last_exc = exc
            recovery = {
                'document_open_attempt': attempt,
                'document_open_max_attempts': max_attempts,
                'exception': repr(exc),
            }
            if attempt == 1:
                update_runtime_status(
                    log_path,
                    phase='local_cli_retry_document_open',
                    detail=f'{detail} retry on existing Hancom runtime',
                    extra={'hwp_document_open_recovery': recovery},
                    append_history=True,
                )
                continue

            close_hwp_instance(active_hwp)
            active_hwp = None
            if attempt < max_attempts:
                recovery['kill_hwp_runtime'] = kill_hwp_runtime()
                update_runtime_status(
                    log_path,
                    phase='recover_hwp_document_open',
                    detail=f'{detail} retry after Hancom document open failure',
                    extra={'hwp_document_open_recovery': recovery},
                    append_history=True,
                )
                active_hwp = create_hwp_instance_with_recovery(
                    Hwp,
                    log_path=log_path,
                    phase='local_cli_create_hwp_instance',
                    detail=detail,
                )
                continue

            update_runtime_status(
                log_path,
                phase='recover_hwp_document_open_failed',
                detail=str(exc),
                extra={'hwp_document_open_recovery': recovery},
                append_history=True,
            )
            raise LocalCliRuntimeError(f'Failed to open Hancom document after runtime recovery: {exc}') from exc

    if last_exc is not None:
        raise LocalCliRuntimeError(f'Failed to open Hancom document: {last_exc}') from last_exc
    raise LocalCliRuntimeError('Failed to open Hancom document for an unknown reason.')


def create_table_at_cursor(hwp: object, *, cols: int, rows: int) -> None:
    if cols <= 0 or rows <= 0:
        raise LocalCliRuntimeError('table requires positive cols and rows.')
    if hasattr(hwp, 'create_table'):
        result = hwp.create_table(rows, cols, True)
        if result is False:
            raise LocalCliRuntimeError('Hancom table creation returned false.')
        return
    native = NativeActionRunner(hwp).execute(
        'TableCreate',
        parameters={'Rows': rows, 'Cols': cols},
        set_name='HTableCreation',
    )
    if not native.succeeded:
        raise LocalCliRuntimeError(native.error or 'Hancom table creation failed.')


def _call_required_hwp_method(hwp: object, method_name: str, *args: Any) -> Any:
    method = _safe_hwp_attr(hwp, method_name)
    if not callable(method):
        raise LocalCliRuntimeError(f'pyhwpx {method_name} is unavailable on this machine')
    result = method(*args)
    if result is False:
        raise LocalCliRuntimeError(f'Hancom {method_name} returned false.')
    return result


def _cancel_selection_if_available(hwp: object) -> dict[str, Any]:
    cancel_method = _safe_hwp_attr(hwp, 'Cancel')
    if callable(cancel_method):
        result = cancel_method()
        return {'method': 'hwp.Cancel', 'succeeded': result is None or bool(result)}

    run = _safe_hwp_attr(_safe_hwp_attr(hwp, 'HAction'), 'Run')
    if callable(run):
        result = run('Cancel')
        return {'method': 'HAction.Run:Cancel', 'succeeded': result is None or bool(result)}

    return {'method': None, 'succeeded': False, 'warning': 'Cancel action unavailable; temporary table selection may remain active.'}


def insert_native_table_at_cursor(hwp: object, *, rows: int, cols: int, cells: list[list[str]], field_name: str) -> dict[str, Any]:
    if rows <= 0 or cols <= 0:
        raise LocalCliRuntimeError('native table insertion requires positive rows and cols.')
    if len(cells) != rows or any(len(row) != cols for row in cells):
        raise LocalCliRuntimeError('native table insertion requires a rectangular cells matrix matching rows/cols.')
    flat_values = [str(value) for row in cells for value in row]
    if not any(value.strip() for value in flat_values):
        raise LocalCliRuntimeError('native table insertion rejects all-empty table content.')

    create_table_at_cursor(hwp, cols=cols, rows=rows)
    _call_required_hwp_method(hwp, 'TableCellBlockExtendAbs')
    _call_required_hwp_method(hwp, 'TableCellBlockExtend')
    _call_required_hwp_method(hwp, 'set_cur_field_name', field_name)
    try:
        _call_required_hwp_method(hwp, 'put_field_text', field_name, flat_values)
    finally:
        try:
            _call_required_hwp_method(hwp, 'set_cur_field_name', '')
        except Exception:
            pass
    cancel_result = _cancel_selection_if_available(hwp)
    warnings = []
    if cancel_result.get('warning'):
        warnings.append(str(cancel_result['warning']))
    return {
        'schema_version': 'local-cli/native-table-insert/v1',
        'strategy': 'create_table_at_cursor -> TableCellBlockExtendAbs -> TableCellBlockExtend -> set_cur_field_name -> put_field_text -> clear field name',
        'rows': rows,
        'cols': cols,
        'filled_cell_count': len(flat_values),
        'field_name': field_name,
        'cancel_selection': cancel_result,
        'source_text_deleted': False,
        'old_plain_text_removal': 'deferred_until_rendered_proof',
        'proof_required_before_cleanup': True,
        'warnings': warnings,
    }


def render_numbered_list_text(count: int) -> str:
    if count <= 0:
        raise LocalCliRuntimeError('list requires a positive count.')
    return '\r\n'.join(f'{index}. Item {index}' for index in range(1, count + 1))


def insert_numbered_list_at_cursor(hwp: object, *, count: int) -> dict[str, Any]:
    text = render_numbered_list_text(count)
    insert_text_at_caret(hwp, text)
    return {
        'count': count,
        'mode': 'numbered_text',
        'preview': text.splitlines()[: min(3, count)],
    }


def _native_close_processes() -> frozenset[int] | None:
    """Read-only constructor baseline. No native launch or broad cleanup."""
    if os.sys.platform != 'win32':
        return None  # portable runtimes have no native ownership proof
    result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                            text=True, timeout=5, check=True)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) != 5 for row in rows):
        raise LocalCliRuntimeError('Native constructor process baseline is unavailable.')
    return frozenset(int(row[1]) for row in rows if row[0].casefold() == 'hwp.exe')


class _NativeCloseProcess:
    """Private exact-process handle; wait only, never terminate or retry."""
    def __init__(self, hwnd: int, before: frozenset[int]):
        import win32api
        import win32event
        import win32process
        import win32ts
        from app.readiness import _process_start_identity
        self.hwnd = hwnd
        self.pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if self.pid in before or self.pid == os.getpid():
            raise LocalCliRuntimeError('Constructor did not return a new owned native process.')
        process = collect_process_snapshot(self.pid)
        if (process.get('alive') is not True or process.get('image_name', '').casefold() != 'hwp.exe'
                or win32ts.ProcessIdToSessionId(self.pid) != win32ts.ProcessIdToSessionId(os.getpid())):
            raise LocalCliRuntimeError('Native constructor process identity is unproved.')
        self.generation = _process_start_identity(self.pid)
        if not self.generation:
            raise LocalCliRuntimeError('Native process generation is unavailable.')
        self.handle = win32api.OpenProcess(0x100000 | 0x1000, False, self.pid)
        try:
            self.verify()
        except BaseException:
            self.handle.Close()
            raise

    def verify(self) -> None:
        import win32event
        import win32process
        from app.readiness import _process_start_identity
        if (win32event.WaitForSingleObject(self.handle, 0) != 258
                or _process_start_identity(self.pid) != self.generation
                or win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid):
            raise LocalCliRuntimeError('Original native process/window identity changed.')

    def wait(self) -> None:
        import win32event
        # An exact handle avoids PID reuse. Timeout/WAIT_FAILED are not exit.
        if win32event.WaitForSingleObject(self.handle, 5000) != 0:
            raise LocalCliRuntimeError('Original native process exit is unproved.')

    def close(self) -> None:
        self.handle.Close()


def _bind_native_close_process(hwnd: int, before: frozenset[int]) -> _NativeCloseProcess:
    return _NativeCloseProcess(hwnd, before)


def discard_live_document(hwp: object) -> bool:
    """One explicit document Clear; True means only that its void call returned.

    Hancom documents Clear as void (Automation manual, pp29/51). This helper
    is NOT full-session closure or OS-exit proof and is not used by normal
    close. Never compose it with wrapper.quit (which would Clear again).
    """
    clear_method = _safe_hwp_attr(hwp, 'clear')
    if not callable(clear_method):
        raise LocalCliRuntimeError('No supported explicit Clear interface.')
    result = clear_method(option=1)
    if result is not None:
        raise LocalCliRuntimeError('Native Clear void return is unproved.')
    return True


def capture_screenshot_artifact(*, session_id: str, session_root: Path, hwp: object, log_path: Path) -> dict[str, Any]:
    marker = {
        'job_id': session_id,
        'execution_run_id': f'local-cli-screenshot-{utc_now_iso().replace(":", "").replace("-", "")}',
        'run_label': 'local-cli-screenshot',
    }
    observation_status, frame_meta = emit_runtime_observation(
        log_path,
        hwp=hwp,
        marker=marker,
        phase_override='local_cli_screenshot',
    )
    artifact_path = session_root / 'metadata' / 'observation_frame_latest.png'
    if not artifact_path.exists():
        raise LocalCliRuntimeError('Hancom screenshot artifact was not created.')
    return {
        'artifact_path': artifact_path,
        'observation_status': observation_status,
        'frame_meta': frame_meta,
    }


def export_document_pdf(*, session_root: Path, source_filename: str, hwp: object, log_path: Path) -> Path:
    ensure_session_layout(session_root)
    stem = Path(source_filename or 'document.hwpx').stem or 'document'
    output_path = (session_root / 'output' / f'{stem}.pdf').resolve()

    requested_pdf_primitive = str(os.environ.get('HWPX_PDF_EXPORT_PRIMITIVE', '') or '').strip().lower()
    available_pdf_primitives = list_available_pdf_export_primitives(hwp)
    if requested_pdf_primitive in {'', 'auto', 'default'}:
        pdf_export_attempts = available_pdf_primitives or ['unavailable']
    else:
        pdf_export_attempts = [requested_pdf_primitive]
        if requested_pdf_primitive not in available_pdf_primitives and available_pdf_primitives:
            pdf_export_attempts.extend([item for item in available_pdf_primitives if item != requested_pdf_primitive])

    export_errors: list[str] = []
    last_error: Exception | None = None
    for attempt_index, attempt_primitive in enumerate(pdf_export_attempts, start=1):
        try:
            save_hwp_as(
                hwp,
                output_path,
                'PDF',
                log_path,
                pdf_primitive=attempt_primitive,
                pdf_probe_context={
                    'local_cli_v1': True,
                    'command': 'export',
                    'attempt_index': attempt_index,
                    'requested_primitive': requested_pdf_primitive or 'default',
                    'available_primitives': available_pdf_primitives,
                },
            )
            break
        except Exception as exc:
            last_error = exc
            export_errors.append(f'{attempt_primitive}: {exc!r}')
    else:
        error_summary = '; '.join(export_errors) if export_errors else 'no PDF export primitive was available'
        raise LocalCliRuntimeError(f'Native PDF export failed across primitives: {error_summary}') from last_error

    if not output_path.exists():
        raise LocalCliRuntimeError('PDF export finished without creating an output file.')
    return output_path


@dataclass
class LocalCliRuntimeHandle:
    session_id: str
    session_root: Path
    working_copy_path: Path
    source_filename: str
    log_path: Path
    hwp: object
    hold_native_custody: Callable[[str, BaseException | None], None] | None = None
    diagnostic_site: str = 'command_handler'
    mark_mutating_intent: Callable[[], None] | None = None
    mutation_intent: bool = False
    observe_pre_save: Callable[[], dict[str, Any]] | None = None


@dataclass
class _LiveCommand:
    command_id: str
    name: str
    handler: Callable[[LocalCliRuntimeHandle], Any] | None
    future: Future[Any]
    state: str = 'queued'
    result: Any = None
    error: BaseException | None = None
    timed_out: bool = False
    sequence: int = 0
    created_at: str = dataclass_field(default_factory=utc_now_iso)
    updated_at: str = dataclass_field(default_factory=utc_now_iso)
    reconciled: bool = False
    semantic_ok: bool | None = None
    may_have_mutated: bool = False
    delta_dirty: bool | None = None
    step_count: int = 0
    failed_step_count: int = 0
    reconciliation_data: dict[str, Any] = dataclass_field(default_factory=dict)
    recovery_for_command_id: str | None = None
    custody_held: bool = False
    native_diagnostic: dict[str, str] | None = None
    save_diagnostic: dict[str, Any] | None = None
    native_close_diagnostic: dict[str, bool] | None = None
    completed_event: threading.Event = dataclass_field(default_factory=threading.Event)


class LocalCliLiveSession:
    def __init__(
        self,
        *,
        session_id: str,
        session_root: Path,
        working_copy_path: Path,
        source_filename: str,
    ):
        self.session_id = session_id
        self.session_root = session_root
        self.working_copy_path = working_copy_path
        self.source_filename = source_filename
        self.log_path = session_log_path(session_root)
        self._commands: Queue[_LiveCommand] = Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f'local-cli-live-{session_id[:8]}',
            daemon=True,
        )
        self._start_future: Future[dict[str, Any]] = Future()
        self._closed = threading.Event()
        # `_closed` describes completed native cleanup.  `_closing` is the
        # admission-control state and must be set before the close sentinel is
        # queued, otherwise a concurrent caller can enqueue work behind a
        # sentinel that terminates the worker without resolving that future.
        self._state_lock = threading.Lock()
        self._closing = False
        self._close_future: Future[Any] | None = None
        self._terminal_error: LocalCliRuntimeError | None = None
        self._quarantined = False
        self._allow_native_cleanup = False
        self._native_custody_held = False
        # Sticky invocation intent, never cleared by a later clean envelope,
        # save, read, false dirty flag, exception attribute or diagnostic site.
        self._lifetime_edit_possible = False
        # Unlike sticky invocation history, this preservation debt can only be
        # discharged by a proved ordinary Save (or acknowledged recovery).
        # Native clean/no-op/export projections cannot clear it.
        self._normal_close_requires_save = False
        self._active_command: _LiveCommand | None = None
        self._active_hwp: object | None = None
        self._active_handle: LocalCliRuntimeHandle | None = None
        self._recovery_target_id: str | None = None
        self._recovery_future: Future[Any] | None = None
        self._recovery_close_enqueued = False
        self._command_counter = 0
        self._commands_by_id: dict[str, _LiveCommand] = {}
        self._last_command_id: str | None = None
        # ``command_status()`` is often called immediately after ``execute()``
        # by the same request thread. Keep that association separate from the
        # process-wide last-command pointer so a concurrent caller cannot make
        # the first request bind the second command's sequence.
        self._caller_command_ids = threading.local()

    def _command_record(self, command: _LiveCommand, *, include_private: bool = False) -> dict[str, Any]:
        command.updated_at = utc_now_iso()
        record: dict[str, Any] = {
            'command_id': command.command_id,
            'command': command.name,
            'sequence': int(command.sequence),
            'state': command.state,
            'timed_out': bool(command.timed_out),
            'reconcilable': command.state in {
                'timed_out_pending_reconciliation',
                'completed_after_timeout',
                'failed_after_timeout',
            },
            'reconciled': bool(command.reconciled),
            'created_at': command.created_at,
            'updated_at': command.updated_at,
            'completed': command.completed_event.is_set(),
            'semantic_ok': command.semantic_ok,
            'may_have_mutated': bool(command.may_have_mutated),
            'delta_dirty': command.delta_dirty,
            'step_count': int(command.step_count),
            'failed_step_count': int(command.failed_step_count),
        }
        if command.custody_held:
            record['custody_held'] = True
        if command.native_diagnostic is not None:
            record['native_diagnostic'] = dict(command.native_diagnostic)
        if include_private and command.native_close_diagnostic is not None:
            record['native_close_diagnostic'] = dict(command.native_close_diagnostic)
        if include_private and command.save_diagnostic is not None:
            record['save_diagnostic'] = dict(command.save_diagnostic)
        if command.result is not None and command.state in {
            'succeeded', 'failed', 'completed_after_timeout', 'failed_after_timeout',
        }:
            bounded_result = _bounded_command_result(command.result)
            if isinstance(bounded_result, dict) and isinstance(command.semantic_ok, bool):
                bounded_result['ok'] = command.semantic_ok
            record['result'] = bounded_result
        if command.error is not None:
            record['error'] = {
                'type': _exception_label(command.error),
                'message': '<redacted>',
            }
        if command.reconciliation_data:
            recovery = command.reconciliation_data.get('recovery')
            if isinstance(recovery, dict):
                record['recovery'] = {
                    key: recovery.get(key)
                    for key in ('state', 'attempt_id', 'error_code')
                    if recovery.get(key) not in (None, '')
                }
        if include_private and command.reconciliation_data:
            record['reconciliation_data'] = command.reconciliation_data
        return record

    def _ensure_reconciliation_data(self, command: _LiveCommand) -> dict[str, Any]:
        data = command.reconciliation_data
        if not isinstance(data, dict):
            data = {}
            command.reconciliation_data = data
        data.setdefault('version', _RECONCILIATION_SCHEMA_VERSION)
        data.setdefault('session_id', self.session_id)
        data.setdefault('command_id', command.command_id)
        data.setdefault('sequence', int(command.sequence))
        data.setdefault('semantic_ok', command.semantic_ok)
        data.setdefault('step_count', int(command.step_count))
        data.setdefault('failed_step_count', int(command.failed_step_count))
        data.setdefault('delta_dirty', command.delta_dirty)
        data.setdefault('may_have_mutated', bool(command.may_have_mutated))
        data.setdefault('document_modified_before_recovery', None)
        session_root = getattr(self, 'session_root', None)
        data.setdefault(
            'session_root_identity',
            _filesystem_identity(session_root) if isinstance(session_root, Path) else None,
        )
        data.setdefault('artifacts', [])
        data.setdefault('recovery', {'state': 'none'})
        return data

    def _artifact_entry(self, path_value: Any, *, kind: str) -> dict[str, Any] | None:
        if kind not in _RECOVERY_ARTIFACT_KINDS:
            return None
        try:
            raw_path = Path(str(path_value))
            root = self.session_root.resolve(strict=True)
            if not raw_path.is_absolute():
                return None
            lexical = raw_path.absolute()
            relative = lexical.relative_to(root)
            current = root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return None
            path = lexical.resolve(strict=True)
            if path != lexical:
                return None
            relative = path.relative_to(root)
        except (OSError, ValueError, TypeError):
            return None
        if any(part in {'.', '..'} for part in relative.parts):
            return None
        if not path.is_file():
            return None
        identity_before = _filesystem_identity(path)
        if identity_before is None:
            return None
        try:
            size_bytes, sha256 = _sha256_file(path)
        except OSError:
            return None
        if _filesystem_identity(path) != identity_before:
            return None
        if size_bytes <= 0:
            return None
        return {
            'kind': kind,
            'relative_path': relative.as_posix(),
            'sha256': sha256,
            'size_bytes': size_bytes,
        }

    def _collect_result_artifacts(self, result: Any, default_kind: str | None = None) -> list[dict[str, Any]]:
        candidates: list[tuple[str, Any]] = []
        if not isinstance(result, dict):
            return []
        direct_path = result.get('artifact_path')
        direct_kind = result.get('artifact_kind') or default_kind
        if direct_path is not None and isinstance(direct_kind, str):
            candidates.append((direct_kind, direct_path))
        artifact_map = result.get('artifacts')
        if isinstance(artifact_map, dict):
            for key, value in artifact_map.items():
                if isinstance(value, (str, Path)):
                    kind = _artifact_kind_from_key(str(key))
                    if kind:
                        candidates.append((kind, value))
        steps = result.get('steps')
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    step_result = step.get('result')
                    if isinstance(step_result, dict):
                        nested_kind = step_result.get('artifact_kind') or _artifact_kind_from_key(str(step.get('op') or ''))
                        nested_path = step_result.get('artifact_path')
                        if nested_path is not None and isinstance(nested_kind, str):
                            candidates.append((nested_kind, nested_path))
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for kind, path in candidates:
            entry = self._artifact_entry(path, kind=kind)
            if entry is None:
                continue
            identity = (str(entry['kind']), str(entry['relative_path']))
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)
        return entries

    def _apply_normalized_outcome(
        self,
        command: _LiveCommand,
        result: Any = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        normalized = normalize_command_outcome(command.name, result, error=error)
        command.semantic_ok = normalized['semantic_ok']
        command.may_have_mutated = bool(normalized['may_have_mutated'])
        command.delta_dirty = normalized['delta_dirty']
        command.step_count = int(normalized['step_count'])
        command.failed_step_count = int(normalized['failed_step_count'])
        data = self._ensure_reconciliation_data(command)
        for key in ('semantic_ok', 'may_have_mutated', 'delta_dirty', 'step_count', 'failed_step_count'):
            data[key] = normalized[key]
        if error is not None:
            data['error_code'] = normalized.get('error_code') or _exception_label(error)
        if result is not None:
            data['artifacts'] = self._collect_result_artifacts(result, command.name)
            data['ordinary_save_confirmed'] = result.get('ordinary_save_confirmed') is True if isinstance(result, dict) else False

    def _persist_command(self, command: _LiveCommand) -> None:
        """Merge one bounded command record into the durable journal."""

        session_root = getattr(self, 'session_root', None)
        if not isinstance(session_root, Path):
            # Preserve compatibility with lightweight lifecycle probes that
            # construct a session without a durable session root. Production
            # sessions always have one and therefore never take this branch.
            return
        journal_path = command_journal_path(session_root)
        record = self._command_record(command, include_private=True)

        def _merge(payload: dict[str, Any]) -> dict[str, Any]:
            payload.setdefault('schema_version', 'local-cli/command-journal/v1')
            payload.setdefault('session_id', self.session_id)
            commands_raw = payload.get('commands')
            commands: list[dict[str, Any]] = [
                item for item in commands_raw if isinstance(item, dict)
            ] if isinstance(commands_raw, list) else []
            existing_index: int | None = None
            existing_record: dict[str, Any] | None = None
            for index, item in enumerate(commands):
                if item.get('command_id') == command.command_id:
                    existing_index = index
                    existing_record = item
                    break
            if existing_index is None:
                commands.append(record)
            else:
                state_rank = {
                    'queued': 0,
                    'running': 1,
                    'timed_out_pending_reconciliation': 2,
                    'succeeded': 3,
                    'failed': 3,
                    'completed_after_timeout': 4,
                    'failed_after_timeout': 4,
                    'cancelled': 4,
                }
                old_rank = state_rank.get(str(existing_record.get('state') or ''), 0)
                new_rank = state_rank.get(str(record.get('state') or ''), 0)
                selected_record = record
                if old_rank > new_rank or (
                    old_rank == new_rank
                    and bool(existing_record.get('reconciled'))
                    and not bool(record.get('reconciled'))
                ) or (
                    old_rank == new_rank
                    and bool(existing_record.get('completed'))
                    and not bool(record.get('completed'))
                ):
                    selected_record = existing_record
                commands[existing_index] = selected_record
            commands.sort(key=lambda item: (int(item.get('sequence', 0)), str(item.get('command_id') or '')))
            protected = [
                item for item in commands
                if item.get('state') == 'timed_out_pending_reconciliation'
                or (bool(item.get('timed_out')) and not bool(item.get('reconciled')))
                or bool(item.get('custody_held'))
            ]
            settled = [item for item in commands if item not in protected]
            payload['commands'] = settled[-MAX_RUNTIME_COMMAND_HISTORY:] + protected
            payload['commands'].sort(key=lambda item: (int(item.get('sequence', 0)), str(item.get('command_id') or '')))
            payload['latest_sequence'] = max(
                (int(item.get('sequence', 0)) for item in payload['commands']),
                default=0,
            )
            return payload

        try:
            update_json_object(
                journal_path,
                _merge,
                default={
                    'schema_version': 'local-cli/command-journal/v1',
                    'session_id': self.session_id,
                    'commands': [],
                },
            )
        except Exception as exc:
            raise LocalCliRuntimeError(f'Could not persist local CLI command journal: {journal_path}') from exc

    def _prune_in_memory_commands(self) -> None:
        commands = list(self._commands_by_id.values())
        protected = [
            command for command in commands
            if command.state == 'timed_out_pending_reconciliation'
            or (command.timed_out and not command.reconciled)
            or command.custody_held
        ]
        settled = [command for command in commands if command not in protected]
        keep = {command.command_id for command in settled[-MAX_RUNTIME_COMMAND_HISTORY:]}
        keep.update(command.command_id for command in protected)
        for command_id, command in list(self._commands_by_id.items()):
            if command_id not in keep:
                self._commands_by_id.pop(command_id, None)
            elif command.reconciled:
                command.handler = None

    def _hold_native_custody(self, command: _LiveCommand, reason: str, cause: BaseException | None, *, may_have_mutated: bool = True) -> None:
        """Retain the owning STA after a numbering mutator was reached with an unknown outcome.

        Only in-memory state is changed here, so the hold is published before any journal/status write can fail.
        There is no operator release in this runtime: the hold lasts for the life of the process.
        """

        with self._state_lock:
            error = command.error if isinstance(command.error, LocalCliNativeCustodyHeldError) else LocalCliNativeCustodyHeldError(reason)
            if cause is not None and error.__cause__ is None:
                error.__cause__ = cause
            command.error = error
            command.custody_held = True
            command.may_have_mutated = command.may_have_mutated or may_have_mutated
            self._native_custody_held = True
            self._quarantined = True
            self._allow_native_cleanup = False
            if self._terminal_error is None:
                self._terminal_error = error
            data = self._ensure_reconciliation_data(command)
            data['may_have_mutated'] = command.may_have_mutated
            data['error_code'] = 'NATIVE_CUSTODY_HELD'
            data['recovery'] = {'state': 'quarantined'}

    def _mark_mutating_intent(self) -> None:
        self._lifetime_edit_possible = True
        if self._active_command is None or self._active_command.name != 'save':
            self._normal_close_requires_save = True
        if self._active_handle is not None:
            self._active_handle.mutation_intent = True

    def _observe_pre_save_is_modified(self) -> dict[str, Any]:
        """One non-defaulting owning-STA read, retained before ordinary Save.

        Only exact Boolean observations permit continuation. Other bounded
        primitives remain typed evidence, never clean-document or close proof.
        write_returned describes the preceding journal call, not Save success
        or a claim about an unreturned/failed receipt write.
        """
        command = self._active_command
        if (threading.current_thread() is not self._thread or command is None
                or self._native_custody_held or command.custody_held
                or command.timed_out or self._terminal_error is not None):
            raise LocalCliRuntimeError('Pre-Save observation requires the healthy owning STA.')
        diagnostic = {
            'read_return_arrived': False, 'is_modified_state': 'ERROR',
            'is_modified_type': 'UNKNOWN', 'read_error': 'UNKNOWN',
            'pre_save_receipt': 'pending', 'save_return_arrived': False,
            'save_returned_false': None,
        }
        command.save_diagnostic = diagnostic
        read_error = None
        try:
            value = self._active_handle.hwp.IsModified
        except BaseException as exc:
            read_error = exc
            diagnostic['is_modified_state'] = 'UNAVAILABLE' if type(exc) is AttributeError else 'ERROR'
            diagnostic['read_error'] = _exception_label(exc)
        else:
            diagnostic['read_return_arrived'] = True
            diagnostic['is_modified_state'] = 'OTHER'
            for cls, label in ((bool, 'bool'), (int, 'int'), (float, 'float'),
                               (type(None), 'NoneType'), (str, 'str')):
                if type(value) is cls:
                    diagnostic['is_modified_type'] = label
                    break
            if (type(value) is bool or value is None
                    or (type(value) is int and -(2**31) <= value < 2**31)
                    or (type(value) is float and math.isfinite(value))):
                diagnostic['is_modified_value'] = value
            if type(value) is bool:
                diagnostic['is_modified_state'] = 'TRUE' if value else 'FALSE'
        try:
            self._persist_command(command)
        except BaseException:
            diagnostic['pre_save_receipt'] = 'write_failed'
            raise
        diagnostic['pre_save_receipt'] = 'write_returned'
        if diagnostic['is_modified_type'] != 'bool':
            raise LocalCliRuntimeError('Pre-Save IsModified observation is not an exact Boolean.') from read_error
        if (self._native_custody_held or command.custody_held or command.timed_out
                or self._terminal_error is not None):
            raise LocalCliRuntimeError('Pre-Save observation lost healthy owning-STA authority.')
        return diagnostic

    def _hold_active_command_custody(self, reason: str, cause: BaseException | None = None, *, may_have_mutated: bool = True) -> None:
        command = self._active_command
        if command is None:
            raise LocalCliRuntimeError('No local CLI command is active on the owning thread.')
        if may_have_mutated:
            self._hold_native_custody(command, reason, cause)
        else:
            self._hold_native_custody(command, reason, cause, may_have_mutated=False)
        if command.name == 'command-bundle' and cause is not None:
            # Existing bundle failure caller; custody is already published.
            # Private journal only, no new SDK reads or release authority.
            command.reconciliation_data['bundle_failure_diagnostic'] = _native_diagnostic('bundle_step', cause)

    def _settle_held_command(
        self,
        command: _LiveCommand,
        *,
        result: Any = None,
        escaped: bool = False,
        reporting_error: BaseException | None = None,
    ) -> None:
        """Record a held command as failed/unknown and resolve its waiting caller without raising."""

        error = command.error if isinstance(command.error, LocalCliNativeCustodyHeldError) else None
        if error is None:
            error = LocalCliNativeCustodyHeldError('The native command outcome is unknown and the session is held.')
            command.error = error
        if reporting_error is not None:
            error.reporting_errors.append(reporting_error)
        command.semantic_ok = False
        # The hold already recorded issued-edit uncertainty independently.
        command.state = 'failed_after_timeout' if command.timed_out else 'failed'
        try:
            self._persist_command(command)
        except BaseException as persist_error:  # noqa: BLE001 - the hold is already published; keep the owning thread
            error.reporting_errors.append(persist_error)
        if not command.future.done():
            if escaped or error.reporting_errors:
                command.future.set_exception(error)
            else:
                command.future.set_result(result)

    def _flush_held_custody_journal(self) -> None:
        for command in list(self._commands_by_id.values()):
            if command.custody_held:
                try:
                    self._persist_command(command)
                except Exception:
                    pass

    def has_unreconciled_reconciliation(self) -> bool:
        self._ensure_lifecycle_state()
        with self._state_lock:
            if self._native_custody_held:
                return True
            return any(
                command.timed_out
                and command.state in {
                    'timed_out_pending_reconciliation',
                    'completed_after_timeout',
                    'failed_after_timeout',
                }
                and not command.reconciled
                for command in self._commands_by_id.values()
            )

    def latest_command_sequence(self) -> int:
        self._ensure_lifecycle_state()
        with self._state_lock:
            return max((int(command.sequence) for command in self._commands_by_id.values()), default=0)

    def reconcile_command(self, command_id: str) -> dict[str, Any]:
        self._ensure_lifecycle_state()
        recovery_future: Future[Any] | None = None
        should_enqueue = False
        with self._state_lock:
            command = self._commands_by_id.get(command_id)
            if command is None:
                return read_command_journal(self.session_root, command_id)
            if command.reconciled:
                return self._command_record(command)
            if self._native_custody_held:
                self._flush_held_custody_journal()
                return self._command_record(command)
            if command.state == 'timed_out_pending_reconciliation':
                return self._command_record(command)
            if command.state not in {'completed_after_timeout', 'failed_after_timeout'}:
                return self._command_record(command)
            data = self._ensure_reconciliation_data(command)
            recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
            recovery_state = str(recovery.get('state') or 'none')
            if getattr(self, '_legacy_lifecycle_shim', False) and recovery_state != 'preserved':
                # A few pre-custody unit seams construct the session with
                # object.__new__ and inject a settled command directly.  They
                # cannot own a native STA or produce a recovery artifact; keep
                # their journal-only acknowledgement behavior isolated from
                # production sessions, which always initialize _active_hwp.
                command.reconciled = True
                self._persist_command(command)
                self._prune_in_memory_commands()
                return self._command_record(command)
            if recovery_state == 'preserved':
                return self._command_record(command)
            if self._recovery_future is not None and self._recovery_target_id == command_id:
                recovery_future = self._recovery_future
            else:
                if self._active_hwp is None or not self._thread.is_alive():
                    recovery['state'] = 'failed'
                    recovery['error_code'] = 'NATIVE_HANDLE_UNAVAILABLE'
                    data['recovery'] = recovery
                    self._persist_command(command)
                    return self._command_record(command)
                recovery.update({
                    'state': 'saving',
                    'attempt_id': f'{self.session_id}:recovery-{command.sequence}-{time.time_ns()}',
                })
                data['recovery'] = recovery
                self._persist_command(command)
                recovery_future = Future()
                self._recovery_future = recovery_future
                self._recovery_target_id = command_id
                should_enqueue = True
        if should_enqueue:
            self._commands.put(_LiveCommand(
                command_id=f'{self.session_id}:recovery-{command_id}',
                name='__recover__',
                handler=None,
                future=recovery_future or Future(),
                recovery_for_command_id=command_id,
            ))
        if recovery_future is not None:
            try:
                recovery_future.result(timeout=120.0)
            except FutureTimeoutError:
                pass
            except Exception:
                pass
        return self.command_status(command_id)

    def acknowledge_reconciliation(self, command_id: str) -> dict[str, Any]:
        """Commit the service projection before permitting native release."""

        self._ensure_lifecycle_state()
        with self._state_lock:
            command = self._commands_by_id.get(command_id)
            if command is None:
                return read_command_journal(self.session_root, command_id)
            if self._native_custody_held:
                self._flush_held_custody_journal()
                return self._command_record(command)
            data = self._ensure_reconciliation_data(command)
            recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
            if recovery.get('state') != 'preserved':
                return self._command_record(command)
            command.reconciled = True
        try:
            self._persist_command(command)
        except Exception:
            with self._state_lock:
                command.reconciled = False
            raise
        with self._state_lock:
            self._allow_native_cleanup = True
        self._prune_in_memory_commands()
        return self.command_status(command_id)

    def _perform_recovery(self, command_id: str, hwp: object) -> None:
        with self._state_lock:
            command = self._commands_by_id.get(command_id)
            if command is None:
                raise LocalCliRuntimeError('The timed-out command is no longer registered for recovery.')
            data = self._ensure_reconciliation_data(command)
            recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
            attempt_id = str(recovery.get('attempt_id') or f'{self.session_id}:recovery-{time.time_ns()}')
        modified_before: bool | None = None
        try:
            pre_recovery_location = snapshot_live_location(
                hwp=hwp,
                source_filename=self.source_filename,
                working_copy_id=self.session_id,
                include_nearby_context=False,
                include_document_snapshot=False,
            )
            if isinstance(pre_recovery_location.get('document_is_modified'), bool):
                modified_before = pre_recovery_location['document_is_modified']
        except Exception:
            pass
        try:
            modified_value = _safe_hwp_attr(hwp, 'IsModified')
            if modified_before is None and isinstance(modified_value, bool):
                modified_before = modified_value
        except Exception:
            pass
        suffix = self.working_copy_path.suffix.lower()
        if suffix not in {'.hwpx', '.hwp'}:
            suffix = '.hwpx'
        native_format = 'HWP' if suffix == '.hwp' else 'HWPX'
        recovery_dir = self.session_root / 'output' / f'recovery-{command.sequence}-{uuid.uuid4().hex}'
        recovery_dir.mkdir(parents=True, exist_ok=False)
        target = recovery_dir / f'recovery{suffix}'
        saved = checked_save_hwp_as(hwp, target, native_format)
        artifact = self._artifact_entry(saved['path'], kind='recovery')
        if artifact is None:
            raise LocalCliRuntimeError('Recovery output failed managed-root custody validation.')
        with self._state_lock:
            command = self._commands_by_id.get(command_id)
            if command is None:
                raise LocalCliRuntimeError('The timed-out command disappeared during recovery.')
            data = self._ensure_reconciliation_data(command)
            data['document_modified_before_recovery'] = modified_before
            data['recovery'] = {
                'state': 'preserved',
                'attempt_id': attempt_id,
                'artifact': artifact,
            }
            existing = data.get('artifacts') if isinstance(data.get('artifacts'), list) else []
            data['artifacts'] = [item for item in existing if item.get('kind') != 'recovery'] if all(isinstance(item, dict) for item in existing) else []
            data['artifacts'].append(artifact)
        self._persist_command(command)

    def start(self, *, timeout: float = 90.0) -> dict[str, Any]:
        self._ensure_lifecycle_state()
        with self._state_lock:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed.is_set():
                raise LocalCliRuntimeError('The live local CLI session is already closed.')
            self._thread.start()
        try:
            return self._start_future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            error = self._terminalize_timeout(
                command_name='start',
                command_id=f'{self.session_id}:start',
                message='Timed out while starting the live local CLI session.',
                enqueue_close=False,
            )
            raise error from exc

    def execute(
        self,
        command_name: str,
        handler: Callable[[LocalCliRuntimeHandle], Any],
        *,
        timeout: float = 90.0,
        prior_dirty: bool = False,
    ) -> Any:
        self._ensure_lifecycle_state()
        with self._state_lock:
            if self._closed.is_set():
                raise LocalCliRuntimeError('The live local CLI session is already closed.')
            if self._closing:
                raise LocalCliRuntimeError('The live local CLI session is closing.')
            if self._terminal_error is not None:
                raise self._terminal_error
            if prior_dirty:
                self._normal_close_requires_save = True
            self._command_counter += 1
            command_id = f'{self.session_id}:command-{self._command_counter}'
            future: Future[Any] = Future()
            # Keep admission and sentinel ordering under one lock. Queue.put is
            # non-blocking for this unbounded queue, so the lock does not hold
            # up the worker or any native command.
            command = _LiveCommand(
                command_id=command_id,
                name=command_name,
                handler=handler,
                future=future,
                sequence=self._command_counter,
            )
            self._commands_by_id[command_id] = command
            self._last_command_id = command_id
            self._caller_command_ids.command_id = command_id
            self._persist_command(command)
            self._commands.put(command)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            error = self._terminalize_timeout(
                command_name=command_name,
                command_id=command_id,
                message=f'Timed out while waiting for local CLI command: {command_name}',
                command=command,
            )
            raise error from exc

    def close(self, *, timeout: float = 30.0, prior_dirty: bool = False) -> None:
        self._ensure_lifecycle_state()
        terminal_error: LocalCliRuntimeError | None = None
        future: Future[Any] | None = None
        close_command: _LiveCommand | None = None
        with self._state_lock:
            if self._closed.is_set():
                return
            if prior_dirty:
                self._normal_close_requires_save = True
            if self._native_custody_held and not self._allow_native_cleanup:
                self._flush_held_custody_journal()
                raise LocalCliRuntimeError(_HELD_CLOSE_MESSAGE)
            if self._quarantined and not self._allow_native_cleanup:
                raise LocalCliRuntimeError(
                    'The live local CLI session is quarantined; reconcile and commit the recovery artifact before close.'
                )
            if self._closing:
                if self._terminal_error is not None:
                    # A command timeout already queued/owns teardown.  Wait
                    # for that same worker to finish instead of returning a
                    # second synthetic close failure or abandoning the
                    # manager registration.
                    terminal_error = self._terminal_error
                else:
                    raise LocalCliRuntimeError('The live local CLI session is already closing.')
            if terminal_error is not None:
                pass
            else:
                self._closing = True
                future = Future()
                self._close_future = future
                self._command_counter += 1
                close_command = _LiveCommand(
                    command_id=f'{self.session_id}:close',
                    name='__close__',
                    handler=None,
                    future=future,
                    sequence=self._command_counter,
                )
                self._commands_by_id[close_command.command_id] = close_command
                self._last_command_id = close_command.command_id
                # Mark closing before queueing the sentinel.  No later execute()
                # call can pass admission after this point.
                self._persist_command(close_command)
                self._commands.put(close_command)
        if terminal_error is not None:
            if self._closed.wait(timeout=timeout):
                return
            raise terminal_error
        if future is None or close_command is None:
            raise LocalCliRuntimeError('The live local CLI session could not establish its close command.')
        deadline = time.monotonic() + timeout
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            error = self._terminalize_timeout(
                command_name='close',
                command_id=f'{self.session_id}:close',
                message='Timed out while closing the live local CLI session.',
                command=close_command,
                enqueue_close=False,
            )
            raise error from exc
        remaining = max(0.0, deadline - time.monotonic())
        if not self._closed.wait(timeout=remaining):
            cleanup_errors = getattr(self, '_cleanup_errors', [])
            if cleanup_errors:
                raise LocalCliRuntimeError(
                    'The live local CLI runtime could not complete cleanup; retain the binding and retry cleanup.'
                )
            # The close command has already settled.  A slow native release is
            # a cleanup condition, not a second command timeout: quarantining
            # the settled close command here can race the finalizer and strand
            # a binding that is otherwise still being released.
            raise LocalCliRuntimeError(
                'The live local CLI runtime is still releasing native resources; retry cleanup without replaying close.'
            )

        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            raise LocalCliRuntimeError('Native close completed; final reporting thread is still settling. Do not replay teardown.')

    def is_terminal(self) -> bool:
        self._ensure_lifecycle_state()
        with self._state_lock:
            return self._terminal_error is not None

    def command_status(self, command_id: str | None = None) -> dict[str, Any]:
        """Return the bounded reconciliation status for one live command."""

        self._ensure_lifecycle_state()
        with self._state_lock:
            caller_command_id = getattr(getattr(self, '_caller_command_ids', None), 'command_id', None)
            resolved_id = command_id or caller_command_id or self._last_command_id
            command = self._commands_by_id.get(resolved_id) if resolved_id else None
            if command is None:
                session_root = getattr(self, 'session_root', None)
                if isinstance(session_root, Path):
                    return read_command_journal(session_root, resolved_id)
                return {'command_id': resolved_id, 'state': 'unknown', 'reconcilable': False}
            status = self._command_record(command)
            status['completed'] = command.completed_event.is_set()
            return status

    def _terminalize_timeout(
        self,
        *,
        command_name: str,
        command_id: str,
        message: str,
        command: _LiveCommand | None = None,
        enqueue_close: bool = True,
    ) -> LocalCliRuntimeTimeoutError:
        persist_command: _LiveCommand | None = None
        completion_preserved = False
        legacy_close_timeout = False
        with self._state_lock:
            if self._terminal_error is not None:
                return self._terminal_error
            state = 'timed_out_pending_reconciliation'
            self._quarantined = True
            self._allow_native_cleanup = False
            if command is not None:
                legacy_close_timeout = bool(
                    getattr(self, '_legacy_lifecycle_shim', False)
                    and command.name == '__close__'
                )
                command.timed_out = True
                # Completion may have won the race immediately before the
                # timeout caller acquired the lock. Preserve its result and
                # expose it as an explicitly reconciliable late completion.
                if command.state in {'succeeded', 'completed_after_timeout'}:
                    command.state = 'completed_after_timeout'
                    completion_preserved = True
                elif command.state in {'failed', 'failed_after_timeout'}:
                    command.state = 'failed_after_timeout'
                    completion_preserved = True
                else:
                    command.state = state
                self._apply_normalized_outcome(command, command.result, error=command.error)
                data = self._ensure_reconciliation_data(command)
                data['recovery'] = {
                    'state': 'quarantined' if completion_preserved else 'pending_execution',
                }
            else:
                synthetic_sequence = max(1, int(self._command_counter) + 1)
                self._command_counter = max(int(self._command_counter), synthetic_sequence)
                synthetic = _LiveCommand(
                    command_id=command_id,
                    name=command_name,
                    handler=None,
                    future=Future(),
                    state=state,
                    timed_out=True,
                    sequence=synthetic_sequence,
                )
                self._commands_by_id[command_id] = synthetic
                self._last_command_id = command_id
                self._apply_normalized_outcome(synthetic, error=LocalCliRuntimeError(message))
                data = self._ensure_reconciliation_data(synthetic)
                data['recovery'] = {'state': 'pending_execution'}
                persist_command = synthetic
            if command is not None:
                persist_command = command
            error = LocalCliRuntimeTimeoutError(
                f'{message} Session entered terminal quarantine; command outcome is reconcilable by command_id={command_id}.',
                command_id=command_id,
                command_state=state,
            )
            self._terminal_error = error
            # Quarantine closes ordinary admission through _terminal_error but
            # deliberately does not enter _closing.  Native state remains
            # owned by this STA until explicit reconciliation commits custody.
            self._closing = legacy_close_timeout
            if command is not None and not command.future.done() and not completion_preserved:
                command.future.set_exception(error)
            # Cancel queued ordinary work.  There is intentionally no
            # automatic close sentinel: the owning STA must remain available
            # for one explicit recovery snapshot.
            self._cancel_pending_commands(
                'The live local CLI session entered quarantine after a command timeout.',
                preserve_command=command if legacy_close_timeout else None,
            )
        if persist_command is not None:
            self._persist_command(persist_command)
        return error

    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._closed.is_set()

    def _ensure_lifecycle_state(self) -> None:
        """Backfill lifecycle fields for legacy/test-constructed instances."""

        # A few compatibility callers construct a session with ``object.__new__``
        # to exercise close admission without starting COM.  Production sessions
        # always receive this field through ``__init__``; give those lightweight
        # instances a stable namespace so lifecycle errors remain actionable
        # instead of raising AttributeError while constructing a command id.
        if not hasattr(self, '_active_hwp'):
            self._legacy_lifecycle_shim = True
        if not hasattr(self, 'session_id'):
            self.session_id = 'legacy-local-cli-session'
        if not hasattr(self, '_state_lock'):
            self._state_lock = threading.Lock()
        if not hasattr(self, '_closing'):
            self._closing = False
        if not hasattr(self, '_close_future'):
            self._close_future = None
        if not hasattr(self, '_terminal_error'):
            self._terminal_error = None
        if not hasattr(self, '_cleanup_errors'):
            self._cleanup_errors = []
        if not hasattr(self, '_quarantined'):
            self._quarantined = False
        if not hasattr(self, '_allow_native_cleanup'):
            self._allow_native_cleanup = False
        if not hasattr(self, '_native_custody_held'):
            self._native_custody_held = False
        if not hasattr(self, '_normal_close_requires_save'):
            self._normal_close_requires_save = False
        if not hasattr(self, '_active_command'):
            self._active_command = None
        if not hasattr(self, '_active_hwp'):
            self._active_hwp = None
        if not hasattr(self, '_active_handle'):
            self._active_handle = None
        if not hasattr(self, '_recovery_target_id'):
            self._recovery_target_id = None
        if not hasattr(self, '_recovery_future'):
            self._recovery_future = None
        if not hasattr(self, '_recovery_close_enqueued'):
            self._recovery_close_enqueued = False
        if not hasattr(self, '_command_counter'):
            self._command_counter = 0
        if not hasattr(self, '_commands_by_id'):
            self._commands_by_id = {}
        if not hasattr(self, '_last_command_id'):
            self._last_command_id = None
        if not hasattr(self, '_caller_command_ids'):
            self._caller_command_ids = threading.local()

    def _cancel_pending_commands(
        self,
        reason: str,
        *,
        preserve_command: _LiveCommand | None = None,
    ) -> None:
        """Resolve every command left behind when the worker is terminating."""

        preserved: list[_LiveCommand] = []
        changed: list[_LiveCommand] = []
        while True:
            try:
                command = self._commands.get_nowait()
            except Empty:
                break
            if preserve_command is command:
                preserved.append(command)
                continue
            was_completed = command.completed_event.is_set()
            if command.future.done():
                if command.state == 'queued':
                    command.state = 'cancelled'
                command.completed_event.set()
                if command.state == 'cancelled' or not was_completed:
                    changed.append(command)
                continue
            if command.timed_out:
                command.completed_event.set()
                if not was_completed:
                    changed.append(command)
                continue
            command.state = 'cancelled'
            command.future.set_exception(LocalCliRuntimeError(reason))
            command.completed_event.set()
            changed.append(command)
        for command in preserved:
            self._commands.put(command)
        for command in changed:
            self._persist_command(command)

    def _close_original_native(self, hwp: Any) -> None:
        """One raw void Quit, no wrapper quit/hidden Clear and no save mode.

        Locked 1.7.2 has an extra constructor CoInitialize and an unconditional
        destructor CoUninitialize. Retire only this original pure-Python wrapper
        after proven native exit: disable automatic destructor replay BEFORE
        explicitly observing that exact destructor once on its owning STA.
        Class retirement changes no COM interface and performs no native call.
        """
        owner = getattr(self, '_native_close_owner', None)
        def healthy() -> None:
            close = self._commands_by_id.get(f'{self.session_id}:close')
            if (threading.current_thread() is not self._thread or self._native_custody_held
                    or (self._quarantined and not self._allow_native_cleanup)
                    or (close is not None and close.timed_out)):
                raise LocalCliRuntimeError('Native close lost healthy owning-STA authority.')
        healthy()
        if self._normal_close_requires_save and not self._allow_native_cleanup:
            raise LocalCliRuntimeError('Native close cannot discard unproved prior edits.')
        if (owner is None or owner['wrapper'] is not hwp
                or type(hwp) is not owner['wrapper_type']
                or hwp.hwp is not owner['dispatch'] or hwp.on_quit is not False
                or getattr(self, '_native_teardown_started', False)):
            raise LocalCliRuntimeError('Qualified full native closure is unavailable: original constructor ownership is unproved.')
        process = owner['process']
        process.verify()
        windows = hwp.XHwpWindows
        window_count = windows.Count
        if type(window_count) is not int or window_count != 1:
            raise LocalCliRuntimeError('Native close requires exactly one window.')
        window_handle = windows.Active_XHwpWindow.WindowHandle
        if type(window_handle) is not int or window_handle != owner['hwnd']:
            raise LocalCliRuntimeError('Native close window identity changed.')
        docs = hwp.XHwpDocuments
        document_count = docs.Count
        if type(document_count) is not int or document_count != 1:
            raise LocalCliRuntimeError('Native close requires exactly one document.')
        document = docs.Active_XHwpDocument
        document_id = document.DocumentID
        if type(document_id) is not int or document_id != owner['document_id']:
            raise LocalCliRuntimeError('Native close document identity changed.')
        for source, attribute in ((document, 'FullName'), (hwp, 'Path')):
            value = getattr(source, attribute)
            if type(value) is not str or Path(value).resolve() != self.working_copy_path.resolve():
                raise LocalCliRuntimeError('Native close document path changed.')
        source = None
        if hwp.IsModified is not False:
            raise LocalCliRuntimeError('Native close requires an exact clean document; save explicitly first.')
        # Drop local child COM references before Quit/release.
        windows = docs = document = None
        healthy()
        process.verify()
        close = self._commands_by_id.get(f'{self.session_id}:close')
        diagnostic = {'quit_entered': True, 'quit_return_arrived': False, 'quit_void_return': False,
                      'process_exit_confirmed': False, 'wrapper_com_return_arrived': False, 'runtime_com_return_arrived': False}
        if close is not None:
            close.native_close_diagnostic = diagnostic
        self._native_teardown_started = True
        # IHwpObject.Quit is void. None is a returned void call, NOT Boolean
        # success; original OS exit and both COM balances are separate predicates.
        result = hwp.hwp.Quit()
        diagnostic['quit_return_arrived'] = True
        diagnostic['quit_void_return'] = result is None
        if result is not None:
            raise LocalCliRuntimeError('Native Quit void contract did not return normally.')
        healthy()
        process.wait()
        diagnostic['process_exit_confirmed'] = True
        healthy()
        original_type = owner['wrapper_type']
        retired_type = type('_RetiredHwp', (original_type,), {'__slots__': (), '__del__': lambda self: None})
        hwp.__class__ = retired_type
        # Release the original dispatch only after exit, before COM uninit.
        hwp.hwp = None
        owner['dispatch'] = None
        original_type.__del__(hwp)  # on_quit=False: no hidden Clear/Quit; observed constructor COM balance
        diagnostic['wrapper_com_return_arrived'] = True

    def _finalize(
        self,
        *,
        hwp: Any,
        pythoncom: Any,
        coinitialized: bool,
        watchdog_stop: Any,
        watchdog_thread: Any,
    ) -> None:
        """Own actual native exit and COM completion before reporting close success."""
        cleanup_errors: list[str] = []
        close = self._commands_by_id.get(f'{self.session_id}:close')
        permitted = not self._quarantined or self._allow_native_cleanup
        native_complete = False

        def report(label: str, operation: Callable[[], Any]) -> None:
            try:
                operation()
            except BaseException:
                cleanup_errors.append(label)

        if watchdog_stop is not None:
            report('watchdog_stop', watchdog_stop.set)
        if watchdog_thread is not None:
            def join_watchdog() -> None:
                watchdog_thread.join(timeout=1.0)
                if watchdog_thread.is_alive():
                    raise LocalCliRuntimeError('Watchdog stop incomplete.')
            report('watchdog_join', join_watchdog)

        if permitted and not cleanup_errors:
            try:
                if hwp is not None:
                    self._close_original_native(hwp)
                elif getattr(self, '_constructor_invoked', False):
                    raise LocalCliRuntimeError('Constructor effects unresolved; owning STA retained.')
                if pythoncom is not None and coinitialized:
                    pythoncom.CoUninitialize()
                    if close is not None and close.native_close_diagnostic is not None:
                        close.native_close_diagnostic['runtime_com_return_arrived'] = True
                native_complete = True
            except BaseException as exc:
                # No fallible formatting or persistence before the hold.
                if close is not None:
                    self._hold_native_custody(close, 'Native close is incomplete; custody retained.', exc)
                    close.native_diagnostic = _native_diagnostic('close_wrapper', exc)
                else:
                    self._native_custody_held = self._quarantined = True
                    self._allow_native_cleanup = False
                    self._terminal_error = LocalCliRuntimeError('Native cleanup is incomplete; custody retained.')
                self._active_hwp = hwp
                cleanup_errors.append('native_teardown')

        if permitted and not native_complete and not self._native_custody_held:
            self._native_custody_held = self._quarantined = True
            self._allow_native_cleanup = False
            self._terminal_error = LocalCliRuntimeError('Cleanup prerequisite failed; custody retained.')
        # Commit bounded in-memory completion BEFORE fallible reporting. Never
        # replay native teardown to repair a journal/status-only failure.
        if native_complete:
            self._closed.set()
            owner = getattr(self, '_native_close_owner', None)
            if owner is not None:
                report('native_process_handle', owner['process'].close)
        if close is not None:
            if native_complete:
                close.result = {'ok': True}
                self._apply_normalized_outcome(close, close.result)
                close.state = 'completed_after_timeout' if close.timed_out else 'succeeded'
            else:
                close.error = close.error or self._terminal_error or LocalCliRuntimeError('Native close incomplete.')
                close.semantic_ok = False
                close.state = 'failed_after_timeout' if close.timed_out else 'failed'
            close.completed_event.set()

        for command in list(self._commands_by_id.values()):
            if native_complete and command is not close:
                if command.timed_out and command.state == 'timed_out_pending_reconciliation':
                    command.state = 'failed_after_timeout'
                command.completed_event.set()
                if not command.future.done():
                    command.future.set_exception(self._terminal_error or LocalCliRuntimeError('The live session stopped before command completion.'))
            report('persist_command', lambda command=command: self._persist_command(command))
        report('finalization_status', lambda: update_runtime_status(
            self.log_path, phase='local_cli_session_finalized' if native_complete else 'local_cli_session_preserved',
            detail=self.source_filename, extra={'cleanup_errors': cleanup_errors}, append_history=True))
        report('cancel_pending_commands', lambda: self._cancel_pending_commands('The live session has stopped.'))
        self._cleanup_errors = list(cleanup_errors)
        if close is not None and not close.future.done():
            if native_complete and not cleanup_errors:
                close.future.set_result(close.result)
            else:
                close.future.set_exception(close.error or LocalCliRuntimeError('Close reporting incomplete; native teardown is not replayed.'))
        # Keep the real SDK wrapper, COM apartment and manager registration alive
        # after ambiguous cleanup. Normal public reconciliation cannot release a
        # held session. A test-only release seam must not grant native authority.
        while self._native_custody_held and not self._allow_native_cleanup:
            pending = self._commands.get()
            if not pending.future.done():
                pending.future.set_exception(LocalCliRuntimeError(_HELD_CLOSE_MESSAGE))

    def _run(self) -> None:
        pythoncom = None
        coinitialized = False
        hwp = None
        watchdog_stop = None
        watchdog_thread = None
        try:
            try:
                import pythoncom  # type: ignore
            except Exception:
                pythoncom = None
            if pythoncom is not None:
                pythoncom.CoInitialize()
                coinitialized = True

            from pyhwpx import Hwp  # type: ignore

            process_session = current_windows_session_snapshot()
            update_runtime_status(
                self.log_path,
                phase='local_cli_session_preflight',
                detail=self.source_filename,
                extra={'local_cli_process_session': process_session},
                append_history=True,
            )
            if os.sys.platform == 'win32' and not bool(process_session.get('interactive')):
                raise LocalCliRuntimeError(
                    'Local CLI live session requires the API process to run in an interactive Windows logon session; '
                    f"current process session_id={process_session.get('session_id')} session_name={process_session.get('session_name')!r}."
                )

            before_native = _native_close_processes()
            self._constructor_invoked = True
            hwp = create_hwp_instance_with_recovery(
                Hwp,
                log_path=self.log_path,
                phase='local_cli_create_hwp_instance',
                detail=f'open {self.source_filename}',
            )
            self._active_hwp = hwp
            if before_native is not None:
                # Bind the original constructor object, not an injected close API.
                if type(hwp) is not Hwp or hwp.on_quit is not False:
                    raise LocalCliRuntimeError('Native constructor wrapper contract is unproved.')
                windows = hwp.XHwpWindows
                if type(windows.Count) is not int or windows.Count != 1:
                    raise LocalCliRuntimeError('Native constructor window count is unproved.')
                hwnd = windows.Active_XHwpWindow.WindowHandle
                if type(hwnd) is not int or hwnd <= 0:
                    raise LocalCliRuntimeError('Native constructor window identity is unproved.')
                docs = hwp.XHwpDocuments
                if type(docs.Count) is not int or docs.Count != 1:
                    raise LocalCliRuntimeError('Native constructor document count is unproved.')
                document_id = docs.Active_XHwpDocument.DocumentID
                if type(document_id) is not int:
                    raise LocalCliRuntimeError('Native constructor document identity is unproved.')
                self._native_close_owner = {
                    'wrapper': hwp, 'wrapper_type': Hwp, 'dispatch': hwp.hwp,
                    'hwnd': hwnd, 'document_id': document_id,
                    'process': _bind_native_close_process(hwnd, before_native),
                }
                windows = docs = None  # no child COM references survive into finalization
            update_runtime_status(
                self.log_path,
                phase='local_cli_create_hwp_instance_succeeded',
                detail=self.source_filename,
                extra={
                    'local_cli_task': 'open',
                    'post_create_snapshot': collect_hwp_snapshot(hwp),
                },
                append_history=True,
            )
            watchdog_stop, watchdog_thread = start_runtime_watchdog(
                self.log_path,
                lambda: hwp,
                lambda: {
                    'job_id': self.session_id,
                    'execution_run_id': f'local-cli-open-{self.session_id}',
                    'run_label': 'local-cli-open',
                },
            )
            hwp = open_document_with_recovery(
                Hwp=Hwp,
                hwp=hwp,
                document_path=self.working_copy_path,
                log_path=self.log_path,
                detail=f'open {self.source_filename}',
                max_attempts=1,
                probe_attempts=1,
            )
            update_runtime_status(
                self.log_path,
                phase='local_cli_document_ready',
                detail=self.source_filename,
                extra={'local_cli_task': 'open'},
            )
            handle = LocalCliRuntimeHandle(
                session_id=self.session_id,
                session_root=self.session_root,
                working_copy_path=self.working_copy_path,
                source_filename=self.source_filename,
                log_path=self.log_path,
                hwp=hwp,
                hold_native_custody=self._hold_active_command_custody,
                mark_mutating_intent=self._mark_mutating_intent,
                observe_pre_save=self._observe_pre_save_is_modified,
            )
            self._active_hwp = hwp
            self._active_handle = handle
            with self._state_lock:
                startup_cancelled = self._terminal_error is not None or self._closing
            if startup_cancelled:
                if not self._start_future.done():
                    self._start_future.set_exception(
                        self._terminal_error
                        or LocalCliRuntimeError('The live local CLI session closed during startup.')
                    )
                if not self._quarantined:
                    return
            startup_location = snapshot_live_location(
                hwp=hwp,
                source_filename=self.source_filename,
                working_copy_id=self.session_id,
                include_nearby_context=False,
                include_document_snapshot=False,
            )
            if not self._start_future.done():
                self._start_future.set_result({'ok': True, 'location': startup_location})
            startup_command = self._commands_by_id.get(f'{self.session_id}:start')
            if startup_command is not None and startup_command.state == 'timed_out_pending_reconciliation':
                with self._state_lock:
                    startup_command.result = {'ok': True, 'location': startup_location}
                    self._apply_normalized_outcome(startup_command, startup_command.result)
                    startup_command.state = 'completed_after_timeout'
                    data = self._ensure_reconciliation_data(startup_command)
                    data['recovery'] = {'state': 'quarantined'}
                    startup_command.completed_event.set()
                    if not startup_command.future.done():
                        startup_command.future.set_result(startup_command.result)
                self._persist_command(startup_command)

            while True:
                command = self._commands.get()
                if command.name == '__close__':
                    refusal: LocalCliRuntimeError | None = None
                    with self._state_lock:
                        if (self._native_custody_held or self._normal_close_requires_save) and not self._allow_native_cleanup:
                            # Admitted before a numbering callback became held; custody is re-checked at dequeue.
                            refusal = LocalCliRuntimeError(_HELD_CLOSE_MESSAGE if self._native_custody_held else 'Normal close requires proved preservation of prior edits.')
                            command.error = refusal
                            self._apply_normalized_outcome(command, error=refusal)
                            command.state = 'failed'
                            self._closing = False
                            self._close_future = None
                            held_commands = [c for c in self._commands_by_id.values() if c.custody_held]
                            if held_commands:
                                self._last_command_id = max(held_commands, key=lambda c: c.sequence).command_id
                        else:
                            command.state = 'running'
                    if refusal is not None:
                        command.completed_event.set()
                        try:
                            self._persist_command(command)
                        except Exception:
                            pass
                        if not command.future.done():
                            command.future.set_exception(refusal)
                        continue
                    # Dequeue is not completion. The actual finalizer owns the
                    # result/future, including failure of the locked wrapper.
                    break
                if command.name == '__recover__':
                    recovery_future = command.future
                    target_id = command.recovery_for_command_id
                    try:
                        if not target_id or self._active_hwp is None:
                            raise LocalCliRuntimeError('Native recovery handle is unavailable.')
                        self._perform_recovery(target_id, self._active_hwp)
                        if not recovery_future.done():
                            recovery_future.set_result(True)
                    except Exception as exc:
                        target = self._commands_by_id.get(target_id or '')
                        if target is not None:
                            with self._state_lock:
                                data = self._ensure_reconciliation_data(target)
                                recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
                                if recovery.get('state') != 'preserved':
                                    recovery['state'] = 'failed'
                                    recovery['error_code'] = type(exc).__name__
                                data['recovery'] = recovery
                            try:
                                self._persist_command(target)
                            except Exception:
                                pass
                        if not recovery_future.done():
                            recovery_future.set_exception(exc)
                    finally:
                        with self._state_lock:
                            if self._recovery_future is recovery_future:
                                self._recovery_future = None
                                self._recovery_target_id = None
                    continue
                with self._state_lock:
                    if command.state != 'queued' or self._terminal_error is not None:
                        if command.state == 'queued':
                            command.state = 'cancelled'
                            command.error = LocalCliRuntimeError(
                                'The live local CLI session terminalized before this command ran.'
                            )
                            if not command.future.done():
                                command.future.set_exception(command.error)
                            command.completed_event.set()
                        continue
                    command.state = 'running'
                self._active_command = command
                handle.diagnostic_site = 'command_handler'
                handle.mutation_intent = False
                try:
                    update_runtime_status(
                        self.log_path,
                        phase=f'local_cli_{command.name}',
                        detail=self.source_filename,
                        extra={'local_cli_task': command.name},
                    )
                    if command.handler is None:
                        raise LocalCliRuntimeError(f'No handler was provided for local CLI command: {command.name}')
                    # All actual service mutators (including arbitrary pycall,
                    # bundles, export and selection/movement helpers) enter
                    # here. Unknown callers fail conservatively. Only audited
                    # read commands avoid current intent; earlier intent stays.
                    if command.name not in {'health_probe', 'find', 'info', 'where', 'screenshot', 'command-bundle'}:
                        self._mark_mutating_intent()
                    command.result = command.handler(handle)
                    handle.diagnostic_site = 'handler_returned'
                    self._apply_normalized_outcome(command, command.result)
                    if command.name in _MUTATING_COMMANDS - {'command-bundle'} and command.semantic_ok is not True and not command.custody_held:
                        # A failed mutator's false per-step flags do not prove
                        # lifetime no-edit or authorize discard.
                        self._hold_native_custody(command, 'Failed native command has an unproved outcome; session retained.', None)
                    with self._state_lock:
                        terminal_state = 'failed' if command.semantic_ok is not True else 'succeeded'
                        if command.timed_out:
                            terminal_state = (
                                'failed_after_timeout' if command.semantic_ok is not True else 'completed_after_timeout'
                            )
                        command.state = terminal_state
                        data = self._ensure_reconciliation_data(command)
                        if command.timed_out:
                            recovery = data.get('recovery') if isinstance(data.get('recovery'), dict) else {}
                            recovery['state'] = 'quarantined'
                            data['recovery'] = recovery
                        # Keep the terminal state, result, journal write, and
                        # future resolution together.  A request timeout must
                        # not observe a half-committed successful mutation and
                        # relabel it as a failed/retryable command.
                        if command.custody_held:
                            self._settle_held_command(command, result=command.result)
                        else:
                            self._persist_command(command)
                            if (command.name == 'save' and command.semantic_ok is True
                                    and command.result.get('ordinary_save_confirmed') is True
                                    and not command.timed_out and not self._quarantined):
                                self._normal_close_requires_save = False
                            if not command.future.done():
                                command.future.set_result(command.result)
                except Exception as exc:
                    # Publish custody before diagnostics or any fallible report.
                    # A handler exception does not establish native no-effect.
                    if not command.custody_held:
                        self._hold_native_custody(command, 'The native command outcome is unknown; session retained.', exc)
                    command.native_diagnostic = _native_diagnostic(handle.diagnostic_site, exc)
                    if command.custody_held:
                        with self._state_lock:
                            self._settle_held_command(command, escaped=True)
                except BaseException as signal:
                    # Trusted invocation history, not a diagnostic label or
                    # an exception's arbitrary attribute callback, decides
                    # whether a signal can propagate as clean pre-edit.
                    if not self._lifetime_edit_possible and not command.custody_held:
                        command.error = signal
                        command.state = 'failed'
                        command.semantic_ok = False
                        if not command.future.done():
                            command.future.set_exception(signal)
                        raise
                    self._hold_native_custody(command, 'Native command interrupted; outcome unknown and session retained.', signal)
                    command.native_diagnostic = _native_diagnostic(handle.diagnostic_site, signal)
                    with self._state_lock:
                        self._settle_held_command(command, escaped=True)
                finally:
                    self._active_command = None
                    command.completed_event.set()
                    for report in (lambda: self._persist_command(command), self._prune_in_memory_commands):
                        try:
                            report()
                        except BaseException as report_error:  # in-memory hold precedes all further reporting
                            # An already returned successful command remains
                            # successful; this later session-report fault must
                            # not discard its possible edits or stop its STA.
                            if not command.custody_held:
                                self._hold_native_custody(command, 'Session reporting failed; native custody retained.', report_error)
                            if isinstance(command.error, LocalCliNativeCustodyHeldError):
                                command.error.reporting_errors.append(report_error)
        except BaseException as exc:
            # Startup may already have allocated or opened native state. Publish
            # custody before resolving startup/reporting; never teardown UNKNOWN.
            if getattr(self, '_constructor_invoked', False):
                self._native_custody_held = self._quarantined = True
                self._allow_native_cleanup = False
                self._active_hwp = hwp
                wrapped = LocalCliNativeCustodyHeldError('Native startup outcome is unknown; owning STA retained.')
            else:
                wrapped = LocalCliRuntimeError('Local CLI startup failed before native construction.')
            wrapped.__cause__ = exc
            self._terminal_error = wrapped
            if not self._start_future.done():
                self._start_future.set_exception(wrapped)
        finally:
            self._finalize(
                hwp=hwp,
                pythoncom=pythoncom,
                coinitialized=coinitialized,
                watchdog_stop=watchdog_stop,
                watchdog_thread=watchdog_thread,
            )


class LocalCliRuntimeManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, LocalCliLiveSession] = {}
        self._session_roots: dict[str, Path] = {}

    def open_session(
        self,
        *,
        session_id: str,
        session_root: Path,
        working_copy_path: Path,
        source_filename: str,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.is_terminal():
                if getattr(existing, 'has_unreconciled_reconciliation', lambda: True)():
                    raise LocalCliRuntimeError(
                        f'Local CLI live session is terminal and requires reconciliation before reuse: {session_id}'
                    )
                if existing.is_alive():
                    raise LocalCliRuntimeError(
                        f'Local CLI live session is terminal and cleanup is still in progress: {session_id}'
                    )
                self._sessions.pop(session_id, None)
            if existing is not None and existing.is_alive():
                raise LocalCliRuntimeError(f'Local CLI live session already exists: {session_id}')
            live_session = LocalCliLiveSession(
                session_id=session_id,
                session_root=session_root,
                working_copy_path=working_copy_path,
                source_filename=source_filename,
            )
            self._sessions[session_id] = live_session
            self._session_roots[session_id] = session_root
        try:
            return live_session.start()
        except Exception:
            with self._lock:
                # A timed-out start owns a still-reconcilable terminal session;
                # retain it until explicit close_session() cleanup completes.
                if not live_session.is_terminal():
                    self._sessions.pop(session_id, None)
                    self._session_roots.pop(session_id, None)
            raise

    def execute(
        self,
        *,
        session_id: str,
        command_name: str,
        handler: Callable[[LocalCliRuntimeHandle], Any],
        timeout: float = 90.0,
        prior_dirty: bool = False,
    ) -> Any:
        session = self.require_session(session_id)
        try:
            return session.execute(command_name, handler, timeout=timeout, **({'prior_dirty': True} if prior_dirty else {}))
        except LocalCliRuntimeError:
            if not session.is_alive() and not session.is_terminal():
                with self._lock:
                    self._sessions.pop(session_id, None)
            raise

    def close_session(self, session_id: str, *, timeout: float = 30.0, prior_dirty: bool = False) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        # Keep the live handle registered until native/COM cleanup has
        # completed. Status probes must not report a false closed state while
        # the close command is still releasing the runtime thread.
        session.close(timeout=timeout, **({'prior_dirty': True} if prior_dirty else {}))
        with self._lock:
            if self._sessions.get(session_id) is session:
                if not getattr(session, 'has_unreconciled_reconciliation', lambda: False)():
                    self._sessions.pop(session_id, None)
                    self._session_roots.pop(session_id, None)

    def has_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            session = self._sessions.get(session_id)
        return bool(session and session.is_alive())

    def command_status(
        self,
        session_id: str,
        command_id: str | None = None,
        *,
        session_root: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            known_root = self._session_roots.get(session_id)
        if session is None:
            root = session_root or known_root
            if root is not None:
                return read_command_journal(root, command_id)
            raise LocalCliRuntimeError('Live local CLI command status is unavailable: session is unknown.')
        return session.command_status(command_id)

    def command_custody(
        self,
        session_id: str,
        command_id: str,
        *,
        session_root: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            known_root = self._session_roots.get(session_id)
        if session is not None:
            with session._state_lock:
                command = session._commands_by_id.get(command_id)
                if command is not None:
                    return session._command_record(command, include_private=True)
        root = session_root or known_root
        if root is None:
            raise LocalCliRuntimeError('Live CLI command custody is unavailable: session is unknown.')
        return read_command_custody(root, command_id)

    def reconcile_command(
        self,
        session_id: str,
        command_id: str,
        *,
        session_root: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            known_root = self._session_roots.get(session_id)
        if session is not None:
            return session.reconcile_command(command_id)
        root = session_root or known_root
        if root is None:
            raise LocalCliRuntimeError('Live local CLI command reconciliation is unavailable: session is unknown.')
        return read_command_journal(root, command_id)

    def acknowledge_reconciliation(
        self,
        session_id: str,
        command_id: str,
        *,
        session_root: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            known_root = self._session_roots.get(session_id)
        if session is not None:
            return session.acknowledge_reconciliation(command_id)
        root = session_root or known_root
        if root is None:
            raise LocalCliRuntimeError('Live CLI reconciliation acknowledgement is unavailable: session is unknown.')
        return acknowledge_command_journal(root, command_id)

    def latest_command_sequence(self, session_id: str, *, session_root: Path | None = None) -> int:
        with self._lock:
            session = self._sessions.get(session_id)
            known_root = self._session_roots.get(session_id)
        if session is not None:
            return session.latest_command_sequence()
        root = session_root or known_root
        if root is None:
            return 0
        payload = read_json_object(command_journal_path(root))
        try:
            return int((payload or {}).get('latest_sequence', 0))
        except (TypeError, ValueError):
            return 0

    def reap_settled_sessions(self) -> int:
        """Drop only in-memory sessions with no outstanding reconciliation."""

        removed = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.is_alive() or session.has_unreconciled_reconciliation():
                    continue
                self._sessions.pop(session_id, None)
                self._session_roots.pop(session_id, None)
                removed += 1
        return removed

    def require_session(self, session_id: str) -> LocalCliLiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or not session.is_alive():
            raise LocalCliRuntimeError('Live local CLI session is unavailable. Re-open the document.')
        return session


_LOCAL_CLI_RUNTIME_MANAGER = LocalCliRuntimeManager()


def get_local_cli_runtime_manager() -> LocalCliRuntimeManager:
    return _LOCAL_CLI_RUNTIME_MANAGER
