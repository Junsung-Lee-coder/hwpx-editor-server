"""Module-level helpers, limits, and error types shared by the local CLI service."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar


from app.edit_ops import (
    _normalize_visible_text,
    _selected_text_contains_probe,
)
from app.local_cli_runtime import (
    LocalCliRuntimeError,
)




T = TypeVar('T')


@dataclass
class LocalCliArtifactDownload:
    """A custody-checked stream whose bytes remain tied to one opened file."""

    stream: Any
    path: Path
    size_bytes: int
    sha256: str
    filename: str = ''

    def close(self) -> None:
        self.stream.close()


def _native_type_action_count(text: str) -> int:
    return 1


_LIVE_HEADING_SPLIT_RE = re.compile(r'[·ㆍ•:：\-–—,，/|]')
_IMAGE_ALLOWED_SUFFIXES = {'.png', '.jpg', '.jpeg', '.bmp'}
_IMAGE_SAFE_STEM_RE = re.compile(r'[^A-Za-z0-9._() -]+')
_CELL_MARGIN_KEYS = ('left', 'right', 'top', 'bottom')


def _cell_margins_safe_attr(hwp: Any, name: str) -> Any:
    """Attribute read that never raises; mirrors the runtime's safe accessor."""

    try:
        return getattr(hwp, name)
    except Exception:
        return None


def _cell_margins_ctrl_summary(ctrl: Any) -> dict[str, Any] | None:
    """Bounded control summary carrying the exact instance identity."""

    if ctrl is None:
        return None
    payload: dict[str, Any] = {}
    for attr in ('CtrlID', 'UserDesc'):
        value = _cell_margins_safe_attr(ctrl, attr)
        if value not in (None, ''):
            payload[attr] = value
    inst_id = _cell_margins_safe_attr(ctrl, 'CtrlInstID')
    if isinstance(inst_id, str) and inst_id:
        payload['CtrlInstID'] = inst_id
    return payload or None


def _safe_hwp_value(hwp: Any, name: str) -> Any:
    """Read a native attribute; call zero-argument methods, never fabricate values."""

    try:
        value = getattr(hwp, name)
    except Exception:
        return None
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _safe_parent_ctrl_summary(hwp: Any) -> dict[str, Any] | None:
    """Direct ParentCtrl summary with exact CtrlInstID when the runtime exposes one."""

    parent_ctrl = _cell_margins_safe_attr(hwp, 'ParentCtrl')
    return _cell_margins_ctrl_summary(parent_ctrl)


def _normalize_cell_margin_readback(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, int | float] = {}
    for key in _CELL_MARGIN_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        numeric = float(raw)
        if not math.isfinite(numeric) or numeric < 0:
            return None
        normalized[key] = int(numeric) if numeric.is_integer() else numeric
    return normalized


def _normalize_vertical_align_readback(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get('available') is not True:
        return None
    raw_value = value.get('value')
    raw_name = value.get('name')
    names = {0: 'top', 1: 'center', 2: 'bottom'}
    if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value not in names:
        return None
    if raw_name != names[raw_value]:
        return None
    return {'value': raw_value, 'name': raw_name}


def _valid_cell_addr(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value)
    )


def _normalize_cell_addr_value(value: Any) -> list[int] | None:
    if _valid_cell_addr(value):
        return [int(value[0]), int(value[1])]
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'([A-Za-z]+)([1-9][0-9]*)', value.strip())
    if match is None:
        return None
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + (ord(char) - ord('A') + 1)
    return [column - 1, int(match.group(2)) - 1]


