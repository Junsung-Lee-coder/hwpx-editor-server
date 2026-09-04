from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.atomic_json import atomic_write_json, read_json_object, update_json_object
from app.config import get_settings
from app.poppler import PopplerResolutionError, resolve_pdftoppm

settings = get_settings()

READINESS_SCHEMA_VERSION = 'hwpx/runtime-readiness/v2'
DEFAULT_READINESS_TTL_SECONDS = 120
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15
MAX_READINESS_ERROR_CHARS = 4096


class ReadinessOwnershipError(RuntimeError):
    """Raised when a superseded worker tries to publish readiness state."""


def _bounded_error(value: object) -> str:
    text = str(value).strip()
    return text[:MAX_READINESS_ERROR_CHARS]


def _process_start_identity(process_id: int) -> str | None:
    """Return a stable process-generation token without exposing command data."""

    if process_id <= 0:
        return None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                return f'win-filetime:{ticks:016x}'
            finally:
                kernel32.CloseHandle(handle)

        stat_path = Path(f'/proc/{process_id}/stat')
        fields = stat_path.read_text(encoding='utf-8').split()
        # procfs field 22 (starttime) is index 21 after the comm field.  The
        # worker executable name is deliberately not included in the token.
        start_ticks = fields[21]
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
        return f'proc:{boot_id}:{start_ticks}'
    except (OSError, IndexError, ValueError, AttributeError):
        return None


_WORKER_START_IDENTITY = _process_start_identity(os.getpid())


def current_worker_identity() -> dict[str, Any]:
    return {
        'pid': os.getpid(),
        'start_identity': _WORKER_START_IDENTITY or f'pid-session:{os.getpid()}',
    }


def new_readiness_run_id() -> str:
    return uuid.uuid4().hex


