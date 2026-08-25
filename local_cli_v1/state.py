from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_state_path() -> Path:
    override = os.environ.get('HWPX_LOCAL_STATE_PATH')
    if override:
        return Path(override).expanduser()
    return Path.home() / '.cache' / 'hwpx-local-cli' / 'state.json'


def load_state(path: Path | None = None) -> dict[str, Any]:
    state_path = path or default_state_path()
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(payload: dict[str, Any], path: Path | None = None) -> Path:
    state_path = path or default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return state_path


def clear_state(path: Path | None = None) -> None:
    state_path = path or default_state_path()
    if state_path.exists():
        state_path.unlink()


def clear_session_binding(path: Path | None = None) -> Path | None:
    state_path = path or default_state_path()
    state = load_state(state_path)
    if not state:
        clear_state(state_path)
        return None

    for key in ('session_id', 'last_find_query'):
        state.pop(key, None)

    if state:
        return save_state(state, state_path)

    clear_state(state_path)
    return None
