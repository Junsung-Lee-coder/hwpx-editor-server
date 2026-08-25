from __future__ import annotations

from typing import Any, Mapping


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    if 'match' in step and step.get('match') not in (None, ''):
        value = str(step.get('match') or '').strip()
        if len(value) > 500:
            raise error_type(f'command-bundle step {index} match is too long', status_code=400)
        step['match'] = value
    elif 'match' in step:
        step['match'] = None
    if 'keep_position' in step and not isinstance(step.get('keep_position'), bool):
        raise error_type(f'command-bundle step {index} keep_position must be boolean when provided', status_code=400)
    return step


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._bundle_style_inspect(handle.hwp, step)  # noqa: SLF001 - packaged command delegates to existing proven primitive.
    result = dict(result)
    result['package_schema_version'] = manifest.get('version') or 'local-cli/style-inspect/v1-package'
    return result, False, list(result.get('warnings') or [])