def resolve_candidate_generation() -> str | None:
    configured = os.environ.get('HWP_CANDIDATE_GENERATION')
    expected_from_environment = None
    if configured:
        value = configured.strip()
        if value and len(value) <= 256 and '\x00' not in value:
            expected_from_environment = value

    configured_manifest = getattr(settings, 'source_manifest', None)
    candidates = []
    if configured_manifest:
        candidates.append(Path(configured_manifest))
    candidates.extend(
        (
            Path(__file__).resolve().parents[1] / 'source-manifest.json',
            Path.cwd() / 'source-manifest.json',
        )
    )
    for manifest_path in candidates:
        try:
            manifest_path = manifest_path.expanduser().resolve()
            raw = manifest_path.read_bytes()
            if len(raw) > 8 * 1024 * 1024:
                continue
            payload = json.loads(raw.decode('utf-8'))
            commit = str(payload.get('commit') or '')
            tree = str(payload.get('tree') or '')
            if len(commit) not in (40, 64) or len(tree) not in (40, 64):
                continue
            if any(char not in '0123456789abcdefABCDEF' for char in commit + tree):
                continue
            import hashlib

            manifest_sha = hashlib.sha256(raw).hexdigest()
            derived = f'{commit.lower()}:{tree.lower()}:{manifest_sha}'
            # Environment configuration is an assertion supplied by the
            # installer, not an authority that can replace the bytes on disk.
            # Refuse a mismatch instead of allowing a forged generation to
            # become the worker's identity.
            if expected_from_environment is not None and expected_from_environment != derived:
                return None
            return derived
        except (OSError, UnicodeError, ValueError, AttributeError):
            continue
    return None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _process_generation_alive(process_id: int, expected_start_identity: str) -> bool:
    if process_id <= 0 or not expected_start_identity:
        return False
    # Windows os.kill(pid, 0) is not a POSIX-style existence probe: it can
    # terminate the target or deliver a console control signal. The Windows
    # start-time token is already the authoritative liveness check.
    if os.name == 'nt':
        actual = _process_start_identity(process_id)
        return actual is not None and actual == expected_start_identity
    try:
        os.kill(process_id, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    actual = _process_start_identity(process_id)
    return actual is not None and actual == expected_start_identity


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def readiness_dir() -> Path:
    return settings.spool_root / 'readiness'


def readiness_artifact_path() -> Path:
    return readiness_dir() / 'worker_ready.json'


def _close_probe_hwp(hwp: Any) -> None:
    for method_name in ('Quit', 'quit', 'Close', 'close'):
        method = getattr(hwp, method_name, None)
        if callable(method):
            method()
            return
    raise RuntimeError('Hancom automation probe object has no supported close method.')


def _construct_probe_hwp(Hwp: Any) -> tuple[Any, str]:
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


def _probe_hwp_automation() -> dict[str, Any]:
    details: dict[str, Any] = {'ok': False}
    pythoncom = None
    coinitialized = False
    hwp = None
    cleanup_errors: list[str] = []
    probe_succeeded = False
    try:
        try:
            import pythoncom  # type: ignore
        except ImportError:
            pythoncom = None

        if pythoncom is not None:
            pythoncom.CoInitialize()
            coinitialized = True

        from pyhwpx import Hwp  # type: ignore

        hwp, constructor = _construct_probe_hwp(Hwp)
        details['constructor'] = constructor
        details['security_module_registration_mode'] = 'manual_post_init'
        probe_succeeded = True
        details['ok'] = True
        details['detail'] = 'Hancom automation probe succeeded.'
        return details
    except Exception as exc:
        details['detail'] = str(exc)
        details['traceback'] = _bounded_error(traceback.format_exc())
        return details
    finally:
        if hwp is not None:
            try:
                _close_probe_hwp(hwp)
                details['probe_closed'] = True
            except Exception as exc:
                cleanup_errors.append(f'probe close: {_bounded_error(exc)}')
                details['probe_closed'] = False
        if pythoncom is not None and coinitialized:
            try:
                pythoncom.CoUninitialize()
                details['com_uninitialized'] = True
            except Exception as exc:
                cleanup_errors.append(f'COM uninitialize: {_bounded_error(exc)}')
                details['com_uninitialized'] = False
        if cleanup_errors:
            details['ok'] = False
            details['cleanup_ok'] = False
            details['cleanup_errors'] = cleanup_errors
            details['detail'] = '; '.join(cleanup_errors)
        else:
            details['cleanup_ok'] = True
        details['probe_succeeded'] = probe_succeeded


def build_pdf_renderer_check(*, explicit: str | None = None) -> dict[str, Any]:
    """Resolve the PDF renderer independently from Hancom automation.

    The result is intentionally structured so installers/verifiers can report a
    renderer failure without conflating it with a COM or API readiness failure.
    """

    configured = explicit
    if configured is None:
        configured = getattr(settings, 'pdftoppm_path', None)
    try:
        resolution = resolve_pdftoppm(explicit=configured)
        # Renderer readiness is returned through the local API and therefore
        # must not disclose user names, install roots, PATH entries, or the
        # candidate list used during resolution. The full resolution object
        # remains available to installer/verifier callers that own the local
        # diagnostics boundary.
        resolved = bool(getattr(resolution, 'ok', False))
        return {
            'ok': resolved,
            'source': str(getattr(resolution, 'source', 'unknown')),
            'detail': 'pdftoppm resolved' if resolved else 'pdftoppm is unavailable',
        }
    except PopplerResolutionError:
        return {
            'ok': False,
            'source': 'explicit-invalid',
            'detail': 'pdftoppm is unavailable',
        }


def build_runtime_readiness_snapshot(
    *,
    probe_hwp: bool,
    run_id: str | None = None,
    candidate_generation: str | None = None,
    worker_identity: dict[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_READINESS_TTL_SECONDS,
) -> dict[str, Any]:
    if ttl_seconds < 1:
        raise ValueError('ttl_seconds must be positive')
    run_id = run_id or new_readiness_run_id()
    candidate_generation = candidate_generation or resolve_candidate_generation()
    worker_identity = worker_identity or current_worker_identity()
    checked_at = datetime.now(timezone.utc)
    expires_at = checked_at.timestamp() + ttl_seconds
    checks: dict[str, Any] = {}
    errors: list[str] = []

    is_windows = sys.platform == 'win32'
    checks['platform'] = {
        'ok': is_windows,
        'detail': sys.platform,
    }
    if not is_windows:
        errors.append('Windows desktop session is required.')

    checks['candidate_generation'] = {
        'ok': bool(candidate_generation),
        'bound': bool(candidate_generation),
    }
    if not candidate_generation:
        errors.append('Installed candidate generation is not bound.')

    checks['pdf_renderer'] = build_pdf_renderer_check()
    if not checks['pdf_renderer'].get('ok'):
        errors.append('pdftoppm PDF renderer is not available.')

    try:
        import pythoncom  # type: ignore  # noqa: F401

        checks['pythoncom_import'] = {'ok': True, 'detail': 'pythoncom import succeeded.'}
    except Exception as exc:
        checks['pythoncom_import'] = {'ok': False, 'detail': str(exc)}
        errors.append('pywin32/pythoncom is not available.')

    try:
        from pyhwpx import Hwp  # type: ignore  # noqa: F401

        checks['pyhwpx_import'] = {'ok': True, 'detail': 'pyhwpx import succeeded.'}
    except Exception as exc:
        checks['pyhwpx_import'] = {'ok': False, 'detail': str(exc)}
        errors.append('pyhwpx is not available.')

    if probe_hwp and is_windows and checks['pyhwpx_import']['ok']:
        checks['hancom_automation'] = _probe_hwp_automation()
        if not checks['hancom_automation'].get('ok'):
            errors.append('Hancom automation probe failed.')
    elif probe_hwp:
        checks['hancom_automation'] = {
            'ok': False,
            'detail': 'Skipped because prerequisite imports/platform were not ready.',
        }

    ready = not errors
    summary = 'ready' if ready else '; '.join(errors)
    return {
        'schema_version': READINESS_SCHEMA_VERSION,
        'ok': True,
        'ready': ready,
        'status': 'ready' if ready else 'not_ready',
        'phase': 'probe_complete',
        'checked_at': checked_at.isoformat(),
        'started_at': checked_at.isoformat(),
        'expires_at': datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        'ttl_seconds': ttl_seconds,
        'freshness_window_seconds': ttl_seconds,
        'worker_name': settings.worker_name,
        'worker_pid': int(worker_identity.get('pid') or 0),
        'worker_start_identity': str(worker_identity.get('start_identity') or ''),
        'run_id': run_id,
        'candidate_generation': candidate_generation,
        'heartbeat': {
            'last_at': checked_at.isoformat(),
            'sequence': 0,
            'interval_seconds': DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        },
        'probe_hwp': probe_hwp,
        'checks': checks,
        'errors': [_bounded_error(error) for error in errors],
        'summary': summary,
        'artifact_path': str(readiness_artifact_path()),
    }


def build_current_run_not_ready_snapshot(
    *,
    run_id: str,
    candidate_generation: str | None,
    worker_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    worker_identity = worker_identity or current_worker_identity()
    expires_at = now.timestamp() + DEFAULT_READINESS_TTL_SECONDS
    return {
        'schema_version': READINESS_SCHEMA_VERSION,
        'ok': True,
        'ready': False,
        'status': 'not_ready',
        'phase': 'probing',
        'checked_at': now.isoformat(),
        'started_at': now.isoformat(),
        'expires_at': datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        'ttl_seconds': DEFAULT_READINESS_TTL_SECONDS,
        'freshness_window_seconds': DEFAULT_READINESS_TTL_SECONDS,
        'worker_name': settings.worker_name,
        'worker_pid': int(worker_identity.get('pid') or 0),
        'worker_start_identity': str(worker_identity.get('start_identity') or ''),
        'run_id': run_id,
        'candidate_generation': candidate_generation,
        'heartbeat': {
            'last_at': now.isoformat(),
            'sequence': 0,
            'interval_seconds': DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        },
        'probe_hwp': True,
        'checks': {'candidate_generation': {'ok': bool(candidate_generation), 'bound': bool(candidate_generation)}},
        'errors': ['Current worker Hancom automation probe is pending.'],
        'summary': 'Current worker Hancom automation probe is pending.',
        'artifact_path': str(readiness_artifact_path()),
    }


def readiness_matches_current_worker(
    snapshot: object,
    *,
    candidate_generation: str | None = None,
    run_id: str | None = None,
) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get('schema_version') != READINESS_SCHEMA_VERSION:
        return False
    if snapshot.get('ok') is not True:
        return False
    if snapshot.get('status') != 'ready' or not bool(snapshot.get('ready')):
        return False
    if snapshot.get('phase') != 'probe_complete':
        return False
    if snapshot.get('errors') not in ([], None):
        return False
    checks = snapshot.get('checks')
    if not isinstance(checks, dict):
        return False
    required_checks = ('platform', 'candidate_generation', 'pdf_renderer', 'pythoncom_import', 'pyhwpx_import')
    if bool(snapshot.get('probe_hwp')):
        required_checks += ('hancom_automation',)
    for check_name in required_checks:
        check = checks.get(check_name)
        if not isinstance(check, dict) or check.get('ok') is not True:
            return False
    if run_id is not None and snapshot.get('run_id') != run_id:
        return False
    expected_generation = candidate_generation or resolve_candidate_generation()
    actual_generation = snapshot.get('candidate_generation')
    if not expected_generation or actual_generation != expected_generation:
        return False
    try:
        worker_pid = int(snapshot.get('worker_pid') or 0)
    except (TypeError, ValueError):
        return False
    worker_start_identity = str(snapshot.get('worker_start_identity') or '')
    if not _process_generation_alive(worker_pid, worker_start_identity):
        return False
    expires_at = _parse_utc(snapshot.get('expires_at'))
    heartbeat = snapshot.get('heartbeat')
    heartbeat_at = _parse_utc(heartbeat.get('last_at') if isinstance(heartbeat, dict) else None)
    now = datetime.now(timezone.utc)
    if expires_at is None or heartbeat_at is None or expires_at <= now:
        return False
    freshness_window = snapshot.get('freshness_window_seconds', snapshot.get('ttl_seconds', DEFAULT_READINESS_TTL_SECONDS))
    try:
        freshness_window = float(freshness_window)
    except (TypeError, ValueError):
        return False
    if freshness_window <= 0 or heartbeat_at > now or (now - heartbeat_at).total_seconds() > freshness_window:
        return False
    return bool(snapshot.get('run_id'))


def touch_runtime_readiness_heartbeat(snapshot: dict[str, Any]) -> dict[str, Any]:
    updated = dict(snapshot)
    now = datetime.now(timezone.utc)
    heartbeat = dict(updated.get('heartbeat') or {})
    try:
        sequence = int(heartbeat.get('sequence') or 0) + 1
    except (TypeError, ValueError):
        sequence = 1
    heartbeat.update({'last_at': now.isoformat(), 'sequence': sequence})
    updated['heartbeat'] = heartbeat
    updated['checked_at'] = now.isoformat()
    try:
        ttl_seconds = max(1, int(updated.get('ttl_seconds') or DEFAULT_READINESS_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl_seconds = DEFAULT_READINESS_TTL_SECONDS
    updated['ttl_seconds'] = ttl_seconds
    updated['freshness_window_seconds'] = ttl_seconds
    updated['expires_at'] = datetime.fromtimestamp(
        now.timestamp() + ttl_seconds,
        timezone.utc,
    ).isoformat()
    return updated


def write_runtime_readiness_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_run_id: str | None = None,
) -> Path:
    path = readiness_artifact_path()
    if expected_run_id is None:
        atomic_write_json(path, snapshot)
    else:
        if not expected_run_id:
            raise ValueError('expected_run_id must not be empty')

        def replace_owned(current: dict[str, Any]) -> dict[str, Any]:
            if current.get('run_id') != expected_run_id:
                raise ReadinessOwnershipError(
                    'Runtime readiness is owned by a different worker run.'
                )
            return snapshot

        update_json_object(path, replace_owned)
    readback = read_json_object(path)
    if readback != snapshot:
        raise OSError('Runtime readiness JSON readback differed after atomic write.')
    return path


def load_runtime_readiness_snapshot() -> dict[str, Any] | None:
    path = readiness_artifact_path()
    try:
        payload = read_json_object(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def build_plain_readiness_failure(task_label: str) -> str:
    snapshot = load_runtime_readiness_snapshot()
    if snapshot is None:
        return (
            f'first_run_readiness_required: {task_label} job acceptance is blocked until the packaged writer v1 '
            'worker reports runtime_readiness.ready=true from the Windows desktop session.'
        )
    summary = str(snapshot.get('summary') or 'runtime readiness is not ready').strip()
    return f'first_run_readiness_failed: {task_label} job acceptance is blocked: {summary}'
