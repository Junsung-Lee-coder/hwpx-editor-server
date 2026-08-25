from __future__ import annotations

from typing import Any, Mapping

_MACRO_MAX_STRING_CHARS = 10000


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    target = str(step.get('target') or '').strip()
    if not target:
        raise error_type(f'command-bundle step {index} anchor_insert requires non-empty target')
    if len(target) > 500:
        raise error_type(f'command-bundle step {index} anchor_insert target is too long')
    position = service._normalize_anchor_insert_position(step.get('position'))  # noqa: SLF001 - command package validates server primitive.
    text = step.get('text')
    fragments = step.get('fragments')
    if text is not None and fragments is not None:
        raise error_type(f'command-bundle step {index} anchor_insert accepts text or fragments, not both')
    if fragments is not None:
        if not isinstance(fragments, list) or not fragments or any(not isinstance(item, str) or item == '' for item in fragments):
            raise error_type(f'command-bundle step {index} anchor_insert fragments must be non-empty strings')
        if sum(len(item) for item in fragments) > _MACRO_MAX_STRING_CHARS:
            raise error_type(f'command-bundle step {index} anchor_insert fragments are too long')
        step['fragments'] = fragments
    elif not isinstance(text, str) or text == '':
        raise error_type(f'command-bundle step {index} anchor_insert requires non-empty text or fragments')
    elif len(text) > _MACRO_MAX_STRING_CHARS:
        raise error_type(f'command-bundle step {index} anchor_insert text is too long')
    step['target'] = target
    step['position'] = position
    return step


def _anchor_insert_text_from_step(step: Mapping[str, Any]) -> str:
    fragments = step.get('fragments')
    if isinstance(fragments, list):
        return ''.join(str(item) for item in fragments)
    return str(step.get('text') or '')


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._perform_anchor_insert(  # noqa: SLF001 - package wrapper for existing guarded primitive.
        handle.hwp,
        target=str(step.get('target') or ''),
        position=str(step.get('position') or 'before-anchor'),
        text=_anchor_insert_text_from_step(step),
        session_root=handle.session_root,
    )
    warnings = [str(item) for item in result.get('warnings') or []]
    return result, True, warnings
