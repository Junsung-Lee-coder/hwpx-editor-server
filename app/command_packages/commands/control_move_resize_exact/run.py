from __future__ import annotations

from typing import Any, Mapping

from app.command_packages.common import run_step_for_op, validate_step_for_op

OP = 'control_move_resize_exact'


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    return validate_step_for_op(OP, service=service, index=index, step=step, manifest=manifest, error_type=error_type)


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    return run_step_for_op(OP, service=service, handle=handle, step=step, binding=binding, manifest=manifest)
