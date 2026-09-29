from __future__ import annotations

from typing import Any, Mapping

from app.command_packages.common import _clean_optional_text, _raise
from app.layout_ops import OP, LayoutError, normalize_request


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    _clean_optional_text(step, index, 'label', error_type)
    allowed = set(manifest.get('allowed_keys') or ())
    unknown = sorted(key for key in step if allowed and key not in allowed)
    if unknown:
        _raise(error_type, f'command-bundle step {index} {OP} does not take: {", ".join(unknown)}')
    try:
        request = normalize_request(step)
    except LayoutError as exc:
        _raise(error_type, f'command-bundle step {index} {OP}: {exc}')
        raise  # unreachable; _raise always raises
    step['expected_pos'] = list(request['expected_pos'])
    if request['kind'] in ('page_setup', 'columns'):
        step['apply_to'] = request['apply_to']
    if request['kind'] == 'columns':
        step['same_width'] = request['same_width']
    return step


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._bundle_layout_exact(handle.hwp, step)  # noqa: SLF001
    return result, True, list(result.get('warnings') or [])
