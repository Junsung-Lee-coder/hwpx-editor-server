from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def type_insert_guard_reason(
    binding: Mapping[str, Any],
    *,
    had_selection: bool,
    restored_cached_selection: bool,
    allow_insert_at_caret: bool,
) -> str | None:
    if allow_insert_at_caret or had_selection or restored_cached_selection:
        return None
    last_selection = binding.get('last_selection') if isinstance(binding.get('last_selection'), Mapping) else None
    if not last_selection:
        return None
    selected_text = str(last_selection.get('selected_text') or '').strip()
    selected_hash = str(last_selection.get('selected_text_hash') or '').strip()
    safe_for_type = last_selection.get('safe_for_type') is not False
    if not safe_for_type or not (selected_text or selected_hash):
        return None
    return (
        'Refusing to type at the caret because a recent safe selection exists but the live selection is no longer active. '
        'Run `hwpx select ...` again to replace it, or pass `--insert-at-caret` to intentionally insert at the caret.'
    )
