from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

settings = get_settings()


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
        details['ok'] = True
        details['detail'] = 'Hancom automation probe succeeded.'
        return details
    except Exception as exc:
        details['detail'] = str(exc)
        details['traceback'] = traceback.format_exc()
        return details
    finally:
        if hwp is not None:
            try:
                _close_probe_hwp(hwp)
            except Exception:
                pass
        if pythoncom is not None and coinitialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def build_runtime_readiness_snapshot(*, probe_hwp: bool) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    errors: list[str] = []

    is_windows = sys.platform == 'win32'
    checks['platform'] = {
        'ok': is_windows,
        'detail': sys.platform,
    }
    if not is_windows:
        errors.append('Windows desktop session is required.')

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
        'ok': True,
        'ready': ready,
        'status': 'ready' if ready else 'not_ready',
        'checked_at': utc_now_iso(),
        'worker_name': settings.worker_name,
        'probe_hwp': probe_hwp,
        'checks': checks,
        'errors': errors,
        'summary': summary,
        'artifact_path': str(readiness_artifact_path()),
    }


def write_runtime_readiness_snapshot(snapshot: dict[str, Any]) -> Path:
    path = readiness_artifact_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def load_runtime_readiness_snapshot() -> dict[str, Any] | None:
    path = readiness_artifact_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
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
