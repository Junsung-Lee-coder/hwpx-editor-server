from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Callable
from typing import Any

from app.atomic_json import atomic_write_json, path_lock, read_json_object, update_json_object


class StatePersistenceError(RuntimeError):
    """Raised when cached CLI state exists but cannot be trusted."""


def default_state_path() -> Path:
    override = os.environ.get('HWPX_LOCAL_STATE_PATH')
    if override:
        return Path(override).expanduser()
    return Path.home() / '.cache' / 'hwpx-local-cli' / 'state.json'


def load_state(path: Path | None = None) -> dict[str, Any]:
    state_path = path or default_state_path()
    try:
        payload = read_json_object(state_path)
    except ValueError as exc:
        raise StatePersistenceError(str(exc)) from exc
    return payload or {}


def save_state(payload: dict[str, Any], path: Path | None = None) -> Path:
    state_path = path or default_state_path()
    if not isinstance(payload, dict):
        raise StatePersistenceError('CLI state payload must be a JSON object.')
    try:
        atomic_write_json(state_path, payload)
    except Exception as exc:
        raise StatePersistenceError(f'Could not persist CLI state: {state_path}') from exc
    return state_path


def update_state(
    updater: Callable[[dict[str, Any]], dict[str, Any]],
    path: Path | None = None,
    *,
    expected_generation: int | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Perform one locked CLI-state read/modify/write transaction.

    ``expected_generation`` and ``expected_session_id`` are checked against
    the state read while the file lock is held. A successful update receives
    the next monotonically increasing ``state_generation`` and is returned
    from the exact verified write.
    """

    state_path = path or default_state_path()
    if expected_generation is not None:
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
            raise StatePersistenceError('Expected CLI state generation is invalid.')
        expected_generation = int(expected_generation)
        if expected_generation < 0:
            raise StatePersistenceError('Expected CLI state generation is invalid.')
    expected_session = str(expected_session_id).strip() if expected_session_id is not None else None

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        current_generation_raw = current.get('state_generation', 0)
        if isinstance(current_generation_raw, bool) or not isinstance(current_generation_raw, int):
            raise StatePersistenceError('CLI state generation is invalid.')
        current_generation = int(current_generation_raw)
        if current_generation < 0:
            raise StatePersistenceError('CLI state generation is invalid.')
        if expected_generation is not None and current_generation != expected_generation:
            raise StatePersistenceError(
                f'CLI state generation conflict: expected {expected_generation}, got {current_generation}.'
            )
        if expected_session is not None and str(current.get('session_id') or '').strip() != expected_session:
            raise StatePersistenceError(
                f'CLI state session conflict: expected {expected_session}, '
                f"got {str(current.get('session_id') or '').strip() or '<none>'}."
            )
        try:
            updated = updater(dict(current))
        except StatePersistenceError:
            raise
        except Exception as exc:
            raise StatePersistenceError(f'CLI state update failed: {state_path}') from exc
        if not isinstance(updated, dict):
            raise StatePersistenceError('CLI state updater must return a JSON object.')
        updated = dict(updated)
        updated['state_generation'] = current_generation + 1
        return updated

    try:
        return update_json_object(state_path, _apply, default={})
    except StatePersistenceError:
        raise
    except Exception as exc:
        raise StatePersistenceError(f'Could not update CLI state atomically: {state_path}') from exc


def clear_state(path: Path | None = None) -> None:
    state_path = path or default_state_path()
    with path_lock(state_path):
        if state_path.exists():
            state_path.unlink()
        if state_path.exists():
            raise StatePersistenceError(f'Could not remove CLI state: {state_path}')


def clear_session_binding(
    path: Path | None = None,
    *,
    expected_generation: int | None = None,
    expected_session_id: str | None = None,
) -> Path | None:
    """Clear a session binding only when the caller still owns its snapshot."""
    state_path = path or default_state_path()
    try:
        update_state(
            lambda state: _clear_owned_binding(
                state,
                expected_generation=expected_generation,
                expected_session_id=expected_session_id,
            ),
            state_path,
            expected_generation=expected_generation,
            expected_session_id=expected_session_id,
        )
    except StatePersistenceError:
        raise
    return state_path if state_path.exists() else None


def _clear_owned_binding(
    state: dict[str, Any],
    *,
    expected_generation: int | None,
    expected_session_id: str | None,
) -> dict[str, Any]:
    """Updater used inside the state-file lock, never against a stale read."""

    active_session_id = str(state.get('session_id') or '').strip() or None
    if active_session_id is not None and (expected_generation is None or expected_session_id is None):
        raise StatePersistenceError(
            'Clearing an active CLI session requires expected_generation and expected_session_id.'
        )
    return {
        key: value
        for key, value in state.items()
        if key not in {'session_id', 'last_find_query'}
    }
