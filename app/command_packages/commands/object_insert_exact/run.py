from __future__ import annotations

from typing import Any, Mapping

from app.command_packages.common import _raise
from app.object_insert import OP, PARAM_KEYS, ObjectInsertError, normalize_step

_BASE_KEYS = ('op', 'operation', 'label', 'kind', 'expected_pos', 'confirm_mutation')


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    allowed = set(manifest.get('allowed_keys') or ()) or {*_BASE_KEYS, *PARAM_KEYS}
    unknown = sorted(key for key in step if key not in allowed)
    if unknown:
        _raise(error_type, f'command-bundle step {index} {OP} has unsupported fields: {", ".join(unknown)}')
    try:
        plan = normalize_step(step)
    except ObjectInsertError as exc:
        _raise(error_type, f'command-bundle step {index} {OP} {exc}')
    step['expected_pos'] = plan['expected_pos']
    if 'apply_to' in plan:
        step['apply_to'] = plan['apply_to']
    if 'treat_as_char' in plan:
        step['treat_as_char'] = plan['treat_as_char']
    return step


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._bundle_object_insert_exact(handle.hwp, step)  # noqa: SLF001
    return result, True, list(result.get('warnings') or [])
