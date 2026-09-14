"""Independent predicates for native HWPX persistence acceptance."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


CELL_MARGIN_KEYS = ('left', 'right', 'top', 'bottom')
_METADATA_ONLY_MEMBERS = frozenset({'Contents/content.hpf', 'Preview/PrvImage.png'})


def expected_uniform_margin_hu(millimeters: float) -> dict[str, int]:
    """Convert one request value without consulting native post-action data."""
    if isinstance(millimeters, bool) or not math.isfinite(float(millimeters)) or millimeters < 0:
        raise ValueError(f'invalid margin in millimeters: {millimeters!r}')
    value_hu = int(round(float(millimeters) * 7200.0 / 25.4))
    return {key: value_hu for key in CELL_MARGIN_KEYS}


def _normalize_margin(value: Mapping[str, Any]) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        raise AssertionError(f'invalid four-side margin readback: {value!r}')
    normalized: dict[str, int | float] = {}
    for key in CELL_MARGIN_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise AssertionError(f'invalid four-side margin readback: {value!r}')
        number = float(raw)
        if not math.isfinite(number) or number < 0:
            raise AssertionError(f'invalid four-side margin readback: {value!r}')
        normalized[key] = int(number) if number.is_integer() else number
    return normalized


def require_exact_margin_transition(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, dict[str, int | float]]:
    """Require a distinct native transition to the independently expected value."""
    normalized_before = _normalize_margin(before)
    normalized_after = _normalize_margin(after)
    normalized_expected = _normalize_margin(expected)
    if normalized_before == normalized_expected:
        raise AssertionError(f'no-op request already present: {normalized_expected!r}')
    if normalized_before == normalized_after:
        raise AssertionError(f'no-op native transition: before={normalized_before!r}')
    if normalized_after != normalized_expected:
        raise AssertionError(
            'native readback does not match requested value: '
            f'requested={normalized_expected!r}; after={normalized_after!r}'
        )
    return {
        'before': normalized_before,
        'after': normalized_after,
        'expected': normalized_expected,
    }


def semantic_content_members(changed_members: Iterable[str]) -> list[str]:
    """Return changed XML/HFP content members, excluding content.hpf metadata."""
    return sorted(
        name
        for name in changed_members
        if name.startswith('Contents/')
        and name.lower().endswith(('.xml', '.hpf'))
        and name.lower() != 'contents/content.hpf'
    )


def require_semantic_target_change(changed_members: Iterable[str], *, target_member: str) -> list[str]:
    """Reject metadata-only or unrelated content changes for one target edit."""
    changed = sorted(set(changed_members))
    semantic = semantic_content_members(changed)
    if not semantic:
        raise AssertionError(f'metadata-only HWPX change: changed_members={changed!r}')
    if target_member not in semantic:
        raise AssertionError(
            f'target content member did not change: target={target_member!r}; semantic={semantic!r}'
        )
    unexpected = [name for name in semantic if name != target_member]
    if unexpected:
        raise AssertionError(f'unexpected non-target content changes: {unexpected!r}')
    unexpected_members = [
        name
        for name in changed
        if name not in semantic and name not in _METADATA_ONLY_MEMBERS
    ]
    if unexpected_members:
        raise AssertionError(f'unexpected non-target HWPX changes: {unexpected_members!r}')
    return semantic