def _valid_numeric_readback(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_observed_cell_format_mutation(
    operation: str,
    before_metrics: Mapping[str, Any],
    after_metrics: Mapping[str, Any],
    changed_metrics: Mapping[str, Any],
    *,
    expected_vertical_align: str | None = None,
    expected_cell_margin_hu: Mapping[str, Any] | None = None,
    expected_cell_addr: Any = None,
) -> None:
    """Reject format commands whose target property was not observed changing.

    Native action return values alone are not persistence evidence.  The
    supported format selectors therefore require an exact, valid before/after
    getter.  The expected value is derived from the request, never from the
    observed post-action value.
    """
    def fail(message: str) -> None:
        raise LocalCliMutationError(
            message,
            mutation_may_have_persisted=True,
            rollback={'attempted': False, 'succeeded': False},
        )

    if operation == 'set-cell-margin':
        before_margin = _normalize_cell_margin_readback(before_metrics.get('cell_margin_hu'))
        after_margin = _normalize_cell_margin_readback(after_metrics.get('cell_margin_hu'))
        if before_margin is None or after_margin is None:
            fail(
                'cell_format_exact has no usable native four-side cell-margin readback; '
                f'before={before_metrics.get("cell_margin_hu")!r}; after={after_metrics.get("cell_margin_hu")!r}'
            )
        before_addr = _normalize_cell_addr_value(before_metrics.get('cell_addr'))
        after_addr = _normalize_cell_addr_value(after_metrics.get('cell_addr'))
        expected_addr = _normalize_cell_addr_value(expected_cell_addr) if expected_cell_addr is not None else None
        if (
            before_addr is None
            or after_addr is None
            or before_addr != after_addr
            or (expected_cell_addr is not None and (expected_addr is None or before_addr != expected_addr))
        ):
            fail(
                'cell_format_exact did not preserve a valid same target cell identity; '
                f'before={before_metrics.get("cell_addr")!r}; after={after_metrics.get("cell_addr")!r}; '
                f'expected={expected_cell_addr!r}'
            )
        if before_margin == after_margin:
            fail(
                'cell_format_exact did not observe changed cell_margin_hu after mutation; '
                f'before={before_margin!r}; after={after_margin!r}'
            )
        if expected_cell_margin_hu is not None:
            expected_margin = _normalize_cell_margin_readback(expected_cell_margin_hu)
            if expected_margin is None:
                fail(
                    f'cell_format_exact expected cell-margin value is invalid: {expected_cell_margin_hu!r}'
                )
            if after_margin != expected_margin:
                fail(
                    'cell_format_exact cell-margin readback mismatched the requested value; '
                    f'requested={expected_margin!r}; after={after_margin!r}'
                )
        changed_margin = changed_metrics.get('cell_margin_hu')
        if isinstance(changed_margin, Mapping) and (
            changed_margin.get('before') != before_metrics.get('cell_margin_hu')
            or changed_margin.get('after') != after_metrics.get('cell_margin_hu')
        ):
            fail(
                'cell_format_exact cell-margin change record does not match native readback; '
                f'changed={changed_margin!r}; before={before_margin!r}; after={after_margin!r}'
            )
        return
    if operation != 'vertical-align':
        return

    before_vertical = before_metrics.get('vertical_align')
    after_vertical = after_metrics.get('vertical_align')
    normalized_before = _normalize_vertical_align_readback(before_vertical)
    normalized_after = _normalize_vertical_align_readback(after_vertical)
    if normalized_before is None:
        fail(
            'cell_format_exact vertical alignment has no usable native before readback; '
            f'before={before_vertical!r}'
        )
    if normalized_after is None:
        fail(
            'cell_format_exact vertical alignment has no usable native after readback; '
            f'after={after_vertical!r}'
        )
    before_addr = _normalize_cell_addr_value(before_metrics.get('cell_addr'))
    after_addr = _normalize_cell_addr_value(after_metrics.get('cell_addr'))
    expected_addr = _normalize_cell_addr_value(expected_cell_addr) if expected_cell_addr is not None else None
    if (
        before_addr is None
        or after_addr is None
        or before_addr != after_addr
        or (expected_cell_addr is not None and (expected_addr is None or before_addr != expected_addr))
    ):
        fail(
            'cell_format_exact did not preserve a valid same target cell identity; '
            f'before={before_metrics.get("cell_addr")!r}; after={after_metrics.get("cell_addr")!r}; '
            f'expected={expected_cell_addr!r}'
        )
    if normalized_before['value'] == normalized_after['value']:
        fail(
            'cell_format_exact did not observe changed vertical alignment after mutation; '
            f'before={before_vertical!r}; after={after_vertical!r}'
        )
    if expected_vertical_align and normalized_after['name'] != expected_vertical_align:
        fail(
            'cell_format_exact vertical alignment readback mismatched the requested value; '
            f'requested={expected_vertical_align!r}; after={after_vertical!r}'
        )


def _remove_visible_spaces(value: str) -> str:
    return ''.join(str(value or '').split())


def _append_live_find_candidate(candidates: list[tuple[str, bool]], value: str, *, allow_whole_word: bool) -> None:
    candidate = ' '.join(str(value or '').split()).strip()
    if len(candidate) >= 4:
        candidates.append((candidate, allow_whole_word))


def _strip_live_heading_prefixes(text: str) -> list[str]:
    variants: list[str] = []
    current = str(text or '').strip()
    patterns = [
        r'^\s*[①-⑳]\s*',
        r'^\s*[(\[]?[0-9]+[)\].:-]?\s*',
        r'^\s*[가-하][.)]\s*',
        r'^\s*[A-Za-z][.)]\s*',
        r'^\s*[■●•◦▪※□▶▷]\s*',
    ]

    for _ in range(4):
        updated = current
        for pattern in patterns:
            updated = re.sub(pattern, '', updated, count=1)
        updated = updated.strip()
        if not updated or updated == current:
            break
        variants.append(updated)
        current = updated

    return variants


def _build_live_heading_candidates(query: str) -> list[tuple[str, bool]]:
    compact = ' '.join(str(query or '').split()).strip()
    candidates: list[tuple[str, bool]] = []
    if not compact or len(compact) >= 40:
        return candidates

    heading_bases = [compact, *_strip_live_heading_prefixes(compact)]
    for base in heading_bases:
        _append_live_find_candidate(candidates, base, allow_whole_word=True)

        punctuation_compact = re.sub(r'([·ㆍ•:：\-–—,，/|])\s+', r'\1', base)
        _append_live_find_candidate(candidates, punctuation_compact, allow_whole_word=True)

        split_match = _LIVE_HEADING_SPLIT_RE.search(base)
        if split_match is None:
            continue

        tail_compact = f'{base[:split_match.end()]}{_remove_visible_spaces(base[split_match.end():])}'
        _append_live_find_candidate(candidates, tail_compact, allow_whole_word=True)

        head = base[:split_match.start()].strip()
        if head and head != base:
            _append_live_find_candidate(candidates, head, allow_whole_word=False)

    return candidates


def _live_candidate_is_query_anchor(*, query: str, candidate: str) -> bool:
    normalized_query = _normalize_visible_text(query).casefold()
    normalized_candidate = _normalize_visible_text(candidate).casefold()
    if len(normalized_candidate) < 4 or normalized_candidate == normalized_query:
        return False
    if normalized_candidate in normalized_query:
        return True

    compact_query = _remove_visible_spaces(normalized_query)
    compact_candidate = _remove_visible_spaces(normalized_candidate)
    return len(compact_candidate) >= 4 and compact_candidate in compact_query


def _selected_text_contains_probe_relaxed(selected_text: str, probe: str) -> bool:
    if _selected_text_contains_probe(selected_text, probe):
        return True
    normalized_selected = _normalize_visible_text(selected_text).casefold()
    normalized_probe = _normalize_visible_text(probe).casefold()
    compact_selected = _remove_visible_spaces(normalized_selected)
    compact_probe = _remove_visible_spaces(normalized_probe)
    return bool(compact_probe) and compact_probe in compact_selected


_MACRO_MAX_PATH_SEGMENTS = 8
_MACRO_MAX_ARGS = 20
_MACRO_MAX_KWARGS = 50
_MACRO_MAX_JSON_DEPTH = 6
_MACRO_MAX_STRING_CHARS = 10000
_MACRO_PREVIEW_STRING_CHARS = 300
_MACRO_PREVIEW_ITEMS = 20
_BUNDLE_MAX_STEPS = 20
# Ops that read and prove the whole document before and after their edit;
# a bundle may run only one of them so both proofs fit its time limit. This
# is separate from the undo policy (_BUNDLE_UNDO_POLICY in local_cli_service).
_BUNDLE_WHOLE_DOCUMENT_PROOF_OPS = frozenset({'object_insert_exact', 'layout_exact'})
_BUNDLE_ALLOWED_OPS = {
    'anchor_insert',
    'context',
    'selection_proof',
    'control_inventory',
    'table_frame_inventory',
    'export_pdf',
    'hwp_action',
    'pyhwpx_call',
    'save_document',
    'set_text_file',
    'get_selected_text',
    'readback',
    'typography_overview',
    'style_inspect',
    'paragraph_style_apply_exact',
    'paragraph_delete_exact',
    'paragraph_join_previous_exact',
    'paragraph_join_next_exact',
    'control_join_previous_exact',
    'paragraph_rehome_exact',
    'control_delete_exact',
    'exact_control_select_proof',
    'control_move_resize_exact',
    'cell_format_exact',
    'cell_row_fit_exact',
    'native_table_insert',
    'anchor_range_replace_native_table',
    'selected_text_delete_exact',
    'table_cell_structure_exact',
    'table_column_width_exact',
    'table_split_exact',
    'object_insert_exact',
    'layout_exact',
    'layout_inspect',
    'where',
}

_ANCHOR_INSERT_POSITIONS = {
    'before-anchor',
    'after-anchor',
    'after-paragraph',
    'before-heading',
}
_BUNDLE_SAFE_HACTION_NAMES = {
    'Cancel',
    'TableCellBlock',
    'TableLeftCell',
    'TableRightCell',
    'TableUpperCell',
    'TableLowerCell',
    'MoveDocBegin',
    'MoveDocEnd',
    'MoveLeft',
    'MoveRight',
    'MoveUp',
    'MoveDown',
}
_BUNDLE_SAFE_PYHWPX_CALLS = {
    'CurFieldState',
    'get_pos',
    'GetPos',
    'get_selected_pos',
    'GetSelectedPos',
    'get_selected_text',
    'GetSelectedText',
    'get_text_file',
    'GetTextFile',
    'MoveDocBegin',
    'MoveDocEnd',
    'MoveLeft',
    'MoveRight',
    'MoveUp',
    'MoveDown',
}


def _clean_asset_filename(filename: str, *, default_stem: str = 'image') -> str:
    original = Path(filename or default_stem).name
    suffix = Path(original).suffix.lower()
    stem = Path(original).stem.strip() or default_stem
    safe_stem = _IMAGE_SAFE_STEM_RE.sub('_', stem).strip(' ._') or default_stem
    return f'{safe_stem}{suffix}'


class LocalCliServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LocalCliMutationError(LocalCliRuntimeError):
    """A native mutation failed after it may have changed document state."""

    def __init__(self, message: str, *, mutation_may_have_persisted: bool, rollback: dict[str, Any]):
        super().__init__(message)
        self.mutation_may_have_persisted = mutation_may_have_persisted
        self.rollback = rollback


class LocalCliCellMarginsGetError(LocalCliRuntimeError):
    """A deterministic getter precondition failed; carries one stable public code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.primary_code = code
