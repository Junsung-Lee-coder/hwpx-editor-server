from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
import os
import threading
import time
from typing import Any, Callable

from app.edit_ops import _capture_nearby_text_context, _snapshot_cursor_context
from app.native_actions import NativeActionRunner
from app.runtime_state import update_runtime_status, utc_now_iso
from app.worker import (
    close_hwp_instance,
    collect_hwp_snapshot,
    create_hwp_instance_with_recovery,
    emit_runtime_observation,
    kill_hwp_runtime,
    list_available_pdf_export_primitives,
    save_hwp_as,
    start_runtime_watchdog,
)


class LocalCliRuntimeError(RuntimeError):
    pass


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
            'Multiline hwpx type is disabled because Hancom paragraph-break insertion is not yet layout-safe. '
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


def save_document(hwp: object) -> None:
    result: Any
    if hasattr(hwp, 'save'):
        result = hwp.save()
    elif hasattr(hwp, 'Save'):
        result = hwp.Save()
    else:
        raise LocalCliRuntimeError('pyhwpx save/Save is unavailable on this machine')
    if result is False:
        raise LocalCliRuntimeError('Hancom save returned false for the active working copy.')


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


def discard_live_document(hwp: object) -> None:
    clear_method = _safe_hwp_attr(hwp, 'clear')
    if callable(clear_method):
        try:
            clear_method(option=1)
        except TypeError:
            clear_method(1)
        return

    close_method = _safe_hwp_attr(hwp, 'close')
    if not callable(close_method):
        close_method = _safe_hwp_attr(hwp, 'Close')
    if callable(close_method):
        try:
            close_method(is_dirty=False)
        except TypeError:
            close_method(False)


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


@dataclass
class _LiveCommand:
    name: str
    handler: Callable[[LocalCliRuntimeHandle], Any] | None
    future: Future[Any]


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

    def start(self, *, timeout: float = 90.0) -> dict[str, Any]:
        self._thread.start()
        try:
            return self._start_future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise LocalCliRuntimeError('Timed out while starting the live local CLI session.') from exc

    def execute(
        self,
        command_name: str,
        handler: Callable[[LocalCliRuntimeHandle], Any],
        *,
        timeout: float = 90.0,
    ) -> Any:
        if self._closed.is_set():
            raise LocalCliRuntimeError('The live local CLI session is already closed.')
        future: Future[Any] = Future()
        self._commands.put(_LiveCommand(name=command_name, handler=handler, future=future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise LocalCliRuntimeError(f'Timed out while waiting for local CLI command: {command_name}') from exc

    def close(self, *, timeout: float = 30.0) -> None:
        if self._closed.is_set():
            return
        future: Future[Any] = Future()
        self._commands.put(_LiveCommand(name='__close__', handler=None, future=future))
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise LocalCliRuntimeError('Timed out while closing the live local CLI session.') from exc

    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._closed.is_set()

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

            hwp = create_hwp_instance_with_recovery(
                Hwp,
                log_path=self.log_path,
                phase='local_cli_create_hwp_instance',
                detail=f'open {self.source_filename}',
            )
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
            )
            self._start_future.set_result(
                {
                    'ok': True,
                    'location': snapshot_live_location(
                        hwp=hwp,
                        source_filename=self.source_filename,
                        working_copy_id=self.session_id,
                        include_nearby_context=False,
                        include_document_snapshot=False,
                    ),
                }
            )

            while True:
                command = self._commands.get()
                if command.name == '__close__':
                    command.future.set_result({'ok': True})
                    break
                try:
                    update_runtime_status(
                        self.log_path,
                        phase=f'local_cli_{command.name}',
                        detail=self.source_filename,
                        extra={'local_cli_task': command.name},
                    )
                    if command.handler is None:
                        raise LocalCliRuntimeError(f'No handler was provided for local CLI command: {command.name}')
                    command.future.set_result(command.handler(handle))
                except Exception as exc:
                    if isinstance(exc, LocalCliRuntimeError):
                        command.future.set_exception(exc)
                    else:
                        command.future.set_exception(LocalCliRuntimeError(str(exc)))
        except Exception as exc:
            wrapped = exc if isinstance(exc, LocalCliRuntimeError) else LocalCliRuntimeError(str(exc))
            if not self._start_future.done():
                self._start_future.set_exception(wrapped)
        finally:
            if watchdog_stop is not None:
                watchdog_stop.set()
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=1.0)
            if hwp is not None:
                try:
                    discard_live_document(hwp)
                except Exception:
                    pass
            close_hwp_instance(hwp)
            if pythoncom is not None and coinitialized:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            self._closed.set()


class LocalCliRuntimeManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, LocalCliLiveSession] = {}

    def open_session(
        self,
        *,
        session_id: str,
        session_root: Path,
        working_copy_path: Path,
        source_filename: str,
    ) -> dict[str, Any]:
        live_session = LocalCliLiveSession(
            session_id=session_id,
            session_root=session_root,
            working_copy_path=working_copy_path,
            source_filename=source_filename,
        )
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.is_alive():
                raise LocalCliRuntimeError(f'Local CLI live session already exists: {session_id}')
            self._sessions[session_id] = live_session
        try:
            return live_session.start()
        except Exception:
            with self._lock:
                self._sessions.pop(session_id, None)
            raise

    def execute(
        self,
        *,
        session_id: str,
        command_name: str,
        handler: Callable[[LocalCliRuntimeHandle], Any],
        timeout: float = 90.0,
    ) -> Any:
        session = self.require_session(session_id)
        try:
            return session.execute(command_name, handler, timeout=timeout)
        except LocalCliRuntimeError:
            if not session.is_alive():
                with self._lock:
                    self._sessions.pop(session_id, None)
            raise

    def close_session(self, session_id: str, *, timeout: float = 30.0) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close(timeout=timeout)

    def has_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            session = self._sessions.get(session_id)
        return bool(session and session.is_alive())

    def require_session(self, session_id: str) -> LocalCliLiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or not session.is_alive():
            raise LocalCliRuntimeError('Live local CLI session is unavailable. Re-open the document.')
        return session


_LOCAL_CLI_RUNTIME_MANAGER = LocalCliRuntimeManager()


def get_local_cli_runtime_manager() -> LocalCliRuntimeManager:
    return _LOCAL_CLI_RUNTIME_MANAGER
