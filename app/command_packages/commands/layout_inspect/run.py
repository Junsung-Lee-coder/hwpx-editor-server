from __future__ import annotations

from typing import Any, Mapping


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    return step


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    result = service._bundle_layout_inspect(handle.hwp, step)  # noqa: SLF001
    return result, False, list(result.get('warnings') or [])
